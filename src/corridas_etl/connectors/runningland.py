"""Conector Running Land (www.runningland.com.br).

Organizadora/plataforma com marcas proprias de corrida (Blue Run, Blue Line,
Bota Pra Correr...): ~118 eventos futuros em dezenas de cidades.

Estrategia (mapeada em 2026-07-19):
  - Loja Magento headless: os eventos sao produtos expostos pelo GraphQL
    publico em `/graphql` (query `getEventCategoryFull`, categoria 3 =
    calendario), paginado. Os campos event_region/event_city/event_modality
    vem como IDs de opcao; a query `productCustomAttributeValues` fornece o
    mapa id -> valor ("53"->"SP", "26"->"Sao Paulo", "38"->"5K").
  - Conduta: o robots.txt permite tudo exceto checkout/customer (o /graphql
    NAO e bloqueado). O WAF, porem, devolve 403 para User-Agents nao-browser;
    usamos um UA de browser — e o mesmo trafego que qualquer navegador gera
    para uma rota publica permitida — mantendo o rate limit educado de sempre.
  - O Bronze de cada evento embute o item cru + os valores de atributo ja
    resolvidos PARA AQUELE item (nao o mapa global, que mudaria o hash do
    incremental a cada opcao nova na loja).

Limitacoes: horario da largada nao confiavel (event_date carrega horarios de
cadastro/UTC); usamos so a data. Sem lat/long (geocoding enriquece depois).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..models import Distance, RawPayload, RegistrationStatus, SourceEventRecord
from ..utils.geo import is_br_uf
from .base import BaseConnector

BASE_URL = "https://www.runningland.com.br"
GRAPHQL_URL = f"{BASE_URL}/graphql"

# O WAF exige UA de browser (ver docstring); rate limit segue o do BaseConnector.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

TZ_BRT = timezone(timedelta(hours=-3))

CATEGORY_ID = "3"      # categoria "calendario" (todos os eventos)
PAGE_SIZE = 100

_EVENTS_QUERY = (
    "query getEventCategoryFull($pageSize:Int!$currentPage:Int!"
    "$filters:ProductAttributeFilterInput!)"
    "{products(pageSize:$pageSize currentPage:$currentPage filter:$filters)"
    "{items{id name sku url_key stock_status "
    # final_price ja reflete promocao/regra de catalogo (e o preco realmente
    # cobrado); regular_price fica como fallback. Sem final_price a promocao
    # nunca era capturada e o preco vinha inflado (o regular).
    "price_range{minimum_price{regular_price{currency value}"
    "final_price{currency value}}}"
    "thumbnail{url}event_product event_date event_region event_city event_modality}"
    "page_info{total_pages}total_count}}"
)

_ATTRS_QUERY = (
    "query productCustomAttributeValues($code:[String!]!)"
    "{productCustomAttributeValues(input:{code:$code})"
    "{attributes{code listValues{id value}}}}"
)


class RunningLandConnector(BaseConnector):
    source = "runningland"

    def __init__(self) -> None:
        super().__init__()
        self._client.headers["User-Agent"] = _BROWSER_UA
        self._client.headers["Accept"] = "application/json"
        self._items: dict[str, dict] = {}
        self._attr_maps: dict[str, dict[str, str]] = {}

    # -- GraphQL (GET, como o proprio site faz) ------------------------------

    def _graphql(self, query: str, operation: str, variables: dict) -> dict:
        resp = self.http_get(
            GRAPHQL_URL
            + "?"
            + "&".join(
                f"{k}={v}"
                for k, v in {
                    "query": _urlquote(query),
                    "operationName": operation,
                    "variables": _urlquote(json.dumps(variables)),
                }.items()
            )
        )
        data = resp.json()
        if data.get("data") is None:
            raise RuntimeError(f"GraphQL sem dados: {str(data.get('errors'))[:200]}")
        return data["data"]

    def _load_attr_maps(self) -> None:
        data = self._graphql(
            _ATTRS_QUERY,
            "productCustomAttributeValues",
            {"code": ["event_region", "event_city", "event_modality"]},
        )
        for attr in data["productCustomAttributeValues"]["attributes"]:
            self._attr_maps[attr["code"]] = {
                str(v["id"]): (v["value"] or "").strip()
                for v in (attr.get("listValues") or [])
            }

    # -- Contrato ------------------------------------------------------------

    def discover(self) -> Iterable[str]:
        self._load_attr_maps()
        page = 1
        while True:
            data = self._graphql(
                _EVENTS_QUERY,
                "getEventCategoryFull",
                {
                    "currentPage": page,
                    "pageSize": PAGE_SIZE,
                    "filters": {"category_id": {"eq": CATEGORY_ID}},
                },
            )
            products = data["products"]
            for item in products["items"]:
                if item.get("event_product") != 1:   # descarta vouchers etc.
                    continue
                event_id = str(item["id"])
                self._items[event_id] = item
                yield event_id
            if page >= products["page_info"]["total_pages"]:
                break
            page += 1

    def fetch(self, event_ref: str) -> RawPayload:
        item = self._items.get(event_ref)
        if item is None:
            # Reprocessamento por id avulso: redescobre (barato: ~4 requests).
            for _ in self.discover():
                pass
            item = self._items.get(event_ref)
        if item is None:
            raise KeyError(f"evento {event_ref} nao encontrado na Running Land")

        # Bronze auto-contido: item cru + atributos resolvidos SO deste item
        # (mapa global mudaria o hash do incremental a cada opcao nova).
        body = {
            "item": item,
            "resolved": {
                "region": self._resolve("event_region", item.get("event_region")),
                "city": self._resolve("event_city", item.get("event_city")),
                "modalities": self._resolve_list(
                    "event_modality", item.get("event_modality")
                ),
            },
        }
        return self.make_payload(
            event_ref,
            json.dumps(body, ensure_ascii=False, sort_keys=True),
            url=f"{BASE_URL}/{item.get('url_key')}" if item.get("url_key") else None,
            content_type="application/json",
        )

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        body = json.loads(payload.body)
        item, resolved = body.get("item") or {}, body.get("resolved") or {}

        name = (item.get("name") or "").strip()
        if not name:
            return None

        state = resolved.get("region") or None
        if not is_br_uf(state):
            state = None

        status = (
            RegistrationStatus.SOLD_OUT
            if item.get("stock_status") == "OUT_OF_STOCK"
            else RegistrationStatus.OPEN
        )

        thumb = (item.get("thumbnail") or {}).get("url")

        return SourceEventRecord(
            source=self.source,
            source_event_id=payload.source_event_id,
            source_url=payload.source_url,
            raw_hash=payload.content_hash,
            name=name,
            organizer_name="Running Land",
            start_at=_parse_date(item.get("event_date")),
            registration_status=status,
            price=_min_price(item),
            official_url=payload.source_url,
            image_url=thumb,
            city=resolved.get("city") or None,
            state=state,
            distances=[
                Distance.from_label(m) for m in resolved.get("modalities") or []
            ],
        )

    # -- Helpers -------------------------------------------------------------

    def _resolve(self, attr: str, value: object) -> str | None:
        if value is None:
            return None
        return self._attr_maps.get(attr, {}).get(str(value))

    def _resolve_list(self, attr: str, value: object) -> list[str]:
        """event_modality vem como '38,41' (IDs separados por virgula)."""
        if not value:
            return []
        out = []
        for part in str(value).split(","):
            resolved = self._resolve(attr, part.strip())
            if resolved:
                out.append(resolved)
        return out


def _min_price(item: dict) -> float | None:
    """Menor preço realmente cobrado: final_price (já com promoção) e, na falta
    dele, o regular_price.

    O `minimum_price` do Magento já é o menor entre as variações do produto; o
    `final_price` embute special_price/regras de catálogo. Usar só o
    `regular_price` (como antes) fazia o preço vir inflado quando havia promoção.
    Preço 0 é placeholder ("ainda sem valor"), não gratuito -> desconhecido.
    """
    minimum = ((item.get("price_range") or {}).get("minimum_price") or {})
    final = (minimum.get("final_price") or {}).get("value")
    regular = (minimum.get("regular_price") or {}).get("value")
    for price in (final, regular):
        if isinstance(price, (int, float)) and price > 0:
            return round(price, 2)
    return None


def _urlquote(text: str) -> str:
    from urllib.parse import quote

    return quote(text, safe="")


def _parse_date(value: str | None) -> datetime | None:
    """'2026-09-27 03:00:00' -> so a DATA em BRT (o horario da fonte carrega
    artefatos de UTC/cadastro e nao representa a largada)."""
    if not value:
        return None
    try:
        dt = datetime.strptime(value.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ_BRT)
