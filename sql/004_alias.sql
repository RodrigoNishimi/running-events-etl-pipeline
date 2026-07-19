-- Aliases de merge (Fase 2).
-- Quando o dedup mescla A <- B, a canonical_key de B passa a apontar para o
-- evento sobrevivente. Assim a proxima carga da fonte que gerava B atualiza o
-- sobrevivente em vez de recriar o evento absorvido (churn recria-e-remescla).

CREATE TABLE IF NOT EXISTS event_alias (
    canonical_key   TEXT PRIMARY KEY,
    event_id        BIGINT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS event_alias_event_idx ON event_alias (event_id);
