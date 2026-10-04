"""Collecteur Google Trends (pytrends) : intérêt de recherche France/Europe.

Contrairement aux autres collecteurs, celui-ci enrichit des produits déjà en
base : pour chaque mot-clé (dérivé du titre, modifiable dans la fiche
produit) on calcule la moyenne récente et la pente de la tendance.

Google Trends limite très vite (HTTP 429) : on espace les requêtes, on met
en cache les résultats (cache_hours) et on plafonne le nombre de mots-clés
par collecte. Si Google bloque, on s'arrête proprement.
"""
from __future__ import annotations

import json
import logging
import random
import time
from datetime import datetime, timedelta

from sqlalchemy import or_, select

from collectors import CollectResult

log = logging.getLogger("product_hunter.trends")


class TrendsBlocked(Exception):
    pass


def _build_client():
    from pytrends.request import TrendReq  # import local : pytrends est lent à importer
    return TrendReq(hl="fr-FR", tz=-60, timeout=(10, 25))


def fetch_trend(pytrends, keyword: str, geos: list[str], timeframe: str, retries: int = 2) -> dict | None:
    """Renvoie {'mean', 'slope', 'series'} moyenné sur les zones, ou None si pas de données."""
    import pandas as pd
    from pytrends.exceptions import ResponseError, TooManyRequestsError

    frames = []
    for geo in geos:
        for attempt in range(retries + 1):
            try:
                pytrends.build_payload([keyword], timeframe=timeframe, geo=geo)
                df = pytrends.interest_over_time()
                break
            except (TooManyRequestsError, ResponseError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if attempt == retries:
                    raise TrendsBlocked(f"Google Trends a refusé la requête ({status or exc})") from exc
                time.sleep(20 * (attempt + 1) + random.uniform(0, 5))
        if df is not None and not df.empty and keyword in df:
            frames.append(df[keyword].astype(float).rename(geo))

    if not frames:
        return None
    series = pd.concat(frames, axis=1).mean(axis=1)
    if series.sum() == 0:
        return {"mean": 0.0, "slope": 0.0, "series": {}}

    n = max(len(series) // 3, 1)
    first, last = series.iloc[:n].mean(), series.iloc[-n:].mean()
    slope = (last - first) / max(series.mean(), 1.0)          # variation relative
    # Volume très faible = données bruitées (un pic isolé suffit à créer une
    # « hausse ») : on atténue la pente en dessous d'une moyenne de 10/100.
    slope *= min(1.0, series.mean() / 10)
    return {
        "mean": round(float(series.iloc[-n:].mean()), 1),         # intérêt récent (0..100)
        "slope": round(max(-1.0, min(1.0, float(slope))), 3),
        "series": {d.strftime("%Y-%m-%d"): round(float(v), 1) for d, v in series.items()},
    }


def collect(cfg: dict, session=None) -> CollectResult:
    """Met à jour les tendances des produits non exclus (prioritaires d'abord)."""
    from db import Product, get_session

    result = CollectResult("google_trends")
    tcfg = cfg.get("google_trends", {})
    if not tcfg.get("enabled", True):
        result.info.append("source désactivée")
        return result

    stale_before = datetime.utcnow() - timedelta(hours=tcfg.get("cache_hours", 24))
    try:
        pytrends = _build_client()
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"initialisation pytrends impossible : {exc}")
        return result

    with get_session() as s:
        products = s.scalars(
            select(Product)
            .where(Product.excluded.is_(False), Product.keyword.is_not(None), Product.keyword != "")
            .where(or_(Product.trend_updated_at.is_(None), Product.trend_updated_at < stale_before))
            .order_by(Product.score.desc().nulls_last(), Product.best_seller_rank.asc().nulls_last())
        ).all()

        # Regroupe par mot-clé : une requête sert plusieurs produits
        by_kw: dict[str, list] = {}
        for p in products:
            by_kw.setdefault(p.keyword.strip().lower(), []).append(p)

        done = 0
        for kw, prods in by_kw.items():
            if done >= tcfg.get("max_keywords_per_run", 15):
                result.info.append(f"{len(by_kw) - done} mots-clés reportés à la prochaine collecte")
                break
            try:
                data = fetch_trend(pytrends, kw, tcfg.get("geos", ["FR"]), tcfg.get("timeframe", "today 3-m"))
            except TrendsBlocked as exc:
                result.errors.append(f"{exc} — arrêt de la source pour cette collecte")
                break
            except Exception as exc:  # noqa: BLE001
                result.errors.append(f"'{kw}' : {type(exc).__name__} {exc}")
                continue
            finally:
                done += 1
                time.sleep(tcfg.get("delay_s", 8) * random.uniform(0.8, 1.3))

            now = datetime.utcnow()
            for p in prods:
                if data is None:   # mot-clé sans volume : on le note pour ne pas le réinterroger
                    p.trend_mean, p.trend_slope, p.trend_data = 0.0, 0.0, "{}"
                else:
                    p.trend_mean, p.trend_slope = data["mean"], data["slope"]
                    p.trend_data = json.dumps(data["series"])
                p.trend_updated_at = now
            result.items.append({"keyword": kw, "products": len(prods), **({k: data[k] for k in ("mean", "slope")} if data else {})})
            s.commit()
    result.info.append(f"{len(result.items)} mots-clés mis à jour")
    return result


if __name__ == "__main__":  # test manuel : python -m collectors.google_trends "lampe lune"
    import sys

    logging.basicConfig(level=logging.INFO)
    kw = sys.argv[1] if len(sys.argv) > 1 else "lampe lune"
    t = fetch_trend(_build_client(), kw, ["FR"], "today 3-m")
    print(kw, {k: v for k, v in (t or {}).items() if k != "series"}, f"{len((t or {}).get('series', {}))} points")
