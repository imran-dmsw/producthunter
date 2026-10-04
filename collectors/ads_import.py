"""Import de CSV exportés depuis Minea, PPSPY ou la Meta Ad Library.

Chaque outil exporte des colonnes différentes : on les reconnaît grâce à
une table de synonymes (insensible à la casse et aux accents). Les lignes
sont regroupées par produit pour calculer :
- le nombre d'annonceurs distincts (pages / boutiques),
- la date de la première et de la dernière pub vue,
- la liste des liens vers les pubs.

Les produits importés sont rapprochés des produits déjà en base (titre
similaire) ; sinon ils sont créés avec la source "ads".
"""
from __future__ import annotations

import hashlib
import io
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from collectors import CollectResult
from collectors.text_utils import keyword_from_title, normalize, parse_price, similarity

log = logging.getLogger("product_hunter.ads")

# Champ interne -> en-têtes possibles (déjà normalisés : minuscules, sans accents)
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "title": ["product name", "product title", "product", "produit", "nom du produit", "title", "titre",
              "ad title", "ad creative link title", "ad_creative_link_title", "headline"],
    "advertiser": ["page name", "page_name", "advertiser", "annonceur", "shop name", "store name", "shop",
                   "boutique", "store", "brand", "domain", "shop url", "store url"],
    "advertisers_count": ["advertisers", "nb advertisers", "number of advertisers", "nombre d annonceurs",
                          "annonceurs", "nb shops", "number of shops", "shops count", "stores"],
    "ad_url": ["ad url", "ad link", "ad_snapshot_url", "ad snapshot url", "lien pub", "lien de la pub",
               "facebook ad url", "tiktok ad url", "post url", "ad library url", "url pub"],
    "landing_url": ["landing page", "landing page url", "landing_url", "product url", "url produit",
                    "website", "link", "lien", "url", "shop link", "store link"],
    "first_seen": ["first seen", "first_seen", "start date", "ad_delivery_start_time", "date de debut",
                   "created at", "creation date", "date de creation", "published", "publication date",
                   "ad creation time", "ad_creation_time", "date"],
    "last_seen": ["last seen", "last_seen", "end date", "ad_delivery_stop_time", "date de fin", "updated at",
                  "last update"],
    "price": ["price", "prix", "selling price", "prix de vente", "product price"],
    "cost": ["cost", "cout", "cost price", "prix d achat", "supplier price", "aliexpress price", "buy price"],
    "supplier_url": ["supplier url", "aliexpress url", "aliexpress link", "lien fournisseur", "supplier link",
                     "cj url", "fournisseur"],
    "image": ["image", "image url", "thumbnail", "media url", "picture", "visuel"],
    "category": ["category", "categorie", "niche"],
}


def _map_columns(df: pd.DataFrame) -> dict[str, str]:
    """Associe chaque champ interne à une colonne du CSV (première correspondance)."""
    normalized = {normalize(c).replace("_", " "): c for c in df.columns}
    mapping: dict[str, str] = {}
    for field, candidates in COLUMN_SYNONYMS.items():
        for cand in candidates:
            cand_n = normalize(cand).replace("_", " ")
            if cand_n in normalized and normalized[cand_n] not in mapping.values():
                mapping[field] = normalized[cand_n]
                break
    return mapping


def read_csv(data: bytes | str | Path) -> pd.DataFrame:
    """Lit un CSV en devinant le séparateur (, ; tab) et l'encodage."""
    raw = Path(data).read_bytes() if isinstance(data, (str, Path)) else data
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(raw), sep=None, engine="python", encoding=enc, dtype=str)
        except UnicodeDecodeError:
            continue
    raise ValueError("encodage du CSV non reconnu")


def _date(value) -> datetime | None:
    if value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == "":
        return None
    text = str(value).strip()
    # Format ISO (2026-08-01…) : année en tête ; sinon format français jour/mois
    iso = len(text) >= 10 and text[4] == "-" and text[:4].isdigit()
    ts = pd.to_datetime(text, errors="coerce", dayfirst=not iso, utc=True)
    if pd.isna(ts):
        # timestamps Unix éventuels
        try:
            ts = pd.to_datetime(int(float(value)), unit="s", utc=True)
        except (TypeError, ValueError):
            return None
    return ts.tz_convert(None).to_pydatetime()


def parse_ads(df: pd.DataFrame, origin: str = "csv") -> tuple[list[dict], dict[str, str]]:
    """Regroupe les lignes du CSV par produit et renvoie des dicts produit."""
    cols = _map_columns(df)
    if "title" not in cols and "landing_url" not in cols:
        raise ValueError(f"colonnes produit introuvables (colonnes du fichier : {list(df.columns)[:15]})")

    def get(row, field):
        col = cols.get(field)
        if not col:
            return None
        v = row.get(col)
        return None if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "" else str(v).strip()

    groups: dict[str, dict] = {}
    for _, row in df.iterrows():
        title = get(row, "title") or (get(row, "landing_url") or "").rstrip("/").split("/")[-1].replace("-", " ")
        if not title:
            continue
        key = keyword_from_title(title, max_words=6) or normalize(title)
        g = groups.setdefault(key, {
            "title": title, "advertisers": set(), "links": [], "first": None, "last": None,
            "count_col": None, "price": None, "cost": None, "supplier_url": None, "image": None,
            "category": None, "landing": None,
        })
        if adv := get(row, "advertiser"):
            g["advertisers"].add(normalize(adv))
        if (n := get(row, "advertisers_count")) and (val := parse_price(n)) is not None:
            g["count_col"] = max(g["count_col"] or 0, int(val))
        for f in ("ad_url", "landing_url"):
            if (link := get(row, f)) and link.startswith("http") and link not in g["links"]:
                g["links"].append(link)
        for f, attr in (("first_seen", "first"), ("last_seen", "last")):
            if d := _date(get(row, f)):
                cur = g[attr]
                g[attr] = d if cur is None else (min(cur, d) if attr == "first" else max(cur, d))
        if d := _date(get(row, "first_seen")):   # une seule date : elle sert aussi de "dernière vue"
            g["last"] = max(g["last"] or d, d)
        g["price"] = g["price"] or parse_price(get(row, "price"))
        g["cost"] = g["cost"] or parse_price(get(row, "cost"))
        g["supplier_url"] = g["supplier_url"] or get(row, "supplier_url")
        g["image"] = g["image"] or get(row, "image")
        g["category"] = g["category"] or get(row, "category")
        g["landing"] = g["landing"] or get(row, "landing_url")

    items = []
    for key, g in groups.items():
        advertisers = g["count_col"] or len(g["advertisers"]) or None
        items.append({
            "source": "ads",
            "source_id": f"{origin}:{hashlib.md5(key.encode()).hexdigest()[:12]}",
            "title": g["title"][:500],
            "url": g["landing"],
            "image_url": g["image"],
            "category": g["category"],
            "price_eur": g["price"],
            "supplier_cost_eur": g["cost"],
            "supplier_url": g["supplier_url"],
            "advertisers_count": advertisers,
            "ad_first_seen": g["first"],
            "ad_last_seen": g["last"],
            "ad_links": g["links"][:30],
            "keyword": keyword_from_title(g["title"]),
        })
    return items, cols


_AD_FIELDS = ("advertisers_count", "ad_first_seen", "ad_last_seen", "supplier_cost_eur", "supplier_url")


def import_into_db(items: list[dict], cfg: dict) -> dict:
    """Rattache les données pubs aux produits existants similaires, crée les autres."""
    from db import Product, get_session, upsert_product

    threshold = cfg.get("scoring", {}).get("title_similarity", 0.72)
    matched = created = 0
    with get_session() as s:
        # Tous les produits (y compris issus d'autres imports pubs) servent au rapprochement
        existing = list(s.scalars(select(Product)).all())
        for it in items:
            best, best_sim = None, 0.0
            for p in existing:
                sim = similarity(it["title"], p.title)
                if sim > best_sim:
                    best, best_sim = p, sim
            if best is not None and best_sim >= threshold and best.source_id != it["source_id"]:
                # Enrichit le produit existant (on garde le max d'annonceurs, la date la plus ancienne)
                best.advertisers_count = max(best.advertisers_count or 0, it["advertisers_count"] or 0) or None
                if it["ad_first_seen"]:
                    best.ad_first_seen = min(filter(None, [best.ad_first_seen, it["ad_first_seen"]]))
                if it["ad_last_seen"]:
                    best.ad_last_seen = max(filter(None, [best.ad_last_seen, it["ad_last_seen"]]))
                links = best.json_field("ad_links", []) + [lk for lk in it["ad_links"] if lk not in (best.json_field("ad_links", []))]
                upsert_product(s, {"source": best.source, "source_id": best.source_id, "ad_links": links[:30],
                                   "supplier_cost_eur": it["supplier_cost_eur"] if not best.supplier_cost_eur else None,
                                   "supplier_url": it["supplier_url"] if not best.supplier_url else None,
                                   "price_eur": it["price_eur"] if not best.price_eur else None,
                                   "image_url": it["image_url"] if not best.image_url else None})
                matched += 1
            else:
                existing.append(upsert_product(s, it))
                created += 1
    return {"matched": matched, "created": created}


def collect(cfg: dict, uploaded: list[tuple[str, bytes]] | None = None) -> CollectResult:
    """Importe les CSV uploadés + ceux présents dans le dossier configuré."""
    from settings import ROOT

    result = CollectResult("ads_import")
    sources: list[tuple[str, bytes]] = list(uploaded or [])
    folder = ROOT / cfg.get("ads_import", {}).get("folder", "data/ads_csv")
    if folder.exists():
        sources += [(f.name, f.read_bytes()) for f in sorted(folder.glob("*.csv"))]
    if not sources:
        result.info.append(f"aucun CSV (déposez des exports dans {folder.relative_to(ROOT)} ou uploadez-les)")
        return result

    for name, raw in sources:
        try:
            df = read_csv(raw)
            items, cols = parse_ads(df, origin=Path(name).stem[:40])
            result.items.extend(items)
            result.info.append(f"{name} : {len(df)} lignes -> {len(items)} produits (colonnes : {cols})")
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"{name} : {exc}")
    return result


if __name__ == "__main__":  # test manuel : python -m collectors.ads_import fichier.csv
    import sys

    logging.basicConfig(level=logging.INFO)
    df = read_csv(Path(sys.argv[1]))
    items, cols = parse_ads(df)
    print("colonnes reconnues :", cols)
    for it in items[:5]:
        print(it["advertisers_count"], it["ad_first_seen"], it["price_eur"], it["title"])
