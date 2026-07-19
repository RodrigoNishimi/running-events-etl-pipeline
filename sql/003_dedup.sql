-- Fase 1: entity resolution (dedup).
-- Registra decisoes de merge (automaticas e manuais) e a fila de revisao
-- para pares na "zona cinzenta".

CREATE TABLE IF NOT EXISTS dedup_review (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- Par avaliado. Para pares mesclados, event_id_b foi absorvido por event_id_a
    -- e ja nao existe na tabela event; os nomes ficam aqui como registro.
    event_id_a      BIGINT NOT NULL,
    event_id_b      BIGINT NOT NULL,
    event_name_a    TEXT NOT NULL,
    event_name_b    TEXT NOT NULL,
    score           NUMERIC(5,4) NOT NULL,
    features        JSONB NOT NULL DEFAULT '{}',   -- evidencias por feature
    guard           TEXT,                          -- motivo de veto ao auto-merge
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','merged','distinct')),
    decided_by      TEXT CHECK (decided_by IN ('auto','manual')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ,
    UNIQUE (event_id_a, event_id_b)
);

CREATE INDEX IF NOT EXISTS dedup_review_status_idx ON dedup_review (status);
