from datetime import datetime

from corridas_etl.resolution.matcher import Decision, EventForMatch, match


def _ev(id: int, name: str, **kw) -> EventForMatch:
    return EventForMatch(id=id, name=name, **kw)


def test_sponsor_prefix_merges():
    """Caso real: 'Run The Bridge 2026' vs 'Brooks Run The Bridge 2026',
    mesmas distancias, sem data/cidade (Iguana)."""
    dists = frozenset({5.0, 10.0, 15.0, 30.0})
    a = _ev(1, "Run The Bridge 2026", distances_km=dists)
    b = _ev(2, "Brooks Run The Bridge 2026", distances_km=dists)
    result = match(a, b)
    assert result.decision == Decision.MERGE, result


def test_kids_variant_never_auto_merges():
    """'Athenas Run Longer' vs 'Athenas KIDS Run Longer' e sub-evento distinto."""
    a = _ev(1, "Mizuno Athenas Run Longer 2026", distances_km=frozenset({7.0, 14.0}))
    b = _ev(2, "Athenas KIDS Run Longer 2026")
    result = match(a, b)
    assert result.decision != Decision.MERGE
    assert result.guard is not None


def test_different_years_are_distinct_editions():
    a = _ev(1, "Maratona de Sao Paulo 2025")
    b = _ev(2, "Maratona de Sao Paulo 2026")
    result = match(a, b)
    assert result.decision != Decision.MERGE
    assert result.guard is not None


def test_cross_source_same_event_with_date_and_city():
    a = _ev(
        1, "MEIA MARATONA DO MARCO ZERO",
        start_at=datetime(2026, 7, 26, 6, 0), city="Recife", state="PE",
    )
    b = _ev(
        2, "2º Meia Maratona do Marco Zero",
        start_at=datetime(2026, 7, 26), city="Recife", state="PE",
    )
    assert match(a, b).decision == Decision.MERGE


def test_far_dates_are_distinct():
    a = _ev(1, "Corrida da Cidade", start_at=datetime(2026, 3, 1))
    b = _ev(2, "Corrida da Cidade", start_at=datetime(2026, 11, 20))
    assert match(a, b).decision == Decision.DISTINCT


def test_conflicting_states_are_distinct():
    a = _ev(1, "Night Run 5K", state="SP", start_at=datetime(2026, 8, 1))
    b = _ev(2, "Night Run 5K", state="BA", start_at=datetime(2026, 8, 1))
    assert match(a, b).decision == Decision.DISTINCT


def test_same_uf_different_cities_penalized():
    """Nomes genericos iguais em cidades diferentes da mesma UF nao devem nem
    entrar na fila de revisao (licao da triagem real de 2026-07-19)."""
    a = _ev(1, "1ª Meia Maratona de Jundiai",
            start_at=datetime(2026, 8, 23), city="Jundiaí", state="SP",
            distances_km=frozenset({21.0975}))
    b = _ev(2, "22ª Meia Maratona de Sao Bernardo do Campo",
            start_at=datetime(2026, 8, 23), city="São Bernardo do Campo", state="SP",
            distances_km=frozenset({21.0975}))
    assert match(a, b).decision == Decision.DISTINCT


def test_unrelated_events_are_distinct():
    a = _ev(1, "Corrida do Pantanal", state="MS")
    b = _ev(2, "Night Run Sao Paulo", state="SP")
    assert match(a, b).decision == Decision.DISTINCT
