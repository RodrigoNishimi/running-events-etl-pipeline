"""Schema canonico (Pydantic) do pipeline.

Fluxo de tipos:
    RawPayload         -> Bronze (o que veio da fonte, sem interpretacao)
    SourceEventRecord  -> Silver (parseado e normalizado, 1 registro POR FONTE)
    CanonicalEvent     -> Gold  (deduplicado, o que vai para o banco)
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from .utils.distances import parse_distance_km
from .utils.text import normalize_name, slugify


class RegistrationStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    COMING_SOON = "coming_soon"
    SOLD_OUT = "sold_out"
    UNKNOWN = "unknown"


class Distance(BaseModel):
    label: str                       # rotulo original da fonte ("Meia Maratona")
    distance_km: float | None = None

    @classmethod
    def from_label(cls, label: str) -> "Distance":
        return cls(label=label.strip(), distance_km=parse_distance_km(label))


class RawPayload(BaseModel):
    """Camada Bronze: exatamente o que a fonte retornou, mais metadados."""

    source: str
    source_event_id: str
    source_url: str | None = None
    fetched_at: datetime
    content_type: str = "text/html"
    body: str                        # HTML ou JSON cru

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


class SourceEventRecord(BaseModel):
    """Camada Silver: um evento como visto POR UMA fonte, ja normalizado.

    Ainda nao deduplicado — dois registros de fontes diferentes podem descrever
    o mesmo evento do mundo real; isso e resolvido na etapa de entity resolution.
    """

    source: str
    source_event_id: str
    source_url: str | None = None
    raw_hash: str | None = None

    name: str
    description: str | None = None
    organizer_name: str | None = None

    start_at: datetime | None = None
    registration_status: RegistrationStatus = RegistrationStatus.UNKNOWN
    official_url: str | None = None
    image_url: str | None = None

    city: str | None = None
    state: str | None = None         # UF (2 letras)
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    distances: list[Distance] = Field(default_factory=list)

    @field_validator("state")
    @classmethod
    def _uppercase_uf(cls, v: str | None) -> str | None:
        return v.upper()[:2] if v else v

    def blocking_key(self) -> str:
        """Chave barata p/ agrupar candidatos a duplicata (blocking).

        Combina nome normalizado + ano-mes da largada. Registros que compartilham
        essa chave sao comparados entre si na etapa de matching.
        """
        ym = self.start_at.strftime("%Y-%m") if self.start_at else "sem-data"
        return f"{normalize_name(self.name)}|{ym}"


class CanonicalEvent(BaseModel):
    """Camada Gold: evento canonico deduplicado, pronto para persistir."""

    canonical_key: str
    slug: str
    name: str
    description: str | None = None
    organizer_name: str | None = None

    start_at: datetime | None = None
    registration_status: RegistrationStatus = RegistrationStatus.UNKNOWN
    official_url: str | None = None
    image_url: str | None = None

    city: str | None = None
    state: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    distances: list[Distance] = Field(default_factory=list)
    sources: list[SourceEventRecord] = Field(default_factory=list)

    @classmethod
    def from_source(cls, rec: SourceEventRecord) -> "CanonicalEvent":
        """Promove um unico registro de fonte a evento canonico (Fase 0: sem merge).

        Na Fase 1, varios SourceEventRecord com o mesmo bloco serao mesclados
        aqui, com precedencia definida por fonte.
        """
        key = _build_canonical_key(rec)
        return cls(
            canonical_key=key,
            slug=slugify(f"{rec.name}-{key[:8]}"),
            name=rec.name,
            description=rec.description,
            organizer_name=rec.organizer_name,
            start_at=rec.start_at,
            registration_status=rec.registration_status,
            official_url=rec.official_url,
            image_url=rec.image_url,
            city=rec.city,
            state=rec.state,
            address=rec.address,
            latitude=rec.latitude,
            longitude=rec.longitude,
            distances=rec.distances,
            sources=[rec],
        )


def _build_canonical_key(rec: SourceEventRecord) -> str:
    """Chave estavel do evento canonico, derivada de nome+data+cidade+UF.

    Estavel => o mesmo evento gera a mesma chave em execucoes futuras, o que
    torna o upsert idempotente. Independe da fonte, para que registros de fontes
    diferentes que descrevam o mesmo evento convirjam para a mesma chave.
    """
    ymd = rec.start_at.strftime("%Y-%m-%d") if rec.start_at else "sem-data"
    basis = f"{normalize_name(rec.name)}|{ymd}|{(rec.city or '').lower()}|{rec.state or ''}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()
