"""Exit code do runner diario.

O agendador (GitHub Actions/cron) so enxerga um numero — ele precisa dizer
QUANTAS fontes falharam, para distinguir "1 de 7 fora do ar" (rotina) de
"5 de 7" ou critico de qualidade (olhar hoje).
"""

from corridas_etl.pipeline.daily import (
    EXIT_OK,
    EXIT_QUALITY_CRITICAL,
    EXIT_SOURCES_BASE,
    MAX_REPORTABLE_FAILURES,
    exit_code,
)


def test_success_is_zero():
    assert exit_code(0, has_criticals=False) == EXIT_OK


def test_quality_critical_without_source_failure():
    assert exit_code(0, has_criticals=True) == EXIT_QUALITY_CRITICAL


def test_code_carries_how_many_sources_failed():
    assert exit_code(1, has_criticals=False) == 11
    assert exit_code(2, has_criticals=False) == 12
    assert exit_code(7, has_criticals=False) == 17
    # O agendador recupera a contagem subtraindo a base.
    assert exit_code(3, has_criticals=False) - EXIT_SOURCES_BASE == 3


def test_source_failure_wins_over_quality_critical():
    """Critico de qualidade quase sempre e consequencia da fonte que nao coletou."""
    assert exit_code(2, has_criticals=True) == 12


def test_stays_in_posix_range():
    """Nunca colide com os codigos do shell (126/127/128+sinal) nem estoura 255."""
    assert exit_code(10_000, has_criticals=False) == EXIT_SOURCES_BASE + MAX_REPORTABLE_FAILURES
    assert exit_code(10_000, has_criticals=False) < 126
