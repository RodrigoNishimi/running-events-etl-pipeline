"""Conector Ticket Sports (www.ticketsports.com.br).

Maior agregador de inscricoes esportivas do Brasil: ~770 corridas de rua ativas
de centenas de organizadoras. E a fonte de maior volume da plataforma.

Estrategia (mapeada em 2026-07-19, respeitando o robots.txt do site):
  - O robots.txt PROIBE `/api/` e o sitemap esta quebrado (aponta p/ localhost).
  - As paginas publicas sao permitidas (`Allow: /`), entao:
      discover -> agentic scraping (Playwright) do calendario publico
                  (/calendario/filters): filtra "Corrida", clica "Carregar mais
                  eventos" ate esgotar e coleta os links `/e/<slug>-<id>`.
      fetch    -> GET simples da pagina do evento (HTML permitido).
      parse    -> JSON-LD schema.org/SportsEvent embutido na pagina (dado
                  estruturado publicado para consumo por maquinas) + extracao
                  de distancias do titulo.
  - Limitacao conhecida: as secoes de conteudo (percursos, kit) sao carregadas
    client-side via a API proibida, entao as distancias vem so do titulo.
    Enriquecimento via pagina renderizada fica para a Fase 1.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..config import settings
from ..models import Distance, RawPayload, RegistrationStatus, SourceEventRecord
from .base import BaseConnector

BASE_URL = "https://www.ticketsports.com.br"
CALENDAR_URL = f"{BASE_URL}/calendario/filters"

# Horario de Brasilia (Brasil nao tem horario de verao desde 2019).
TZ_BRT = timezone(timedelta(hours=-3))

_JSON_LD_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>', re.DOTALL
)
_EVENT_ID_RE = re.compile(r"-(\d+)/?$")

# Distancias no titulo: "5K", "10 km", "21,1K"... Exige o token k/km para
# evitar falsos positivos (edicoes, anos, "40 anos").
_TITLE_KM_RE = re.compile(r"\b(\d{1,3}(?:[.,]\d{1,2})?)\s*k(?:m)?\b", re.IGNORECASE)

_STATUS_MAP = {
    "https://schema.org/InStock": RegistrationStatus.OPEN,
    "https://schema.org/SoldOut": RegistrationStatus.SOLD_OUT,
    "https://schema.org/OutOfStock": RegistrationStatus.CLOSED,
}

_UF_RE = re.compile(r"^[A-Z]{2}$")


class TicketSportsConnector(BaseConnector):
    source = "ticketsports"

    # -- Descoberta (agentic scraping do calendario publico) -----------------

    def discover(self) -> Iterable[str]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise SystemExit(
                "O conector ticketsports precisa do Playwright para a descoberta:\n"
                '  pip install "corridas-etl[browser]" && playwright install chromium'
            )

        urls: list[str] = []
        seen: set[str] = set()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(user_agent=settings.user_agent)
            # networkidle nunca dispara aqui (analytics/polling continuos);
            # esperamos o DOM + o primeiro card de evento aparecer.
            page.goto(CALENDAR_URL, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_selector("a[href*='/e/']", timeout=30_000)

            # Filtro "Corrida" (corrida de rua) no painel "Modalidade".
            try:
                page.get_by_role("button", name="Modalidade").first.click()
                page.get_by_role("button", name="Corrida", exact=True).first.click()
                page.wait_for_timeout(2_500)
            except Exception:
                # Se o filtro mudar de layout, seguimos sem filtro (o parse
                # ainda funciona; so coletamos esportes a mais).
                pass

            # Clica "Carregar mais eventos" ate o botao sumir/estagnar.
            stable_rounds = 0
            while stable_rounds < 3:
                before = len(seen)
                for href in page.eval_on_selector_all(
                    "a[href*='/e/']", "els => els.map(e => e.href)"
                ):
                    href = href.split("?")[0]
                    if href not in seen:
                        seen.add(href)
                        urls.append(href)

                more = page.get_by_role("button", name=re.compile("carregar mais", re.I))
                if more.count() == 0:
                    break
                try:
                    more.first.click(timeout=5_000)
                except Exception:
                    break
                # Rate limit cortes: cada clique dispara 1 request do proprio site.
                page.wait_for_timeout(int(settings.request_delay_seconds * 1000))

                stable_rounds = stable_rounds + 1 if len(seen) == before else 0

            browser.close()

        return urls

    # -- Fetch (pagina publica do evento; permitida pelo robots.txt) ---------

    def fetch(self, event_ref: str) -> RawPayload:
        resp = self.http_get(event_ref)
        return self.make_payload(
            self._event_id(event_ref), resp.text, url=event_ref, content_type="text/html"
        )

    # -- Parse (JSON-LD SportsEvent) -----------------------------------------

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        ld = self._extract_json_ld(payload.body)
        if ld is None or ld.get("@type") != "SportsEvent":
            return None

        name = (ld.get("name") or "").strip()
        if not name:
            return None

        location_name = (ld.get("location") or {}).get("name") or ""
        city, state, country = _parse_location(location_name)

        offers = _first_offer(ld.get("offers"))
        status = _STATUS_MAP.get(offers.get("availability"), RegistrationStatus.UNKNOWN)

        organizer = (ld.get("organizer") or {}).get("name")

        return SourceEventRecord(
            source=self.source,
            source_event_id=payload.source_event_id,
            source_url=payload.source_url,
            raw_hash=payload.content_hash,
            name=name,
            description=ld.get("description"),
            organizer_name=organizer,
            start_at=_parse_start_date(ld.get("startDate")),
            registration_status=status,
            official_url=ld.get("url") or payload.source_url,
            image_url=_first_image(ld.get("image")),
            city=city,
            state=state,
            country=country,
            address=location_name or None,
            distances=_distances_from_title(name),
        )

    # -- Helpers -------------------------------------------------------------

    @staticmethod
    def _event_id(url: str) -> str:
        match = _EVENT_ID_RE.search(url)
        return match.group(1) if match else url.rstrip("/").rsplit("/", 1)[-1]

    @staticmethod
    def _extract_json_ld(html: str) -> dict | None:
        for block in _JSON_LD_RE.findall(html):
            try:
                data = json.loads(block)
            except json.JSONDecodeError:
                continue
            items = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict) and item.get("@type") == "SportsEvent":
                    return item
        return None


def _parse_start_date(value: str | None) -> datetime | None:
    """'2026-07-25T15:00' (sem timezone) -> datetime em horario de Brasilia."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=TZ_BRT)


def _first_offer(offers: object) -> dict:
    """Normaliza o `offers` do schema.org, que pode ser um Offer unico OU uma
    lista de Offers. Sem isso, uma lista quebraria o `.get('availability')`
    (AttributeError) e o evento inteiro seria perdido."""
    if isinstance(offers, list):
        return next((o for o in offers if isinstance(o, dict)), {})
    return offers if isinstance(offers, dict) else {}


def _first_image(image: object) -> str | None:
    """Normaliza o `image` do schema.org, que pode ser string, lista OU
    ImageObject ({'url': ...}). O codigo antigo assumia lista e fazia image[0]:
    um `image` string viraria image_url='h' (primeiro caractere) — corrupcao
    silenciosa do campo."""
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


# Nome de pais por extenso (como o Ticket Sports escreve) -> ISO-3166 alpha-2.
_COUNTRY_NAMES = {
    "brasil": "BR", "brazil": "BR",
    "chile": "CL", "uruguai": "UY", "uruguay": "UY", "argentina": "AR",
    "paraguai": "PY", "paraguay": "PY", "portugal": "PT", "espanha": "ES",
    "estados unidos": "US", "eua": "US", "usa": "US",
}


def _parse_location(location_name: str) -> tuple[str | None, str | None, str]:
    """Extrai (cidade, UF, pais ISO-2) de um endereco do Ticket Sports.

    Formatos reais (2026-07-19):
      'Praca X: Praca X, Matinhos, PR, Brasil'                  -> (Matinhos, PR, BR)
      'Punta del Este: ..., Punta del Este, MA, Uruguai'        -> (Punta del Este, None, UY)
      'Porto: 100, Porto, 13, Portugal'                         -> (Porto, None, PT)

    Regras: o ultimo segmento costuma ser o pais; a UF/subdivisao vem antes.
    Para paises != BR a subdivisao estrangeira ('MA', '13') NAO e uma UF valida,
    entao state fica None. Defesas contra lixo (numero de rua, prefixo 'Local:').
    """
    parts = [p.strip() for p in location_name.split(",") if p.strip()]
    if not parts:
        return None, None, "BR"

    # Pais: ultimo segmento, se for um nome de pais conhecido.
    country = "BR"
    if parts[-1].lower() in _COUNTRY_NAMES:
        country = _COUNTRY_NAMES[parts[-1].lower()]
        parts = parts[:-1]

    # Procura o token de subdivisao (2 letras) varrendo de tras pra frente.
    for i in range(len(parts) - 1, 0, -1):
        if _UF_RE.match(parts[i]):
            city = parts[i - 1].split(":")[-1].strip()
            if not city or city.isdigit():
                city = None
            # Subdivisao so vale como UF em eventos brasileiros.
            state = parts[i] if country == "BR" else None
            return city, state, country

    # Sem token de UF (comum em internacional apos remover o pais): a cidade
    # costuma ser o segmento nao-numerico que mais se repete no endereco
    # ('Porto: 100, Porto, 13' -> Porto; 'Punta del Este, 100, Punta del Este').
    if country != "BR":
        return _modal_city(parts), None, country
    return None, None, country


def _modal_city(parts: list[str]) -> str | None:
    from collections import Counter

    candidates: list[str] = []
    for part in parts:
        for chunk in part.split(":"):
            chunk = chunk.strip()
            # descarta numeros de rua e tokens curtos de subdivisao
            if chunk and not chunk.isdigit() and not _UF_RE.match(chunk):
                candidates.append(chunk)
    if not candidates:
        return None
    return Counter(candidates).most_common(1)[0][0]


def _distances_from_title(title: str) -> list[Distance]:
    distances: list[Distance] = []
    seen: set[float] = set()

    lowered = title.lower()
    if "meia maratona" in lowered:
        d = Distance.from_label("Meia Maratona")
        distances.append(d)
        seen.add(d.distance_km)  # type: ignore[arg-type]

    for m in _TITLE_KM_RE.finditer(title):
        km = float(m.group(1).replace(",", "."))
        # Faixa plausivel de corrida de rua; descarta ruido tipo "2026K".
        if 0.4 <= km <= 120 and km not in seen:
            seen.add(km)
            distances.append(Distance(label=m.group(0).strip(), distance_km=km))

    return distances
