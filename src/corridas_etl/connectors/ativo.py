"""Conector Ativo.com (www.ativo.com).

Grande portal/agregador de eventos esportivos. O calendario e JS-rendered no
navegador, mas os dados vem de UM unico JSON estatico publico
(`/eventos.json`, ~2200 corridas de rua) — nao precisa de Playwright.

Estrategia (mapeada em 2026-07-19):
  discover -> GET /eventos.json (1 request), filtra corridas de rua
              (tipo_de_evento == "C") nao suspensas; cacheia os itens em
              memoria e emite os ids.
  fetch    -> monta o RawPayload de um evento a partir do item ja em cache
              (o /index.json por evento e identico ao item da lista, entao
              re-baixar seria desperdicio). O raw guardado e o item isolado.
  parse    -> campos do item (titulo/cidade com entidades HTML -> unescape,
              data, distancias estruturadas, pais no path do post_json).

robots.txt permite (bloqueia so areas de e-commerce). Como e um so request
grande, cacheamos e nao ha rate limit relevante.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from ..models import Distance, RawPayload, RegistrationStatus, SourceEventRecord
from ..utils.geo import is_br_uf
from .base import BaseConnector

EVENTS_URL = "https://www.ativo.com/eventos.json"
SIGNUP_URL = "https://pay.ativo.com/evento/{id}"

TZ_BRT = timezone(timedelta(hours=-3))

# O Ativo expoe um arquivo historico enorme (eventos desde 2015). Para uma
# plataforma de descoberta so interessam os proximos; ignoramos o que ja passou
# ha mais de PAST_GRACE_DAYS (mantem provas recentissimas ainda "quentes").
PAST_GRACE_DAYS = 3

# Codigo do pais no path do post_json: .../eventos/<continente>/<pais>/<uf>/...
_COUNTRY_IN_PATH_RE = re.compile(r"/eventos/[^/]+/([a-z]{2})/")


class AtivoConnector(BaseConnector):
    source = "ativo"

    def __init__(self) -> None:
        super().__init__()
        self._items: dict[str, dict] = {}

    def discover(self) -> Iterable[str]:
        cutoff = date.today() - timedelta(days=PAST_GRACE_DAYS)
        resp = self.http_get(EVENTS_URL)
        for item in resp.json():
            if item.get("tipo_de_evento") != "C":       # so corrida de rua
                continue
            if _truthy(item.get("fl_suspenso")):          # descarta suspensos
                continue
            dt = _parse_dt(item.get("dt_evento"))
            if dt is not None and dt.date() < cutoff:     # descarta passados
                continue
            event_id = str(item.get("id_evento") or "").strip()
            if not event_id:
                continue
            self._items[event_id] = item
            yield event_id

    def fetch(self, event_ref: str) -> RawPayload:
        item = self._items.get(event_ref)
        if item is None:
            # fetch chamado sem discover (ex.: reprocessamento por id avulso):
            # busca o dump e localiza o item.
            for it in self.http_get(EVENTS_URL).json():
                if str(it.get("id_evento")) == event_ref:
                    item = it
                    break
        if item is None:
            raise KeyError(f"evento {event_ref} nao encontrado no dump do Ativo")

        page_url = (item.get("post_json") or "").replace("/index.json", "") or None
        return self.make_payload(
            event_ref,
            json.dumps(item, ensure_ascii=False, sort_keys=True),
            url=page_url,
            content_type="application/json",
        )

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        item = json.loads(payload.body)
        name = html_lib.unescape((item.get("post_title") or "").strip())
        if not name:
            return None

        country = _country_from_post_json(item.get("post_json") or "")
        city = html_lib.unescape((item.get("ds_cidade") or "").strip()) or None
        raw_state = (item.get("ds_estado") or "").strip()
        # Ativo as vezes poe 'BR' (codigo de pais) ou lixo no campo de UF.
        state = raw_state.upper() if (country == "BR" and is_br_uf(raw_state)) else None

        return SourceEventRecord(
            source=self.source,
            source_event_id=payload.source_event_id,
            source_url=payload.source_url,
            raw_hash=payload.content_hash,
            name=name,
            organizer_name=item.get("nome_organizador") or None,
            start_at=_parse_dt(item.get("dt_evento")),
            # A inscricao encerra/abre nao vem explicita; fl_resultado=1 indica
            # prova ja realizada (tem resultado) -> inscricao encerrada.
            registration_status=(
                RegistrationStatus.CLOSED
                if _truthy(item.get("fl_resultado"))
                else RegistrationStatus.UNKNOWN
            ),
            official_url=SIGNUP_URL.format(id=payload.source_event_id),
            image_url=_clean_image(item.get("thumbnail")),
            city=city,
            state=state,
            country=country,
            distances=_distances(item.get("distancias")),
        )


def _truthy(v: object) -> bool:
    return str(v).strip() not in ("", "0", "None", "none", "False", "false")


def _parse_dt(value: str | None) -> datetime | None:
    """'2026-08-16 00:00:00' -> datetime em horario de Brasilia (00:00 = hora
    desconhecida, mantida como meia-noite)."""
    if not value:
        return None
    try:
        dt = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=TZ_BRT)


def _country_from_post_json(post_json: str) -> str:
    m = _COUNTRY_IN_PATH_RE.search(post_json)
    return m.group(1).upper() if m else "BR"


def _clean_image(url: str | None) -> str | None:
    """Alguns thumbnails vem com extensao corrompida no dump ('.pço', '.alq');
    aceita so URLs http plausiveis."""
    if not url or not url.startswith("http"):
        return None
    return url if re.search(r"\.(jpg|jpeg|png|webp|gif)$", url, re.IGNORECASE) else url


def _distances(raw: object) -> list[Distance]:
    if not isinstance(raw, list):
        return []
    out: list[Distance] = []
    seen: set[float | None] = set()
    for d in raw:
        label = (d.get("ds_distancia") or "").strip() if isinstance(d, dict) else ""
        if not label:
            continue
        dist = Distance.from_label(label)
        if dist.distance_km not in seen or dist.distance_km is None:
            seen.add(dist.distance_km)
            out.append(dist)
    return out
