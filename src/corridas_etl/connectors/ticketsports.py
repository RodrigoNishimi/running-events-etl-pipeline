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
        city, state = _parse_city_state(location_name)

        offers = ld.get("offers") or {}
        status = _STATUS_MAP.get(offers.get("availability"), RegistrationStatus.UNKNOWN)

        images = ld.get("image") or []
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
            image_url=images[0] if images else None,
            city=city,
            state=state,
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


def _parse_city_state(location_name: str) -> tuple[str | None, str | None]:
    """Extrai (cidade, UF) de 'Praca X: Praca X, Matinhos, PR, Brasil'.

    Defesas contra enderecos reais malformados (observados em 2026-07-19):
      - numero de rua no lugar da cidade ('Av. X, 150, RJ') -> cidade None;
      - nome do local grudado ('AABB - ARACAJU : Aracaju, SE') -> so o que
        vem depois do ultimo ':'.
    """
    parts = [p.strip() for p in location_name.split(",") if p.strip()]
    # Procura o padrao <cidade>, <UF> varrendo de tras pra frente ("Brasil" e opcional).
    for i in range(len(parts) - 1, 0, -1):
        if _UF_RE.match(parts[i]):
            city = parts[i - 1].split(":")[-1].strip()
            if not city or city.isdigit():
                return None, parts[i]
            return city, parts[i]
    return None, None


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
