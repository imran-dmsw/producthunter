"""Import d'offres fournisseur depuis un CSV (export DSers / AutoDS, liste
AliExpress copiée à la main, catalogue d'un agent de sourcing…).

Colonnes reconnues (synonymes, insensible à la casse/accents) : titre, URL,
prix d'achat, frais de port, délai (min/max ou "7-12"), stock, note, ventes,
image, et optionnellement l'id du produit product-hunter visé (`product_id`).
Sans `product_id`, l'offre est rattachée au produit dont le titre ou la
requête de recherche ressemble le plus.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

import pandas as pd
from sqlalchemy import select

from collectors.ads_import import read_csv
from collectors.text_utils import normalize, parse_price, similarity
from suppliers import parse_days

log = logging.getLogger("product_hunter.suppliers.csv")

SYNONYMS = {
    "product_id": ["product id", "product_id", "id produit", "ph id"],
    "supplier": ["supplier", "fournisseur", "source", "platform", "plateforme"],
    "title": ["title", "titre", "product title", "product name", "nom", "name"],
    "url": ["url", "link", "lien", "product url", "supplier url", "aliexpress url", "aliexpress link"],
    "price": ["price", "prix", "cost", "cout", "prix d achat", "product cost", "unit price", "sale price"],
    "shipping": ["shipping", "shipping cost", "frais de port", "livraison", "shipping fee", "freight"],
    "delivery": ["delivery", "delivery time", "delai", "delai de livraison", "shipping time", "aging"],
    "delivery_min": ["delivery min", "delai min", "min days"],
    "delivery_max": ["delivery max", "delai max", "max days"],
    "shipping_method": ["shipping method", "methode", "logistic", "carrier", "transporteur"],
    "stock": ["stock", "inventory", "quantity", "quantite"],
    "rating": ["rating", "note", "store rating", "seller rating", "note vendeur"],
    "orders": ["orders", "sold", "ventes", "commandes", "sales"],
    "image": ["image", "image url", "photo", "main image"],
}


def _map_columns(df: pd.DataFrame) -> dict[str, str]:
    norm = {normalize(c).replace("_", " "): c for c in df.columns}
    mapping: dict[str, str] = {}
    for field, cands in SYNONYMS.items():
        for cand in cands:
            key = normalize(cand).replace("_", " ")
            if key in norm and norm[key] not in mapping.values():
                mapping[field] = norm[key]
                break
    return mapping


def _rating(value) -> float | None:
    """'4.8' / '96.5%' (avis positifs) -> note /5."""
    if value is None:
        return None
    v = parse_price(value)
    if v is None:
        return None
    return round(v / 20, 2) if ("%" in str(value) or v > 5) else round(v, 2)


def parse_offers(df: pd.DataFrame) -> tuple[list[dict], dict]:
    cols = _map_columns(df)
    if "title" not in cols or "price" not in cols:
        raise ValueError(f"colonnes titre et prix obligatoires (trouvé : {list(df.columns)[:12]})")

    def get(row, field):
        c = cols.get(field)
        v = row.get(c) if c else None
        return None if v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() == "" else str(v).strip()

    offers = []
    for _, row in df.iterrows():
        title = get(row, "title")
        if not title:
            continue
        dmin, dmax = parse_days(get(row, "delivery"))
        if get(row, "delivery_min") or get(row, "delivery_max"):
            dmin = int(parse_price(get(row, "delivery_min")) or 0) or dmin
            dmax = int(parse_price(get(row, "delivery_max")) or 0) or dmax
        url = get(row, "url")
        stock = parse_price(get(row, "stock"))
        orders = parse_price(get(row, "orders"))
        pid = parse_price(get(row, "product_id"))
        offers.append({
            "target_product_id": int(pid) if pid else None,
            "supplier": (get(row, "supplier") or "csv").lower()[:30],
            "supplier_product_id": hashlib.md5((url or title).encode()).hexdigest()[:16],
            "title": title[:500],
            "url": url,
            "image_url": get(row, "image"),
            "price_eur": parse_price(get(row, "price")),
            "shipping_eur": parse_price(get(row, "shipping")),
            "shipping_method": get(row, "shipping_method"),
            "delivery_days_min": dmin,
            "delivery_days_max": dmax,
            "stock": int(stock) if stock is not None else None,
            "rating": _rating(get(row, "rating")),
            "orders": int(orders) if orders is not None else None,
            "raw": {k: get(row, k) for k in cols},
        })
    return offers, cols


def import_offers(sources: list[tuple[str, bytes]], cfg: dict) -> dict:
    """Rattache les offres CSV aux produits (représentants) et les enregistre."""
    from db import SUPPLIER_FOUND, Product, get_session, upsert_offer

    report = {"fichiers": 0, "offres": 0, "rattachées": 0, "non rattachées": [], "erreurs": []}
    with get_session() as s:
        reps = [p for p in s.scalars(select(Product)).all() if p.is_representative]
        for name, raw in sources:
            try:
                offers, _ = parse_offers(read_csv(raw))
            except Exception as exc:  # noqa: BLE001
                report["erreurs"].append(f"{name} : {exc}")
                continue
            report["fichiers"] += 1
            for o in offers:
                report["offres"] += 1
                target = s.get(Product, o.pop("target_product_id")) if o.get("target_product_id") else None
                if target is None:
                    best, best_sim = None, 0.0
                    for p in reps:
                        sim = max(similarity(o["title"], p.title),
                                  similarity(o["title"], p.search_query or "") if p.search_query else 0)
                        if sim > best_sim:
                            best, best_sim = p, sim
                    if best is None or best_sim < cfg["matching"]["min_match_score"]:
                        report["non rattachées"].append(o["title"][:60])
                        continue
                    target, o["match_score"] = best, round(best_sim, 3)
                else:
                    o.pop("target_product_id", None)
                    o["match_score"] = 1.0       # rattachement explicite par l'utilisateur
                rep_id = target.cluster_id or target.id
                upsert_offer(s, rep_id, o)
                s.get(Product, rep_id).supplier_status = SUPPLIER_FOUND
                report["rattachées"] += 1
    return report


def folder_sources(cfg: dict) -> list[tuple[str, bytes]]:
    from settings import ROOT

    folder = ROOT / cfg.get("aliexpress", {}).get("csv_folder", "data/supplier_csv")
    return [(f.name, f.read_bytes()) for f in sorted(Path(folder).glob("*.csv"))] if folder.exists() else []
