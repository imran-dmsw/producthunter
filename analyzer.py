"""Analyse IA des meilleurs produits via l'API Claude.

Pour chaque produit, Claude renvoie un JSON (format imposé par un schéma) :
angle marketing, public cible, 3 accroches, risques, note "wow" et verdict
go/no-go. Les résultats sont mis en cache dans la table `analyses` pour ne
pas repayer un appel tant que l'analyse a moins de `analyzer.cache_days`.

La clé est lue depuis l'environnement (fichier .env) : ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import anthropic
from sqlalchemy import select

from db import Analysis, Product, get_session
from scoring import estimated_cost

log = logging.getLogger("product_hunter.analyzer")

SYSTEM_PROMPT = """Tu es un expert e-commerce spécialisé dans le dropshipping Shopify sur le marché \
France / Europe. Tu évalues des produits pour un vendeur qui teste des produits niche avec de la \
publicité Meta et TikTok. Sois concret, honnête et critique : un "no-go" argumenté vaut mieux \
qu'un enthousiasme injustifié. Tiens compte de la réglementation européenne (marquage CE, \
sécurité des produits GPSR, propriété intellectuelle, droit de rétractation de 14 jours) et des \
délais de livraison depuis la Chine. Réponds en français."""

# Schéma JSON imposé à la réponse (structured outputs)
ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "angle_marketing": {"type": "string", "description": "Angle principal de communication"},
        "probleme_resolu": {"type": "string", "description": "Problème concret résolu ou désir satisfait"},
        "public_cible": {"type": "string", "description": "Persona : âge, sexe, centres d'intérêt, situation"},
        "accroches": {"type": "array", "items": {"type": "string"}, "description": "Exactement 3 accroches de pub"},
        "wow_score": {"type": "integer", "description": "0-10 : effet wow / démonstration visuelle / problème résolu"},
        "risques": {
            "type": "object",
            "properties": {
                "saturation": {"type": "string"},
                "retours": {"type": "string"},
                "legal": {"type": "string"},
            },
            "required": ["saturation", "retours", "legal"],
            "additionalProperties": False,
        },
        "prix_conseille_eur": {"type": "number", "description": "Prix de vente conseillé en euros"},
        "verdict": {"type": "string", "enum": ["go", "no-go", "à creuser"]},
        "justification": {"type": "string", "description": "2-3 phrases expliquant le verdict"},
    },
    "required": ["angle_marketing", "probleme_resolu", "public_cible", "accroches", "wow_score",
                 "risques", "prix_conseille_eur", "verdict", "justification"],
    "additionalProperties": False,
}


class AnalyzerError(Exception):
    pass


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _product_brief(p: Product, cfg: dict) -> str:
    cost, estimated = estimated_cost(p, cfg)
    details = p.json_field("score_details", {})
    lines = [
        f"Titre : {p.title}",
        f"Source : {p.source} ({p.store or 'n/a'})",
        f"Catégorie : {p.category or 'inconnue'}",
        f"Prix de vente observé : {p.price_eur or 'inconnu'} €",
        f"Prix d'achat : {cost or 'inconnu'} €" + (" (estimation)" if estimated and cost else ""),
        f"Annonceurs détectés : {p.advertisers_count or 'inconnu'}",
        f"Première pub vue : {p.ad_first_seen:%Y-%m-%d}" if p.ad_first_seen else "Première pub vue : inconnue",
        f"Boutiques concurrentes suivies vendant un produit similaire : {p.saturation_stores or 'inconnu'}",
        f"Google Trends FR (mot-clé '{p.keyword}') : moyenne {p.trend_mean}, pente {p.trend_slope}"
        if p.trend_mean is not None else "Google Trends : non mesuré",
        f"Score actuel : {p.score}/100",
    ]
    if details:
        lines.append("Détail du score : " + ", ".join(f"{k}={v.get('valeur')}" for k, v in details.items()))
    if p.description:
        lines.append(f"Description : {p.description[:1200]}")
    return "\n".join(lines)


def _call_claude(client: anthropic.Anthropic, cfg: dict, brief: str) -> dict:
    acfg = cfg.get("analyzer", {})
    kwargs = dict(
        model=acfg.get("model", "claude-opus-5-5"),
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        output_config={
            "effort": acfg.get("effort", "medium"),
            "format": {"type": "json_schema", "schema": ANALYSIS_SCHEMA},
        },
        messages=[{
            "role": "user",
            "content": "Analyse ce produit pour une boutique Shopify ciblant la France/Europe :\n\n" + brief,
        }],
    )
    if acfg.get("use_fallbacks", True):
        # Si la requête est déclinée par un filtre de sécurité, l'API la rejoue
        # automatiquement sur un modèle de repli recommandé.
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")

    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise AnalyzerError("clé API invalide (ANTHROPIC_API_KEY)") from exc
    except anthropic.RateLimitError as exc:
        raise AnalyzerError("limite de requêtes Claude atteinte, réessayez plus tard") from exc
    except anthropic.BadRequestError as exc:
        raise AnalyzerError(f"requête refusée : {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AnalyzerError(f"erreur API {exc.status_code} : {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AnalyzerError("connexion à l'API Claude impossible") from exc

    if response.stop_reason == "refusal":
        raise AnalyzerError("Claude a refusé d'analyser ce produit")
    if response.stop_reason == "max_tokens":
        raise AnalyzerError("réponse tronquée (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise AnalyzerError("réponse vide")
    data = json.loads(text)
    data["accroches"] = data.get("accroches", [])[:3]
    data["wow_score"] = max(0, min(10, int(data.get("wow_score", 0))))
    data["_model"] = response.model
    return data


def get_cached(session, product_id: int, cache_days: int) -> Analysis | None:
    since = datetime.utcnow() - timedelta(days=cache_days)
    return session.scalar(
        select(Analysis).where(Analysis.product_id == product_id, Analysis.created_at >= since)
        .order_by(Analysis.created_at.desc())
    )


def latest_analysis(product_id: int) -> dict | None:
    with get_session() as s:
        a = s.scalar(select(Analysis).where(Analysis.product_id == product_id).order_by(Analysis.created_at.desc()))
        return {**a.result, "_date": a.created_at} if a else None


def analyze_product(product_id: int, cfg: dict, force: bool = False, client: anthropic.Anthropic | None = None) -> dict:
    """Analyse un produit (ou renvoie l'analyse en cache). Met à jour wow_score."""
    acfg = cfg.get("analyzer", {})
    with get_session() as s:
        p = s.get(Product, product_id)
        if p is None:
            raise AnalyzerError(f"produit {product_id} introuvable")
        if not force and (cached := get_cached(s, product_id, acfg.get("cache_days", 30))):
            return {**cached.result, "_cached": True}
        brief = _product_brief(p, cfg)

    # Appel API hors transaction : la base reste disponible pendant l'attente
    if client is None:
        if not has_api_key():
            raise AnalyzerError("ANTHROPIC_API_KEY manquante : ajoutez-la dans le fichier .env")
        client = anthropic.Anthropic(max_retries=3)
    data = _call_claude(client, cfg, brief)

    with get_session() as s:
        s.add(Analysis(product_id=product_id, model=data.get("_model", acfg.get("model")),
                       result_json=json.dumps(data, ensure_ascii=False)))
        s.get(Product, product_id).wow_score = data["wow_score"]
    return data


def analyze_top(cfg: dict, top_n: int | None = None, progress=None) -> dict:
    """Analyse les N meilleurs produits éligibles, puis recalcule les scores (effet wow)."""
    from scoring import rescore_all

    top_n = top_n or cfg.get("analyzer", {}).get("top_n", 10)
    with get_session() as s:
        ids = s.scalars(select(Product.id).where(Product.excluded.is_(False))
                        .order_by(Product.score.desc().nulls_last()).limit(top_n)).all()
    done, cached, errors = 0, 0, []
    for i, pid in enumerate(ids):
        if progress:
            progress(f"Analyse IA {i + 1}/{len(ids)}", i / max(len(ids), 1))
        try:
            res = analyze_product(pid, cfg)
            cached += bool(res.get("_cached"))
            done += 1
        except AnalyzerError as exc:
            errors.append(f"produit {pid} : {exc}")
            if "ANTHROPIC_API_KEY" in str(exc) or "clé API" in str(exc):
                break   # inutile de continuer sans clé valide
    rescore_all(cfg)
    return {"analysés": done, "depuis le cache": cached, "erreurs": errors}
