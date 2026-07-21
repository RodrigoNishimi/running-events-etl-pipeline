-- Busca textual direto no Postgres (substitui o indice Meilisearch).
-- pg_trgm da matching fuzzy (tolerancia a erro de digitacao) e unaccent
-- normaliza acentos ("sao paulo" encontra "São Paulo"). Aplicar como os
-- demais arquivos desta pasta:
--   psql "$DATABASE_URL" -f sql/008_search.sql

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS unaccent;

-- unaccent() nao e IMMUTABLE (o dicionario e configuravel por sessao), o que
-- impede usa-la em indice de expressao; este wrapper fixa o dicionario padrao.
CREATE OR REPLACE FUNCTION f_unaccent(text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$ SELECT public.unaccent('public.unaccent'::regdictionary, $1) $$;

-- Expressao buscavel do evento (nome + cidade + UF), normalizada. O app
-- (RunnersHub, src/lib/search.ts) usa EXATAMENTE esta expressao no WHERE
-- para o planner aproveitar o indice (gin_trgm_ops cobre LIKE/ILIKE e os
-- operadores de similaridade do pg_trgm).
CREATE INDEX IF NOT EXISTS event_search_trgm_idx ON event
    USING GIN (
        f_unaccent(lower(name || ' ' || coalesce(city, '') || ' ' || coalesce(state, '')))
        gin_trgm_ops
    );
