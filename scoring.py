"""Filtres éliminatoires et score /100.

Chaque critère donne une note 0..1, multipliée par son poids (config.yaml,
section `weights`). Le total est ramené sur 100 même si la somme des poids
diffère de 100. Une donnée absente reçoit `scoring.missing_data_value`.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select

from collectors.text_utils import STOPWORDS, normalize

CRITERIA_LABELS = {
    "marge": "Marge estimée",
    "tendance": "Tendance Google Trends",
    "annonceurs": "Nombre d'annonceurs",
    "anciennete_pubs": "Ancienneté des pubs",
    "saturation": "Faible saturation",
    "wow": "Effet wow / problème résolu",
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------
# Filtres
# ---------------------------------------------------------------------
_kw_regex_cache: dict[str, re.Pattern] = {}


def _kw_pattern(word: str) -> re.Pattern:
    """Motif « mot entier » (évite que 'tea' matche 'steak')."""
    if word not in _kw_regex_cache:
        _kw_regex_cache[word] = re.compile(rf"(?<![a-z0-9]){re.escape(normalize(word))}(?![a-z0-9])")
    return _kw_regex_cache[word]


def estimated_cost(p, cfg: dict) -> tuple[float | None, bool]:
    """(prix d'achat, estimé ?) — estimation via filters.estimated_cost_ratio si inconnu."""
    if p.supplier_cost_eur:
        return p.supplier_cost_eur, False
    if p.price_eur:
        return round(p.price_eur * cfg["filters"].get("estimated_cost_ratio", 0.3), 2), True
    return None, True


def check_filters(p, cfg: dict) -> list[str]:
    """Renvoie la liste des raisons d'exclusion (vide = produit éligible)."""
    f = cfg["filters"]
    reasons: list[str] = []
    if p.price_eur is not None:
        if p.price_eur < f["min_price_eur"]:
            reasons.append(f"prix {p.price_eur:.2f} € < {f['min_price_eur']} €")
        elif p.price_eur > f["max_price_eur"]:
            reasons.append(f"prix {p.price_eur:.2f} € > {f['max_price_eur']} €")
    if p.supplier_cost_eur and p.price_eur:
        markup = p.price_eur / p.supplier_cost_eur
        if markup < f["min_markup"]:
            reasons.append(f"marge x{markup:.1f} < x{f['min_markup']}")
    if p.weight_grams and f.get("max_weight_grams") and p.weight_grams > f["max_weight_grams"]:
        reasons.append(f"poids {p.weight_grams:.0f} g > {f['max_weight_grams']} g")

    haystack = normalize(" ".join(filter(None, [p.title, p.category, p.tags, p.vendor, (p.description or "")[:600]])))
    for motif, words in (f.get("excluded_keywords") or {}).items():
        hits = [w for w in words if _kw_pattern(str(w)).search(haystack)]
        if hits:
            reasons.append(f"{motif.replace('_', ' ')} ({', '.join(hits[:3])})")
    return reasons


# ---------------------------------------------------------------------
# Saturation : combien de boutiques suivies vendent un produit similaire
# ---------------------------------------------------------------------
def _tokens(title: str) -> frozenset[str]:
    return frozenset(w for w in normalize(title).split() if w not in STOPWORDS and len(w) > 2 and not w.isdigit())


def compute_saturation(products: list, min_jaccard: float = 0.5) -> dict[int, int]:
    """product.id -> nombre de boutiques distinctes vendant un titre similaire.

    Index inversé par mot pour éviter de comparer toutes les paires.
    """
    toks = {p.id: _tokens(p.title) for p in products}
    stores = {p.id: (p.store or p.source) for p in products}
    index: dict[str, list[int]] = defaultdict(list)
    for pid, t in toks.items():
        for w in t:
            index[w].append(pid)

    result: dict[int, int] = {}
    for p in products:
        t = toks[p.id]
        seen_stores = {stores[p.id]}
        candidates = {c for w in t if len(index[w]) < 300 for c in index[w]}   # ignore les mots trop fréquents
        for c in candidates:
            if c == p.id or stores[c] in seen_stores:
                continue
            u = t | toks[c]
            if u and len(t & toks[c]) / len(u) >= min_jaccard:
                seen_stores.add(stores[c])
        # Les annonceurs importés comptent aussi comme boutiques concurrentes
        result[p.id] = max(len(seen_stores), p.advertisers_count or 0)
    return result


# ---------------------------------------------------------------------
# Score
# ---------------------------------------------------------------------
def score_product(p, cfg: dict) -> tuple[float, dict]:
    sc = cfg["scoring"]
    w = cfg["weights"]
    missing = sc.get("missing_data_value", 0.3)
    notes: dict[str, dict] = {}

    # Marge
    cost, is_estimated = estimated_cost(p, cfg)
    if cost and p.price_eur:
        markup = p.price_eur / cost
        note = _clamp((markup - sc["markup_min"]) / (sc["markup_max"] - sc["markup_min"]))
        notes["marge"] = {"note": note, "valeur": f"x{markup:.1f}" + (" (achat estimé)" if is_estimated else ""),
                          "marge_eur": round(p.price_eur - cost, 2)}
    else:
        notes["marge"] = {"note": missing, "valeur": "prix inconnu", "manquant": True}

    # Tendance : moyenne récente + pente
    if p.trend_updated_at is not None and p.trend_mean is not None:
        note = 0.5 * _clamp(p.trend_mean / 60) + 0.5 * _clamp(((p.trend_slope or 0) + 1) / 2)
        if p.trend_mean == 0:
            note = 0.0
        notes["tendance"] = {"note": note, "valeur": f"moy. {p.trend_mean:.0f}/100, pente {p.trend_slope or 0:+.2f}"}
    else:
        notes["tendance"] = {"note": missing, "valeur": "non mesurée", "manquant": True}

    # Annonceurs : plage idéale
    n = p.advertisers_count
    if n:
        lo, hi, hard = sc["advertisers_ideal_min"], sc["advertisers_ideal_max"], sc["advertisers_hard_max"]
        if n < lo:
            note = n / lo
        elif n <= hi:
            note = 1.0
        else:
            note = _clamp(1 - (n - hi) / max(hard - hi, 1))
        notes["annonceurs"] = {"note": note, "valeur": f"{n} annonceur(s)"}
    else:
        notes["annonceurs"] = {"note": missing, "valeur": "aucune donnée pub", "manquant": True}

    # Ancienneté des pubs (une pub qui tourne depuis > 30 j est rentable)
    if p.ad_first_seen:
        end = p.ad_last_seen or datetime.utcnow()
        days = max((end - p.ad_first_seen).days, 0)
        notes["anciennete_pubs"] = {"note": _clamp(days / sc["ad_age_target_days"]), "valeur": f"{days} jours"}
    else:
        notes["anciennete_pubs"] = {"note": missing, "valeur": "aucune donnée pub", "manquant": True}

    # Saturation
    if p.saturation_stores is not None:
        note = 1 - _clamp((p.saturation_stores - 1) / sc["saturation_max_stores"])
        notes["saturation"] = {"note": note, "valeur": f"{p.saturation_stores} boutique(s)"}
    else:
        notes["saturation"] = {"note": missing, "valeur": "non calculée", "manquant": True}

    # Effet wow (analyse Claude)
    if p.wow_score is not None:
        notes["wow"] = {"note": _clamp(p.wow_score / 10), "valeur": f"{p.wow_score:.0f}/10"}
    else:
        notes["wow"] = {"note": missing, "valeur": "pas encore analysé", "manquant": True}

    total_w = sum(w.get(k, 0) for k in CRITERIA_LABELS) or 1
    for k, d in notes.items():
        d["poids"] = w.get(k, 0)
        d["points"] = round(d["note"] * d["poids"] * 100 / total_w, 1)
        d["note"] = round(d["note"], 3)
    score = round(sum(d["points"] for d in notes.values()), 1)
    return score, notes


def rescore_all(cfg: dict) -> dict:
    """Recalcule filtres, saturation et score de tous les produits en base."""
    from db import Product, get_session

    with get_session() as s:
        products = s.scalars(select(Product)).all()
        sat = compute_saturation(products)
        excluded = 0
        for p in products:
            p.saturation_stores = sat.get(p.id)
            reasons = check_filters(p, cfg)
            p.excluded = bool(reasons)
            p.exclusion_reasons = json.dumps(reasons, ensure_ascii=False) if reasons else None
            excluded += p.excluded
            p.score, details = score_product(p, cfg)
            p.score_details = json.dumps(details, ensure_ascii=False)
    return {"products": len(products), "excluded": excluded, "eligible": len(products) - excluded}


if __name__ == "__main__":  # test manuel : python scoring.py
    from settings import load_config

    print(rescore_all(load_config()))
