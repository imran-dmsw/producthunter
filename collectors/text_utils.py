"""Petites fonctions texte partagées (nettoyage HTML, mot-clé, similarité)."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from bs4 import BeautifulSoup

# Mots vides retirés pour construire un mot-clé Google Trends à partir d'un titre
STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "et", "en", "pour", "avec", "sans", "au", "aux",
    "à", "a", "the", "and", "for", "with", "of", "in", "on", "to", "new", "nouveau", "nouvelle", "pro",
    "premium", "set", "lot", "pack", "pcs", "pièces", "x", "cm", "mm", "ml", "édition", "edition",
}


def strip_html(html: str | None, limit: int = 2000) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text)[:limit]


def normalize(text: str) -> str:
    """Minuscules, sans accents ni ponctuation."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9 ]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def keyword_from_title(title: str, max_words: int = 3) -> str:
    """Construit un mot-clé court et recherchable (ex. 'lampe lune 3d')."""
    words = [w for w in normalize(title).split() if w not in STOPWORDS and not w.isdigit() and len(w) > 1]
    return " ".join(words[:max_words])


def similarity(a: str, b: str) -> float:
    """Similarité 0..1 entre deux titres (mots triés pour ignorer l'ordre)."""
    na = " ".join(sorted(set(normalize(a).split())))
    nb = " ".join(sorted(set(normalize(b).split())))
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def parse_price(text: str | None) -> float | None:
    """'1 234,56 €' / '€12.99' / '29.90' -> float."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    m = re.search(r"\d[\d\s.,]*", str(text).replace(" ", " ").replace("\xa0", " "))
    if not m:
        return None
    raw = m.group(0).replace(" ", "")
    if "," in raw and "." in raw:          # 1.234,56 ou 1,234.56
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        return float(raw.rstrip("."))
    except ValueError:
        return None
