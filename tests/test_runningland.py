import json
from datetime import datetime

from corridas_etl.connectors.runningland import RunningLandConnector, _parse_date
from corridas_etl.models import RawPayload, RegistrationStatus

# Payload Bronze como o fetch grava: item cru do GraphQL (capturado 2026-07-19)
# + atributos resolvidos daquele item.
_BODY = {
    "item": {
        "id": 53317,
        "name": "Blue Line - São Paulo 2026",
        "sku": "BLN26SP1",
        "url_key": "blue-line-s-o-paulo-2026",
        "stock_status": "IN_STOCK",
        "event_product": 1,
        "event_date": "2026-09-27 03:00:00",
        "event_region": 53,
        "event_city": 26,
        "event_modality": "38,41",
        "thumbnail": {"url": "https://magento.runningland.com.br/media/x.jpg"},
        "price_range": {"minimum_price": {"regular_price": {"currency": "BRL", "value": 209.99}}},
    },
    "resolved": {"region": "SP", "city": "São Paulo", "modalities": ["5K", "10K"]},
}


def _payload(body: dict) -> RawPayload:
    return RawPayload(
        source="runningland",
        source_event_id=str(body["item"]["id"]),
        source_url="https://www.runningland.com.br/blue-line-s-o-paulo-2026",
        fetched_at=datetime(2026, 7, 19),
        content_type="application/json",
        body=json.dumps(body, ensure_ascii=False),
    )


def _conn() -> RunningLandConnector:
    return RunningLandConnector.__new__(RunningLandConnector)


def test_parse_event():
    rec = _conn().parse(_payload(_BODY))
    assert rec is not None
    assert rec.name == "Blue Line - São Paulo 2026"
    assert rec.organizer_name == "Running Land"
    assert (rec.city, rec.state, rec.country) == ("São Paulo", "SP", "BR")
    # so a data importa (o horario da fonte e artefato de UTC/cadastro)
    assert rec.start_at.date() == datetime(2026, 9, 27).date()
    assert {d.distance_km for d in rec.distances} == {5.0, 10.0}
    assert rec.registration_status == RegistrationStatus.OPEN


def test_out_of_stock_is_sold_out():
    body = json.loads(json.dumps(_BODY))
    body["item"]["stock_status"] = "OUT_OF_STOCK"
    rec = _conn().parse(_payload(body))
    assert rec.registration_status == RegistrationStatus.SOLD_OUT


def test_invalid_region_becomes_null_state():
    body = json.loads(json.dumps(_BODY))
    body["resolved"]["region"] = " "        # opcao vazia da loja
    rec = _conn().parse(_payload(body))
    assert rec.state is None


def test_parse_date_drops_unreliable_time():
    dt = _parse_date("2026-11-01 14:48:00")
    assert dt.hour == 0 and dt.date() == datetime(2026, 11, 1).date()
    assert _parse_date(None) is None
    assert _parse_date("lixo") is None
