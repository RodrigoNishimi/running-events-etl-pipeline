"""Conector Iguana Sports (iguanasports.com.br).

Organizadora grande de eventos premium (Nike SP City Marathon, Run The Bridge,
Athenas Run Longer, Venus Women's Half Marathon). Poucos eventos, mas de alto
perfil e milhares de participantes cada.

Estrategia (mapeada em 2026-07-19):
  - Loja Shopify: o catalogo publico `/products.json` lista os eventos como
    produtos; o detalhe vem de `/products/<handle>.js` (endpoint storefront),
    que — ao contrario do `.json` — inclui `available` por variante, alem das
    distancias estruturadas na option "Distancia". robots.txt permite.
  - Limitacao conhecida: o JSON de produto nao traz data nem cidade do evento
    (ficam para enriquecimento na Fase 1 via pagina do evento).
  - O mesmo evento aparece como >1 produto (ex.: "X" e "X - Idosos e
    estudantes"). O parse remove o sufixo de publico para que os registros
    convirjam para o mesmo evento canonico.
"""

from __future__ import annotations

import json
import re
from typing import Iterable

from ..models import Distance, RawPayload, RegistrationStatus, SourceEventRecord
from .base import BaseConnector

BASE_URL = "https://iguanasports.com.br"

# Sufixos de publico/lote no titulo do produto que NAO sao eventos distintos.
_AUDIENCE_SUFFIX_RE = re.compile(
    r"\s*[-–]\s*(idosos(\s+e\s+estudantes)?|estudantes|pcd)\s*$", re.IGNORECASE
)


class IguanaSportsConnector(BaseConnector):
    source = "iguanasports"

    def discover(self) -> Iterable[str]:
        resp = self.http_get(f"{BASE_URL}/products.json?limit=250")
        for product in resp.json().get("products", []):
            yield product["handle"]

    def fetch(self, event_ref: str) -> RawPayload:
        # `.js` (storefront) e nao `.json`: so ele traz `available` por variante.
        url = f"{BASE_URL}/products/{event_ref}.js"
        resp = self.http_get(url)
        return self.make_payload(
            event_ref,
            resp.text,
            url=f"{BASE_URL}/products/{event_ref}",
            content_type="application/json",
        )

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        product = json.loads(payload.body)
        raw_title = (product.get("title") or "").strip()
        if not raw_title:
            return None
        # "Run The Bridge 2026 - Idosos e estudantes" -> "Run The Bridge 2026",
        # para que os varios produtos do mesmo evento convirjam no canonico.
        name = _AUDIENCE_SUFFIX_RE.sub("", raw_title)

        available = product.get("available")
        if available is None:
            available = any(v.get("available") for v in product.get("variants", []))
        status = RegistrationStatus.OPEN if available else RegistrationStatus.SOLD_OUT

        return SourceEventRecord(
            source=self.source,
            source_event_id=payload.source_event_id,
            source_url=payload.source_url,
            raw_hash=payload.content_hash,
            name=name,
            description=_strip_html(product.get("description")) or None,
            organizer_name="Iguana Sports",
            registration_status=status,
            official_url=payload.source_url,
            image_url=_image_url(product),
            distances=_distances_from_options(product.get("options") or []),
        )


def _distances_from_options(options: list[dict]) -> list[Distance]:
    """Le a option Shopify "Distancia" (ex.: values=["5K","10K","15K","30K"])."""
    for opt in options:
        opt_name = (opt.get("name") or "").lower()
        if "dist" in opt_name:
            return [Distance.from_label(v) for v in opt.get("values", [])]
    return []


def _image_url(product: dict) -> str | None:
    """featured_image/images do `.js` sao strings, possivelmente protocol-relative."""
    url = product.get("featured_image") or next(iter(product.get("images") or []), None)
    if not url:
        return None
    return f"https:{url}" if url.startswith("//") else url


def _strip_html(html: str | None) -> str:
    if not html:
        return ""
    return re.sub(r"<[^>]+>", " ", html).strip()
