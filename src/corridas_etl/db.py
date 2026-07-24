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
from .utils.text import slugify


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


def load_source_state(
    conn: psycopg.Connection, source: str
) -> dict[str, tuple[str, int]]:
    """{source_event_id: (raw_hash, parse_version)} da ultima coleta.

    Base do incremental: um payload so e pulado se o hash bruto E a versao do
    parser que o processou continuarem iguais. Se a logica de parse mudou (versao
    do conector bumpada), o registro e reprocessado mesmo com o payload inalterado.
    """
    rows = conn.execute(
        "SELECT source_event_id, raw_hash, parse_version FROM source_record WHERE source = %s",
        (source,),
    ).fetchall()
    return {r[0]: (r[1], r[2]) for r in rows if r[1]}


def touch_source_records(
    conn: psycopg.Connection, source: str, source_event_ids: list[str]
) -> int:
    """Renova last_seen_at dos registros inalterados (sem reprocessar).

    Assim a checagem de saude por fonte nao acusa 'coleta velha' so porque o
    conteudo nao mudou — nos VIMOS o evento nesta execucao.
    """
    if not source_event_ids:
        return 0
    return conn.execute(
        """UPDATE source_record SET last_seen_at = now()
           WHERE source = %s AND source_event_id = ANY(%s)""",
        (source, source_event_ids),
    ).rowcount


def _upsert_organizer(conn: psycopg.Connection, name: str | None) -> int | None:
    """Garante a organizadora na tabela `organizer` e retorna seu id."""
    if not name or not name.strip():
        return None
    slug = slugify(name)
    row = conn.execute(
        """
        INSERT INTO organizer (slug, name) VALUES (%s, %s)
        ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        (slug, name.strip()),
    ).fetchone()
    return row[0]


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
    organizer_id = _upsert_organizer(conn, event.organizer_name)
    params = {
        "canonical_key": event.canonical_key,
        "slug": event.slug,
        "name": event.name,
        "organizer_id": organizer_id,
        "description": event.description,
        "start_at": event.start_at,
        "registration_status": event.registration_status.value,
        "price": event.price,
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
                    registration_status = CASE WHEN %(registration_status)s = 'unknown'
                        THEN registration_status ELSE %(registration_status)s END,
                    organizer_id = COALESCE(organizer_id, %(organizer_id)s),
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
                    canonical_key, slug, name, description, organizer_id,
                    start_at, registration_status, price, official_url, image_url,
                    city, state, country, address, latitude, longitude, updated_at
                ) VALUES (
                    %(canonical_key)s, %(slug)s, %(name)s, %(description)s, %(organizer_id)s,
                    %(start_at)s, %(registration_status)s, %(price)s, %(official_url)s, %(image_url)s,
                    %(city)s, %(state)s, %(country)s, %(address)s, %(latitude)s, %(longitude)s, now()
                )
                ON CONFLICT (canonical_key) DO UPDATE SET
                    name = EXCLUDED.name,
                    registration_status = CASE WHEN EXCLUDED.registration_status = 'unknown'
                        THEN event.registration_status ELSE EXCLUDED.registration_status END,
                    organizer_id = COALESCE(EXCLUDED.organizer_id, event.organizer_id),
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
                    event_id, source, source_event_id, source_url, raw_hash,
                    parse_version, price, last_seen_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (source, source_event_id) DO UPDATE SET
                    event_id = EXCLUDED.event_id,
                    source_url = EXCLUDED.source_url,
                    raw_hash = EXCLUDED.raw_hash,
                    parse_version = EXCLUDED.parse_version,
                    price = EXCLUDED.price,
                    last_seen_at = now()
                """,
                (event_id, src.source, src.source_event_id, src.source_url,
                 src.raw_hash, src.parse_version, src.price),
            )

        # Preco do evento = MENOR entre suas fontes (deterministico -> estavel;
        # o trigger so registra mudanca quando esse minimo realmente muda). So
        # atualiza se diferiu, para nao gerar UPDATE/trigger a toa.
        cur.execute(
            """
            UPDATE event e SET price = sub.min_price
            FROM (SELECT min(price) AS min_price FROM source_record WHERE event_id = %s) sub
            WHERE e.id = %s AND e.price IS DISTINCT FROM sub.min_price
            """,
            (event_id, event_id),
        )

    return event_id
