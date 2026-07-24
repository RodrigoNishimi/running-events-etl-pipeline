-- Guarda de versao do parser no gate incremental.
--
-- O incremental por hash (pipeline/run.py) pula reprocessar payloads cujo
-- conteudo bruto nao mudou (mesmo content_hash). Mas quando a LOGICA de parse
-- muda — ex.: a correcao do status do Ativo, que deixou de inferir "encerrada"
-- de fl_resultado — o payload bruto continua identico e a correcao NUNCA chega
-- ao banco, mesmo re-rodando o pipeline. So `--full` resolvia, e manualmente.
--
-- Guardando a versao do parser que gerou cada source_record, o gate reprocessa
-- automaticamente quando a versao do conector muda, ainda que o payload seja
-- igual. Assim, correcoes de parse propagam sozinhas na proxima coleta.
--
-- DEFAULT 1 = versao-base dos conectores (BaseConnector.parse_version). Linhas
-- ja existentes viram "v1"; um conector que bumpar sua versao acima disso
-- (ex.: AtivoConnector.parse_version = 2) forca o reprocesso das SUAS linhas.
ALTER TABLE source_record
    ADD COLUMN IF NOT EXISTS parse_version INTEGER NOT NULL DEFAULT 1;
