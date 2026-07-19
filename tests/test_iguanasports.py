import json
from datetime import datetime

from corridas_etl.connectors.iguanasports import IguanaSportsConnector
from corridas_etl.models import CanonicalEvent, RawPayload, RegistrationStatus

# Fixture reduzida do /products/run-the-bridge-2026.js (storefront; capturado
# 2026-07-19). O `.js` — ao contrario do `.json` — traz `available`.
_PRODUCT = {
    "id": 10684976300305,
    "title": "Brooks Run The Bridge 2026",
    "handle": "run-the-bridge-2026",
    "available": True,
    "description": "<p>A corrida da ponte.</p>",
    "featured_image": "//cdn.shopify.com/x.jpg",
    "images": ["//cdn.shopify.com/x.jpg"],
    "options": [
        {"name": "Distância", "position": 1, "values": ["5K", "10K", "15K", "30K"]},
        {"name": "Camiseta", "position": 2, "values": ["P", "M", "G"]},
    ],
    "variants": [
        {"title": "5K / P", "price": 23990, "available": True},
        {"title": "10K / M", "price": 25990, "available": False},
    ],
}


def _payload(product: dict, handle: str = "run-the-bridge-2026") -> RawPayload:
    return RawPayload(
        source="iguanasports",
        source_event_id=handle,
        source_url=f"https://iguanasports.com.br/products/{handle}",
        fetched_at=datetime(2026, 7, 19, 12, 0),
        content_type="application/json",
        body=json.dumps(product, ensure_ascii=False),
    )


def _conn() -> IguanaSportsConnector:
    return IguanaSportsConnector.__new__(IguanaSportsConnector)


def test_parse_product():
    rec = _conn().parse(_payload(_PRODUCT))
    assert rec is not None
    assert rec.name == "Brooks Run The Bridge 2026"
    assert rec.organizer_name == "Iguana Sports"
    assert rec.registration_status == RegistrationStatus.OPEN
    assert {d.distance_km for d in rec.distances} == {5.0, 10.0, 15.0, 30.0}


def test_audience_products_converge_to_same_canonical_event():
    """'X' e 'X - Idosos e estudantes' devem colapsar no mesmo evento canonico."""
    base = _conn().parse(_payload(_PRODUCT))

    elderly = json.loads(json.dumps(_PRODUCT))
    elderly["title"] = "Brooks Run The Bridge 2026 - Idosos e estudantes"
    elderly["handle"] = "run-the-bridge-2026-idosos"
    other = _conn().parse(_payload(elderly, handle="run-the-bridge-2026-idosos"))

    assert other.name == base.name
    key_a = CanonicalEvent.from_source(base).canonical_key
    key_b = CanonicalEvent.from_source(other).canonical_key
    assert key_a == key_b


def test_sold_out_when_product_unavailable():
    sold_out = json.loads(json.dumps(_PRODUCT))
    sold_out["available"] = False
    for v in sold_out["variants"]:
        v["available"] = False
    rec = _conn().parse(_payload(sold_out))
    assert rec.registration_status == RegistrationStatus.SOLD_OUT


def test_image_url_protocol_relative():
    rec = _conn().parse(_payload(_PRODUCT))
    assert rec.image_url == "https://cdn.shopify.com/x.jpg"
