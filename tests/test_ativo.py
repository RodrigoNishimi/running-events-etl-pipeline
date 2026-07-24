import json
from datetime import date, datetime, timedelta

from corridas_etl.connectors.ativo import AtivoConnector
from corridas_etl.models import RawPayload, RegistrationStatus

_FUTURE = (date.today() + timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")
_PAST = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d 00:00:00")

# Item reduzido do /eventos.json real (capturado 2026-07-19). Data no futuro
# para representar um evento com inscricao (o dump so traz eventos vindouros).
_ITEM = {
    "id_evento": "40315",
    "post_title": "Corrida do Bem Eco - Marab&aacute;",
    "dt_evento": _FUTURE,
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
    # fl_resultado=1 mesmo em provas futuras: NAO indica inscricao encerrada
    # (bug antigo). Deixado como 1 de proposito para travar a regressao.
    "fl_resultado": "1",
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
    assert {d.distance_km for d in rec.distances} == {5.0, 10.0}
    assert rec.official_url == "https://pay.ativo.com/evento/40315"
    # Prova futura, nao suspensa -> inscricao presumida aberta (mesmo com
    # fl_resultado=1, que NAO e sinal de inscricao encerrada).
    assert rec.registration_status == RegistrationStatus.OPEN


def test_past_event_is_closed():
    item = {**_ITEM, "dt_evento": _PAST}
    rec = _conn().parse(_payload(item))
    assert rec.registration_status == RegistrationStatus.CLOSED


def test_suspended_event_is_closed():
    item = {**_ITEM, "fl_suspenso": 1}
    rec = _conn().parse(_payload(item))
    assert rec.registration_status == RegistrationStatus.CLOSED


def test_fl_resultado_does_not_close_future_event():
    """Regressao: fl_resultado=1 nao pode encerrar a inscricao de prova futura."""
    item = {**_ITEM, "fl_resultado": "1", "dt_evento": _FUTURE}
    rec = _conn().parse(_payload(item))
    assert rec.registration_status == RegistrationStatus.OPEN


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
