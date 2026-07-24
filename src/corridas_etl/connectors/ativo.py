"""Conector Ativo.com (www.ativo.com).

Grande portal/agregador de eventos esportivos. O calendario e JS-rendered no
navegador, mas os dados vem de UM unico JSON estatico publico
(`/eventos.json`, ~2200 corridas de rua) — nao precisa de Playwright.

Estrategia (mapeada em 2026-07-19, status revisado em 2026-07-24):
  discover -> GET /eventos.json (1 request), filtra corridas de rua
              (tipo_de_evento == "C") nao suspensas; cacheia os itens em
              memoria e emite os ids.
  fetch    -> monta o RawPayload de um evento a partir do item ja em cache. O
              dump NAO traz o status de inscricao — esse sinal so existe na
              pagina de inscricao por evento (pay.ativo.com/evento/<id>). Entao,
              para provas dentro de STATUS_WINDOW_DAYS, fetch tambem baixa essa
              pagina e anexa o status classificado (_pay_status) ao raw.
  parse    -> campos do item (titulo/cidade com entidades HTML -> unescape,
              data, distancias estruturadas, pais no path do post_json) e o
              status de inscricao vindo de _pay_status.

robots.txt de www.ativo.com bloqueia so areas de e-commerce; pay.ativo.com nao
tem robots e o path /evento/ e livre. Como quase todo o dump e historico, a
janela deixa o custo em ~1 request por prova futura (poucas dezenas por coleta).
"""

from __future__ import annotations

import html as html_lib
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import httpx

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

# O status de inscricao NAO vem no dump (eventos.json) — so na pagina de
# inscricao por evento (pay.ativo.com). Buscar essa pagina custa 1 request por
# evento, entao so o fazemos para provas dentro desta janela (as que interessam
# ao usuario e onde o status muda). Fora da janela o status fica UNKNOWN em vez
# de ser chutado. Compromisso custo-de-rede x cobertura (modo hibrido).
STATUS_WINDOW_DAYS = 180

# Codigo do pais no path do post_json: .../eventos/<continente>/<pais>/<uf>/...
_COUNTRY_IN_PATH_RE = re.compile(r"/eventos/[^/]+/([a-z]{2})/")


class AtivoConnector(BaseConnector):
    source = "ativo"
    # v3: status de inscricao passou a vir da pagina de inscricao por evento
    # (pay.ativo.com), unica fonte que distingue aberta/encerrada/em breve. O
    # dump nao carrega esse sinal, entao v1 (fl_resultado) e v2 (futuro->aberto)
    # eram ambos chute. Bump forca reprocesso p/ a correcao chegar ao banco.
    # Ver _registration_status / _classify_pay_page e sql/010_parse_version.
    parse_version = 3

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

        # O status de inscricao nao esta no dump: anexamos o sinal da pagina de
        # inscricao (pay.ativo.com), so para provas dentro da janela. Vira parte
        # do raw (Bronze) para o incremental detectar mudancas de status.
        item = {**item, "_pay_status": self._pay_status(event_ref, item)}

        page_url = (item.get("post_json") or "").replace("/index.json", "") or None
        return self.make_payload(
            event_ref,
            json.dumps(item, ensure_ascii=False, sort_keys=True),
            url=page_url,
            content_type="application/json",
        )

    def _pay_status(self, event_ref: str, item: dict) -> str | None:
        """Classifica a inscricao pela pagina pay.ativo.com/evento/<id>.

        Retorna 'open' | 'closed' | 'coming_soon' para provas dentro da janela;
        None se fora da janela ou se a pagina nao pode ser lida/classificada
        (nesses casos o status resultante e UNKNOWN — nao chutamos).
        """
        dt = _parse_dt(item.get("dt_evento"))
        if dt is None:
            return None
        days = (dt.date() - date.today()).days
        if not (0 <= days <= STATUS_WINDOW_DAYS):
            return None
        try:
            body = self.http_get(SIGNUP_URL.format(id=event_ref)).text
        except httpx.HTTPStatusError as e:
            # pay.ativo.com serve paginas de status (ex.: "esgotado") com codigos
            # nao-2xx (418). O corpo ainda traz o sinal — classificamos mesmo assim.
            body = e.response.text if e.response is not None else ""
        except Exception:
            return None       # falha de transporte real -> UNKNOWN, sem chute
        return _classify_pay_page(body)

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
            registration_status=_registration_status(item),
            official_url=SIGNUP_URL.format(id=payload.source_event_id),
            image_url=_clean_image(item.get("thumbnail")),
            city=city,
            state=state,
            country=country,
            distances=_distances(item.get("distancias")),
        )


def _truthy(v: object) -> bool:
    return str(v).strip() not in ("", "0", "None", "none", "False", "false")


def _registration_status(item: dict) -> RegistrationStatus:
    """Status da inscricao do evento do Ativo.

    O status REAL so existe na pagina de inscricao por evento (pay.ativo.com),
    anexada em fetch() como `_pay_status`. O dump (eventos.json) NAO o carrega —
    todos os flags (fl_resultado, fl_suspenso, situacao_cadastro, datas) sao
    identicos entre provas abertas e encerradas, entao qualquer inferencia a
    partir dele e chute. Historico dos bugs:

      - v1 usava `fl_resultado=1` -> "encerrada": errado, esse flag vem 1 ate
        para provas FUTURAS com inscricao aberta.
      - v2 assumia "futura e nao suspensa" -> ABERTA: errado, marcava como
        aberta provas com inscricao ja encerrada ou ainda "em breve".

    Ordem atual:
      - `fl_suspenso` verdadeiro -> evento suspenso (CLOSED).
      - `dt_evento` no passado    -> prova ja aconteceu (CLOSED).
      - `_pay_status` da pagina de inscricao -> open / closed / coming_soon.
      - sem sinal (fora da janela ou pagina ilegivel) -> UNKNOWN (nao chuta).
    """
    if _truthy(item.get("fl_suspenso")):
        return RegistrationStatus.CLOSED
    dt = _parse_dt(item.get("dt_evento"))
    if dt is not None and dt.date() < date.today():
        return RegistrationStatus.CLOSED
    return {
        "open": RegistrationStatus.OPEN,
        "closed": RegistrationStatus.CLOSED,
        "coming_soon": RegistrationStatus.COMING_SOON,
        "sold_out": RegistrationStatus.SOLD_OUT,
    }.get(item.get("_pay_status"), RegistrationStatus.UNKNOWN)


def _classify_pay_page(body: str) -> str | None:
    """Le a pagina pay.ativo.com/evento/<id> -> status de inscricao.

    Estados observados na fonte (2026-07-24):
      - "vagas ja preenchidas / alta demanda / esgotado"  -> sold_out
        (a pagina de esgotado tambem diz "encerradas", entao vem ANTES).
      - "Inscricoes encerradas!" (tambem "... para publico geral") -> closed.
      - "Inscricoes em breve..."                          -> coming_soon.
      - fluxo real de kits/checkout (Escolha do Kit, Quero este, R$) -> open.
    Retorna None se nada casar (pagina inesperada -> status UNKNOWN, sem chute).
    """
    low = html_lib.unescape(body).lower()
    if "esgotad" in low or "preenchid" in low:
        return "sold_out"
    if "encerrad" in low:
        return "closed"
    if "em breve" in low:
        return "coming_soon"
    if re.search(r"escolha do kit|quero este|adicionar|r\$\s*\d", low):
        return "open"
    return None


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
    """Aceita só URLs de imagem plausíveis.

    Alguns thumbnails vêm com a extensão corrompida no dump do Ativo
    ('.pço', '.alq', '.aoe', '.pçw'...); a extensão real (jpg/png) se perdeu e
    não é recuperável, então preferimos NENHUMA imagem a um link quebrado.

    BUG CORRIGIDO: o `return url if ... else url` devolvia a URL nos dois ramos
    (no-op), então as extensões corrompidas passavam direto para o campo.
    """
    if not url or not url.startswith("http"):
        return None
    return url if re.search(r"\.(jpg|jpeg|png|webp|gif)$", url, re.IGNORECASE) else None


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
