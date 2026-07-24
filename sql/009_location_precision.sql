-- Precisao da localizacao do evento.
-- Distingue coordenada do LOCAL exato (nivel rua/venue) de coordenada que e
-- apenas o CENTROIDE da cidade (o que o passo `geocode` do enrich produz via
-- Nominatim cidade+UF). O app usa isso para nao cravar um alfinete no centro
-- da cidade — o que faria o usuario achar que a corrida ocorre ali.
--
--   'exact' -> lat/long apontam o local real da largada; pode marcar no mapa
--   'city'  -> lat/long sao o centro da cidade; mostrar a cidade como um todo
--   NULL    -> sem coordenadas

ALTER TABLE event
    ADD COLUMN IF NOT EXISTS location_precision TEXT
        CHECK (location_precision IN ('exact', 'city'));

-- Backfill: toda coordenada ja existente veio do geocode (nivel cidade),
-- pois nenhum conector fornece lat/long exata hoje.
UPDATE event
   SET location_precision = 'city'
 WHERE latitude IS NOT NULL
   AND longitude IS NOT NULL
   AND location_precision IS NULL;
