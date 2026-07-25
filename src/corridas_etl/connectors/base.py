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

import logging
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Iterable

import httpx

from ..config import settings
from ..models import RawPayload, SourceEventRecord

log = logging.getLogger(__name__)

# Status que valem uma nova tentativa: sao TRANSITORIOS (a fonte esta viva, so
# pediu calma ou teve um soluco). 403/404 ficam de fora de proposito — sao
# deterministicos, repetir so gasta tempo e incomoda a fonte.
#
# Passou a importar quando o pipeline saiu da maquina local para a nuvem: em CI
# o IP e de datacenter e compartilhado com o mundo todo, entao 429/503 aparecem
# em situacoes que nunca ocorriam rodando em casa.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_MAX_SECONDS = 60.0


class BaseConnector(ABC):
    #: Identificador curto e estavel da fonte (ex.: "ativo", "yescom").
    source: str

    #: Versao da logica de parse(). Bumpar quando parse() passar a REINTERPRETAR
    #: payloads antigos de forma diferente (ex.: correcao de como o status e
    #: inferido). O gate incremental (pipeline/run.py) reprocessa todo
    #: source_record cuja parse_version gravada != esta, mesmo com o payload
    #: bruto inalterado — assim a correcao chega ao banco sem depender de --full.
    parse_version: int = 1

    #: Intervalo minimo entre requisicoes DESTA fonte, em segundos. None = usa o
    #: global (ETL_REQUEST_DELAY_SECONDS). Suba em fontes que respondem 429 — e
    #: mais educado (e mais rapido no fim) esperar do que apanhar e repetir.
    request_delay_seconds: float | None = None

    def __init__(self) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=30.0,
            follow_redirects=True,
        )
        self._last_request_ts = 0.0

    # -- Rede (com rate limiting cortes) -----------------------------------

    @property
    def _delay_seconds(self) -> float:
        if self.request_delay_seconds is not None:
            return self.request_delay_seconds
        return settings.request_delay_seconds

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_ts
        wait = self._delay_seconds - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request_ts = time.monotonic()

    def http_get(self, url: str) -> httpx.Response:
        """GET educado: rate limit da fonte + retry com backoff no que e transitorio.

        Repete apenas RETRY_STATUSES e falhas de transporte (DNS/conexao/timeout),
        honrando o `Retry-After` quando a fonte informa quanto esperar. Erros
        deterministicos (403, 404, ...) sobem na primeira tentativa.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._throttle()
            last = attempt == MAX_ATTEMPTS
            try:
                resp = self._client.get(url)
            except httpx.TransportError as exc:
                if last:
                    raise
                self._backoff(attempt, None, url, reason=type(exc).__name__)
                continue

            if resp.status_code in RETRY_STATUSES and not last:
                self._backoff(
                    attempt, resp.headers.get("Retry-After"), url, reason=str(resp.status_code)
                )
                continue

            resp.raise_for_status()   # na ultima tentativa, o erro sobe daqui
            return resp

        raise RuntimeError("inalcancavel: a ultima tentativa sempre retorna ou levanta")

    def _backoff(self, attempt: int, retry_after: str | None, url: str, *, reason: str) -> None:
        """Espera antes de repetir: `Retry-After` da fonte ou backoff exponencial."""
        delay = _parse_retry_after(retry_after)
        if delay is None:
            delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            delay += random.uniform(0, delay * 0.25)   # jitter: nao sincroniza retries
        delay = min(delay, BACKOFF_MAX_SECONDS)
        log.warning(
            "%s: %s em %s — tentativa %d/%d, aguardando %.1fs",
            self.source, reason, url, attempt, MAX_ATTEMPTS, delay,
        )
        time.sleep(delay)
        # A espera ja serve como intervalo entre requisicoes: nao cobrar de novo.
        self._last_request_ts = time.monotonic()

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


def _parse_retry_after(value: str | None) -> float | None:
    """`Retry-After` -> segundos de espera. Aceita os dois formatos do HTTP:
    delta em segundos ("120") ou data absoluta ("Wed, 21 Oct 2026 07:28:00 GMT").
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
