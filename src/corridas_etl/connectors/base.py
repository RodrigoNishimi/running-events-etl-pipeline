"""Interface comum de conector.

Cada organizadora/fonte implementa uma subclasse de `BaseConnector`. Assim,
adicionar uma fonte nova = escrever uma classe, sem tocar no restante do pipeline.
Quando uma fonte muda de layout, apenas o seu conector quebra (isolamento).

O contrato tem tres passos:
    discover()      -> ids/urls dos eventos disponiveis na fonte
    fetch(id/url)   -> RawPayload (Bronze) — o que a fonte retornou, sem parsear
    parse(payload)  -> SourceEventRecord (Silver) — normalizado

Separar fetch de parse permite reprocessar o Bronze sem re-acessar a rede.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Iterable

import httpx

from ..config import settings
from ..models import RawPayload, SourceEventRecord


class BaseConnector(ABC):
    #: Identificador curto e estavel da fonte (ex.: "ativo", "yescom").
    source: str

    #: Versao da logica de parse(). Bumpar quando parse() passar a REINTERPRETAR
    #: payloads antigos de forma diferente (ex.: correcao de como o status e
    #: inferido). O gate incremental (pipeline/run.py) reprocessa todo
    #: source_record cuja parse_version gravada != esta, mesmo com o payload
    #: bruto inalterado — assim a correcao chega ao banco sem depender de --full.
    parse_version: int = 1

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=30.0,
            follow_redirects=True,
        )
        self._last_request_ts = 0.0

    # -- Rede (com rate limiting cortes) -----------------------------------

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        wait = settings.request_delay_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def http_get(self, url: str) -> httpx.Response:
        """GET educado: respeita o rate limit configurado por fonte."""
        self._throttle()
        resp = self._client.get(url)
        resp.raise_for_status()
        return resp

    def make_payload(
        self, source_event_id: str, body: str, *, url: str | None = None, content_type: str = "text/html"
    ) -> RawPayload:
        return RawPayload(
            source=self.source,
            source_event_id=source_event_id,
            source_url=url,
            fetched_at=datetime.now(timezone.utc),
            content_type=content_type,
            body=body,
        )

    # -- Contrato a implementar por cada fonte ------------------------------

    @abstractmethod
    def discover(self) -> Iterable[str]:
        """Retorna os identificadores (ids ou urls) dos eventos da fonte."""

    @abstractmethod
    def fetch(self, event_ref: str) -> RawPayload:
        """Baixa o conteudo bruto de um evento (camada Bronze)."""

    @abstractmethod
    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        """Converte o payload bruto em um registro normalizado (camada Silver).

        Retorna None se o payload nao for um evento valido (ex.: pagina removida).
        """

    def close(self) -> None:
        self._client.close()
