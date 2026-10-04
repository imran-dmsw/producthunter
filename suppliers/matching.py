"""Matching fournisseur : pour chaque produit détecté (représentant de groupe),
chercher le même produit chez CJ Dropshipping et AliExpress.

1. Sélection des produits à traiter (non exclus par les mots-clés, jamais
   cherchés ou recherche plus vieille que `matching.recheck_days`), par
   demande décroissante, plafonnée à `matching.max_products_per_run`.
2. Requête de recherche anglaise : traduite par Claude en un seul appel
   (si clé disponible), sinon mot-clé tiré du titre.
3. Chaque source renvoie des offres réelles ; une source en échec ou non
   configurée est ignorée sans arrêter les autres.
4. Aucune offre -> statut « fournisseur introuvable » (exclu du top).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import or_, select

from collectors import CollectResult
from collectors.text_utils import STOPWORDS, keyword_from_title, normalize
from suppliers import SupplierError, SupplierNotConfigured, aliexpress, cj

log = logging.getLogger("product_hunter.matching")

SOURCES = {"cj": cj.search, "aliexpress": aliexpress.search}


def coverage_score(query: str):
    """Fonction de similarité : part des mots de la requête présents dans le titre fournisseur."""
    q = {w for w in normalize(query).split() if w not in STOPWORDS and len(w) > 2}

    def score(title: str) -> float:
        if not q:
            return 0.0
        t = set(normalize(title).split())
        # tolère pluriels simples (brush/brushes)
        hits = sum(1 for w in q if w in t or w + "s" in t or w + "es" in t or w.rstrip("s") in t)
        return hits / len(q)
    return score


def products_to_match(cfg: dict, product_ids: list[int] | None = None) -> list[int]:
    from db import SUPPLIER_PENDING, Product, get_session
    from scoring import keyword_exclusions

    m = cfg["matching"]
    stale = datetime.utcnow() - timedelta(days=m.get("recheck_days", 7))
    with get_session() as s:
        q = select(Product)
        if product_ids:
            q = q.where(Product.id.in_(product_ids))
        else:
            q = q.where(or_(Product.supplier_status == SUPPLIER_PENDING, Product.supplier_checked_at.is_(None),
                            Product.supplier_checked_at < stale))
        candidates = [p for p in s.scalars(q).all() if p.is_representative]
        if not product_ids:
            candidates = [p for p in candidates
                          if not keyword_exclusions([p.title, p.category, p.tags, p.vendor], cfg)]

        def demand(p):
            return ((p.advertisers_count or 0) > 0, -(p.best_seller_rank or 10_000), p.trend_mean or 0)
        candidates.sort(key=demand, reverse=True)
        limit = len(candidates) if product_ids else m.get("max_products_per_run", 25)
        return [p.id for p in candidates[:limit]]


def ensure_queries(ids: list[int], cfg: dict, result: CollectResult) -> None:
    """Remplit Product.search_query (traduction Claude groupée, sinon mot-clé)."""
    import analyzer
    from db import Product, get_session

    with get_session() as s:
        todo = [(p.id, p.title) for p in (s.get(Product, i) for i in ids) if not p.search_query]
    if not todo:
        return
    translated: dict[int, str] = {}
    if cfg["matching"].get("translate_queries", True) and analyzer.has_api_key():
        try:
            for start in range(0, len(todo), 40):
                translated.update(analyzer.suggest_search_queries(todo[start:start + 40], cfg))
        except analyzer.AnalyzerError as exc:
            result.errors.append(f"traduction des requêtes : {exc} — mots-clés bruts utilisés")
    elif cfg["matching"].get("translate_queries", True):
        result.info.append("pas de clé Anthropic : requêtes = mots-clés du titre (moins précis pour les titres FR)")
    with get_session() as s:
        for pid, title in todo:
            s.get(Product, pid).search_query = translated.get(pid) or keyword_from_title(title, max_words=4)


def match_products(cfg: dict, product_ids: list[int] | None = None, progress=None) -> CollectResult:
    from db import SUPPLIER_FOUND, SUPPLIER_NOT_FOUND, Product, get_session, upsert_offer

    result = CollectResult("fournisseurs")
    ids = products_to_match(cfg, product_ids)
    if not ids:
        result.info.append("aucun produit à rechercher (tous déjà traités récemment)")
        return result
    ensure_queries(ids, cfg, result)

    enabled = {name: fn for name, fn in SOURCES.items() if cfg.get(name, {}).get("enabled", True)}
    disabled_reason: dict[str, str] = {}
    for i, pid in enumerate(ids):
        with get_session() as s:
            p = s.get(Product, pid)
            query, title = p.search_query, p.title
        if progress:
            progress(f"Fournisseurs {i + 1}/{len(ids)} : {query}", i / len(ids))
        found, errors_here = [], 0
        for name, fn in enabled.items():
            if name in disabled_reason:
                continue
            try:
                found += fn(query, cfg, coverage_score(query))
            except SupplierNotConfigured as exc:
                disabled_reason[name] = str(exc)
                result.info.append(f"{name} ignoré : {exc}")
            except SupplierError as exc:
                errors_here += 1
                result.errors.append(f"{name} « {query} » : {exc}")
            except Exception as exc:  # noqa: BLE001 — une source qui plante n'arrête pas les autres
                errors_here += 1
                log.exception("source %s", name)
                result.errors.append(f"{name} « {query} » : {type(exc).__name__} {exc}")

        with get_session() as s:
            p = s.get(Product, pid)
            for off in found:
                upsert_offer(s, pid, off)
            active = [n for n in enabled if n not in disabled_reason]
            if found or p.offers:
                p.supplier_status = SUPPLIER_FOUND
            elif active and errors_here < len(active):
                p.supplier_status = SUPPLIER_NOT_FOUND     # au moins une source a répondu : vraiment introuvable
            # sinon (toutes les sources en erreur / non configurées) : statut inchangé, on réessaiera
            p.supplier_checked_at = datetime.utcnow() if (found or (active and errors_here < len(active))) else p.supplier_checked_at
        result.items.append({"product_id": pid, "query": query, "offers": len(found), "title": title[:60]})

    if len(disabled_reason) == len(enabled):
        result.errors.append("aucune source fournisseur configurée : ajoutez CJ_API_KEY et/ou les clés AliExpress "
                             "(Réglages → Clés API), ou importez un CSV fournisseur")
    found_n = sum(1 for it in result.items if it["offers"])
    result.info.append(f"{found_n}/{len(result.items)} produits avec au moins une offre")
    return result
