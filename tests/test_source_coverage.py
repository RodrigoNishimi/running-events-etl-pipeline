"""Guarda contra fonte registrada que ninguem roda nem monitora.

Bug real que ja aconteceu duas vezes: o conector e escrito e registrado, mas
esquecido em `daily.SOURCES` (nao roda) ou na lista do quality (roda, mas
ninguem percebe se parar). O runningland ficou fora do daily; liverun e
tfsports ficaram fora do quality ate 2026-07-25.
"""

from corridas_etl.connectors.registry import available_sources
from corridas_etl.pipeline.daily import SOURCES
from corridas_etl.pipeline.quality import expected_sources


def test_daily_runs_every_registered_source():
    faltando = set(available_sources()) - set(SOURCES)
    assert not faltando, f"fontes registradas que o daily nao roda: {sorted(faltando)}"


def test_daily_has_no_phantom_source():
    fantasma = set(SOURCES) - set(available_sources())
    assert not fantasma, f"daily roda fontes que nao existem no registry: {sorted(fantasma)}"


def test_quality_monitors_every_registered_source():
    faltando = set(available_sources()) - set(expected_sources())
    assert not faltando, f"fontes registradas que o quality nao monitora: {sorted(faltando)}"
