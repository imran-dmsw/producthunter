"""Orchestration d'une collecte complète.

Ordre : Shopify -> Amazon -> import pubs -> filtres/score (pour prioriser)
-> Google Trends sur les meilleurs produits éligibles -> score final.
Chaque source est isolée : si l'une échoue, les autres continuent.
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from collectors import CollectResult, ads_import, amazon_movers, google_trends, shopify_stores
from db import Run, get_session, upsert_product
from scoring import rescore_all

log = logging.getLogger("product_hunter.pipeline")

Progress = Callable[[str, float], None]


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
    sources: tuple[str, ...] = ("shopify", "amazon", "ads", "trends"),
    uploaded_csvs: list[tuple[str, bytes]] | None = None,
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
        run_source("shopify", "Boutiques Shopify…", 0.05, _shopify)

    if "amazon" in sources:
        def _amazon():
            r = amazon_movers.collect(cfg); _save_items(r); return r
        run_source("amazon", "Amazon Movers & Shakers…", 0.30, _amazon)

    if "ads" in sources:
        def _ads():
            r = ads_import.collect(cfg, uploaded_csvs)
            if r.items:
                r.info.append(str(ads_import.import_into_db(r.items, cfg)))
            return r
        run_source("ads", "Import des CSV publicitaires…", 0.45, _ads)

    progress("Filtres et score provisoire…", 0.55)
    rescore_all(cfg)

    if "trends" in sources:
        run_source("google_trends", "Google Trends (lent, limité par Google)…", 0.60,
                   lambda: google_trends.collect(cfg))

    progress("Score final…", 0.95)
    report["scoring"] = rescore_all(cfg)

    with get_session() as s:
        s.add(Run(report_json=json.dumps(report, ensure_ascii=False, default=str)))
    progress("Collecte terminée", 1.0)
    return report


if __name__ == "__main__":  # python pipeline.py [sources…]
    import sys

    from settings import load_config

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    srcs = tuple(sys.argv[1:]) or ("shopify", "amazon", "ads", "trends")
    print(json.dumps(run_collection(load_config(), sources=srcs), indent=2, ensure_ascii=False, default=str))
