"""Acesso ao Postgres e carga idempotente (upsert) da camada Gold.

O upsert usa `canonical_key` (estavel) como chave de conflito, de modo que rodar
o pipeline duas vezes atualiza o evento em vez de duplica-lo.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import psycopg

from .config import settings
from .models import CanonicalEvent


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    conn = psycopg.connect(settings.database_url)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def upsert_event(conn: psycopg.Connection, event: CanonicalEvent) -> int:
    """Insere ou atualiza um evento canonico e retorna seu id.

    Tambem grava as distancias e os source_records associados.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO event (
                canonical_key, slug, name, description,
                start_at, registration_status, official_url, image_url,
                city, state, address, latitude, longitude, updated_at
            ) VALUES (
                %(canonical_key)s, %(slug)s, %(name)s, %(description)s,
                %(start_at)s, %(registration_status)s, %(official_url)s, %(image_url)s,
                %(city)s, %(state)s, %(address)s, %(latitude)s, %(longitude)s, now()
            )
            ON CONFLICT (canonical_key) DO UPDATE SET
                name = EXCLUDED.name,
                description = EXCLUDED.description,
                start_at = EXCLUDED.start_at,
                registration_status = EXCLUDED.registration_status,
                official_url = EXCLUDED.official_url,
                image_url = EXCLUDED.image_url,
                city = EXCLUDED.city,
                state = EXCLUDED.state,
                address = EXCLUDED.address,
                latitude = EXCLUDED.latitude,
                longitude = EXCLUDED.longitude,
                updated_at = now()
            RETURNING id
            """,
            {
                "canonical_key": event.canonical_key,
                "slug": event.slug,
                "name": event.name,
                "description": event.description,
                "start_at": event.start_at,
                "registration_status": event.registration_status.value,
                "official_url": event.official_url,
                "image_url": event.image_url,
                "city": event.city,
                "state": event.state,
                "address": event.address,
                "latitude": event.latitude,
                "longitude": event.longitude,
            },
        )
        event_id = cur.fetchone()[0]

        for dist in event.distances:
            cur.execute(
                """
                INSERT INTO event_distance (event_id, label, distance_km)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id, label) DO UPDATE
                    SET distance_km = EXCLUDED.distance_km
                """,
                (event_id, dist.label, dist.distance_km),
            )

        for src in event.sources:
            cur.execute(
                """
                INSERT INTO source_record (
                    event_id, source, source_event_id, source_url, raw_hash, last_seen_at
                ) VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (source, source_event_id) DO UPDATE SET
                    event_id = EXCLUDED.event_id,
                    source_url = EXCLUDED.source_url,
                    raw_hash = EXCLUDED.raw_hash,
                    last_seen_at = now()
                """,
                (event_id, src.source, src.source_event_id, src.source_url, src.raw_hash),
            )

    return event_id
