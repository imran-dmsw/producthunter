"""Calcul de la marge réelle par commande et du prix de vente conseillé.

Profit net = prix de vente
             - prix d'achat - livraison France          (coût rendu)
             - frais de paiement   (economics.payment_fees_pct % du prix)
             - coût pub estimé     (economics.ad_cost_pct % du prix)

Prix conseillé :
- prix « plancher » qui assure la marge nette visée (target_net_margin_pct) ;
- si les concurrents vendent plus cher (médiane), on s'aligne sur eux
  (le marché accepte ce prix) ; sinon on garde le plancher et on le signale.
"""
from __future__ import annotations

import math
from statistics import median


def round_up_ending(value: float, ending: float = 0.90) -> float:
    """Plus petit prix x,90 >= value (ex. 27,31 -> 27,90 ; 27,95 -> 28,90)."""
    base = math.floor(value)
    candidate = base + ending
    return round(candidate if candidate >= value - 1e-9 else candidate + 1, 2)


def round_down_ending(value: float, ending: float = 0.90) -> float:
    """Plus grand prix x,90 <= value (ex. 39,95 -> 39,90 ; 40,00 -> 39,90)."""
    base = math.floor(value)
    candidate = base + ending
    return round(candidate if candidate <= value + 1e-9 else candidate - 1, 2)


def economics(landed_cost: float, competitor_prices: list[float], cfg: dict) -> dict:
    """Économie unitaire à partir du coût rendu (achat + livraison, en €)."""
    e = cfg["economics"]
    variable_pct = (e["payment_fees_pct"] + e["ad_cost_pct"]) / 100
    target = e["target_net_margin_pct"] / 100
    ending = e.get("price_ending", 0.90)

    denom = 1 - variable_pct - target
    if denom <= 0:
        raise ValueError("frais + pub + marge visée >= 100 % du prix : réglages incohérents")
    floor_price = round_up_ending(landed_cost / denom, ending)

    prices = [p for p in competitor_prices if p]
    market = round(median(prices), 2) if prices else None
    if market and market >= floor_price:
        recommended = max(round_down_ending(market, ending), floor_price)
        basis = "aligné sur la médiane des concurrents"
    else:
        recommended = floor_price
        basis = ("concurrents moins chers : prix plancher pour la marge visée" if market
                 else "aucun prix concurrent : prix plancher pour la marge visée")

    payment = round(recommended * e["payment_fees_pct"] / 100, 2)
    ads = round(recommended * e["ad_cost_pct"] / 100, 2)
    net = round(recommended - landed_cost - payment - ads, 2)
    return {
        "recommended_price": recommended,
        "market_price": market,
        "floor_price": floor_price,
        "landed_cost": round(landed_cost, 2),
        "payment_fees": payment,
        "ad_cost": ads,
        "net_profit": net,
        "net_margin_pct": round(net / recommended * 100, 1) if recommended else None,
        "basis": basis,
    }


if __name__ == "__main__":  # python margins.py
    cfg = {"economics": {"payment_fees_pct": 3, "ad_cost_pct": 25, "target_net_margin_pct": 20, "price_ending": 0.9}}
    for landed, comp in [(12.0, [39.9, 44.9, 49.9]), (12.0, []), (25.0, [29.9])]:
        print(landed, comp, economics(landed, comp, cfg))
