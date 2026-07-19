"""Camada Bronze: persiste o payload bruto de cada fonte.

Guardar o raw permite reprocessar (re-parsear) sem re-crawlear a fonte, alem de
servir de auditoria. Na Fase 0 gravamos em disco local; a interface e simples o
suficiente para trocar por S3/MinIO depois sem mexer no resto do pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import settings
from ..models import RawPayload


class RawStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or settings.raw_storage_dir

    def _path(self, payload: RawPayload) -> Path:
        # Particiona por fonte e data de coleta: data/raw/<fonte>/<AAAA-MM-DD>/<id>.json
        day = payload.fetched_at.strftime("%Y-%m-%d")
        safe_id = payload.source_event_id.replace("/", "_")
        return self.base_dir / payload.source / day / f"{safe_id}.json"

    def save(self, payload: RawPayload) -> Path:
        path = self._path(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            payload.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return path

    def load(self, path: Path) -> RawPayload:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return RawPayload.model_validate(data)
