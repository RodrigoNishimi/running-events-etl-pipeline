"""Constantes geograficas do Brasil (UFs) e validacao."""

from __future__ import annotations

# Unidades federativas do Brasil (27, incluindo o DF).
BR_UFS: frozenset[str] = frozenset(
    {
        "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
        "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
        "SP", "SE", "TO",
    }
)


def is_br_uf(value: str | None) -> bool:
    """True se `value` for uma UF brasileira valida (ex.: descarta 'BR', 'XX')."""
    return bool(value) and value.strip().upper() in BR_UFS
