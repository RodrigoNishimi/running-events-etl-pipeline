from datetime import datetime

from corridas_etl.models import CanonicalEvent, SourceEventRecord


def _rec(name: str, source: str) -> SourceEventRecord:
    return SourceEventRecord(
        source=source,
        source_event_id="1",
        name=name,
        start_at=datetime(2026, 9, 12, 19, 0),
        city="São Paulo",
        state="sp",  # deve virar "SP"
    )


def test_state_normalized_to_uf():
    assert _rec("SP Night Run", "a").state == "SP"


def test_canonical_key_is_source_independent():
    """O mesmo evento visto por fontes diferentes converge para a mesma chave."""
    a = CanonicalEvent.from_source(_rec("Maratona de São Paulo", "ativo"))
    b = CanonicalEvent.from_source(_rec("MARATONA SAO PAULO", "yescom"))
    assert a.canonical_key == b.canonical_key


def test_canonical_key_is_stable_across_runs():
    a = CanonicalEvent.from_source(_rec("SP Night Run", "ativo"))
    b = CanonicalEvent.from_source(_rec("SP Night Run", "ativo"))
    assert a.canonical_key == b.canonical_key
