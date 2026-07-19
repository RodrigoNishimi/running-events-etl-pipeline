"""Configuracao central, carregada de variaveis de ambiente (.env)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    database_url: str
    raw_storage_dir: Path
    user_agent: str
    request_delay_seconds: float

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.environ.get(
                "DATABASE_URL",
                "postgresql://corridas:corridas@localhost:5432/corridas",
            ),
            raw_storage_dir=Path(os.environ.get("RAW_STORAGE_DIR", "./data/raw")),
            user_agent=os.environ.get(
                "ETL_USER_AGENT", "CorridasBot/0.1 (+https://example.com/bot)"
            ),
            request_delay_seconds=float(os.environ.get("ETL_REQUEST_DELAY_SECONDS", "2.0")),
        )


settings = Settings.from_env()
