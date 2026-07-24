import json
from datetime import date, datetime, timedelta

from corridas_etl.connectors.tfsports import (
    TFSportsConnector,
    _distances_from_infos,
    _parse_location,
    _status,
)
from corridas_etl.models import RawPayload, RegistrationStatus

TZ = "-03:00"
_FUTURE = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d")
_PAST = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d")


def _page(ld: dict, event_data: dict) -> str:
    """Monta uma pagina com os DOIS blocos estruturados que o TFSports publica:
    JSON-LD schema.org/Event + __NEXT_DATA__ (com pageData.attributes.eventData)."""
    nd = {"props": {"pageProps": {"pageData": {"attributes": {"eventData": event_data}}}}}
    return (
        "<html><head>"
        f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'
        "</head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd, ensure_ascii=False)}</script>'
        "</body></html>"
    )


def _ld(**over) -> dict:
    base = {
        "@context": "https://schema.org", "@type": "Event",
        "name": "RioMar Recife", "startDate": _FUTURE, "endDate": _FUTURE,
        "url": "https://www.tfsports.com.br/run-series/rio-mar-recife-2026/",
        "location": {"@type": "Place",
                     "name": " Av. República do Líbano, 251 - Pina, Recife - PE, 51110-160"},
        "image": ["https://cdn.tfsports/kv.png"],
        "organizer": {"@type": "Organization", "name": "TFSports"},
    }
    base.update(over)
    return base


def _event_data(**over) -> dict:
    base = {
        "isSubscriptionClosed": None,
        "subscriptionCta": {"url": "https://link-prod.tfsports.com.br/events/riomar-recife-2026"},
        "coverImage": {"data": {"attributes": {"url": "https://cdn.tfsports/cover.png"}}},
        "infos": [
            {"title": "Descrição", "text": "<p>maior circuito, +200 mil pessoas</p>"},
            {"title": "Cronograma",
             "text": "<p>5h - Largada 21km<br>5h30 - Largada 10km<br>5h45 - Largada 5km</p>"},
        ],
    }
    base.update(over)
    return base


def _conn() -> TFSportsConnector:
    return TFSportsConnector.__new__(TFSportsConnector)


def _payload(html: str) -> RawPayload:
    return RawPayload(
        source="tfsports", source_event_id="rio-mar-recife-2026",
        source_url="https://www.tfsports.com.br/run-series/rio-mar-recife-2026/",
        fetched_at=datetime(2026, 7, 24), content_type="text/html", body=html,
    )


# --- parse -----------------------------------------------------------------

def test_parse_full_event():
    rec = _conn().parse(_payload(_page(_ld(), _event_data())))
    assert rec is not None
    assert rec.name == "RioMar Recife"
    assert rec.start_at.strftime("%Y-%m-%d") == _FUTURE
    assert rec.city == "Recife"
    assert rec.state == "PE"
    assert rec.country == "BR"
    assert rec.organizer_name == "TFSports"
    assert rec.registration_status == RegistrationStatus.OPEN     # CTA presente
    assert rec.official_url.startswith("https://link-prod.tfsports.com.br/")
    assert rec.image_url == "https://cdn.tfsports/kv.png"          # imagem do JSON-LD
    assert {d.distance_km for d in rec.distances} == {21.0, 10.0, 5.0}
    assert "Recife - PE" in rec.address


def test_image_falls_back_to_cover_when_ld_has_none():
    rec = _conn().parse(_payload(_page(_ld(image=None), _event_data())))
    assert rec.image_url == "https://cdn.tfsports/cover.png"


def test_parse_none_when_no_structured_data():
    """Etapa ainda nao publicada (shell ISR sem JSON-LD nem eventData) -> None."""
    html = '<html><body><script id="__NEXT_DATA__" type="application/json">{"props":{}}</script></body></html>'
    assert _conn().parse(_payload(html)) is None


# --- status ----------------------------------------------------------------

def test_status_open_closed_unknown():
    future = datetime.fromisoformat(f"{_FUTURE}T00:00:00{TZ}")
    past = datetime.fromisoformat(f"{_PAST}T00:00:00{TZ}")
    assert _status(_event_data(), future) == RegistrationStatus.OPEN
    assert _status(_event_data(isSubscriptionClosed=True), future) == RegistrationStatus.CLOSED
    # evento ja realizado vence tudo
    assert _status(_event_data(), past) == RegistrationStatus.CLOSED
    # sem CTA e sem flag -> nao chuta
    assert _status(_event_data(subscriptionCta={}), future) == RegistrationStatus.UNKNOWN


def test_past_event_is_closed_end_to_end():
    rec = _conn().parse(_payload(_page(_ld(startDate=_PAST, endDate=_PAST), _event_data())))
    assert rec.registration_status == RegistrationStatus.CLOSED


# --- location --------------------------------------------------------------

def test_parse_location_comma_and_hyphen_separators():
    # separador virgula antes da cidade
    assert _parse_location("Rua X, 360 - Vila Olímpia, São Paulo - SP, 04551-000") == ("São Paulo", "SP")
    # separador hifen antes da cidade (endereco de aeroporto)
    assert _parse_location("Aeroporto - Av. Rocha Pombo - Curitiba - SC, 83010-900") == ("Curitiba", "SC")
    # sem CEP (fallback)
    assert _parse_location("Praça Central - Santos - SP") == ("Santos", "SP")


def test_parse_location_unparseable_returns_none():
    assert _parse_location("") == (None, None)
    assert _parse_location("Local a definir") == (None, None)


# --- distancias ------------------------------------------------------------

def test_distances_from_cronograma_text():
    infos = [{"title": "Cronograma",
              "text": "<p>5h30 - Largada 22km<br>6h30 Largada 10km<br>7h30 5km</p>"}]
    dists = _distances_from_infos(infos)
    assert {d.distance_km for d in dists} == {22.0, 10.0, 5.0}    # horarios (5h30) nao viram distancia


def test_distances_empty_when_no_infos():
    assert _distances_from_infos(None) == []
    assert _distances_from_infos([]) == []


# --- discovery -------------------------------------------------------------

def test_discover_filters_run_series_from_sitemap(monkeypatch):
    sitemap = """<urlset>
      <url><loc>https://www.tfsports.com.br/run-series/rio-mar-recife-2026</loc></url>
      <url><loc>https://www.tfsports.com.br/tf-experience/sao-jose-2026</loc></url>
      <url><loc>https://www.tfsports.com.br/run-series/santos-ii-2026/</loc></url>
      <url><loc>https://www.tfsports.com.br/quem-somos</loc></url>
    </urlset>"""

    class _Resp:
        text = sitemap

    c = _conn()
    monkeypatch.setattr(c, "http_get", lambda url: _Resp())
    urls = list(c.discover())
    # so run-series, sempre com barra final canonica, sem tf-experience/institucionais
    assert urls == [
        "https://www.tfsports.com.br/run-series/rio-mar-recife-2026/",
        "https://www.tfsports.com.br/run-series/santos-ii-2026/",
    ]
