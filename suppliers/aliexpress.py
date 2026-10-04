"""Client AliExpress Affiliate API (Open Platform) — https://openservice.aliexpress.com

Passerelle : https://api-sg.aliexpress.com/sync
- aliexpress.affiliate.product.query    : recherche (prix en EUR, livraison FR)
- aliexpress.affiliate.product.shipping.get : frais de port + délai réels vers la France

Signature : paramètres (hors `sign`) triés par nom, concaténés « clévaleur… »,
HMAC-SHA256 avec l'App Secret, hexadécimal en majuscules.

Clés .env : ALIEXPRESS_APP_KEY, ALIEXPRESS_APP_SECRET, ALIEXPRESS_TRACKING_ID (optionnel).

Note : l'API affiliés ne publie ni le stock, ni la note de la boutique ;
la "note" utilisée est le taux d'avis positifs du produit (evaluate_rate,
ex. 96 % -> 4,8/5). Le stock reste inconnu (None).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time

import requests

from collectors.text_utils import parse_price
from db import cache_get, cache_set
from suppliers import SupplierError, SupplierNotConfigured

log = logging.getLogger("product_hunter.suppliers.aliexpress")

GATEWAY = "https://api-sg.aliexpress.com/sync"


def sign(params: dict, secret: str) -> str:
    """Signature HMAC-SHA256 de la passerelle AliExpress Open Platform."""
    payload = "".join(f"{k}{params[k]}" for k in sorted(params) if k != "sign" and params[k] is not None)
    return hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest().upper()


class AliExpressClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("aliexpress", {})
        self.app_key = os.getenv("ALIEXPRESS_APP_KEY", "").strip()
        self.secret = os.getenv("ALIEXPRESS_APP_SECRET", "").strip()
        self.tracking_id = os.getenv("ALIEXPRESS_TRACKING_ID", "").strip() or None
        if not (self.app_key and self.secret):
            raise SupplierNotConfigured("ALIEXPRESS_APP_KEY / ALIEXPRESS_APP_SECRET absentes (Réglages → Clés API)")
        self.session = requests.Session()
        self._last = 0.0

    def call(self, method: str, **biz) -> dict:
        params = {"app_key": self.app_key, "method": method, "sign_method": "sha256",
                  "timestamp": str(int(time.time() * 1000)), **{k: str(v) for k, v in biz.items() if v is not None}}
        params["sign"] = sign(params, self.secret)
        for attempt in range(3):
            wait = 0.6 - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                resp = self.session.post(GATEWAY, data=params, timeout=30)
                data = resp.json()
            except (requests.RequestException, ValueError) as exc:
                if attempt == 2:
                    raise SupplierError(f"AliExpress injoignable : {exc}") from exc
                time.sleep(2 * (attempt + 1))
                continue
            if "error_response" in data:
                err = data["error_response"]
                if "limit" in str(err.get("code", "")).lower() or "frequency" in str(err.get("msg", "")).lower():
                    time.sleep(3 * (attempt + 1))
                    continue
                raise SupplierError(f"AliExpress {method} : {err.get('code')} {err.get('msg')}")
            key = method.replace(".", "_") + "_response"
            result = (data.get(key) or {}).get("resp_result") or {}
            code = str(result.get("resp_code", ""))
            if code not in ("200", ""):
                if code == "405":        # aucun résultat
                    return {}
                raise SupplierError(f"AliExpress {method} : {code} {result.get('resp_msg')}")
            return result.get("result") or {}
        raise SupplierError(f"AliExpress {method} : trop de requêtes")

    def _cached(self, key: str, fn):
        hit = cache_get(key, self.cfg.get("cache_hours", 24))
        if hit is not None:
            return hit
        value = fn()
        cache_set(key, value)
        return value

    # ------------------------------------------------------------------
    def search(self, query: str, size: int = 10) -> list[dict]:
        def run():
            res = self.call("aliexpress.affiliate.product.query", keywords=query, page_no=1, page_size=size,
                            target_currency=self.cfg.get("target_currency", "EUR"),
                            target_language=self.cfg.get("target_language", "FR"),
                            ship_to_country=self.cfg.get("ship_to_country", "FR"),
                            tracking_id=self.tracking_id)
            products = res.get("products") or []
            if isinstance(products, dict):            # certaines réponses : {"product": [...]}
                products = products.get("product") or []
            return products
        return self._cached(f"ae:search:{query.lower()}:{size}", run)

    def shipping(self, item: dict) -> dict:
        def run():
            return self.call("aliexpress.affiliate.product.shipping.get",
                             product_id=item.get("product_id"), sku_id=item.get("sku_id"),
                             ship_to_country=self.cfg.get("ship_to_country", "FR"),
                             target_currency=self.cfg.get("target_currency", "EUR"),
                             target_sale_price=item.get("target_sale_price"),
                             target_language=self.cfg.get("target_language", "FR"),
                             tax_rate=item.get("tax_rate") or "0")
        return self._cached(f"ae:shipping:{item.get('product_id')}:{item.get('sku_id')}", run)

    def offer(self, item: dict) -> dict | None:
        price = parse_price(item.get("target_sale_price"))
        if price is None or not item.get("sku_id"):
            return None
        ship = self.shipping(item)
        fee = parse_price(ship.get("shipping_fee")) if ship else None
        dmin = ship.get("min_delivery_days") if ship else None
        dmax = ship.get("max_delivery_days") or ship.get("delivery_days") if ship else None
        rate = parse_price(item.get("evaluate_rate"))
        return {
            "supplier": "aliexpress",
            "supplier_product_id": str(item.get("product_id")),
            "variant_id": str(item.get("sku_id")),
            "title": item.get("product_title") or "?",
            "url": item.get("product_detail_url") or f"https://fr.aliexpress.com/item/{item.get('product_id')}.html",
            "image_url": item.get("product_main_image_url"),
            "price_eur": price,
            "shipping_eur": fee,
            "shipping_method": "AliExpress (vers FR)" if fee is not None else None,
            "delivery_days_min": int(dmin) if dmin not in (None, "") else None,
            "delivery_days_max": int(dmax) if dmax not in (None, "") else None,
            "stock": None,                                       # non publié par l'API affiliés
            "rating": round(rate / 20, 2) if rate is not None else None,   # % avis positifs -> /5
            "orders": int(item["lastest_volume"]) if item.get("lastest_volume") not in (None, "") else None,
            "raw": {"shop": item.get("shop_name"), "evaluate_rate": item.get("evaluate_rate"),
                    "promotion_link": item.get("promotion_link"), "category": item.get("first_level_category_name"),
                    "ship_from": (ship or {}).get("ship_from_country"), "currency": item.get("target_sale_price_currency")},
        }


def search(query: str, cfg: dict, score_fn) -> list[dict]:
    client = AliExpressClient(cfg)
    m = cfg["matching"]
    items = client.search(query, size=m.get("candidates_per_supplier", 10))
    ranked = sorted(items, key=lambda it: score_fn(it.get("product_title") or ""), reverse=True)
    offers = []
    for it in ranked[: m.get("details_per_supplier", 3)]:
        sc = score_fn(it.get("product_title") or "")
        if sc < m["min_match_score"]:
            continue
        try:
            off = client.offer(it)
        except SupplierError as exc:
            log.warning("AliExpress offre %s : %s", it.get("product_id"), exc)
            continue
        if off:
            off["match_score"] = round(sc, 3)
            offers.append(off)
    return offers
