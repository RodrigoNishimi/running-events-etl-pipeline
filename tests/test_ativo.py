import json
from datetime import datetime

from corridas_etl.connectors.ativo import AtivoConnector
from corridas_etl.models import RawPayload, RegistrationStatus

# Item reduzido do /eventos.json real (capturado 2026-07-19).
_ITEM = {
    "id_evento": "40315",
    "post_title": "Corrida do Bem Eco - Marab&aacute;",
    "dt_evento": "2026-08-09 00:00:00",
    "ds_cidade": "Marab&aacute;",
    "ds_estado": "PA",
    "nome_organizador": "Eco Eventos",
    "distancias": [
        {"ds_distancia": "5k"},
        {"ds_distancia": "10k"},
    ],
    "thumbnail": "https://media.ativo.com/upload/evento/40315/img_logo_evento.jpg",
    "post_json": "https://www.ativo.com/calendario/eventos/america-do-sul/br/pa/maraba/corrida-de-rua/40315/x/index.json",
    "fl_suspenso": 0,
    "fl_resultado": "0",
}


def _payload(item: dict) -> RawPayload:
    return RawPayload(
        source="ativo",
        source_event_id=str(item["id_evento"]),
        source_url="https://www.ativo.com/x",
        fetched_at=datetime(2026, 7, 19),
        content_type="application/json",
        body=json.dumps(item, ensure_ascii=False),
    )


def _conn() -> AtivoConnector:
    return AtivoConnector.__new__(AtivoConnector)


def test_parse_event():
    rec = _conn().parse(_payload(_ITEM))
    assert rec is not None
    assert rec.name == "Corrida do Bem Eco - Marabá"       # entidade HTML resolvida
    assert rec.city == "Marabá"
    assert rec.state == "PA"
    assert rec.country == "BR"
    assert rec.start_at.date() == datetime(2026, 8, 9).date()
    assert {d.distance_km for d in rec.distances} == {5.0, 10.0}
    assert rec.official_url == "https://pay.ativo.com/evento/40315"
    assert rec.registration_status == RegistrationStatus.UNKNOWN


def test_finished_event_is_closed():
    item = {**_ITEM, "fl_resultado": "1"}
    rec = _conn().parse(_payload(item))
    assert rec.registration_status == RegistrationStatus.CLOSED


def test_country_from_post_json_path():
    item = {
        **_ITEM,
        "post_json": "https://www.ativo.com/calendario/eventos/america-do-sul/uy/mo/punta/corrida-de-rua/1/x/index.json",
        "ds_estado": "MO",
    }
    rec = _conn().parse(_payload(item))
    assert rec.country == "UY"
    assert rec.state is None          # subdivisão estrangeira não é UF


def test_empty_distances_ok():
    item = {**_ITEM, "distancias": []}
    rec = _conn().parse(_payload(item))
    assert rec.distances == []
