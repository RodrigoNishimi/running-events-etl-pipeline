"""Retry/backoff do BaseConnector.http_get.

Regressao da falha do runner diario em CI (2026-07-25): o IP de datacenter do
GitHub Actions leva 429 do Shopify (Iguana) onde a maquina local nunca levava.
O que importa aqui: repetir o que e transitorio, NAO repetir o que e definitivo,
e honrar o `Retry-After` da fonte em vez de chutar o tempo de espera.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from corridas_etl.connectors.base import MAX_ATTEMPTS, BaseConnector, _parse_retry_after
from corridas_etl.models import RawPayload, SourceEventRecord


class _StubConnector(BaseConnector):
    """Conector minimo: so existe para exercitar a camada de rede."""

    source = "stub"

    def discover(self):        # pragma: no cover - nao usado
        return []

    def fetch(self, event_ref: str) -> RawPayload:      # pragma: no cover - nao usado
        raise NotImplementedError

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:   # pragma: no cover
        return None


@pytest.fixture
def connector(monkeypatch):
    """Conector com sleep neutralizado — o teste nao deve esperar de verdade."""
    monkeypatch.setattr("corridas_etl.connectors.base.time.sleep", lambda _s: None)
    c = _StubConnector()
    c.request_delay_seconds = 0.0
    yield c
    c.close()


def _responses(connector, statuses, headers=None):
    """Faz o cliente devolver `statuses` em sequencia; retorna a lista de chamadas."""
    calls: list[str] = []

    def fake_get(url):
        calls.append(url)
        status = statuses[min(len(calls) - 1, len(statuses) - 1)]
        return httpx.Response(
            status,
            headers=headers or {},
            request=httpx.Request("GET", url),
            text="ok" if status < 400 else "erro",
        )

    connector._client.get = fake_get
    return calls


def test_retries_429_and_succeeds(connector):
    calls = _responses(connector, [429, 429, 200])

    resp = connector.http_get("https://exemplo/x")

    assert resp.status_code == 200
    assert len(calls) == 3


def test_gives_up_after_max_attempts(connector):
    calls = _responses(connector, [429])

    with pytest.raises(httpx.HTTPStatusError):
        connector.http_get("https://exemplo/x")

    assert len(calls) == MAX_ATTEMPTS


def test_does_not_retry_403(connector):
    """403 e deterministico: repetir so incomoda a fonte (o liverun trata com browser)."""
    calls = _responses(connector, [403])

    with pytest.raises(httpx.HTTPStatusError):
        connector.http_get("https://exemplo/x")

    assert len(calls) == 1


def test_retries_transport_error_then_succeeds(connector):
    calls: list[str] = []

    def fake_get(url):
        calls.append(url)
        if len(calls) == 1:
            raise httpx.ConnectTimeout("timeout", request=httpx.Request("GET", url))
        return httpx.Response(200, request=httpx.Request("GET", url), text="ok")

    connector._client.get = fake_get

    assert connector.http_get("https://exemplo/x").status_code == 200
    assert len(calls) == 2


def test_honors_retry_after_header(connector, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("corridas_etl.connectors.base.time.sleep", slept.append)
    _responses(connector, [429, 200], headers={"Retry-After": "7"})

    connector.http_get("https://exemplo/x")

    assert slept == [7.0]      # a fonte mandou esperar 7s: nao chutamos o backoff


def test_per_source_delay_overrides_global(connector):
    """Fonte que pede calma (Iguana) sobrescreve o intervalo global."""
    connector.request_delay_seconds = 6.0
    assert connector._delay_seconds == 6.0

    connector.request_delay_seconds = None
    from corridas_etl.config import settings

    assert connector._delay_seconds == settings.request_delay_seconds


class TestParseRetryAfter:
    def test_seconds(self):
        assert _parse_retry_after("120") == 120.0

    def test_http_date(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=60)
        assert 55 <= (_parse_retry_after(format_datetime(future)) or 0) <= 61

    def test_past_date_is_zero(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        assert _parse_retry_after(format_datetime(past)) == 0.0

    def test_garbage_and_empty(self):
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after("logo ali") is None
