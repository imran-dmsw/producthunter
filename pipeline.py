"""Orchestration d'une collecte complète.

1. Demande   : boutiques Shopify concurrentes, import CSV de pubs
2. Regroupement des concurrents + score provisoire (pour prioriser)
3. Google Trends sur les produits prioritaires
4. Matching fournisseur (CJ Dropshipping, AliExpress) + import CSV fournisseur
5. Choix de l'offre, marge réelle, filtres, score final
Chaque source est isolée : si l'une échoue, les autres continuent.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from collectors import CollectResult, ads_import, google_trends, shopify_stores
from db import Run, get_session, upsert_product
from scoring import rescore_all
from suppliers import csv_import, matching

log = logging.getLogger("product_hunter.pipeline")

Progress = Callable[[str, float], None]
ALL_SOURCES = ("shopify", "ads", "trends", "suppliers")


def _save_items(result: CollectResult) -> None:
    with get_session() as s:
        for item in result.items:
            try:
                with s.begin_nested():          # un produit invalide n'annule pas les autres
                    upsert_product(s, item)
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"enregistrement '{item.get('title', '?')[:40]}' : {exc}")


def run_collection(
    cfg: dict,
    sources: tuple[str, ...] = ALL_SOURCES,
    uploaded_csvs: list[tuple[str, bytes]] | None = None,
    supplier_csvs: list[tuple[str, bytes]] | None = None,
    progress: Progress | None = None,
) -> dict:
    progress = progress or (lambda msg, pct: log.info("%s (%.0f%%)", msg, pct * 100))
    report: dict[str, dict] = {}

    def run_source(name: str, label: str, pct: float, fn: Callable[[], CollectResult]) -> None:
        progress(label, pct)
        try:
            res = fn()
        except Exception as exc:  # noqa: BLE001 — garde-fou ultime
            log.exception("source %s", name)
            res = CollectResult(name, errors=[f"erreur inattendue : {type(exc).__name__} {exc}"])
        report[name] = res.summary()

    if "shopify" in sources:
        def _shopify():
            r = shopify_stores.collect(cfg); _save_items(r); return r
        run_source("shopify", "Boutiques Shopify concurrentes…", 0.03, _shopify)

    if "ads" in sources:
        def _ads():
            r = ads_import.collect(cfg, uploaded_csvs)
            if r.items:
                r.info.append(str(ads_import.import_into_db(r.items, cfg)))
            return r
        run_source("ads", "Import des CSV publicitaires…", 0.15, _ads)

    progress("Regroupement des concurrents…", 0.2)
    rescore_all(cfg)

    if "trends" in sources:
        run_source("google_trends", "Google Trends (lent, limité par Google)…", 0.25,
                   lambda: google_trends.collect(cfg))

    if "suppliers" in sources:
        rescore_all(cfg)
        run_source("fournisseurs", "Recherche fournisseurs (CJ, AliExpress)…", 0.5,
                   lambda: matching.match_products(cfg, progress=lambda m, p: progress(m, 0.5 + 0.4 * p)))

    files = list(supplier_csvs or []) + csv_import.folder_sources(cfg)
    if files:
        def _sup_csv():
            rep = csv_import.import_offers(files, cfg)
            return CollectResult("fournisseurs_csv", items=[{}] * rep["rattachées"], errors=rep["erreurs"],
                                 info=[f"{rep['offres']} offres, {rep['rattachées']} rattachées"]
                                 + [f"non rattachée : {t}" for t in rep["non rattachées"][:10]])
        run_source("fournisseurs_csv", "Import CSV fournisseurs…", 0.92, _sup_csv)

    progress("Marges, filtres et score final…", 0.96)
    report["scoring"] = rescore_all(cfg)

    with get_session() as s:
        s.add(Run(report_json=json.dumps(report, ensure_ascii=False, default=str)))
    progress("Collecte terminée", 1.0)
    return report


if __name__ == "__main__":  # python pipeline.py [shopify ads trends suppliers]
    import sys

    from settings import load_config

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    srcs = tuple(sys.argv[1:]) or ALL_SOURCES
    print(json.dumps(run_collection(load_config(), sources=srcs), indent=2, ensure_ascii=False, default=str))
