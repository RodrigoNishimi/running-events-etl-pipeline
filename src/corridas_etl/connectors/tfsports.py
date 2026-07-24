"""Conector TFSports (www.tfsports.com.br).

Plataforma da Track&Field que promove o Santander Track&Field Run Series — o
maior circuito de corridas de rua da America Latina em numero de etapas
(distancias de 4km a meia maratona). ~25 etapas de corrida no calendario.

Estrategia (mapeada em 2026-07-24):
  - Site Next.js (SSG). robots.txt libera tudo (`Allow: /`) e ha SITEMAP.
  - discover -> GET /sitemap.xml, coleta as URLs /run-series/<slug> (o produto
    "corrida de rua"; ignoramos /tf-experience e /continue-em-movimento, que sao
    experiencias/aulas de bem-estar).
  - fetch    -> GET da pagina da etapa (HTML permitido; segue o redirect para a
                barra final).
  - parse    -> a pagina traz DOIS blocos estruturados:
                  * JSON-LD schema.org/Event (nome, data, local, imagem,
                    organizador) — dado publicado para consumo por maquinas;
                  * __NEXT_DATA__ -> pageData.attributes.eventData, com o status
                    de inscricao (`isSubscriptionClosed`), o link de inscricao
                    (`subscriptionCta.url`) e o cronograma (`infos`), de onde
                    extraimos as distancias ("Largada 21km/10km/5km").

Conduta: robots.txt libera tudo. O Next.js, porem, so entrega a pagina COMPLETA
(com JSON-LD e __NEXT_DATA__) para User-Agents de browser; a etapas ainda em
render sob demanda (ISR) devolve um shell vazio a UAs nao-browser. Usamos um UA
de browser para uma rota publica permitida — o mesmo trafego de qualquer
navegador — mantendo o rate limit educado do BaseConnector.

Limitacao: as distancias nao vem estruturadas; sao lidas best-effort do texto do
cronograma (as etapas Run Series sao padronizadas, mas o texto pode variar).
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from selectolax.parser import HTMLParser

from ..models import Distance, RawPayload, RegistrationStatus, SourceEventRecord
from ..utils.geo import is_br_uf
from .base import BaseConnector

BASE_URL = "https://www.tfsports.com.br"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"

TZ_BRT = timezone(timedelta(hours=-3))

# O Next.js do TFSports so serve a pagina completa a UAs de browser (ver docstring).
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

_RUN_SERIES_RE = re.compile(r"<loc>\s*(https?://[^<]*/run-series/[^<]+?)\s*</loc>")

# "Cidade - UF, 51110-160" no fim do endereco -> (Cidade, UF). O separador antes
# da cidade pode ser virgula ('Vila Olimpia, Sao Paulo - SP') OU hifen ('Rocha
# Pombo - Curitiba - SC'), por isso aceitamos os dois. Ancorado no CEP no fim.
_LOCATION_CEP_RE = re.compile(
    r"(?:^|[,\-])\s*([^,\-]+?)\s*-\s*([A-Z]{2})\s*,?\s*\d{5}-?\d{3}\s*$"
)
# Fallback sem CEP: "... Cidade - UF"
_LOCATION_RE = re.compile(r"(?:^|[,\-])\s*([^,\-]+?)\s*-\s*([A-Z]{2})\s*$")

# Distancias no texto do cronograma: "21km", "10 km", "5K". Exige o token k/km.
_KM_RE = re.compile(r"\b(\d{1,3}(?:[.,]\d{1,2})?)\s*k(?:m)?\b", re.IGNORECASE)


class TFSportsConnector(BaseConnector):
    source = "tfsports"

    def __init__(self) -> None:
        super().__init__()
        self._client.headers["User-Agent"] = _BROWSER_UA

    # -- Descoberta (sitemap) ------------------------------------------------

    def discover(self) -> Iterable[str]:
        sitemap = self.http_get(SITEMAP_URL).text
        seen: set[str] = set()
        for url in _RUN_SERIES_RE.findall(sitemap):
            url = url if url.endswith("/") else url + "/"    # canonico com barra
            if url not in seen:
                seen.add(url)
                yield url

    # -- Fetch (pagina publica da etapa) -------------------------------------

    def fetch(self, event_ref: str) -> RawPayload:
        resp = self.http_get(event_ref)
        return self.make_payload(
            _slug(event_ref), resp.text, url=event_ref, content_type="text/html"
        )

    # -- Parse (JSON-LD Event + eventData do __NEXT_DATA__) ------------------

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        tree = HTMLParser(payload.body)
        ld = _json_ld_event(tree)
        event_data = _event_data(tree)
        if ld is None and event_data is None:
            return None

        name = ((ld or {}).get("name") or "").strip()
        if not name:
            return None

        start_at = _parse_date((ld or {}).get("startDate"))
        end_at = _parse_date((ld or {}).get("endDate")) or start_at

        location_name = ((ld or {}).get("location") or {}).get("name") or ""
        city, state = _parse_location(location_name)
        # eventData as vezes tem cidade/UF proprios; usa como reforco.
        city = city or ((event_data or {}).get("city") or None)
        ed_state = (event_data or {}).get("state")
        if state is None and is_br_uf(ed_state):
            state = str(ed_state).upper()

        cta = (event_data or {}).get("subscriptionCta") or {}
        official_url = cta.get("url") or (ld or {}).get("url") or payload.source_url

        return SourceEventRecord(
            source=self.source,
            source_event_id=payload.source_event_id,
            source_url=payload.source_url,
            raw_hash=payload.content_hash,
            name=name,
            description=(ld or {}).get("description"),
            organizer_name=((ld or {}).get("organizer") or {}).get("name") or "TFSports",
            start_at=start_at,
            registration_status=_status(event_data, end_at),
            official_url=official_url,
            image_url=_first_image((ld or {}).get("image")) or _cover_image(event_data),
            city=city,
            state=state,
            address=_clean_address(location_name) or None,
            distances=_distances_from_infos((event_data or {}).get("infos")),
        )


# -- Extracao de dado estruturado da pagina --------------------------------


def _json_ld_event(tree: HTMLParser) -> dict | None:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (json.JSONDecodeError, ValueError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "Event":
                return item
    return None


def _event_data(tree: HTMLParser) -> dict | None:
    node = tree.css_first("#__NEXT_DATA__")
    if node is None:
        return None
    try:
        data = json.loads(node.text())
    except (json.JSONDecodeError, ValueError):
        return None
    try:
        return data["props"]["pageProps"]["pageData"]["attributes"]["eventData"]
    except (KeyError, TypeError):
        return None


# -- Helpers de campo -------------------------------------------------------


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def _parse_date(value: str | None) -> datetime | None:
    """'2026-08-16' -> datetime em horario de Brasilia (meia-noite)."""
    if not value:
        return None
    try:
        d = datetime.strptime(value.strip()[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return d.replace(tzinfo=TZ_BRT)


def _parse_location(location_name: str) -> tuple[str | None, str | None]:
    """(cidade, UF) do endereco do JSON-LD ('..., Recife - PE, 51110-160')."""
    text = (location_name or "").strip()
    m = _LOCATION_CEP_RE.search(text) or _LOCATION_RE.search(text)
    if not m:
        return None, None
    city = m.group(1).strip() or None
    uf = m.group(2)
    return city, (uf if is_br_uf(uf) else None)


def _clean_address(location_name: str) -> str:
    """Remove emoji/pin e espacos de borda do endereco (o campo location do
    eventData vem prefixado com um pin; o do JSON-LD, as vezes, com espaco)."""
    return re.sub(r"^[^\w]+", "", (location_name or "").strip()).strip()


def _status(event_data: dict | None, end_at: datetime | None) -> RegistrationStatus:
    """Status da inscricao.

    Ordem: evento ja realizado -> CLOSED; `isSubscriptionClosed` verdadeiro ->
    CLOSED; ha link de inscricao ativo -> OPEN; senao UNKNOWN (nao chuta).
    """
    if end_at is not None and end_at.date() < date.today():
        return RegistrationStatus.CLOSED
    ed = event_data or {}
    if ed.get("isSubscriptionClosed") is True:
        return RegistrationStatus.CLOSED
    if (ed.get("subscriptionCta") or {}).get("url"):
        return RegistrationStatus.OPEN
    return RegistrationStatus.UNKNOWN


def _first_image(image: object) -> str | None:
    """Normaliza o `image` do schema.org (string, lista ou ImageObject)."""
    if isinstance(image, str):
        return image or None
    if isinstance(image, dict):
        return image.get("url") or None
    if isinstance(image, list):
        for item in image:
            url = _first_image(item)
            if url:
                return url
    return None


def _cover_image(event_data: dict | None) -> str | None:
    try:
        return event_data["coverImage"]["data"]["attributes"]["url"] or None
    except (KeyError, TypeError):
        return None


def _distances_from_infos(infos: object) -> list[Distance]:
    """Distancias lidas do texto do cronograma ('Largada 21km/10km/5km').

    Best-effort: junta os blocos de `infos`, remove HTML e varre tokens km/K
    numa faixa plausivel de corrida de rua (descarta horarios como '5h30', que
    nao tem o token k).
    """
    if not isinstance(infos, list):
        return []
    text = " ".join(
        (block.get("text") or "") for block in infos if isinstance(block, dict)
    )
    text = re.sub(r"<[^>]+>", " ", text)

    out: list[Distance] = []
    seen: set[float] = set()
    for m in _KM_RE.finditer(text):
        km = float(m.group(1).replace(",", "."))
        if 0.4 <= km <= 120 and km not in seen:
            seen.add(km)
            out.append(Distance(label=m.group(0).strip(), distance_km=km))
    return out
