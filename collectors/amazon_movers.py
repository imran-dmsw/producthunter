"""Collecteur Amazon Movers & Shakers (amazon.fr).

Ces pages listent les produits dont le classement des ventes progresse le
plus en 24 h. Amazon affiche parfois un captcha aux robots : dans ce cas on
le signale et on passe à la suite sans planter. Le HTML d'Amazon change
souvent, d'où plusieurs sélecteurs de repli.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from collectors import CollectResult
from collectors.http_client import BlockedError, HttpClient
from collectors.text_utils import keyword_from_title, parse_price

log = logging.getLogger("product_hunter.amazon")

CAPTCHA_MARKERS = ("captcha", "validatecaptcha", "Saisissez les caractères", "Enter the characters you see")


def _is_captcha(html: str) -> bool:
    head = html[:20000].lower()
    return any(m.lower() in head for m in CAPTCHA_MARKERS)


def parse_page(html: str, domain: str, category: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select("div[id^=gridItemRoot]") or soup.select("div.zg-grid-general-faceout") \
        or soup.select("li.zg-item-immersion")
    items = []
    for card in cards:
        link = card.select_one("a.a-link-normal[href*='/dp/']") or card.select_one("a[href*='/dp/']")
        if not link:
            continue
        m = re.search(r"/dp/([A-Z0-9]{10})", link.get("href", ""))
        if not m:
            continue
        asin = m.group(1)
        img = card.select_one("img")
        # Le titre est dans un div à classe générée (_cDEzb_p13n-sc-css-line-clamp…) ; à défaut, l'alt de l'image
        title_el = card.select_one("div[class*=line-clamp]") or card.select_one("span.a-size-small") \
            or card.select_one("div.p13n-sc-truncate")
        title = (title_el.get_text(strip=True) if title_el else "") or (img.get("alt", "") if img else "")
        price_el = card.select_one("span[class*=p13n-sc-price]") or card.select_one("span.a-color-price") \
            or card.select_one("span.a-price span.a-offscreen")
        rank_el = card.select_one("span.zg-bdg-text")
        gain_el = card.select_one("span.zg-percent-change")
        if not title:
            continue
        items.append({
            "source": "amazon",
            "source_id": f"{domain}:{asin}",
            "title": title[:500],
            "url": f"https://{domain}/dp/{asin}",
            "image_url": img.get("src") if img else None,
            "store": domain.replace("www.", ""),
            "category": category,
            "price_eur": parse_price(price_el.get_text(strip=True)) if price_el else None,
            "best_seller_rank": int(re.sub(r"\D", "", rank_el.get_text()) or 0) or None if rank_el else None,
            "description": f"Progression ventes 24h : {gain_el.get_text(strip=True)}" if gain_el else None,
            "keyword": keyword_from_title(title),
        })
    return items


def collect(cfg: dict) -> CollectResult:
    result = CollectResult("amazon")
    acfg = cfg.get("amazon", {})
    if not acfg.get("enabled", True):
        result.info.append("source désactivée")
        return result
    domain = acfg.get("domain", "www.amazon.fr")
    client = HttpClient(cfg.get("http"))

    blocked = False
    for cat in acfg.get("categories", []):
        # Movers & Shakers d'abord ; Amazon sert souvent une grille vide aux
        # robots sur cette page -> repli sur les Meilleures ventes de la catégorie.
        for page_type in ("movers-and-shakers", "bestsellers"):
            url = f"https://{domain}/gp/{page_type}/{cat}"
            try:
                html = client.get(url).text
            except BlockedError as exc:
                result.errors.append(f"{cat} : bloqué ({exc})")
                break
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"{cat} ({page_type}) : {type(exc).__name__} {exc}")
                continue
            if _is_captcha(html):
                result.errors.append(f"{cat} : captcha Amazon — source ignorée pour cette collecte")
                blocked = True
                break
            items = parse_page(html, domain, cat)
            for it in items:
                it["description"] = (it.get("description") or
                                     ("Movers & Shakers" if page_type == "movers-and-shakers" else "Meilleures ventes"))
            if items:
                result.items.extend(items)
                result.info.append(f"{cat} : {len(items)} produits ({page_type})")
                break
            result.info.append(f"{cat} : grille {page_type} vide")
        if blocked:
            break   # inutile d'insister, les pages suivantes seront aussi bloquées
    return result


if __name__ == "__main__":  # test manuel : python -m collectors.amazon_movers
    from settings import load_config

    logging.basicConfig(level=logging.INFO)
    r = collect(load_config())
    print(r.summary())
    for it in r.items[:5]:
        print(it["best_seller_rank"], it["price_eur"], it["title"][:80], "| kw:", it["keyword"])
