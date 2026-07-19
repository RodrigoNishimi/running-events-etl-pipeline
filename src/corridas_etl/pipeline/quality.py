"""Checks de qualidade de dados (Fase 2).

    python -m corridas_etl.pipeline.quality            # relatorio + exit code
    python -m corridas_etl.pipeline.quality --strict   # warnings tambem falham

Tres grupos de verificacao:

  SAUDE POR FONTE  o sinal mais importante de conector quebrado e silencio:
                   0 registros, ou ultima coleta velha demais.
  ANOMALIAS        registros que violam regras do dominio (UF invalida, data
                   implausivel, distancia fora da faixa de corrida de rua).
  COBERTURA        % de eventos com data/cidade/distancias — mede o quanto os
                   conectores + enriquecimento estao entregando.

Exit code 1 quando ha alerta CRITICO (fonte sumida/quebrada), para o runner
diario/CI poder falhar. Warnings so falham com --strict.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("corridas_etl.quality")

# Fontes que DEVEM ter registros no banco (conector rodando em producao).
EXPECTED_SOURCES = ("ticketsports", "iguanasports", "yescom")

# Idade maxima da coleta mais recente por fonte antes de alertar.
MAX_FETCH_AGE_HOURS = 48

# Faixa plausivel p/ corrida de rua (km).
DIST_MIN_KM, DIST_MAX_KM = 0.4, 120.0

_UFS = {
    "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS",
    "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC",
    "SP", "SE", "TO",
}


@dataclass
class Report:
    criticals: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)

    def critical(self, msg: str) -> None:
        self.criticals.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    def info(self, msg: str) -> None:
        self.infos.append(msg)


# -- Saude por fonte ----------------------------------------------------------

def check_source_health(conn: psycopg.Connection, report: Report) -> None:
    rows = conn.execute(
        """
        SELECT source, count(*), max(last_seen_at),
               EXTRACT(EPOCH FROM (now() - max(last_seen_at))) / 3600
        FROM source_record GROUP BY source
        """
    ).fetchall()
    by_source = {r[0]: r for r in rows}

    for source in EXPECTED_SOURCES:
        row = by_source.get(source)
        if row is None or row[1] == 0:
            report.critical(f"fonte '{source}': NENHUM registro no banco (conector quebrado?)")
            continue
        _, count, last, age_h = row
        if age_h is not None and age_h > MAX_FETCH_AGE_HOURS:
            report.critical(
                f"fonte '{source}': ultima coleta ha {age_h:.0f}h (> {MAX_FETCH_AGE_HOURS}h)"
            )
        else:
            report.info(f"fonte '{source}': {count} registros, ultima coleta {last:%Y-%m-%d %H:%M}")


# -- Anomalias ----------------------------------------------------------------

def check_anomalies(conn: psycopg.Connection, report: Report) -> None:
    bad_uf = conn.execute(
        "SELECT count(*) FROM event WHERE state IS NOT NULL AND state <> ALL(%s)",
        (list(_UFS),),
    ).fetchone()[0]
    if bad_uf:
        report.warn(f"{bad_uf} evento(s) com UF invalida")

    past_open = conn.execute(
        """SELECT count(*) FROM event
           WHERE start_at < now() - interval '1 day' AND registration_status = 'open'"""
    ).fetchone()[0]
    if past_open:
        report.warn(f"{past_open} evento(s) ja realizados mas ainda 'open' (stale)")

    far_future = conn.execute(
        "SELECT count(*) FROM event WHERE start_at > now() + interval '2 years'"
    ).fetchone()[0]
    if far_future:
        report.warn(f"{far_future} evento(s) com data a mais de 2 anos no futuro")

    bad_dist = conn.execute(
        "SELECT count(*) FROM event_distance WHERE distance_km < %s OR distance_km > %s",
        (DIST_MIN_KM, DIST_MAX_KM),
    ).fetchone()[0]
    if bad_dist:
        report.warn(f"{bad_dist} distancia(s) fora da faixa {DIST_MIN_KM}-{DIST_MAX_KM} km")

    orphans = conn.execute(
        """SELECT count(*) FROM event e
           WHERE NOT EXISTS (SELECT 1 FROM source_record sr WHERE sr.event_id = e.id)"""
    ).fetchone()[0]
    if orphans:
        report.warn(f"{orphans} evento(s) sem nenhum source_record (orfaos de merge?)")

    # Bounding box do Brasil incluindo ilhas oceanicas (Noronha ~-32.4,
    # Trindade ~-29.3 de longitude). Fora disso = geocoding suspeito.
    out_of_bounds = conn.execute(
        """SELECT count(*) FROM event WHERE latitude IS NOT NULL
           AND NOT (latitude BETWEEN -34 AND 6 AND longitude BETWEEN -74 AND -28)"""
    ).fetchone()[0]
    if out_of_bounds:
        report.warn(f"{out_of_bounds} evento(s) geocodificados fora do Brasil")


# -- Cobertura ----------------------------------------------------------------

def check_coverage(conn: psycopg.Connection, report: Report) -> None:
    total, with_date, with_city, with_dist = conn.execute(
        """
        SELECT count(*),
               count(*) FILTER (WHERE start_at IS NOT NULL),
               count(*) FILTER (WHERE city IS NOT NULL),
               count(*) FILTER (WHERE EXISTS
                   (SELECT 1 FROM event_distance d WHERE d.event_id = event.id))
        FROM event
        """
    ).fetchone()
    if not total:
        report.critical("banco sem nenhum evento")
        return

    def pct(n: int) -> str:
        return f"{100 * n / total:.0f}%"

    report.info(
        f"cobertura ({total} eventos): data {pct(with_date)}, cidade {pct(with_city)}, "
        f"distancias {pct(with_dist)}"
    )
    if with_date / total < 0.5:
        report.warn(f"cobertura de data baixa ({pct(with_date)})")


# -- Orquestracao -------------------------------------------------------------

def run_quality(conn: psycopg.Connection) -> Report:
    report = Report()
    check_source_health(conn, report)
    check_anomalies(conn, report)
    check_coverage(conn, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Checks de qualidade de dados")
    parser.add_argument("--strict", action="store_true", help="Warnings tambem falham")
    args = parser.parse_args(argv)

    from ..db import connect

    with connect() as conn:
        report = run_quality(conn)

    for msg in report.infos:
        log.info("OK    %s", msg)
    for msg in report.warnings:
        log.warning("WARN  %s", msg)
    for msg in report.criticals:
        log.error("CRIT  %s", msg)

    log.info(
        "qualidade: %d critico(s), %d warning(s)",
        len(report.criticals), len(report.warnings),
    )
    if report.criticals or (args.strict and report.warnings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
