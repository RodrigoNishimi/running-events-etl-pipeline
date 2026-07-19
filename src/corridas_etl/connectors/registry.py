"""Registro central dos conectores disponiveis.

O pipeline resolve conectores por nome (`--source ativo`) via este mapa.
Registre cada conector novo aqui.
"""

from __future__ import annotations

from .base import BaseConnector
from .exemplo_ativo import ExemploAtivoConnector
from .iguanasports import IguanaSportsConnector
from .ticketsports import TicketSportsConnector
from .yescom import YescomConnector

_CONNECTORS: dict[str, type[BaseConnector]] = {
    ExemploAtivoConnector.source: ExemploAtivoConnector,
    TicketSportsConnector.source: TicketSportsConnector,
    IguanaSportsConnector.source: IguanaSportsConnector,
    YescomConnector.source: YescomConnector,
    # Proximo (mapeado em 2026-07-19):
    # - ativo: calendario JS-rendered; exige Playwright.
}


def get_connector(source: str) -> BaseConnector:
    try:
        return _CONNECTORS[source]()
    except KeyError:
        disponiveis = ", ".join(sorted(_CONNECTORS)) or "(nenhum)"
        raise SystemExit(f"Fonte desconhecida: {source!r}. Disponiveis: {disponiveis}")


def available_sources() -> list[str]:
    return sorted(_CONNECTORS)
