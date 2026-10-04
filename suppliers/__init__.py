"""Sources fournisseur (CJ Dropshipping, AliExpress, import CSV).

Chaque source expose `search(query, cfg) -> list[dict]` : une liste d'offres
réelles au format de `db.upsert_offer` (supplier, supplier_product_id, title,
url, image_url, price_eur, shipping_eur, delivery_days_min/max, stock,
rating, orders, raw). Un champ inconnu reste None : rien n'est estimé.
Les erreurs lèvent `SupplierError` ; le matching continue avec les autres sources.
"""
from __future__ import annotations

import re


class SupplierError(Exception):
    pass


class SupplierNotConfigured(SupplierError):
    """Clé d'API absente : la source est simplement ignorée."""


def parse_days(text) -> tuple[int | None, int | None]:
    """'7-12' / '7~12 days' / '10' / 12 -> (min, max)."""
    if text is None:
        return None, None
    if isinstance(text, (int, float)):
        return int(text), int(text)
    nums = [int(n) for n in re.findall(r"\d+", str(text))]
    if not nums:
        return None, None
    return min(nums[:2]), max(nums[:2])
