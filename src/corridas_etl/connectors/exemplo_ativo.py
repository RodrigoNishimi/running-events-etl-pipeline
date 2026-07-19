"""Conector de EXEMPLO (esqueleto didatico).

Gera eventos sinteticos para o pipeline rodar ponta-a-ponta sem depender de
rede. Use-o como molde para um conector real: os comentarios marcam onde entram
o `discover`/`fetch` de rede e o parse de HTML/JSON reais.

Estrategia recomendada num conector real (do mais barato ao mais caro):
  1. Tentar o endpoint JSON interno do site (aba Network) -> estruturado e estavel.
  2. Sitemap/feed para descobrir URLs.
  3. Parse de HTML estatico (selectolax).
  4. Renderizacao JS com Playwright (corridas_etl[browser]) — so em ultimo caso.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable

from ..models import Distance, RawPayload, RegistrationStatus, SourceEventRecord
from .base import BaseConnector

# Dados sinteticos que simulam a resposta JSON de uma fonte. Num conector real
# isto viria de self.http_get(url).json() ou do parse do HTML.
_FAKE_SOURCE_DATA = [
    {
        "id": "sp-night-run-2026",
        "name": "SP Night Run 2026",
        "organizer": "Iguana Sports",
        "date": "2026-09-12T19:00:00-03:00",
        "status": "open",
        "url": "https://exemplo.com.br/sp-night-run-2026",
        "image": "https://exemplo.com.br/img/sp-night.jpg",
        "city": "São Paulo",
        "state": "SP",
        "address": "Parque Ibirapuera",
        "lat": -23.5874,
        "lng": -46.6576,
        "distances": ["5k", "10k", "Meia Maratona"],
    },
    {
        "id": "rio-beach-run-2026",
        "name": "Rio Beach Run 2026",
        "organizer": "Live!Run",
        "date": "2026-10-04T07:00:00-03:00",
        "status": "coming_soon",
        "url": "https://exemplo.com.br/rio-beach-run-2026",
        "image": None,
        "city": "Rio de Janeiro",
        "state": "RJ",
        "address": "Praia de Copacabana",
        "lat": -22.9711,
        "lng": -43.1822,
        "distances": ["5k", "10k"],
    },
]


class ExemploAtivoConnector(BaseConnector):
    source = "exemplo_ativo"

    def discover(self) -> Iterable[str]:
        # Real: buscar sitemap/listagem paginada e emitir uma URL por evento.
        #   resp = self.http_get("https://www.ativo.com.br/eventos?pagina=1")
        #   for url in _extrair_urls(resp.text): yield url
        for item in _FAKE_SOURCE_DATA:
            yield item["id"]

    def fetch(self, event_ref: str) -> RawPayload:
        # Real: baixar a pagina/endpoint do evento.
        #   resp = self.http_get(event_ref)
        #   return self.make_payload(event_ref, resp.text, url=event_ref,
        #                            content_type=resp.headers.get("content-type", "text/html"))
        item = next(i for i in _FAKE_SOURCE_DATA if i["id"] == event_ref)
        return RawPayload(
            source=self.source,
            source_event_id=event_ref,
            source_url=item["url"],
            fetched_at=datetime.now(timezone.utc),
            content_type="application/json",
            body=json.dumps(item, ensure_ascii=False),
        )

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        # Real (HTML): usar selectolax para extrair campos do payload.body.
        #   tree = HTMLParser(payload.body); name = tree.css_first("h1").text()
        data = json.loads(payload.body)

        return SourceEventRecord(
            source=self.source,
            source_event_id=payload.source_event_id,
            source_url=payload.source_url,
            raw_hash=payload.content_hash,
            name=data["name"],
            organizer_name=data.get("organizer"),
            start_at=_parse_date(data.get("date")),
            registration_status=_parse_status(data.get("status")),
            official_url=data.get("url"),
            image_url=data.get("image"),
            city=data.get("city"),
            state=data.get("state"),
            address=data.get("address"),
            latitude=data.get("lat"),
            longitude=data.get("lng"),
            distances=[Distance.from_label(d) for d in data.get("distances", [])],
        )


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    # dateutil lida com os varios formatos PT-BR na pratica; aqui o dado ja e ISO.
    return datetime.fromisoformat(value)


def _parse_status(value: str | None) -> RegistrationStatus:
    try:
        return RegistrationStatus(value) if value else RegistrationStatus.UNKNOWN
    except ValueError:
        return RegistrationStatus.UNKNOWN
