"""Orquestracao da Fase 0.

Roda o ciclo completo para uma fonte:
    discover -> fetch (Bronze/raw) -> parse (Silver) -> canonicalize (Gold) -> upsert

Uso:
    python -m corridas_etl.pipeline.run --source ativo
    python -m corridas_etl.pipeline.run --source ativo --dry-run   # sem banco
    python -m corridas_etl.pipeline.run --source ticketsports --full # ignora incremental

Na Fase 2 este entrypoint vira um "asset"/"flow" no orquestrador (Prefect/Dagster),
mas a logica de negocio permanece a mesma.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..connectors.registry import available_sources, get_connector
from ..models import CanonicalEvent, RegistrationStatus
from ..storage.raw import RawStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("corridas_etl")


def run_source(
    source: str, *, dry_run: bool = False, limit: int | None = None, full: bool = False
) -> int:
    """Roda o ciclo de uma fonte.

    Incremental (padrao): eventos cujo payload bruto nao mudou desde a ultima
    coleta (mesmo hash) sao pulados no parse/upsert — so renovamos last_seen_at.
    `--full` forca reprocessar tudo (ex.: apos mudar a logica de parse).
    """
    connector = get_connector(source)
    raw_store = RawStore()
    canonical: list[CanonicalEvent] = []

    # Hashes da ultima coleta, para detectar o que nao mudou.
    known_hashes: dict[str, str] = {}
    if not dry_run and not full:
        from ..db import connect, load_source_hashes

        with connect() as conn:
            known_hashes = load_source_hashes(conn, source)

    unchanged_ids: list[str] = []

    try:
        refs = list(connector.discover())
        log.info("[%s] %d eventos descobertos", source, len(refs))
        if limit is not None:
            refs = refs[:limit]
            log.info("[%s] limitando a %d eventos (--limit)", source, len(refs))

        for ref in refs:
            payload = connector.fetch(ref)          # Bronze

            # Incremental: hash igual ao da ultima coleta -> nada mudou.
            prev = known_hashes.get(payload.source_event_id)
            if prev and prev == payload.content_hash:
                unchanged_ids.append(payload.source_event_id)
                continue

            raw_path = raw_store.save(payload)
            log.debug("[%s] raw salvo em %s", source, raw_path)

            record = connector.parse(payload)       # Silver
            if record is None:
                log.warning("[%s] ref %s ignorada (nao e evento valido)", source, ref)
                continue

            canonical.append(CanonicalEvent.from_source(record))
    finally:
        connector.close()

    # Consolida registros que caem na MESMA chave canonica nesta rodada (ex.:
    # produtos "regular" e "idosos/estudantes" do mesmo evento na Iguana). Sem
    # isso, cada um faria um upsert e o preco/status "piscaria" entre eles a
    # cada execucao, gerando mudancas espurias no outbox.
    canonical = _consolidate(canonical)

    log.info(
        "[%s] %d novos/alterados, %d inalterados (pulados)",
        source, len(canonical), len(unchanged_ids),
    )

    if dry_run:
        for ev in canonical:
            dists = ", ".join(f"{d.label}={d.distance_km}" for d in ev.distances)
            log.info("  - %s | %s | %s/%s | [%s]", ev.name, ev.start_at, ev.city, ev.state, dists)
        return len(canonical)

    # Carga idempotente no Postgres. Import tardio para permitir --dry-run sem psycopg/DB.
    from ..db import connect, touch_source_records, upsert_event

    with connect() as conn:
        for ev in canonical:
            upsert_event(conn, ev)
        touched = touch_source_records(conn, source, unchanged_ids)
    log.info("[%s] %d gravados, %d inalterados renovados", source, len(canonical), touched)
    return len(canonical)


# Prioridade de status quando registros do mesmo evento discordam (maior vence).
_STATUS_RANK = {
    RegistrationStatus.OPEN: 4,
    RegistrationStatus.COMING_SOON: 3,
    RegistrationStatus.SOLD_OUT: 2,
    RegistrationStatus.CLOSED: 1,
    RegistrationStatus.UNKNOWN: 0,
}


def _consolidate(events: list[CanonicalEvent]) -> list[CanonicalEvent]:
    """Funde CanonicalEvents com a mesma canonical_key (dentro de uma rodada).

    Preco = menor conhecido; status = de maior prioridade; distancias = uniao;
    demais campos = primeiro nao-nulo. Fontes acumuladas.
    """
    by_key: dict[str, CanonicalEvent] = {}
    for ev in events:
        base = by_key.get(ev.canonical_key)
        if base is None:
            by_key[ev.canonical_key] = ev.model_copy(deep=True)
            continue
        base.price = _min_opt(base.price, ev.price)
        if _STATUS_RANK[ev.registration_status] > _STATUS_RANK[base.registration_status]:
            base.registration_status = ev.registration_status
        seen = {d.distance_km for d in base.distances}
        base.distances.extend(d for d in ev.distances if d.distance_km not in seen)
        base.sources.extend(ev.sources)
        for field in ("description", "start_at", "official_url", "image_url",
                      "city", "state", "address", "latitude", "longitude",
                      "organizer_name"):
            if getattr(base, field) is None and getattr(ev, field) is not None:
                setattr(base, field, getattr(ev, field))
    return list(by_key.values())


def _min_opt(a: float | None, b: float | None) -> float | None:
    vals = [x for x in (a, b) if x is not None]
    return min(vals) if vals else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Pipeline ETL de corridas (Fase 0)")
    parser.add_argument("--source", required=True, help=f"Fonte: {', '.join(available_sources())}")
    parser.add_argument(
        "--dry-run", action="store_true", help="Nao grava no banco; apenas mostra o resultado"
    )
    parser.add_argument(
        "--limit", type=int, default=None, help="Processa no maximo N eventos (util em dev)"
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Reprocessa tudo, ignorando o incremental por hash",
    )
    args = parser.parse_args(argv)

    run_source(args.source, dry_run=args.dry_run, limit=args.limit, full=args.full)
    return 0


if __name__ == "__main__":
    sys.exit(main())
