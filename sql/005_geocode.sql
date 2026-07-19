-- Cache de geocoding (Fase 2).
-- Uma linha por (cidade, UF) ja consultada no Nominatim. Misses tambem sao
-- cacheados (lat/long NULL) para nao re-consultar em toda execucao.

CREATE TABLE IF NOT EXISTS geocode_cache (
    city        TEXT NOT NULL,
    state       TEXT NOT NULL,
    latitude    NUMERIC(9,6),
    longitude   NUMERIC(9,6),
    provider    TEXT NOT NULL DEFAULT 'nominatim',
    resolved_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (city, state)
);
