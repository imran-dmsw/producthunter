"""Collecteur Shopify : lit le catalogue public /products.json de boutiques
concurrentes et, si possible, l'ordre "best-selling" de /collections/all.

Toutes les boutiques Shopify n'exposent pas ces endpoints (certaines les
bloquent) : une boutique en échec est simplement ignorée.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from collectors import CollectResult
from collectors.http_client import BlockedError, HttpClient
from collectors.text_utils import keyword_from_title, strip_html

log = logging.getLogger("product_hunter.shopify")

# Taux de conversion approximatifs vers l'euro (les boutiques FR sont en EUR ;
# /products.json renvoie les prix dans la devise de la boutique).
FX_TO_EUR = {"EUR": 1.0, "USD": 0.92, "GBP": 1.17, "CHF": 1.05, "CAD": 0.68, "AUD": 0.61}


def _clean_domain(domain: str) -> str:
    return re.sub(r"^https?://", "", domain.strip()).strip("/")


def _store_currency(client: HttpClient, base: str) -> str:
    """Devise de la boutique via /cart.js (léger) ; EUR par défaut."""
    try:
        return client.get(f"{base}/cart.js").json().get("currency", "EUR")
    except Exception:  # noqa: BLE001 — information facultative
        return "EUR"


def fetch_products(client: HttpClient, base: str, max_pages: int) -> list[dict]:
    products: list[dict] = []
    for page in range(1, max_pages + 1):
        resp = client.get(f"{base}/products.json", params={"limit": 250, "page": page})
        batch = resp.json().get("products", [])
        products.extend(batch)
        if len(batch) < 250:
            break
    return products


def fetch_best_seller_ranks(client: HttpClient, base: str) -> dict[str, int]:
    """handle -> rang dans /collections/all?sort_by=best-selling (page 1 et 2)."""
    ranks: dict[str, int] = {}
    for page in (1, 2):
        resp = client.get(f"{base}/collections/all", params={"sort_by": "best-selling", "page": page})
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select('a[href*="/products/"]'):
            m = re.search(r"/products/([^/?#]+)", a.get("href", ""))
            if m and m.group(1) not in ranks:
                ranks[m.group(1)] = len(ranks) + 1
    return ranks


def _to_item(p: dict, domain: str, base: str, fx: float, ranks: dict[str, int]) -> dict | None:
    variants = p.get("variants") or []
    prices = [float(v["price"]) for v in variants if v.get("price")]
    if not prices:
        return None
    grams = [v.get("grams") for v in variants if v.get("grams")]
    images = p.get("images") or []
    tags = p.get("tags")
    return {
        "source": "shopify",
        "source_id": f"{domain}:{p['id']}",
        "title": p.get("title", "").strip(),
        "url": f"{base}/products/{p.get('handle')}",
        "image_url": images[0]["src"] if images else None,
        "store": domain,
        "category": p.get("product_type") or None,
        "vendor": p.get("vendor"),
        "tags": ", ".join(tags) if isinstance(tags, list) else tags,
        "description": strip_html(p.get("body_html")),
        "price_eur": round(min(prices) * fx, 2),
        "weight_grams": float(min(grams)) if grams else None,
        "best_seller_rank": ranks.get(p.get("handle")),
        "keyword": keyword_from_title(p.get("title", "")),
    }


def collect(cfg: dict) -> CollectResult:
    result = CollectResult("shopify")
    scfg = cfg.get("shopify", {})
    if not scfg.get("enabled", True):
        result.info.append("source désactivée")
        return result
    client = HttpClient(cfg.get("http"))

    for raw_domain in scfg.get("stores", []):
        domain = _clean_domain(raw_domain)
        base = f"https://{domain}"
        try:
            products = fetch_products(client, base, scfg.get("max_pages", 2))
        except BlockedError as exc:
            result.errors.append(f"{domain} : bloqué ({exc})")
            continue
        except Exception as exc:  # noqa: BLE001 — une boutique en échec ne doit pas tout arrêter
            result.errors.append(f"{domain} : {type(exc).__name__} {exc}")
            continue

        ranks: dict[str, int] = {}
        if scfg.get("fetch_best_sellers", True):
            try:
                ranks = fetch_best_seller_ranks(client, base)
            except Exception as exc:  # noqa: BLE001
                result.info.append(f"{domain} : best-sellers indisponibles ({type(exc).__name__})")

        currency = _store_currency(client, base)
        fx = FX_TO_EUR.get(currency, 1.0)
        if currency not in FX_TO_EUR:
            result.info.append(f"{domain} : devise {currency} inconnue, prix laissés tels quels")

        count = 0
        for p in products:
            item = _to_item(p, domain, base, fx, ranks)
            if item:
                result.items.append(item)
                count += 1
        result.info.append(f"{domain} : {count} produits ({currency}), {len(ranks)} best-sellers classés")
    return result


if __name__ == "__main__":  # test manuel : python -m collectors.shopify_stores
    from settings import load_config

    logging.basicConfig(level=logging.INFO)
    r = collect(load_config())
    print(r.summary())
    for it in sorted(r.items, key=lambda x: x["best_seller_rank"] or 9999)[:5]:
        print(it["best_seller_rank"], it["price_eur"], it["title"], "| kw:", it["keyword"])
