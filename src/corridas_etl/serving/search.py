"""Indexacao no Meilisearch para a busca facetada do app (camada de Serving).

    python -m corridas_etl.serving.search              # reindexa tudo
    python -m corridas_etl.serving.search --dry-run     # mostra docs sem servidor
    python -m corridas_etl.serving.search --future-only # so eventos a partir de hoje

O Postgres continua sendo a fonte da verdade; o Meilisearch e um indice
derivado, reconstruivel a qualquer momento a partir dele. A logica que importa
— transformar uma linha de evento no documento de busca — e `build_document`,
testavel sem servidor.

Facetas expostas ao app: estado, cidade, pais, distancias (km), mes, ano,
status de inscricao e geo (_geo) para "corridas perto de mim".
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("corridas_etl.search")

INDEX_UID = "events"
PRIMARY_KEY = "id"

# Configuracao do indice: o que e buscavel, filtravel e ordenavel.
INDEX_SETTINGS: dict = {
    "searchableAttributes": ["name", "city", "organizer_name", "state"],
    "filterableAttributes": [
        "state", "city", "country", "distances_km",
        "month", "month_name", "year", "registration_status", "sources", "_geo",
    ],
    "sortableAttributes": ["start_timestamp", "_geo"],
    # Ranking: eventos mais proximos no tempo primeiro (empate do textual).
    "rankingRules": [
        "words", "typo", "proximity", "attribute", "sort", "exactness",
        "start_timestamp:asc",
    ],
}

# Query que alimenta o indice (uma linha por evento canonico).
_SELECT = """
    SELECT e.id, e.slug, e.name, e.description, e.start_at,
           e.registration_status, e.official_url, e.image_url,
           e.city, e.state, e.country, e.latitude, e.longitude,
           o.name AS organizer_name,
           COALESCE(array_agg(DISTINCT d.distance_km)
                    FILTER (WHERE d.distance_km IS NOT NULL), ARRAY[]::numeric[]) AS dists,
           (SELECT array_agg(DISTINCT sr.source)
            FROM source_record sr WHERE sr.event_id = e.id) AS sources
    FROM event e
    LEFT JOIN event_distance d ON d.event_id = e.id
    LEFT JOIN organizer o ON o.id = e.organizer_id
    __WHERE__
    GROUP BY e.id, o.name
"""

_MONTHS_PT = [
    "", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
    "julho", "agosto", "setembro", "outubro", "novembro", "dezembro",
]


def build_document(row: dict) -> dict:
    """Transforma uma linha de evento no documento de busca do Meilisearch.

    Nucleo testavel: normaliza distancias (ordenadas, unicas), deriva mes/ano
    para facetas, monta _geo e o timestamp de ordenacao.
    """
    start_at: datetime | None = row.get("start_at")
    distances = sorted({float(km) for km in (row.get("dists") or [])})

    doc: dict = {
        "id": row["id"],
        "slug": row["slug"],
        "name": row["name"],
        "description": row.get("description"),
        "city": row.get("city"),
        "state": row.get("state"),
        "country": row.get("country") or "BR",
        "organizer_name": row.get("organizer_name"),
        "registration_status": row.get("registration_status"),
        "official_url": row.get("official_url"),
        "image_url": row.get("image_url"),
        "distances_km": distances,
        "distance_labels": [_km_label(km) for km in distances],
        "sources": list(row.get("sources") or []),
        "start_at": start_at.isoformat() if start_at else None,
        "start_timestamp": int(start_at.timestamp()) if start_at else None,
        "year": start_at.year if start_at else None,
        "month": start_at.month if start_at else None,
        "month_name": _MONTHS_PT[start_at.month] if start_at else None,
    }

    lat, lng = row.get("latitude"), row.get("longitude")
    if lat is not None and lng is not None:
        # Meilisearch espera _geo = {lat, lng} para filtros/ordenacao geo.
        doc["_geo"] = {"lat": float(lat), "lng": float(lng)}

    return doc


def _km_label(km: float) -> str:
    """5.0 -> '5k'; 21.0975 -> '21k' (rotulo amigavel para chip de filtro)."""
    return f"{int(round(km))}k"


def fetch_documents(conn, *, future_only: bool) -> list[dict]:
    where = ""
    params: tuple = ()
    if future_only:
        where = "WHERE e.start_at IS NULL OR e.start_at >= %s"
        params = (datetime.now(timezone.utc),)
    rows = conn.execute(_SELECT.replace("__WHERE__", where), params)
    cols = [c.name for c in rows.description]
    return [build_document(dict(zip(cols, r))) for r in rows.fetchall()]


def reindex(documents: list[dict]) -> None:
    """Envia os documentos ao Meilisearch (configura o indice antes)."""
    try:
        import meilisearch
    except ImportError:
        raise SystemExit(
            'O reindex precisa do cliente Meilisearch:\n'
            '  pip install "corridas-etl[search]"'
        )

    from ..config import settings

    client = meilisearch.Client(settings.meili_url, settings.meili_master_key)
    index = client.index(INDEX_UID)
    client.create_index(INDEX_UID, {"primaryKey": PRIMARY_KEY})
    index.update_settings(INDEX_SETTINGS)
    task = index.add_documents(documents, primary_key=PRIMARY_KEY)
    log.info("enviados %d documentos ao indice '%s' (task %s)",
             len(documents), INDEX_UID, getattr(task, "task_uid", task))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Indexa eventos no Meilisearch")
    parser.add_argument("--dry-run", action="store_true", help="Nao envia; imprime amostra")
    parser.add_argument("--future-only", action="store_true", help="So eventos a partir de hoje")
    args = parser.parse_args(argv)

    from ..db import connect

    with connect() as conn:
        documents = fetch_documents(conn, future_only=args.future_only)
    log.info("%d documentos construidos", len(documents))

    if args.dry_run:
        print(json.dumps({"settings": INDEX_SETTINGS, "sample": documents[:3]},
                         ensure_ascii=False, indent=2, default=str))
        return 0

    reindex(documents)
    return 0


if __name__ == "__main__":
    sys.exit(main())
