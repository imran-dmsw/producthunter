"""Chargement / sauvegarde de config.yaml et des variables d'environnement."""
from __future__ import annotations

import copy
import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

# Charge .env une seule fois (les clés ne sont jamais écrites dans le code).
load_dotenv(ROOT / ".env")


def load_config() -> dict:
    """Relit config.yaml à chaque appel (les réglages peuvent changer à chaud)."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _merge(target, source) -> None:
    """Recopie `source` dans le document ruamel `target` en conservant les commentaires."""
    for key in list(target.keys()):
        if key not in source:
            del target[key]
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        elif isinstance(value, list) and isinstance(target.get(key), list):
            if list(target[key]) != value:
                target[key][:] = value      # sur place : garde le style [a, b] ou "- a"
        elif target.get(key) != value:
            target[key] = value


def save_config(cfg: dict) -> None:
    """Écrit config.yaml (page Réglages) sans perdre les commentaires du fichier."""
    from ruamel.yaml import YAML

    rt = YAML()
    rt.preserve_quotes = True
    rt.width = 4096                       # pas de retour à la ligne forcé
    rt.indent(mapping=2, sequence=4, offset=2)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        doc = rt.load(f)
    _merge(doc, copy.deepcopy(cfg))
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        rt.dump(doc, f)


def db_path() -> Path:
    p = Path(os.getenv("PRODUCT_HUNTER_DB", "data/product_hunter.db"))
    return p if p.is_absolute() else ROOT / p
