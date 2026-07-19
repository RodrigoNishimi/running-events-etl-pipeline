"""Registro central dos conectores disponiveis.

O pipeline resolve conectores por nome (`--source ativo`) via este mapa.
Registre cada conector novo aqui.
"""

from __future__ import annotations

from .ativo import AtivoConnector
from .base import BaseConnector
from .iguanasports import IguanaSportsConnector
from .runningland import RunningLandConnector
from .ticketsports import TicketSportsConnector
from .yescom import YescomConnector

_CONNECTORS: dict[str, type[BaseConnector]] = {
    TicketSportsConnector.source: TicketSportsConnector,
    RunningLandConnector.source: RunningLandConnector,
    AtivoConnector.source: AtivoConnector,
    IguanaSportsConnector.source: IguanaSportsConnector,
    YescomConnector.source: YescomConnector,
}


def get_connector(source: str) -> BaseConnector:
    try:
        return _CONNECTORS[source]()
    except KeyError:
        disponiveis = ", ".join(sorted(_CONNECTORS)) or "(nenhum)"
        raise SystemExit(f"Fonte desconhecida: {source!r}. Disponiveis: {disponiveis}")


def available_sources() -> list[str]:
    return sorted(_CONNECTORS)
