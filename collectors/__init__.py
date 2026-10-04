"""Collecteurs de données produits.

Chaque collecteur expose une fonction `collect(cfg) -> CollectResult` qui ne
lève jamais d'exception : les erreurs sont stockées dans `result.errors` pour
que la collecte continue avec les autres sources si l'une est bloquée.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CollectResult:
    source: str
    items: list[dict] = field(default_factory=list)   # dicts prêts pour db.upsert_product
    errors: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.items) or not self.errors

    def summary(self) -> dict:
        return {"items": len(self.items), "errors": self.errors, "info": self.info}
