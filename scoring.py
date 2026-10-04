"""Regroupement des concurrents, choix de l'offre fournisseur, filtres
éliminatoires et score /100.

Règle de base : aucune donnée inventée. Un produit sans offre fournisseur
réelle (prix d'achat + livraison France) n'a ni marge ni prix conseillé et
ne peut pas entrer dans le top.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from collectors.text_utils import STOPWORDS, normalize
from margins import economics

CRITERIA_LABELS = {
    "marge": "Marge nette",
    "demande": "Demande",
    "saturation": "Faible saturation",
    "fiabilite_fournisseur": "Fiabilité fournisseur",
    "wow": "Effet wow / problème résolu",
}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# =====================================================================
# Regroupement : un même produit vendu par plusieurs boutiques
# =====================================================================
# Couleurs, tailles et matières : ne distinguent pas deux produits (variantes)
VARIANT_WORDS = set("""
noir black blanc white gris grey gray rouge red bleu blue vert green jaune yellow rose pink violet purple
orange marron brown beige creme cream bordeaux kaki khaki navy marine dore gold argent silver multi multicolore
clair fonce light dark charcoal marl heather sand natural naturel olive burgundy lilac taupe mint teal cherry
xs xl xxl xxxl small medium large taille size regular tall short petite homme femme men mens women womens unisex
""".split())


def _tokens(title: str) -> set[str]:
    return {w for w in normalize(title).split()
            if w not in STOPWORDS and w not in VARIANT_WORDS and len(w) > 2 and not w.isdigit()}


def cluster_products(products: list, min_jaccard: float = 0.55) -> dict[int, int]:
    """product.id -> id du représentant de son groupe.

    Regroupement « autour d'un représentant » : un produit rejoint un groupe
    seulement s'il ressemble au représentant lui-même (pas d'enchaînement
    A~B~C qui mélangerait des produits différents). Les mots très fréquents
    (marque de la boutique…) et les variantes (couleurs, tailles) sont ignorés.
    """
    toks = {p.id: _tokens(p.title) for p in products}
    freq = Counter(w for t in toks.values() for w in t)
    too_common = {w for w, n in freq.items() if n > max(15, 0.05 * len(products))}
    toks = {pid: t - too_common for pid, t in toks.items()}

    # Les meilleurs candidats représentants passent en premier
    ordered = sorted(products, key=lambda p: (p.best_seller_rank or 10_000, p.source != "shopify", p.id))
    reps: list[int] = []
    index: dict[str, list[int]] = defaultdict(list)     # mot -> représentants le contenant
    result: dict[int, int] = {}
    for p in ordered:
        t = toks[p.id]
        best, best_sim = None, 0.0
        if len(t) >= 2:
            for r in {r for w in t for r in index[w]}:
                u = t | toks[r]
                sim = len(t & toks[r]) / len(u) if u else 0.0
                if sim > best_sim:
                    best, best_sim = r, sim
        if best is not None and best_sim >= min_jaccard:
            result[p.id] = best
        else:
            result[p.id] = p.id
            reps.append(p.id)
            for w in t:
                index[w].append(p.id)
    return result


# =====================================================================
# Filtres
# =====================================================================
_kw_regex_cache: dict[str, re.Pattern] = {}


def _kw_pattern(word: str) -> re.Pattern:
    if word not in _kw_regex_cache:
        _kw_regex_cache[word] = re.compile(rf"(?<![a-z0-9]){re.escape(normalize(word))}(?![a-z0-9])")
    return _kw_regex_cache[word]


def keyword_exclusions(texts: list[str | None], cfg: dict) -> list[str]:
    haystack = normalize(" ".join(t for t in texts if t))
    reasons = []
    for motif, words in (cfg["filters"].get("excluded_keywords") or {}).items():
        hits = [w for w in words if _kw_pattern(str(w)).search(haystack)]
        if hits:
            reasons.append(f"{motif.replace('_', ' ')} ({', '.join(hits[:3])})")
    return reasons


def offer_exclusions(offer, cfg: dict) -> list[str]:
    """Raisons pour lesquelles une offre fournisseur n'est pas acceptable."""
    f = cfg["filters"]
    reasons = []
    if offer.price_eur is None:
        reasons.append("prix d'achat inconnu")
    if offer.shipping_eur is None:
        reasons.append("frais de port France inconnus")
    if offer.delivery_days_max is None:
        reasons.append("délai de livraison France inconnu")
    elif offer.delivery_days_max > f["max_delivery_days"]:
        reasons.append(f"livraison {offer.delivery_days_max} j > {f['max_delivery_days']} j")
    if offer.supplier not in f.get("rating_not_required_for", []):
        if offer.rating is None:
            reasons.append("note vendeur inconnue")
        elif offer.rating < f["min_supplier_rating"]:
            reasons.append(f"note vendeur {offer.rating:.1f} < {f['min_supplier_rating']}")
    if f.get("require_stock", True) and offer.stock is not None and offer.stock <= 0:
        reasons.append("rupture de stock")
    if offer.weight_grams and f.get("max_weight_grams") and offer.weight_grams > f["max_weight_grams"]:
        reasons.append(f"poids {offer.weight_grams:.0f} g > {f['max_weight_grams']} g")
    if offer.match_score is not None and offer.match_score < cfg["matching"]["min_match_score"]:
        reasons.append(f"correspondance faible ({offer.match_score:.2f})")
    return reasons


def economic_exclusions(econ: dict, cfg: dict) -> list[str]:
    f = cfg["filters"]
    reasons = []
    price = econ["recommended_price"]
    if price < f["min_price_eur"]:
        reasons.append(f"prix conseillé {price:.2f} € < {f['min_price_eur']} €")
    elif price > f["max_price_eur"]:
        reasons.append(f"prix conseillé {price:.2f} € > {f['max_price_eur']} €")
    if econ["net_profit"] < f["min_net_profit_eur"]:
        reasons.append(f"profit net {econ['net_profit']:.2f} € < {f['min_net_profit_eur']} €")
    return reasons


# =====================================================================
# Notes par critère
# =====================================================================
def _avg(parts: list[float]) -> float | None:
    return sum(parts) / len(parts) if parts else None


def demand_note(agg: dict, sc: dict) -> tuple[float | None, str]:
    parts, labels = [], []
    if agg["trend_mean"] is not None:
        note = 0.0 if agg["trend_mean"] == 0 else \
            0.5 * _clamp(agg["trend_mean"] / 60) + 0.5 * _clamp(((agg["trend_slope"] or 0) + 1) / 2)
        parts.append(note)
        labels.append(f"Trends {agg['trend_mean']:.0f}/100")
    if agg["advertisers"]:
        n, lo, hi, hard = agg["advertisers"], sc["advertisers_ideal_min"], sc["advertisers_ideal_max"], sc["advertisers_hard_max"]
        parts.append(n / lo if n < lo else 1.0 if n <= hi else _clamp(1 - (n - hi) / max(hard - hi, 1)))
        labels.append(f"{n} annonceurs")
    if agg["ad_first_seen"]:
        days = max(((agg["ad_last_seen"] or datetime.utcnow()) - agg["ad_first_seen"]).days, 0)
        parts.append(_clamp(days / sc["ad_age_target_days"]))
        labels.append(f"pubs {days} j")
    if agg["best_rank"]:
        parts.append(_clamp(1 - (agg["best_rank"] - 1) / 50, 0.2, 1.0))
        labels.append(f"best-seller #{agg['best_rank']}")
    return _avg(parts), ", ".join(labels)


def supplier_note(offer, sc: dict, cfg: dict) -> tuple[float | None, str]:
    if offer is None:
        return None, ""
    parts, labels = [], []
    f = cfg["filters"]
    if offer.delivery_days_max is not None:
        best, worst = sc["delivery_days_best"], f["max_delivery_days"]
        parts.append(_clamp(1 - (offer.delivery_days_max - best) / max(worst - best, 1) * 0.7))
        labels.append(f"{offer.delivery_days_min or '?'}-{offer.delivery_days_max} j")
    if offer.rating is not None:
        lo = f["min_supplier_rating"]
        parts.append(_clamp(0.4 + 0.6 * (offer.rating - lo) / max(sc["rating_best"] - lo, 0.01)))
        labels.append(f"note {offer.rating:.1f}")
    if offer.stock is not None:
        parts.append(_clamp(math.log10(max(offer.stock, 1)) / 3))   # 1000 unités -> 1
        labels.append(f"stock {offer.stock}")
    if offer.orders is not None:
        parts.append(_clamp(math.log10(max(offer.orders, 1)) / 3))
        labels.append(f"{offer.orders} ventes")
    if offer.match_score is not None:
        parts.append(_clamp(offer.match_score))
        labels.append(f"correspondance {offer.match_score:.0%}")
    return _avg(parts), ", ".join(labels)


# =====================================================================
# Recalcul global
# =====================================================================
def _aggregate(members: list) -> dict:
    """Signaux de demande agrégés sur tous les produits du groupe."""
    ads = [m for m in members if m.advertisers_count]
    firsts = [m.ad_first_seen for m in members if m.ad_first_seen]
    lasts = [m.ad_last_seen for m in members if m.ad_last_seen]
    trends = [m for m in members if m.trend_updated_at is not None and m.trend_mean is not None]
    trend = max(trends, key=lambda m: m.trend_mean) if trends else None
    ranks = [m.best_seller_rank for m in members if m.best_seller_rank]
    competitors, seen = [], set()
    for m in members:
        # Boutiques Shopify suivies + boutiques vues dans les imports de pubs (landing page)
        store = m.store or (re.sub(r"^https?://(www\.)?", "", m.url).split("/")[0] if m.url else None)
        if store and store not in seen:
            seen.add(store)
            competitors.append({"store": store, "url": m.url, "price": m.price_eur, "title": m.title})
    return {
        "advertisers": max((m.advertisers_count for m in ads), default=None),
        "ad_first_seen": min(firsts) if firsts else None,
        "ad_last_seen": max(lasts) if lasts else None,
        "trend_mean": trend.trend_mean if trend else None,
        "trend_slope": trend.trend_slope if trend else None,
        "best_rank": min(ranks) if ranks else None,
        "competitors": competitors,
        "ad_links": [lk for m in members for lk in m.json_field("ad_links", [])][:30],
    }


def evaluate(rep, members: list, cfg: dict) -> dict:
    """Calcule tout pour un représentant de groupe et écrit les champs du produit."""
    sc, w = cfg["scoring"], cfg["weights"]
    missing = sc.get("missing_data_value", 0.3)
    agg = _aggregate(members)
    rep.competitors = json.dumps(agg["competitors"], ensure_ascii=False)
    rep.saturation_stores = max(len(agg["competitors"]), agg["advertisers"] or 0)

    reasons = keyword_exclusions([rep.title, rep.category, rep.tags, rep.vendor, (rep.description or "")[:600]], cfg)

    # ---- choix de l'offre fournisseur ----
    competitor_prices = [c["price"] for c in agg["competitors"] if c["price"]]
    best, best_econ = None, None
    valid = []
    for o in rep.offers:
        if offer_exclusions(o, cfg):
            continue
        econ = economics(o.landed_cost, competitor_prices, cfg)
        valid.append((o, econ))
    if rep.offer_locked and (locked := rep.selected_offer()):
        best = locked
        best_econ = economics(locked.landed_cost, competitor_prices, cfg) if locked.landed_cost is not None else None
        reasons += [f"offre choisie : {r}" for r in offer_exclusions(locked, cfg)]
    elif valid:
        # Offre conforme la moins chère rendue en France (meilleure correspondance à égalité)
        best, best_econ = min(valid, key=lambda x: (x[0].landed_cost, -(x[0].match_score or 0)))

    from db import SUPPLIER_FOUND, SUPPLIER_NOT_FOUND, SUPPLIER_PENDING
    if rep.supplier_status == SUPPLIER_PENDING and not rep.offers:
        reasons.append("fournisseur non encore cherché")
    elif best is None:
        reasons.append(SUPPLIER_NOT_FOUND if not rep.offers else "aucune offre fournisseur conforme")
        if rep.offers:
            rep.supplier_status = SUPPLIER_FOUND
    rep.selected_offer_id = best.id if best else (rep.selected_offer_id if rep.offer_locked else None)

    if best_econ:
        reasons += economic_exclusions(best_econ, cfg)
        rep.recommended_price = best_econ["recommended_price"]
        rep.landed_cost = best_econ["landed_cost"]
        rep.net_profit = best_econ["net_profit"]
        rep.net_margin_pct = best_econ["net_margin_pct"]
    else:
        rep.recommended_price = rep.landed_cost = rep.net_profit = rep.net_margin_pct = None

    # ---- score ----
    notes: dict[str, dict] = {}
    if best_econ:
        m = _clamp((best_econ["net_margin_pct"] - sc["margin_pct_min"]) / (sc["margin_pct_max"] - sc["margin_pct_min"]))
        notes["marge"] = {"note": m, "valeur": f"{best_econ['net_profit']:.2f} € ({best_econ['net_margin_pct']:.0f} %)"}
    else:
        notes["marge"] = {"note": 0.0, "valeur": "pas d'offre fournisseur", "manquant": True}
    d, dl = demand_note(agg, sc)
    notes["demande"] = {"note": d if d is not None else missing, "valeur": dl or "aucun signal", "manquant": d is None}
    sat = rep.saturation_stores
    notes["saturation"] = {"note": 1 - _clamp((sat - 1) / sc["saturation_max_stores"]) if sat else missing,
                           "valeur": f"{sat} concurrent(s)" if sat else "inconnue", "manquant": not sat}
    s_note, s_label = supplier_note(best, sc, cfg)
    notes["fiabilite_fournisseur"] = {"note": s_note if s_note is not None else 0.0,
                                      "valeur": s_label or "pas d'offre", "manquant": s_note is None}
    notes["wow"] = {"note": _clamp(rep.wow_score / 10) if rep.wow_score is not None else missing,
                    "valeur": f"{rep.wow_score:.0f}/10" if rep.wow_score is not None else "pas encore analysé",
                    "manquant": rep.wow_score is None}

    total_w = sum(w.get(k, 0) for k in CRITERIA_LABELS) or 1
    for k, dct in notes.items():
        dct["poids"] = w.get(k, 0)
        dct["points"] = round(dct["note"] * dct["poids"] * 100 / total_w, 1)
        dct["note"] = round(dct["note"], 3)
    rep.score = round(sum(dct["points"] for dct in notes.values()), 1)
    rep.score_details = json.dumps({**notes, "_economics": best_econ, "_ad_links": agg["ad_links"]},
                                   ensure_ascii=False, default=str)
    rep.excluded = bool(reasons)
    rep.exclusion_reasons = json.dumps(reasons, ensure_ascii=False) if reasons else None
    return {"excluded": rep.excluded}


def rescore_all(cfg: dict) -> dict:
    """Regroupe les produits, choisit les offres, applique filtres et scores."""
    from db import Product, get_session

    with get_session() as s:
        products = s.scalars(select(Product).options(selectinload(Product.offers))).all()
        clusters = cluster_products(products, cfg["scoring"].get("cluster_similarity", 0.55))
        groups: dict[int, list] = defaultdict(list)
        for p in products:
            p.cluster_id = clusters[p.id]
            groups[p.cluster_id].append(p)
        by_id = {p.id: p for p in products}
        eligible = excluded = 0
        for rep_id, members in groups.items():
            rep = by_id[rep_id]
            evaluate(rep, members, cfg)
            eligible += not rep.excluded
            excluded += rep.excluded
            for m in members:
                if m.id != rep_id:      # les doublons suivent leur représentant
                    m.excluded, m.score = True, rep.score
                    m.exclusion_reasons = json.dumps([f"même produit que #{rep_id}"], ensure_ascii=False)
    return {"products": len(products), "groups": len(groups), "eligible": eligible, "excluded": excluded}


if __name__ == "__main__":  # python scoring.py
    from settings import load_config

    print(rescore_all(load_config()))
