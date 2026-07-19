-- Upgrade OPCIONAL: habilita queries geoespaciais via PostGIS.
-- Aplique apenas em um Postgres com a extensao disponivel (ex.: imagem
-- postgis/postgis do docker-compose). O pipeline funciona sem isso —
-- ele grava latitude/longitude e esta coluna gerada as converte.

CREATE EXTENSION IF NOT EXISTS postgis;

ALTER TABLE event
    ADD COLUMN IF NOT EXISTS geopoint geography(Point, 4326)
    GENERATED ALWAYS AS (
        CASE
            WHEN latitude IS NULL OR longitude IS NULL THEN NULL
            ELSE ST_SetSRID(ST_MakePoint(longitude::float8, latitude::float8), 4326)::geography
        END
    ) STORED;

CREATE INDEX IF NOT EXISTS event_geo_idx ON event USING GIST (geopoint);
