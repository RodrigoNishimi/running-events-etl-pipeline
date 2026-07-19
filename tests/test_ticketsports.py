import json
from datetime import datetime

from corridas_etl.connectors.ticketsports import (
    TicketSportsConnector,
    _distances_from_title,
    _parse_city_state,
)
from corridas_etl.models import RawPayload, RegistrationStatus

# Fixture reduzida do JSON-LD real da pagina /e/MARATONA+DO+LITORAL-74246
# (capturado em 2026-07-19). Se o site mudar o formato, atualize aqui.
_JSON_LD = {
    "@context": "https://schema.org",
    "@type": "SportsEvent",
    "name": "MARATONA DO LITORAL",
    "startDate": "2026-07-25T15:00",
    "eventStatus": "https://schema.org/EventScheduled",
    "location": {
        "@type": "Place",
        "name": "Praça Dante Luiz Júnior: Praça Dante Luiz Júnior, Matinhos, PR, Brasil",
    },
    "offers": {"@type": "Offer", "availability": "https://schema.org/InStock"},
    "organizer": {"@type": "Organization", "name": "SportS360 Consultoria e Eventos LTDA"},
    "image": ["https://cdn.ticketsports.com.br/x.png"],
    "description": "Confira o evento MARATONA DO LITORAL.",
    "url": "https://www.ticketsports.com.br/e/MARATONA+DO+LITORAL-74246",
}


def _payload(ld: dict) -> RawPayload:
    html = (
        "<html><head><script type=\"application/ld+json\">"
        + json.dumps(ld, ensure_ascii=False)
        + "</script></head><body></body></html>"
    )
    return RawPayload(
        source="ticketsports",
        source_event_id="74246",
        source_url="https://www.ticketsports.com.br/e/MARATONA+DO+LITORAL-74246",
        fetched_at=datetime(2026, 7, 19, 12, 0),
        body=html,
    )


def test_parse_json_ld_event():
    rec = TicketSportsConnector.parse(TicketSportsConnector.__new__(TicketSportsConnector), _payload(_JSON_LD))
    assert rec is not None
    assert rec.name == "MARATONA DO LITORAL"
    assert rec.city == "Matinhos"
    assert rec.state == "PR"
    assert rec.organizer_name == "SportS360 Consultoria e Eventos LTDA"
    assert rec.registration_status == RegistrationStatus.OPEN
    assert rec.start_at.year == 2026 and rec.start_at.hour == 15
    assert rec.start_at.tzinfo is not None


def test_parse_ignores_non_event_pages():
    conn = TicketSportsConnector.__new__(TicketSportsConnector)
    ld = {"@type": "WebSite", "name": "Ticket Sports"}
    assert conn.parse(_payload(ld)) is None


def test_event_id_from_url():
    assert TicketSportsConnector._event_id(
        "https://www.ticketsports.com.br/e/MARATONA+DO+LITORAL-74246"
    ) == "74246"


def test_parse_city_state_variants():
    assert _parse_city_state("Praça X: Praça X, Matinhos, PR, Brasil") == ("Matinhos", "PR")
    assert _parse_city_state("Rio de Janeiro, RJ") == ("Rio de Janeiro", "RJ")
    assert _parse_city_state("Não informado") == (None, None)
    # numero de rua no lugar da cidade -> so a UF (caso real de 2026-07-19)
    assert _parse_city_state("Av. Brasil, 150, RJ, Brasil") == (None, "RJ")
    # nome do local grudado com ':' -> fica so a cidade
    assert _parse_city_state("AABB - ARACAJU : Aracaju, SE, Brasil") == ("Aracaju", "SE")


def test_distances_from_title():
    kms = {d.distance_km for d in _distances_from_title("NIGHT RUN 5K e 10K - 3ª edição")}
    assert kms == {5.0, 10.0}
    # "2026" e "3ª" nao devem virar distancia
    assert _distances_from_title("CORRIDA DA PADROEIRA 2026") == []
    meia = _distances_from_title("2º MEIA MARATONA DO MARCO ZERO")
    assert meia[0].distance_km == 21.0975
