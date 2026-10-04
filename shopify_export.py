"""Export CSV au format officiel d'import produits Shopify.

En-têtes = modèle actuel de Shopify (help.shopify.com/csv/product_template.csv).
Shopify accepte aussi les anciens noms (Handle, Body (HTML), Variant Price,
Image Src…), mais on suit le modèle courant.

Contenu :
- titre / description HTML / tags / SEO : issus de la génération IA (Claude) ;
  sans fiche IA, le titre détecté est utilisé et la description reste vide ;
- prix = prix de vente conseillé, « Cost per item » = coût rendu (achat + port) ;
- image = photo du FOURNISSEUR (jamais celle d'un concurrent : droits d'auteur) ;
- produit créé en brouillon par défaut (shopify_export.status).
"""
from __future__ import annotations

import csv
import io
import re
import unicodedata

from analyzer import latest_analysis
from db import Product, get_session

HEADERS = [
    "Title", "URL handle", "Description", "Vendor", "Product category", "Type", "Tags",
    "Published on online store", "Status", "SKU", "Variant Barcodes",
    "Option1 name", "Option1 value", "Option1 Linked To", "Option2 name", "Option2 value", "Option2 Linked To",
    "Option3 name", "Option3 value", "Option3 Linked To",
    "Price", "Compare-at price", "Cost per item", "Charge tax", "Tax code",
    "Unit price total measure", "Unit price total measure unit", "Unit price base measure",
    "Unit price base measure unit", "Inventory tracker", "Inventory quantity",
    "Continue selling when out of stock", "Weight value (grams)", "Weight unit for display",
    "Requires shipping", "Fulfillment service", "Product image URL", "Image position", "Image alt text",
    "Variant image URL", "Gift card", "SEO title", "SEO description",
    "Color (product.metafields.shopify.color-pattern)", "Google Shopping / Google product category",
    "Google Shopping / Gender", "Google Shopping / Age group", "Google Shopping / Manufacturer part number (MPN)",
    "Google Shopping / Ad group name", "Google Shopping / Ads labels", "Google Shopping / Condition",
    "Google Shopping / Custom product", "Google Shopping / Custom label 0", "Google Shopping / Custom label 1",
    "Google Shopping / Custom label 2", "Google Shopping / Custom label 3", "Google Shopping / Custom label 4",
    "Packed product length", "Packed product width", "Packed product height", "Packed product dimension unit",
]


def slugify(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:80] or "produit"


def build_rows(product_ids: list[int], cfg: dict) -> tuple[list[dict], list[str]]:
    """Lignes CSV + avertissements (produits ignorés, fiches IA manquantes…)."""
    ex = cfg.get("shopify_export", {})
    rows: list[dict] = []
    warnings: list[str] = []
    handles: set[str] = set()
    with get_session() as s:
        for pid in product_ids:
            p = s.get(Product, pid)
            offer = p.selected_offer() if p else None
            if p is None or offer is None or p.recommended_price is None:
                warnings.append(f"#{pid} ignoré : pas d'offre fournisseur retenue")
                continue
            ai = latest_analysis(pid) or {}
            if not ai:
                warnings.append(f"#{pid} « {p.title[:40]} » : fiche IA non générée (description vide)")
            title = ai.get("titre_fr") or p.title
            handle = base = slugify(title)
            n = 2
            while handle in handles:
                handle, n = f"{base}-{n}", n + 1
            handles.add(handle)

            row = dict.fromkeys(HEADERS, "")
            row.update({
                "Title": title,
                "URL handle": handle,
                "Description": ai.get("description_html", ""),
                "Vendor": ex.get("vendor", ""),
                "Type": ai.get("type_produit") or p.category or "",
                "Tags": ", ".join(ai.get("tags") or []),
                "Published on online store": "true" if ex.get("published") else "false",
                "Status": ex.get("status", "draft"),
                "SKU": f"PH-{p.id}-{offer.supplier.upper()}",
                "Option1 name": "Title",
                "Option1 value": "Default Title",
                "Price": f"{p.recommended_price:.2f}",
                "Cost per item": f"{offer.landed_cost:.2f}" if offer.landed_cost is not None else "",
                "Charge tax": "true",
                "Inventory tracker": "shopify" if ex.get("inventory_qty") else "",
                "Inventory quantity": str(ex.get("inventory_qty") or ""),
                "Continue selling when out of stock": "deny",
                "Weight value (grams)": f"{offer.weight_grams:.0f}" if offer.weight_grams else "",
                "Weight unit for display": "g" if offer.weight_grams else "",
                "Requires shipping": "true",
                "Fulfillment service": "manual",
                "Product image URL": offer.image_url or "",
                "Image position": "1" if offer.image_url else "",
                "Image alt text": title if offer.image_url else "",
                "Gift card": "false",
                "SEO title": ai.get("seo_title", ""),
                "SEO description": ai.get("seo_description", ""),
            })
            rows.append(row)
    return rows, warnings


def export_csv(product_ids: list[int], cfg: dict) -> tuple[bytes, list[str], int]:
    rows, warnings = build_rows(product_ids, cfg)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=HEADERS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8"), warnings, len(rows)
