import pytest

from corridas_etl.utils.distances import HALF_MARATHON_KM, MARATHON_KM, parse_distance_km


@pytest.mark.parametrize(
    "label,expected",
    [
        ("5k", 5.0),
        ("5 km", 5.0),
        ("10K", 10.0),
        ("21,1km", 21.1),
        ("Meia Maratona", HALF_MARATHON_KM),
        ("meia", HALF_MARATHON_KM),
        ("Maratona", MARATHON_KM),
        ("Corrida Kids", None),
        ("Caminhada", None),
    ],
)
def test_parse_distance_km(label, expected):
    assert parse_distance_km(label) == expected
