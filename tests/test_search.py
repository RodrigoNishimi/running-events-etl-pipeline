from datetime import datetime, timezone

from corridas_etl.serving.search import build_document


def _row(**kw) -> dict:
    base = {
        "id": 1, "slug": "corrida-x-abcd1234", "name": "Corrida X",
        "description": "desc", "start_at": datetime(2026, 8, 30, 6, 0, tzinfo=timezone.utc),
        "registration_status": "open", "official_url": "http://x", "image_url": None,
        "city": "São Paulo", "state": "SP", "country": "BR",
        "latitude": None, "longitude": None, "organizer_name": "Org",
        "dists": [10.0, 5.0, 5.0], "sources": ["ticketsports", "ativo"],
    }
    base.update(kw)
    return base


def test_build_document_basic():
    doc = build_document(_row())
    assert doc["id"] == 1
    assert doc["distances_km"] == [5.0, 10.0]              # ordenado, sem duplicata
    assert doc["distance_labels"] == ["5k", "10k"]
    assert doc["year"] == 2026 and doc["month"] == 8
    assert doc["month_name"] == "agosto"
    assert doc["start_timestamp"] == int(_row()["start_at"].timestamp())
    assert set(doc["sources"]) == {"ticketsports", "ativo"}
    assert "_geo" not in doc                               # sem coordenadas


def test_geo_included_when_coordinates_present():
    doc = build_document(_row(latitude=-23.55, longitude=-46.63))
    assert doc["_geo"] == {"lat": -23.55, "lng": -46.63}


def test_half_marathon_label_rounds():
    doc = build_document(_row(dists=[21.0975]))
    assert doc["distances_km"] == [21.0975]
    assert doc["distance_labels"] == ["21k"]


def test_missing_date_is_tolerated():
    doc = build_document(_row(start_at=None))
    assert doc["start_at"] is None
    assert doc["start_timestamp"] is None
    assert doc["year"] is None and doc["month_name"] is None
