"""Testa a lógica incremental do run_source com um conector e um "banco" fake."""

from __future__ import annotations

from datetime import datetime, timezone

import corridas_etl.pipeline.run as run_mod
from corridas_etl.connectors.base import BaseConnector
from corridas_etl.models import RawPayload, SourceEventRecord


class _FakeConnector(BaseConnector):
    source = "fake"

    def __init__(self, bodies: dict[str, str]) -> None:
        # sem super().__init__(): nao queremos httpx client de verdade
        self._bodies = bodies

    def discover(self):
        return list(self._bodies)

    def fetch(self, event_ref: str) -> RawPayload:
        return RawPayload(
            source=self.source,
            source_event_id=event_ref,
            source_url=f"http://x/{event_ref}",
            fetched_at=datetime.now(timezone.utc),
            content_type="application/json",
            body=self._bodies[event_ref],
        )

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        return SourceEventRecord(
            source=self.source,
            source_event_id=payload.source_event_id,
            raw_hash=payload.content_hash,
            name=f"Evento {payload.source_event_id}",
        )

    def close(self) -> None:
        pass


def _patch(monkeypatch, connector, known_state):
    """Isola run_source de rede/DB/disco.

    `known_state`: {source_event_id: (raw_hash, parse_version)} da coleta anterior.
    """
    monkeypatch.setattr(run_mod, "get_connector", lambda s: connector)
    monkeypatch.setattr(run_mod.RawStore, "save", lambda self, payload: None)

    upserted: list = []
    touched: dict = {}

    class _FakeConn:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    monkeypatch.setattr(run_mod, "connect", lambda: _FakeConn(), raising=False)
    # db.* são importados dentro da função; injeta no módulo db
    import corridas_etl.db as db
    monkeypatch.setattr(db, "connect", lambda: _FakeConn())
    monkeypatch.setattr(db, "load_source_state", lambda conn, source: known_state)
    monkeypatch.setattr(db, "upsert_event", lambda conn, ev: upserted.append(ev.name) or 1)
    monkeypatch.setattr(
        db, "touch_source_records",
        lambda conn, source, ids: touched.update({"ids": ids}) or len(ids),
    )
    return upserted, touched


def test_unchanged_events_are_skipped(monkeypatch):
    conn = _FakeConnector({"a": '{"v":1}', "b": '{"v":2}'})
    # 'a' com hash e parse_version conhecidos e iguais -> pulado; 'b' novo -> upsert
    hash_a = conn.fetch("a").content_hash
    upserted, touched = _patch(monkeypatch, conn, {"a": (hash_a, conn.parse_version)})

    run_mod.run_source("fake")

    assert upserted == ["Evento b"]          # só o alterado/novo foi gravado
    assert touched["ids"] == ["a"]           # o inalterado teve last_seen renovado


def test_bumped_parse_version_reprocesses_unchanged_payload(monkeypatch):
    """Mesmo payload (hash igual), mas a versao do parser mudou -> reprocessa.

    Regressao do bug do Ativo: a correcao da logica de parse nao chegava ao banco
    porque o payload bruto nao mudava e o incremental pulava o registro.
    """
    conn = _FakeConnector({"a": '{"v":1}'})
    conn.parse_version = 2                    # conector bumpou a logica de parse
    hash_a = conn.fetch("a").content_hash
    # banco ainda tem o registro gravado pela versao 1 do parser
    upserted, touched = _patch(monkeypatch, conn, {"a": (hash_a, 1)})

    run_mod.run_source("fake")

    assert upserted == ["Evento a"]          # reprocessado apesar do hash igual
    assert touched.get("ids", []) == []      # nao foi tratado como inalterado


def test_full_reprocesses_everything(monkeypatch):
    conn = _FakeConnector({"a": '{"v":1}', "b": '{"v":2}'})
    hash_a = conn.fetch("a").content_hash
    upserted, touched = _patch(monkeypatch, conn, {"a": (hash_a, conn.parse_version)})

    run_mod.run_source("fake", full=True)

    assert sorted(upserted) == ["Evento a", "Evento b"]   # --full ignora hashes
    assert touched.get("ids", []) == []