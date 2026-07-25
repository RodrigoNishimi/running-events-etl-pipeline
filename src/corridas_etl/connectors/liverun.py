"""Conector LIVE! Run XP (liverun.com.br).

Organizadora do circuito LIVE! Run XP (meias-maratonas 21K, maratonas 42K,
etapas Run de 5/10K e experiencias): ~26 etapas futuras em dezenas de cidades,
de marco a dezembro.

Estrategia (mapeada em 2026-07-24):
  - Site Astro server-rendered (SSR), sem JSON-LD nem API publica exposta;
    robots.txt libera tudo (`Allow: /`).
  - A pagina /calendario e a fonte primaria: UM request traz todos os cards de
    etapa, cada um com cidade+UF, data (dd/mm), distancias e status de inscricao,
    alem do link /etapa/<slug>. A data com ANO nao aparece em lugar nenhum de
    forma estruturada (o card mostra so dd/mm; a pagina do evento renderiza a
    data de formas diferentes por tipo de etapa) — inferimos o ano pela regra do
    calendario rolante (a proxima ocorrencia daquele dd/mm).
  - A pagina /etapa/<slug> complementa (best-effort, sem derrubar a coleta) com
    endereco completo (para geocoding) e a imagem do evento.

Decisoes de campo:
  - nome: composto `LIVE! <tipo> <cidade>` — o <tipo> (Run/42K/21K/Experience)
    vem do slug; a <cidade> vem do card (acentuada e precisa: e a cidade do
    LOCAL, ex. Pinhais/PR na "etapa Curitiba"). O nome oficial usa a cidade de
    marketing, mas preferimos a do local por ser acentuada e coerente com o geo.
  - status: texto do card ("Inscricoes abertas"/"Ultimas vagas" -> OPEN etc.).
  - distancias: so as com km numerico do card ("Corrida Kids" nao tem distancia).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import httpx
from selectolax.parser import HTMLParser

from ..models import Distance, RawPayload, RegistrationStatus, SourceEventRecord
from ..utils.geo import is_br_uf
from ..utils.render import BROWSER_UA
from .base import BaseConnector

log = logging.getLogger(__name__)

BASE_URL = "https://liverun.com.br"
CALENDAR_URL = f"{BASE_URL}/calendario"

# O site fica atras de um WAF que recusa clientes nao-browser — invisivel
# rodando em casa, mas evidente a partir de IPs de datacenter (CI): 403 no
# /calendario. O UA de browser resolve a maioria; quando nem isso basta
# (checagem de TLS/JS), o discover cai para o Playwright.
_WAF_STATUSES = frozenset({401, 403, 503})

TZ_BRT = timezone(timedelta(hours=-3))

# O calendario mostra so dd/mm. Ancoramos o ano na proxima ocorrencia daquele
# dia: se a data no ano corrente ja passou ha mais que esta folga, e a do ano
# seguinte (calendario rolante). A folga mantem etapas recentissimas "quentes".
PAST_GRACE_DAYS = 7

# Segmento "Cidade - UF" do card (UF em 2 maiusculas, com espacos ao redor do
# hifen — evita casar "Inscreva-se").
_CITY_UF_RE = re.compile(r"^(.+?)\s+-\s+([A-Z]{2})$")
_DDMM_RE = re.compile(r"^(\d{1,2})/(\d{1,2})$")


class LiveRunConnector(BaseConnector):
    source = "liverun"

    def __init__(self) -> None:
        super().__init__()
        self._client.headers["User-Agent"] = BROWSER_UA
        # Cache dos cards do calendario, indexado por slug (preenchido em discover).
        self._cards: dict[str, dict] = {}

    # -- Descoberta (1 request no calendario) --------------------------------

    def _calendar_html(self) -> str:
        """HTML do /calendario: HTTP direto e, se o WAF barrar, browser real.

        O fallback custa um Chromium headless, entao so vale por ser UM request
        (o calendario inteiro vem de uma vez). As paginas /etapa/<slug> seguem
        em HTTP puro: sao ~26 e ja sao best-effort.
        """
        try:
            return self.http_get(CALENDAR_URL).text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _WAF_STATUSES:
                raise
            log.warning(
                "liverun: %s no calendario via HTTP — refazendo com browser (Playwright)",
                exc.response.status_code,
            )

        from ..utils.render import page_html

        return page_html(CALENDAR_URL)

    def discover(self) -> Iterable[str]:
        html = self._calendar_html()
        for slug, card in _parse_calendar(html):
            self._cards[slug] = card
            yield slug

    # -- Fetch (card do calendario + extras da pagina da etapa) --------------

    def fetch(self, event_ref: str) -> RawPayload:
        card = self._cards.get(event_ref)
        if card is None:
            # Reprocessamento por slug avulso: redescobre (barato: 1 request).
            for _ in self.discover():
                pass
            card = self._cards.get(event_ref)
        if card is None:
            raise KeyError(f"etapa {event_ref} nao encontrada no calendario LIVE! Run")

        event_url = f"{BASE_URL}/etapa/{event_ref}"
        address, image_url = None, None
        try:
            # Extras (endereco/imagem) sao best-effort: uma etapa sem eles nao
            # deve derrubar a coleta — os campos essenciais ja vem do card.
            address, image_url = _extract_page_extras(self.http_get(event_url).text)
        except Exception:
            pass

        body = {
            **card,
            "slug": event_ref,
            "event_url": event_url,
            "address": address,
            "image_url": image_url,
        }
        return self.make_payload(
            event_ref,
            json.dumps(body, ensure_ascii=False, sort_keys=True),
            url=event_url,
            content_type="application/json",
        )

    # -- Parse ---------------------------------------------------------------

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        body = json.loads(payload.body)

        city = (body.get("city") or "").strip() or None
        if city is None:
            return None
        raw_uf = (body.get("uf") or "").strip()
        state = raw_uf.upper() if is_br_uf(raw_uf) else None

        name = f"LIVE! {_name_type(body.get('slug') or '')} {city}"

        return SourceEventRecord(
            source=self.source,
            source_event_id=payload.source_event_id,
            source_url=payload.source_url,
            raw_hash=payload.content_hash,
            name=name,
            organizer_name="LIVE! Run",
            start_at=_event_datetime(body.get("day"), body.get("month")),
            registration_status=_map_status(body.get("status_text")),
            official_url=body.get("event_url"),
            image_url=body.get("image_url"),
            city=city,
            state=state,
            address=body.get("address"),
            distances=_distances(body.get("distance_labels") or []),
        )


# -- Helpers de descoberta/parse -------------------------------------------


def _parse_calendar(html: str) -> list[tuple[str, dict]]:
    """Extrai (slug, card) de cada etapa no /calendario.

    Cada etapa e um <a href="/etapa/<slug>"> cujo texto reune, em pedacos,
    cidade+UF, dd/mm, distancias e status. Lemos por conteudo (nao por classe,
    que e hash instavel de CSS-in-JS).
    """
    tree = HTMLParser(html)
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for a in tree.css("a"):
        href = a.attributes.get("href") or ""
        if "/etapa/" not in href:
            continue
        slug = href.rstrip("/").rsplit("/etapa/", 1)[-1]
        if not slug or slug in seen:
            continue

        parts = [p.strip() for p in a.text(separator="|", strip=True).split("|")]
        # Descarta lixo de CSS-in-JS (regras que vazam como texto) e vazios.
        parts = [p for p in parts if p and "{" not in p and "css-" not in p]

        city = uf = day = month = None
        status_text = None
        labels: list[str] = []
        for p in parts:
            m = _CITY_UF_RE.match(p)
            if m and city is None:
                city, uf = m.group(1).strip(), m.group(2)
                continue
            m = _DDMM_RE.match(p)
            if m and day is None:
                day, month = int(m.group(1)), int(m.group(2))
                continue
            if Distance.from_label(p).distance_km is not None:
                labels.append(p)
                continue
            if re.search(r"inscri|vaga|esgot|encerr|em breve", p, re.IGNORECASE):
                status_text = p

        # Sem cidade nem data o card nao e uma etapa util (ex.: banner do topo).
        if city is None and day is None:
            continue

        seen.add(slug)
        out.append(
            (
                slug,
                {
                    "city": city,
                    "uf": uf,
                    "day": day,
                    "month": month,
                    "distance_labels": labels,
                    "status_text": status_text,
                },
            )
        )
    return out


def _extract_page_extras(html: str) -> tuple[str | None, str | None]:
    """Da pagina /etapa/<slug>: (endereco completo, url da imagem do evento)."""
    tree = HTMLParser(html)

    address = None
    for node in tree.css("h1, h2, h3, p, address, span"):
        txt = node.text(strip=True)
        if txt and re.search(r"\d{5}-?\d{3}", txt) and len(txt) < 160:
            address = txt
            break

    image_url = None
    for img in tree.css("img"):
        src = img.attributes.get("src") or ""
        # Imagem do evento (nao os thumbnails dos kits).
        if "events/" in src and "/kits/" not in src:
            image_url = src
            break

    return address, image_url


def _name_type(slug: str) -> str:
    """Tipo da etapa a partir do slug: Run (padrao), 42K, 21K ou Experience."""
    if slug.startswith("live42k"):
        return "42K"
    if slug.startswith("live21k"):
        return "21K"
    if slug.startswith("live-experience"):
        return "Experience"
    return "Run"


def _event_datetime(day: object, month: object) -> datetime | None:
    """dd/mm -> datetime BRT no ANO da proxima ocorrencia (calendario rolante).

    O calendario nao publica o ano. Assumimos que cada etapa e a proxima
    ocorrencia daquele dia: usa o ano corrente, salvo se a data ja passou ha
    mais que PAST_GRACE_DAYS — nesse caso, o ano seguinte.
    """
    if not isinstance(day, int) or not isinstance(month, int):
        return None
    today = date.today()
    try:
        cand = date(today.year, month, day)
    except ValueError:
        return None
    if cand < today - timedelta(days=PAST_GRACE_DAYS):
        try:
            cand = date(today.year + 1, month, day)
        except ValueError:
            return None
    return datetime(cand.year, cand.month, cand.day, tzinfo=TZ_BRT)


def _map_status(status_text: str | None) -> RegistrationStatus:
    if not status_text:
        return RegistrationStatus.UNKNOWN
    low = status_text.lower()
    if "esgot" in low:
        return RegistrationStatus.SOLD_OUT
    if "encerr" in low:
        return RegistrationStatus.CLOSED
    if "em breve" in low:
        return RegistrationStatus.COMING_SOON
    if "abert" in low or "vaga" in low:      # "Inscricoes abertas" / "Ultimas vagas"
        return RegistrationStatus.OPEN
    return RegistrationStatus.UNKNOWN


def _distances(labels: list[str]) -> list[Distance]:
    out: list[Distance] = []
    seen: set[float] = set()
    for label in labels:
        d = Distance.from_label(label)
        if d.distance_km is not None and d.distance_km not in seen:
            seen.add(d.distance_km)
            out.append(d)
    return out
