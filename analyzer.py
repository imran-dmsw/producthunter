"""Génération IA via l'API Claude.

1. `generate_listing` : pour un produit avec fournisseur, Claude produit un
   JSON (format imposé par schéma) : titre FR, description Shopify en HTML,
   3 accroches pub, public cible, tags, SEO, risques, note "wow", verdict
   go/no-go. Résultats mis en cache (table `analyses`, `analyzer.cache_days`).
2. `suggest_search_queries` : traduit en un seul appel une liste de titres
   (souvent FR) en requêtes de recherche anglaises pour CJ / AliExpress.

Clé lue depuis l'environnement (fichier .env) : ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

import anthropic
from sqlalchemy import select

from db import Analysis, Product, get_session

log = logging.getLogger("product_hunter.analyzer")

SYSTEM_PROMPT = """Tu es un expert e-commerce spécialisé dans le dropshipping Shopify sur le marché \
français. Tu prépares des fiches produit prêtes à publier et tu évalues honnêtement leur potentiel \
pour un vendeur qui teste avec de la publicité Meta et TikTok. Un "no-go" argumenté vaut mieux \
qu'un enthousiasme injustifié. Tiens compte de la réglementation européenne (marquage CE, sécurité \
des produits GPSR, propriété intellectuelle, droit de rétractation de 14 jours). N'invente aucune \
caractéristique technique absente des données fournies : reste sur les bénéfices et l'usage. \
Rédige en français naturel, sans majuscules abusives ni emojis."""

LISTING_SCHEMA = {
    "type": "object",
    "properties": {
        "titre_fr": {"type": "string", "description": "Titre produit Shopify FR, 40-70 caractères, sans marque"},
        "description_html": {"type": "string", "description": "Description Shopify en HTML simple (p, ul, li, strong)"},
        "accroches": {"type": "array", "items": {"type": "string"}, "description": "Exactement 3 accroches de pub"},
        "public_cible": {"type": "string"},
        "angle_marketing": {"type": "string"},
        "probleme_resolu": {"type": "string"},
        "type_produit": {"type": "string", "description": "Type de produit Shopify (ex. Accessoires animaux)"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "4 à 8 tags Shopify"},
        "seo_title": {"type": "string", "description": "<= 70 caractères"},
        "seo_description": {"type": "string", "description": "<= 160 caractères"},
        "wow_score": {"type": "integer", "description": "0-10 : effet wow / démonstration visuelle / problème résolu"},
        "risques": {
            "type": "object",
            "properties": {"saturation": {"type": "string"}, "retours": {"type": "string"}, "legal": {"type": "string"}},
            "required": ["saturation", "retours", "legal"],
            "additionalProperties": False,
        },
        "verdict": {"type": "string", "enum": ["go", "no-go", "à creuser"]},
        "justification": {"type": "string", "description": "2-3 phrases"},
    },
    "required": ["titre_fr", "description_html", "accroches", "public_cible", "angle_marketing",
                 "probleme_resolu", "type_produit", "tags", "seo_title", "seo_description", "wow_score",
                 "risques", "verdict", "justification"],
    "additionalProperties": False,
}

QUERIES_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "integer"}, "query": {"type": "string"}},
                "required": ["id", "query"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["queries"],
    "additionalProperties": False,
}


class AnalyzerError(Exception):
    pass


def has_api_key() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY"))


def _client() -> anthropic.Anthropic:
    if not has_api_key():
        raise AnalyzerError("ANTHROPIC_API_KEY manquante : ajoutez-la dans Réglages → Clés API")
    return anthropic.Anthropic(max_retries=3)


def _call_json(client: anthropic.Anthropic, cfg: dict, system: str, prompt: str, schema: dict,
               effort: str | None = None) -> tuple[dict, str]:
    """Appel Claude avec sortie JSON imposée. Renvoie (données, modèle)."""
    acfg = cfg.get("analyzer", {})
    kwargs = dict(
        model=acfg.get("model", "claude-opus-5-5"),
        max_tokens=16000,
        system=system,
        output_config={"effort": effort or acfg.get("effort", "medium"),
                       "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    if acfg.get("use_fallbacks", True):
        # Si un filtre de sécurité décline la requête, l'API la rejoue sur un modèle de repli.
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.AuthenticationError as exc:
        raise AnalyzerError("clé API Anthropic invalide") from exc
    except anthropic.RateLimitError as exc:
        raise AnalyzerError("limite de requêtes Claude atteinte, réessayez plus tard") from exc
    except anthropic.BadRequestError as exc:
        raise AnalyzerError(f"requête refusée : {exc.message}") from exc
    except anthropic.APIStatusError as exc:
        raise AnalyzerError(f"erreur API {exc.status_code} : {exc.message}") from exc
    except anthropic.APIConnectionError as exc:
        raise AnalyzerError("connexion à l'API Claude impossible") from exc

    if response.stop_reason == "refusal":
        raise AnalyzerError("Claude a refusé la demande")
    if response.stop_reason == "max_tokens":
        raise AnalyzerError("réponse tronquée (max_tokens)")
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise AnalyzerError("réponse vide")
    return json.loads(text), response.model


# =====================================================================
# Requêtes de recherche fournisseur
# =====================================================================
def suggest_search_queries(items: list[tuple[int, str]], cfg: dict, client=None) -> dict[int, str]:
    """[(id, titre)] -> {id: requête anglaise courte} en un seul appel."""
    if not items:
        return {}
    client = client or _client()
    listing = "\n".join(f"{pid}\t{title}" for pid, title in items)
    data, _ = _call_json(
        client, cfg,
        "Tu convertis des titres de produits e-commerce en requêtes de recherche fournisseur.",
        "Pour chaque produit (id<TAB>titre), donne une requête de recherche en anglais de 2 à 5 mots, "
        "comme on la taperait sur AliExpress ou CJ Dropshipping pour trouver le même produit générique : "
        "sans marque, sans couleur ni taille, avec le nom générique de l'objet.\n\n" + listing,
        QUERIES_SCHEMA, effort="low",
    )
    return {q["id"]: q["query"].strip() for q in data.get("queries", []) if q.get("query")}


# =====================================================================
# Fiche produit
# =====================================================================
def _product_brief(p: Product) -> str:
    details = p.json_field("score_details", {})
    econ = details.get("_economics") or {}
    offer = p.selected_offer()
    competitors = p.json_field("competitors", [])
    lines = [
        f"Produit détecté : {p.title}",
        f"Catégorie : {p.category or 'inconnue'}",
        f"Concurrents qui le vendent : {len(competitors)} "
        + ", ".join(f"{c['store']} ({c['price']} €)" for c in competitors[:6] if c.get("store")),
    ]
    if offer:
        lines += [
            f"Fournisseur ({offer.supplier}) : {offer.title}",
            f"Prix d'achat {offer.price_eur} € + livraison France {offer.shipping_eur} € "
            f"({offer.delivery_days_min}-{offer.delivery_days_max} jours, {offer.shipping_method or 'méthode n/c'})",
            f"Note vendeur : {offer.rating or 'n/c'} · stock : {offer.stock if offer.stock is not None else 'n/c'} "
            f"· ventes : {offer.orders if offer.orders is not None else 'n/c'}",
        ]
    if econ:
        lines.append(f"Prix de vente conseillé : {econ['recommended_price']} € · profit net estimé "
                     f"{econ['net_profit']} € ({econ['net_margin_pct']} %) après pub et frais")
    if p.advertisers_count:
        lines.append(f"Annonceurs détectés : {p.advertisers_count}")
    if p.trend_mean is not None:
        lines.append(f"Google Trends FR : moyenne {p.trend_mean}, pente {p.trend_slope}")
    if p.description:
        lines.append(f"Description concurrente : {p.description[:1200]}")
    if offer and offer.raw_json:
        raw = offer.json_field("raw_json", {})
        desc = raw.get("description") or raw.get("productDescription") or ""
        if desc:
            lines.append(f"Description fournisseur : {str(desc)[:1500]}")
    return "\n".join(lines)


def latest_analysis(product_id: int) -> dict | None:
    with get_session() as s:
        a = s.scalar(select(Analysis).where(Analysis.product_id == product_id).order_by(Analysis.created_at.desc()))
        return {**a.result, "_date": a.created_at} if a else None


def generate_listing(product_id: int, cfg: dict, force: bool = False, client=None) -> dict:
    """Génère (ou relit en cache) la fiche Shopify d'un produit. Met à jour wow_score."""
    acfg = cfg.get("analyzer", {})
    with get_session() as s:
        p = s.get(Product, product_id)
        if p is None:
            raise AnalyzerError(f"produit {product_id} introuvable")
        if not force:
            since = datetime.utcnow() - timedelta(days=acfg.get("cache_days", 30))
            cached = s.scalar(select(Analysis).where(Analysis.product_id == product_id, Analysis.created_at >= since)
                              .order_by(Analysis.created_at.desc()))
            if cached:
                return {**cached.result, "_cached": True}
        brief = _product_brief(p)

    # Appel hors transaction : la base reste disponible pendant l'attente
    data, model = _call_json(client or _client(), cfg, SYSTEM_PROMPT,
                             "Prépare la fiche Shopify et ton évaluation pour ce produit :\n\n" + brief,
                             LISTING_SCHEMA)
    data["accroches"] = data.get("accroches", [])[:3]
    data["wow_score"] = max(0, min(10, int(data.get("wow_score", 0))))
    data["_model"] = model
    with get_session() as s:
        s.add(Analysis(product_id=product_id, model=model, result_json=json.dumps(data, ensure_ascii=False)))
        s.get(Product, product_id).wow_score = data["wow_score"]
    return data


def generate_top(cfg: dict, top_n: int | None = None, progress=None) -> dict:
    """Génère les fiches des N meilleurs produits éligibles (fournisseur trouvé)."""
    from scoring import rescore_all

    top_n = top_n or cfg.get("analyzer", {}).get("top_n", 10)
    with get_session() as s:
        ids = s.scalars(select(Product.id).where(Product.excluded.is_(False), Product.selected_offer_id.is_not(None))
                        .order_by(Product.score.desc().nulls_last()).limit(top_n)).all()
    done = cached = 0
    errors: list[str] = []
    for i, pid in enumerate(ids):
        if progress:
            progress(f"Génération IA {i + 1}/{len(ids)}", i / max(len(ids), 1))
        try:
            res = generate_listing(pid, cfg)
            cached += bool(res.get("_cached"))
            done += 1
        except AnalyzerError as exc:
            errors.append(f"produit {pid} : {exc}")
            if "ANTHROPIC_API_KEY" in str(exc) or "invalide" in str(exc):
                break
    rescore_all(cfg)
    return {"générés": done, "depuis le cache": cached, "erreurs": errors}
