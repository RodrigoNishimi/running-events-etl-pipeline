-- Suporte a eventos internacionais (Fase 2).
-- Organizadoras brasileiras às vezes listam provas fora do país (Maratona de
-- Punta del Este, do Porto, Atacama). A UF (state) só é significativa quando
-- country = 'BR'; para os demais, state fica NULL e country carrega o ISO-2.

ALTER TABLE event ADD COLUMN IF NOT EXISTS country TEXT NOT NULL DEFAULT 'BR';

CREATE INDEX IF NOT EXISTS event_country_idx ON event (country);
