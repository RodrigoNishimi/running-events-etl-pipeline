-- Fase 3: notificações de mudança (preço/status).
-- O pipeline detecta e REGISTRA mudanças num outbox (event_change); a entrega
-- (e-mail/push) é responsabilidade do serviço de notificação do app, que
-- consome esta tabela. Um trigger AFTER UPDATE centraliza a detecção — pega
-- qualquer caminho (upsert, merge/alias) sem espalhar lógica no código.

ALTER TABLE event ADD COLUMN IF NOT EXISTS price NUMERIC(10,2);
-- Preço por fonte: event.price é derivado como o MENOR entre as fontes do
-- evento (determinístico -> estável entre execuções, sem "piscar" quando o
-- mesmo evento tem várias fontes/produtos com preços diferentes).
ALTER TABLE source_record ADD COLUMN IF NOT EXISTS price NUMERIC(10,2);

CREATE TABLE IF NOT EXISTS event_change (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id     BIGINT NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    field        TEXT NOT NULL CHECK (field IN ('registration_status', 'price')),
    old_value    TEXT,
    new_value    TEXT,
    detected_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    notified_at  TIMESTAMPTZ            -- NULL = ainda no feed (não despachado)
);

CREATE INDEX IF NOT EXISTS event_change_pending_idx
    ON event_change (detected_at) WHERE notified_at IS NULL;

CREATE OR REPLACE FUNCTION log_event_change() RETURNS trigger AS $$
BEGIN
    -- Status: sempre relevante (aberta -> esgotada/encerrada e vice-versa).
    IF NEW.registration_status IS DISTINCT FROM OLD.registration_status THEN
        INSERT INTO event_change (event_id, field, old_value, new_value)
        VALUES (NEW.id, 'registration_status',
                OLD.registration_status, NEW.registration_status);
    END IF;

    -- Preço: só quando havia um preço ANTES (null -> X é população inicial,
    -- não uma mudança que interesse notificar).
    IF NEW.price IS DISTINCT FROM OLD.price
       AND OLD.price IS NOT NULL AND NEW.price IS NOT NULL THEN
        INSERT INTO event_change (event_id, field, old_value, new_value)
        VALUES (NEW.id, 'price', OLD.price::text, NEW.price::text);
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS event_change_trg ON event;
CREATE TRIGGER event_change_trg
    AFTER UPDATE ON event
    FOR EACH ROW EXECUTE FUNCTION log_event_change();
