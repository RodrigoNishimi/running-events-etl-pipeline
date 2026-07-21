"""Runner diario: o pipeline completo em uma execucao (Fase 2).

    python -m corridas_etl.pipeline.daily                  # tudo
    python -m corridas_etl.pipeline.daily --limit 20       # dev: N eventos/fonte
    python -m corridas_etl.pipeline.daily --skip-enrich    # sem Playwright extra

Etapas (na ordem):
  1. EXTRACT+LOAD  todos os conectores reais, com ISOLAMENTO DE FALHAS:
                   uma fonte quebrada nao derruba as demais (o erro vira
                   alerta no relatorio final e no exit code).
  2. DEDUP         entity resolution automatica + fila de revisao.
  3. ENRICH        datas da Iguana, distancias do Ticket Sports e geocoding
                   (limitados por execucao p/ espalhar o custo).
  4. NOTIFY        reporta o feed de mudancas de preco/status (o trigger ja
                   registrou; o app consome via pipeline.notify).
  5. QUALITY       relatorio de saude/anomalias/cobertura.

A busca do app roda direto no Postgres (pg_trgm/PostGIS, ver sql/008_search.sql)
— nao ha mais etapa de reindexacao: o dado gravado aqui ja e pesquisavel.

Exit code 1 se alguma fonte falhou ou a qualidade acusou critico — bom para
agendadores (Task Scheduler/cron/CI) sinalizarem o problema.
"""

from __future__ import annotations

import argparse
import logging
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("corridas_etl.daily")

# Fontes reais, na ordem de execucao (rapidas primeiro; ticketsports por
# ultimo por ser o mais lento — descoberta agentica).
SOURCES = ("iguanasports", "runningland", "yescom", "ativo", "ticketsports")

# Paginas renderizadas por execucao no enriquecimento de distancias.
ENRICH_DISTANCES_PER_RUN = 25

# Consultas novas ao Nominatim por execucao (cache faz o resto).
GEOCODE_PER_RUN = 100


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pipeline diario completo")
    parser.add_argument("--limit", type=int, default=None, help="Max eventos por fonte (dev)")
    parser.add_argument("--full", action="store_true", help="Ignora o incremental por hash")
    parser.add_argument("--skip-enrich", action="store_true")
    parser.add_argument(
        "--sources", nargs="*", default=list(SOURCES),
        help=f"Fontes a rodar (default: {' '.join(SOURCES)})",
    )
    args = parser.parse_args(argv)

    from ..db import connect
    from .dedup import run_dedup
    from .enrich import enrich_geocode, enrich_iguana_dates, enrich_ticketsports_distances
    from .quality import run_quality
    from .run import run_source

    failed_sources: list[str] = []

    # -- 1. Extract + Load ---------------------------------------------------
    for source in args.sources:
        log.info("=== fonte: %s ===", source)
        try:
            run_source(source, limit=args.limit, full=args.full)
        except Exception:
            # Isolamento: registra e segue para a proxima fonte.
            log.exception("fonte '%s' FALHOU", source)
            failed_sources.append(source)

    # -- 2. Dedup ------------------------------------------------------------
    log.info("=== dedup ===")
    with connect() as conn:
        run_dedup(conn)

    # -- 3. Enrich -----------------------------------------------------------
    if not args.skip_enrich:
        log.info("=== enriquecimento ===")
        with connect() as conn:
            try:
                enrich_iguana_dates(conn)
            except Exception:
                log.exception("enriquecimento iguana-dates falhou")
            try:
                enrich_ticketsports_distances(conn, ENRICH_DISTANCES_PER_RUN)
            except Exception:
                log.exception("enriquecimento ticketsports-distances falhou")
            try:
                enrich_geocode(conn, GEOCODE_PER_RUN)
            except Exception:
                log.exception("enriquecimento geocode falhou")

    # -- 4. Notificações -----------------------------------------------------
    # O trigger já registrou as mudanças de preço/status durante os upserts;
    # aqui só reportamos o tamanho do feed pendente (o serviço de notificação
    # do app consome via `python -m corridas_etl.pipeline.notify --json`).
    log.info("=== feed de mudanças ===")
    with connect() as conn:
        from .notify import build_feed

        pending = build_feed(conn, only_pending=True)
    log.info("%d mudança(s) de preço/status no feed pendente", len(pending))

    # -- 5. Quality ----------------------------------------------------------
    log.info("=== qualidade ===")
    with connect() as conn:
        report = run_quality(conn)
    for msg in report.infos:
        log.info("OK    %s", msg)
    for msg in report.warnings:
        log.warning("WARN  %s", msg)
    for msg in report.criticals:
        log.error("CRIT  %s", msg)

    if failed_sources:
        log.error("fontes com falha nesta execucao: %s", ", ".join(failed_sources))
    ok = not failed_sources and not report.criticals
    log.info("pipeline diario: %s", "SUCESSO" if ok else "COM PROBLEMAS")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
