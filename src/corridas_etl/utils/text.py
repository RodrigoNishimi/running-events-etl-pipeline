"""Normalizacao de texto: slugs e chaves de comparacao para dedup."""

from __future__ import annotations

import re

from unidecode import unidecode

# Palavras genericas que atrapalham o matching de nomes de eventos.
_STOPWORDS = {"corrida", "run", "running", "de", "da", "do", "e", "a", "o", "the"}


def slugify(text: str) -> str:
    """Converte um texto em slug ASCII: 'Meia Maratona de SP' -> 'meia-maratona-de-sp'."""
    ascii_text = unidecode(text).lower()
    ascii_text = re.sub(r"[^a-z0-9]+", "-", ascii_text)
    return ascii_text.strip("-")


def normalize_name(name: str) -> str:
    """Chave normalizada de um nome para blocking/matching de dedup.

    Remove acentos, pontuacao e stopwords, ordena os tokens restantes.
    'Maratona de São Paulo' e 'MARATONA SAO PAULO' colapsam na mesma chave.
    """
    ascii_text = unidecode(name).lower()
    tokens = re.findall(r"[a-z0-9]+", ascii_text)
    kept = [t for t in tokens if t not in _STOPWORDS]
    return " ".join(sorted(kept))
