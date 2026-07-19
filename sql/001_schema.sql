-- Schema canonico (camada Gold) da plataforma de corridas.
-- Nao exige PostGIS: lat/long ficam em colunas numericas simples.
-- Para habilitar queries geoespaciais ("corridas num raio de X km"),
-- aplique sql/002_postgis.sql em um Postgres com a extensao disponivel.

-- ---------------------------------------------------------------------------
-- Organizadoras (Yescom, Iguana Sports, Live!Run, ...)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS organizer (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    slug        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    website     TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Evento canonico (uma edicao especifica de uma corrida, ja deduplicado).
-- A chave de negocio e o canonical_key (derivado de nome+data+local).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS event (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    canonical_key   TEXT UNIQUE NOT NULL,      -- chave estavel p/ upsert idempotente
    slug            TEXT UNIQUE NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT,
    organizer_id    BIGINT REFERENCES organizer(id),

    start_at        TIMESTAMPTZ,               -- data/hora de largada (pode ter so a data)
    registration_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (registration_status IN ('open','closed','coming_soon','sold_out','unknown')),
    official_url    TEXT,                       -- link de inscricao oficial
    image_url       TEXT,

    -- Localizacao
    city            TEXT,
    state           TEXT,                       -- UF (2 letras)
    address         TEXT,
    latitude        NUMERIC(9,6),               -- WGS84; ver sql/002_postgis.sql
    longitude       NUMERIC(9,6),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS event_start_at_idx   ON event (start_at);
CREATE INDEX IF NOT EXISTS event_city_state_idx ON event (state, city);

-- Distancias oferecidas por um evento (5k, 10k, 21.0975k, 42.195k, kids...).
CREATE TABLE IF NOT EXISTS event_distance (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id    BIGINT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,                  -- rotulo original ("Meia Maratona")
    distance_km NUMERIC(7,4),                   -- normalizado (21.0975); NULL se nao aplicavel
    UNIQUE (event_id, label)
);

-- ---------------------------------------------------------------------------
-- Rastreabilidade: de quais fontes/URLs este evento canonico foi montado.
-- Essencial para dedup, auditoria e para desfazer merges errados.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS source_record (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id        BIGINT REFERENCES event(id) ON DELETE SET NULL,
    source          TEXT NOT NULL,              -- nome do conector ("ativo", "yescom")
    source_event_id TEXT NOT NULL,              -- id do evento na fonte
    source_url      TEXT,
    raw_hash        TEXT,                       -- hash do payload bruto (Bronze)
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, source_event_id)
);

CREATE INDEX IF NOT EXISTS source_record_event_idx ON source_record (event_id);
