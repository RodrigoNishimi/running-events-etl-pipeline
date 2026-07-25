import json
from datetime import date, datetime, timedelta

import httpx
import pytest

from corridas_etl.connectors.liverun import (
    LiveRunConnector,
    _event_datetime,
    _extract_page_extras,
    _map_status,
    _name_type,
    _parse_calendar,
)
from corridas_etl.models import RawPayload, RegistrationStatus

# Calendario reduzido: cada etapa e um <a href="/etapa/slug"> cujo texto reune,
# em pedacos, cidade+UF, dd/mm, distancias e status (como o site real). O
# primeiro <a> simula um banner sem dados uteis (deve ser descartado).
_CALENDAR = """
<html><body>
  <a href="/etapa/banner"><div class="css-x">.css-x{color:red}</div></a>
  <a href="/etapa/live-run-sorocaba-2026">
     <span>Inscreva-se</span><span>Sorocaba - SP</span><span>26/07</span>
     <span>5 Km</span><span>10 Km</span><span>CORRIDA KIDS</span>
     <span>Inscrições abertas</span>
  </a>
  <a href="/etapa/live42k-brasilia">
     <span>Brasília - DF</span><span>01/08</span>
     <span>42 km</span><span>21 km</span><span>10 km</span><span>5 km</span>
     <span>Últimas vagas</span>
  </a>
</body></html>
"""

_EVENT_PAGE = """
<html><body>
  <h1>Sorocaba-SP</h1>
  <h3>Av. Dom Aguirre, 714 - Jardim Maria do Carmo, Sorocaba - SP, 18090-001</h3>
  <img src="https://imagens.liveoficial.com.br/app-experience/events/kits/abc.png"/>
  <img src="https://imagens.liveoficial.com.br/app-experience/events/POSTER.png"/>
</body></html>
"""


def _conn() -> LiveRunConnector:
    return LiveRunConnector.__new__(LiveRunConnector)


def _payload(body: dict) -> RawPayload:
    return RawPayload(
        source="liverun",
        source_event_id=body["slug"],
        source_url=body.get("event_url"),
        fetched_at=datetime(2026, 7, 24),
        content_type="application/json",
        body=json.dumps(body, ensure_ascii=False),
    )


# --- discovery / calendario -----------------------------------------------

def test_parse_calendar_extracts_cards():
    cards = dict(_parse_calendar(_CALENDAR))
    assert "banner" not in cards                      # banner sem dados foi descartado
    assert set(cards) == {"live-run-sorocaba-2026", "live42k-brasilia"}

    soro = cards["live-run-sorocaba-2026"]
    assert soro["city"] == "Sorocaba"
    assert soro["uf"] == "SP"
    assert (soro["day"], soro["month"]) == (26, 7)
    assert soro["distance_labels"] == ["5 Km", "10 Km"]   # "CORRIDA KIDS" fora
    assert "abert" in soro["status_text"].lower()


def test_extract_page_extras_prefers_poster_over_kit_image():
    address, image = _extract_page_extras(_EVENT_PAGE)
    assert address == "Av. Dom Aguirre, 714 - Jardim Maria do Carmo, Sorocaba - SP, 18090-001"
    # imagem do evento, nao o thumbnail do kit (/kits/)
    assert image.endswith("/events/POSTER.png")


# --- fallback do WAF (403 em CI) -------------------------------------------
#
# Regressao do runner diario de 2026-07-25: do IP de datacenter do GitHub
# Actions o /calendario voltou 403. O conector passou a se anunciar como browser
# e, se ainda assim for barrado, refaz o unico request via Playwright.

def _http_error(conn, status: int):
    """Faz o http_get do conector levantar `status` como o httpx levantaria."""
    def raise_status(url):
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError(
            f"{status}", request=request, response=httpx.Response(status, request=request)
        )

    conn.http_get = raise_status


def test_calendar_falls_back_to_browser_on_403(monkeypatch):
    conn = _conn()
    _http_error(conn, 403)
    monkeypatch.setattr(
        "corridas_etl.utils.render.page_html", lambda url, **kw: _CALENDAR
    )

    cards = dict(_parse_calendar(conn._calendar_html()))

    assert set(cards) == {"live-run-sorocaba-2026", "live42k-brasilia"}


def test_calendar_does_not_fall_back_on_404(monkeypatch):
    """404 nao e WAF: nao gasta um Chromium — o erro sobe e isola a fonte."""
    conn = _conn()
    _http_error(conn, 404)
    monkeypatch.setattr(
        "corridas_etl.utils.render.page_html",
        lambda url, **kw: pytest.fail("nao deveria abrir o browser em 404"),
    )

    with pytest.raises(httpx.HTTPStatusError):
        conn._calendar_html()


def test_connector_announces_browser_ua():
    conn = LiveRunConnector()
    try:
        assert "Mozilla/5.0" in conn._client.headers["User-Agent"]
    finally:
        conn.close()


# --- parse -----------------------------------------------------------------

def test_parse_composes_name_and_fields():
    body = {
        "slug": "live-run-sorocaba-2026",
        "event_url": "https://liverun.com.br/etapa/live-run-sorocaba-2026",
        "city": "Sorocaba", "uf": "SP", "day": 26, "month": 7,
        "distance_labels": ["5 Km", "10 Km"],
        "status_text": "Inscrições abertas",
        "address": "Av. Dom Aguirre, 714 - Sorocaba - SP, 18090-001",
        "image_url": "https://imagens.liveoficial.com.br/app-experience/events/POSTER.png",
    }
    rec = _conn().parse(_payload(body))
    assert rec.name == "LIVE! Run Sorocaba"
    assert rec.city == "Sorocaba"
    assert rec.state == "SP"
    assert rec.country == "BR"
    assert rec.organizer_name == "LIVE! Run"
    assert rec.registration_status == RegistrationStatus.OPEN
    assert {d.distance_km for d in rec.distances} == {5.0, 10.0}
    assert rec.official_url.endswith("/etapa/live-run-sorocaba-2026")
    assert rec.image_url.endswith("POSTER.png")


def test_parse_returns_none_without_city():
    body = {"slug": "live-run-x", "city": None, "uf": None, "day": 1, "month": 8,
            "distance_labels": [], "status_text": None}
    assert _conn().parse(_payload(body)) is None


def test_state_dropped_when_not_a_uf():
    body = {"slug": "live-run-x-2026", "city": "Cidade", "uf": "ZZ", "day": 1,
            "month": 8, "distance_labels": [], "status_text": "Inscrições abertas"}
    rec = _conn().parse(_payload(body))
    assert rec.state is None


# --- helpers ---------------------------------------------------------------

def test_name_type_from_slug():
    assert _name_type("live42k-brasilia") == "42K"
    assert _name_type("live21k-campinas-2026") == "21K"
    assert _name_type("live-experience-bonito-2026") == "Experience"
    assert _name_type("live-run-sorocaba-2026") == "Run"


def test_event_datetime_rolls_to_next_occurrence():
    today = date.today()
    fut = today + timedelta(days=40)
    dt = _event_datetime(fut.day, fut.month)
    assert dt is not None and dt.date() >= today          # proxima ocorrencia futura
    assert (dt.month, dt.day) == (fut.month, fut.day)

    past = today - timedelta(days=40)
    dt2 = _event_datetime(past.day, past.month)
    assert dt2 is not None and dt2.date() > today          # rolou para o ano seguinte
    assert (dt2.month, dt2.day) == (past.month, past.day)


def test_event_datetime_invalid_returns_none():
    assert _event_datetime(None, None) is None
    assert _event_datetime(31, 2) is None                  # 31 de fevereiro


def test_map_status():
    assert _map_status("Inscrições abertas") == RegistrationStatus.OPEN
    assert _map_status("Últimas vagas") == RegistrationStatus.OPEN
    assert _map_status("Inscrições em breve") == RegistrationStatus.COMING_SOON
    assert _map_status("Esgotado") == RegistrationStatus.SOLD_OUT
    assert _map_status("Inscrições encerradas") == RegistrationStatus.CLOSED
    assert _map_status(None) == RegistrationStatus.UNKNOWN
