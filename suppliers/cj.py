"""Client CJ Dropshipping (API v2) — https://developers.cjdropshipping.com

- Authentification : POST authentication/getAccessToken {"apiKey"} -> accessToken
  (valable 180 jours, mis en cache en base), envoyé dans l'en-tête CJ-Access-Token.
- Recherche      : GET product/listV2?keyWord=…
- Détail/variants: GET product/query?pid=…
- Frais de port  : POST logistic/freightCalculate (CN -> FR)
Limite : 1 requête/seconde. Tous les prix CJ sont en USD (convertis en EUR).
Chaque réponse est mise en cache (cj.cache_hours) pour ne pas rappeler l'API.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

import requests

from db import cache_get, cache_set
from suppliers import SupplierError, SupplierNotConfigured, parse_days

log = logging.getLogger("product_hunter.suppliers.cj")

BASE = "https://developers.cjdropshipping.com/api2.0/v1"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "product"


class CJClient:
    def __init__(self, cfg: dict):
        self.cfg = cfg.get("cj", {})
        self.usd_eur = cfg["economics"].get("usd_to_eur", 0.92)
        self.api_key = os.getenv("CJ_API_KEY", "").strip()
        if not self.api_key:
            raise SupplierNotConfigured("CJ_API_KEY absente (Réglages → Clés API)")
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"
        self._last = 0.0
        self._token: str | None = None

    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        wait = self.cfg.get("request_delay_s", 1.2) - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _raw(self, method: str, path: str, *, params=None, body=None, auth=True) -> dict:
        headers = {"CJ-Access-Token": self.token()} if auth else {}
        for attempt in range(3):
            self._throttle()
            try:
                resp = self.session.request(method, f"{BASE}/{path}", params=params,
                                            data=json.dumps(body) if body is not None else None,
                                            headers=headers, timeout=30)
            except requests.RequestException as exc:
                if attempt == 2:
                    raise SupplierError(f"CJ injoignable : {exc}") from exc
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code == 429:
                time.sleep(3 * (attempt + 1))
                continue
            try:
                data = resp.json()
            except ValueError as exc:
                raise SupplierError(f"CJ : réponse non JSON (HTTP {resp.status_code})") from exc
            if data.get("code") == 200 or data.get("result") is True:
                return data.get("data")
            if data.get("code") == 1600200:          # dépassement du débit
                time.sleep(3 * (attempt + 1))
                continue
            if auth and data.get("code") in (1600001, 1600003):   # token invalide/expiré
                cache_set("cj:token", None)
                self._token = None
                headers = {"CJ-Access-Token": self.token()}
                continue
            raise SupplierError(f"CJ {path} : {data.get('code')} {data.get('message')}")
        raise SupplierError(f"CJ {path} : trop de requêtes (429)")

    def token(self) -> str:
        if self._token:
            return self._token
        cached = cache_get("cj:token", max_age_hours=24 * 170)
        if cached:
            self._token = cached
            return cached
        data = self._raw("POST", "authentication/getAccessToken", body={"apiKey": self.api_key}, auth=False)
        if not data or not data.get("accessToken"):
            raise SupplierError("CJ : token non reçu (clé API invalide ?)")
        self._token = data["accessToken"]
        cache_set("cj:token", self._token)
        return self._token

    def _cached(self, key: str, fn):
        hours = self.cfg.get("cache_hours", 24)
        hit = cache_get(key, hours)
        if hit is not None:
            return hit
        value = fn()
        cache_set(key, value)
        return value

    # ------------------------------------------------------------------
    def search(self, query: str, size: int = 10) -> list[dict]:
        def call():
            data = self._raw("GET", "product/listV2", params={"keyWord": query, "page": 1, "size": size,
                                                              "orderBy": 0, "features": "enable_category"})
            items = []
            for block in (data or {}).get("content", []) or []:
                items.extend(block.get("productList", []) or [])
            return items
        return self._cached(f"cj:search:{query.lower()}:{size}", call)

    def product(self, pid: str) -> dict:
        return self._cached(f"cj:product:{pid}", lambda: self._raw("GET", "product/query", params={"pid": pid}) or {})

    def freight(self, vid: str, country: str = "FR") -> list[dict]:
        body = {"startCountryCode": "CN", "endCountryCode": country, "products": [{"quantity": 1, "vid": vid}]}
        return self._cached(f"cj:freight:{vid}:{country}",
                            lambda: self._raw("POST", "logistic/freightCalculate", body=body) or [])

    # ------------------------------------------------------------------
    def offer(self, item: dict, max_days: int) -> dict | None:
        """Construit une offre complète (prix variant + livraison FR) pour un résultat de recherche."""
        pid = item.get("id") or item.get("pid")
        detail = self.product(pid)
        variants = detail.get("variants") or []
        priced = [v for v in variants if v.get("variantSellPrice") is not None and v.get("vid")]
        if not priced:
            return None
        variant = min(priced, key=lambda v: float(v["variantSellPrice"]))
        options = self.freight(variant["vid"])
        if not options:
            return None

        def parsed(opt):
            dmin, dmax = parse_days(opt.get("logisticAging"))
            return opt, dmin, dmax, float(opt.get("logisticPrice") or 0)
        opts = [parsed(o) for o in options if o.get("logisticPrice") is not None]
        if not opts:
            return None
        # Méthode la moins chère respectant le délai max ; sinon la plus rapide
        within = [o for o in opts if o[2] is not None and o[2] <= max_days]
        opt, dmin, dmax, ship_usd = (min(within, key=lambda o: o[3]) if within
                                     else min(opts, key=lambda o: (o[2] or 999, o[3])))
        stock = item.get("warehouseInventoryNum")
        return {
            "supplier": "cj",
            "supplier_product_id": str(pid),
            "variant_id": variant["vid"],
            "title": detail.get("productNameEn") or item.get("nameEn") or "?",
            "url": f"https://cjdropshipping.com/product/{_slug(detail.get('productNameEn') or '')}-p-{pid}.html",
            "image_url": detail.get("bigImage") or item.get("bigImage"),
            "price_eur": round(float(variant["variantSellPrice"]) * self.usd_eur, 2),
            "shipping_eur": round(ship_usd * self.usd_eur, 2),
            "shipping_method": opt.get("logisticName"),
            "delivery_days_min": dmin,
            "delivery_days_max": dmax,
            "stock": int(stock) if stock not in (None, "") else None,
            "rating": None,                                   # CJ ne publie pas de note vendeur
            "orders": None,                                   # CJ ne publie pas de volume de ventes

            "weight_grams": float(variant.get("variantWeight") or detail.get("productWeight") or 0) or None,
            "raw": {"description": (detail.get("description") or "")[:3000], "variant": variant.get("variantKey"),
                    "category": detail.get("categoryName"), "freight_options": options[:6],
                    "listed_by_stores": detail.get("listedNum") or item.get("listedNum")},
        }


def search(query: str, cfg: dict, score_fn) -> list[dict]:
    """Recherche CJ -> offres complètes pour les meilleurs candidats (par similarité)."""
    client = CJClient(cfg)
    m = cfg["matching"]
    items = client.search(query, size=m.get("candidates_per_supplier", 10))
    ranked = sorted(items, key=lambda it: score_fn(it.get("nameEn") or ""), reverse=True)
    offers = []
    for it in ranked[: m.get("details_per_supplier", 3)]:
        sc = score_fn(it.get("nameEn") or "")
        if sc < m["min_match_score"]:
            continue
        try:
            off = client.offer(it, cfg["filters"]["max_delivery_days"])
        except SupplierError as exc:
            log.warning("CJ offre %s : %s", it.get("id"), exc)
            continue
        if off:
            off["match_score"] = round(sc, 3)
            offers.append(off)
    return offers
