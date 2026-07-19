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

    Regras:
      - Se a canonical_key foi absorvida por um merge (event_alias), atualiza o
        evento SOBREVIVENTE em vez de recriar o absorvido; nome/identidade do
        sobrevivente sao preservados.
      - Campos anulaveis usam COALESCE: a fonte preenche lacunas mas nunca
        apaga dado ja existente (ex.: cidade/data vindas do enriquecimento).
    """
    params = {
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
        "country": event.country,
        "address": event.address,
        "latitude": event.latitude,
        "longitude": event.longitude,
    }
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_id FROM event_alias WHERE canonical_key = %s",
            (event.canonical_key,),
        )
        alias = cur.fetchone()

        if alias:
            # Chave absorvida por merge: atualiza o sobrevivente (identidade
            # dele prevalece; a fonte so preenche lacunas e renova o status).
            event_id = alias[0]
            cur.execute(
                """
                UPDATE event SET
                    registration_status = %(registration_status)s,
                    description = COALESCE(description, %(description)s),
                    start_at    = COALESCE(start_at, %(start_at)s),
                    official_url= COALESCE(official_url, %(official_url)s),
                    image_url   = COALESCE(image_url, %(image_url)s),
                    city        = COALESCE(city, %(city)s),
                    state       = COALESCE(state, %(state)s),
                    country     = %(country)s,
                    address     = COALESCE(address, %(address)s),
                    latitude    = COALESCE(latitude, %(latitude)s),
                    longitude   = COALESCE(longitude, %(longitude)s),
                    updated_at  = now()
                WHERE id = %(event_id)s
                """,
                {**params, "event_id": event_id},
            )
        else:
            cur.execute(
                """
                INSERT INTO event (
                    canonical_key, slug, name, description,
                    start_at, registration_status, official_url, image_url,
                    city, state, country, address, latitude, longitude, updated_at
                ) VALUES (
                    %(canonical_key)s, %(slug)s, %(name)s, %(description)s,
                    %(start_at)s, %(registration_status)s, %(official_url)s, %(image_url)s,
                    %(city)s, %(state)s, %(country)s, %(address)s, %(latitude)s, %(longitude)s, now()
                )
                ON CONFLICT (canonical_key) DO UPDATE SET
                    name = EXCLUDED.name,
                    registration_status = EXCLUDED.registration_status,
                    description = COALESCE(EXCLUDED.description, event.description),
                    start_at    = COALESCE(EXCLUDED.start_at, event.start_at),
                    official_url= COALESCE(EXCLUDED.official_url, event.official_url),
                    image_url   = COALESCE(EXCLUDED.image_url, event.image_url),
                    city        = COALESCE(EXCLUDED.city, event.city),
                    state       = COALESCE(EXCLUDED.state, event.state),
                    country     = EXCLUDED.country,
                    address     = COALESCE(EXCLUDED.address, event.address),
                    latitude    = COALESCE(EXCLUDED.latitude, event.latitude),
                    longitude   = COALESCE(EXCLUDED.longitude, event.longitude),
                    updated_at = now()
                RETURNING id
                """,
                params,
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
