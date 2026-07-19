"""Normalizacao de distancias de corrida.

As organizadoras usam rotulos livres ('5k', '5 km', 'Meia Maratona', '42K').
Aqui convertemos para quilometros padronizados, para permitir filtro e dedup.
"""

from __future__ import annotations

import re

# Distancias oficiais de atletismo (km). Meia = 21.0975, maratona = 42.195.
HALF_MARATHON_KM = 21.0975
MARATHON_KM = 42.195

# Sinonimos textuais -> km.
_NAMED = {
    "meia maratona": HALF_MARATHON_KM,
    "meia": HALF_MARATHON_KM,
    "half": HALF_MARATHON_KM,
    "maratona": MARATHON_KM,
    "marathon": MARATHON_KM,
}

# Ex.: "10k", "10 km", "10,5km", "21.1 K"
_NUMERIC = re.compile(r"(\d+(?:[.,]\d+)?)\s*k(?:m)?\b", re.IGNORECASE)


def parse_distance_km(label: str) -> float | None:
    """Extrai a distancia em km de um rotulo livre, ou None se nao aplicavel.

    >>> parse_distance_km("5k")
    5.0
    >>> parse_distance_km("Meia Maratona")
    21.0975
    >>> parse_distance_km("Corrida Kids")   # sem distancia -> None
    """
    text = label.strip().lower()

    for name, km in _NAMED.items():
        if name in text:
            return km

    match = _NUMERIC.search(text)
    if match:
        return float(match.group(1).replace(",", "."))

    return None
