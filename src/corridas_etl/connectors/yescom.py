"""Conector Yescom (www.yescom.com.br).

Organizadora das maiores corridas do pais (Sao Silvestre, Maratona Internacional
de Sao Paulo, Volta da Pampulha, Dez Milhas Garoto). Poucos eventos (~13), mas
de altissimo perfil.

Estrategia (mapeada em 2026-07-19):
  - Site estatico sem robots.txt; cada evento e um microsite proprio em
    /<slug>/<ano>/(cidade/)?index.(html|asp), linkado da homepage.
  - Nao ha dado estruturado (sem JSON-LD, sem API): o parse e BEST-EFFORT
    heuristico e assume ruido:
      nome  <- <title> da pagina
      data  <- data mais frequente no texto que caia no ano do URL
      local <- mapa de dicas por slug/segmento + busca textual de capitais
      dist. <- apenas nomeadas no titulo ("Meia Maratona", "Dez Milhas");
               kms soltos no texto sao marcadores de percurso (ruido).
  - Campos ausentes ficam NULL e podem ser preenchidos por enriquecimento.
"""

from __future__ import annotations

import html as html_lib
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..models import Distance, RawPayload, RegistrationStatus, SourceEventRecord
from ..utils.distances import HALF_MARATHON_KM, MARATHON_KM
from .base import BaseConnector

BASE_URL = "https://www.yescom.com.br"

TZ_BRT = timezone(timedelta(hours=-3))

_EVENT_LINK_RE = re.compile(
    r'href="((?:https?://www\.yescom\.com\.br)?/\w[\w./-]*?/20\d\d/[\w./-]*?index\.(?:html|asp))"'
)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL)
_YEAR_IN_URL_RE = re.compile(r"/(20\d\d)/")

_MONTHS_PT = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}
_DATE_LONG_RE = re.compile(
    r"\b(\d{1,2})\s+de\s+([a-zçã]+)(?:\s+de\s+(\d{4}))?", re.IGNORECASE
)
_DATE_NUM_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

# Dicas de local por fragmento de URL ou de titulo (microsites nao declaram
# cidade de forma estruturada). Mantenha curto: sao ~13 eventos conhecidos.
_PLACE_HINTS: list[tuple[str, str, str]] = [
    ("saopaulo", "São Paulo", "SP"),
    ("/sp/", "São Paulo", "SP"),
    ("maratonasp", "São Paulo", "SP"),
    ("meiasp", "São Paulo", "SP"),
    ("saosilvestre", "São Paulo", "SP"),
    ("/rj/", "Rio de Janeiro", "RJ"),
    ("meiadorio", "Rio de Janeiro", "RJ"),
    ("pampulha", "Belo Horizonte", "MG"),
    ("bh-vix", "Belo Horizonte", "MG"),
    ("garoto", "Vitória", "ES"),          # Dez Milhas/Cachorrida Garoto (Vila Velha/ES)
]
_CITY_TEXT_RE = re.compile(
    r"\b(São Paulo|Sao Paulo|Rio de Janeiro|Belo Horizonte|Vit[óo]ria|Vila Velha)\b"
)
_CITY_TO_UF = {
    "sao paulo": ("São Paulo", "SP"),
    "são paulo": ("São Paulo", "SP"),
    "rio de janeiro": ("Rio de Janeiro", "RJ"),
    "belo horizonte": ("Belo Horizonte", "MG"),
    "vitoria": ("Vitória", "ES"),
    "vitória": ("Vitória", "ES"),
    "vila velha": ("Vila Velha", "ES"),
}


class YescomConnector(BaseConnector):
    source = "yescom"

    def discover(self) -> Iterable[str]:
        resp = self.http_get(BASE_URL + "/")
        seen: set[str] = set()
        for match in _EVENT_LINK_RE.finditer(resp.text):
            url = match.group(1)
            if not url.startswith("http"):
                url = BASE_URL + url
            if url not in seen:
                seen.add(url)
                yield url

    def fetch(self, event_ref: str) -> RawPayload:
        resp = self.http_get(event_ref)
        # id estavel: caminho sem dominio nem index.* ("maratonasp/2027")
        event_id = re.sub(r"^https?://[^/]+/|/index\.(html|asp)$", "", event_ref).strip("/")
        return self.make_payload(event_id, resp.text, url=event_ref, content_type="text/html")

    def parse(self, payload: RawPayload) -> SourceEventRecord | None:
        html = payload.body
        title_match = _TITLE_RE.search(html)
        if not title_match:
            return None
        name = html_lib.unescape(title_match.group(1)).strip()
        name = re.sub(r"\s+", " ", name)
        if not name:
            return None

        url = payload.source_url or ""
        year_match = _YEAR_IN_URL_RE.search(url)
        year = int(year_match.group(1)) if year_match else None

        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.DOTALL)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html_lib.unescape(re.sub(r"\s+", " ", text))

        city, state = _guess_place(url, name, text)

        return SourceEventRecord(
            source=self.source,
            source_event_id=payload.source_event_id,
            source_url=url or None,
            raw_hash=payload.content_hash,
            name=name,
            organizer_name="Yescom",
            start_at=_guess_event_date(text, year),
            registration_status=RegistrationStatus.UNKNOWN,
            official_url=url or None,
            city=city,
            state=state,
            distances=_distances_from_name(name),
        )


def _guess_event_date(text: str, year: int | None) -> datetime | None:
    """Data mais frequente no texto que caia no ano do evento (do URL).

    Microsites citam a data da prova varias vezes (home, regulamento, retirada
    de kit cita outras); a moda dentro do ano correto e um bom chute.
    """
    if year is None:
        return None
    votes: Counter[tuple[int, int]] = Counter()
    for m in _DATE_LONG_RE.finditer(text):
        day, month_name, y = m.group(1), m.group(2).lower(), m.group(3)
        month = _MONTHS_PT.get(month_name)
        if month and (y is None or int(y) == year):
            votes[(month, int(day))] += 1 if y is None else 2  # data com ano vale mais
    for m in _DATE_NUM_RE.finditer(text):
        day, month, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y == year and 1 <= month <= 12 and 1 <= day <= 31:
            votes[(month, day)] += 2
    if not votes:
        return None
    (month, day), _ = votes.most_common(1)[0]
    try:
        return datetime(year, month, day, tzinfo=TZ_BRT)
    except ValueError:
        return None


def _guess_place(url: str, name: str, text: str) -> tuple[str | None, str | None]:
    haystack = (url + " " + name).lower()
    for hint, city, uf in _PLACE_HINTS:
        if hint in haystack:
            return city, uf
    cities = _CITY_TEXT_RE.findall(text)
    if cities:
        # cidade mais citada no texto
        top = Counter(c.lower() for c in cities).most_common(1)[0][0]
        return _CITY_TO_UF.get(top, (None, None))
    return None, None


def _distances_from_name(name: str) -> list[Distance]:
    lowered = name.lower()
    if "meia" in lowered:
        return [Distance(label="Meia Maratona", distance_km=HALF_MARATHON_KM)]
    if "maratona" in lowered and "meia" not in lowered:
        return [Distance(label="Maratona", distance_km=MARATHON_KM)]
    if "dez milhas" in lowered:
        return [Distance(label="Dez Milhas", distance_km=16.09)]
    return []
