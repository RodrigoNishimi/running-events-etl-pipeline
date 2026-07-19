"""Orquestracao da Fase 0.

Roda o ciclo completo para uma fonte:
    discover -> fetch (Bronze/raw) -> parse (Silver) -> canonicalize (Gold) -> upsert

Uso:
    python -m corridas_etl.pipeline.run --source exemplo_ativo
    python -m corridas_etl.pipeline.run --source exemplo_ativo --dry-run   # sem banco

Na Fase 2 este entrypoint vira um "asset"/"flow" no orquestrador (Prefect/Dagster),
mas a logica de negocio permanece a mesma.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..connectors.registry import available_sources, get_connector
from ..models import CanonicalEvent
from ..storage.raw import RawStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("corridas_etl")


def run_source(source: str, *, dry_run: bool = False, limit: int | None = None) -> int:
    connector = get_connector(source)
    raw_store = RawStore()
    canonical: list[CanonicalEvent] = []

    try:
        refs = list(connector.discover())
        log.info("[%s] %d eventos descobertos", source, len(refs))
        if limit is not None:
            refs = refs[:limit]
            log.info("[%s] limitando a %d eventos (--limit)", source, len(refs))

        for ref in refs:
            payload = connector.fetch(ref)          # Bronze
            raw_path = raw_store.save(payload)
            log.debug("[%s] raw salvo em %s", source, raw_path)

            record = connector.parse(payload)       # Silver
            if record is None:
                log.warning("[%s] ref %s ignorada (nao e evento valido)", source, ref)
                continue

            # Gold (Fase 0: 1 registro -> 1 evento canonico; sem merge entre fontes).
            canonical.append(CanonicalEvent.from_source(record))
    finally:
        connector.close()

    log.info("[%s] %d eventos canonicos gerados", source, len(canonical))

    if dry_run:
        for ev in canonical:
            dists = ", ".join(f"{d.label}={d.distance_km}" for d in ev.distances)
            log.info("  - %s | %s | %s/%s | [%s]", ev.name, ev.start_at, ev.city, ev.state, dists)
        return len(canonical)

    # Carga idempotente no Postgres. Import tardio para permitir --dry-run sem psycopg/DB.
    from ..db import connect, upsert_event

    with connect() as conn:
        for ev in canonical:
            upsert_event(conn, ev)
    log.info("[%s] %d eventos gravados no banco", source, len(canonical))
    return len(canonical)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pipeline ETL de corridas (Fase 0)")
    parser.add_argument("--source", required=True, help=f"Fonte: {', '.join(available_sources())}")
    parser.add_argument(
        "--dry-run", action="store_true", help="Nao grava no banco; apenas mostra o resultado"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Processa no maximo N eventos (util em dev)"
    )
    args = parser.parse_args(argv)

    run_source(args.source, dry_run=args.dry_run, limit=args.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
