"""Enriquecimento (Fase 1): preenche lacunas que os conectores nao cobrem.

    python -m corridas_etl.pipeline.enrich --step iguana-dates
    python -m corridas_etl.pipeline.enrich --step ticketsports-distances --limit 10

Passos:
  iguana-dates
      O JSON de produto Shopify da Iguana nao tem data nem cidade; os cards da
      homepage (renderizados por JS) tem: "Nike SP City Marathon 2026" /
      "São Paulo | SP | Brasil" / "26 Jul 2026 05:15". Renderizamos a homepage
      UMA vez e casamos os cards com os eventos do banco por fuzzy matching.

  ticketsports-distances
      As distancias so vem no titulo (o conteudo completo e client-side).
      Para eventos sem nenhuma distancia, renderizamos a pagina publica do
      evento e extraimos "5km/10km/21km..." do texto visivel.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from datetime import datetime, timedelta, timezone

import psycopg
from rapidfuzz import fuzz

from ..utils.distances import parse_distance_km
from ..utils.render import pages_inner_text
from ..utils.text import normalize_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("corridas_etl.enrich")

TZ_BRT = timezone(timedelta(hours=-3))

_MONTHS_PT = {
    "jan": 1, "fev": 2, "mar": 3, "abr": 4, "mai": 5, "jun": 6,
    "jul": 7, "ago": 8, "set": 9, "out": 10, "nov": 11, "dez": 12,
}

# "26 Jul 2026 05:15" (hora opcional)
_CARD_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(Jan|Fev|Mar|Abr|Mai|Jun|Jul|Ago|Set|Out|Nov|Dez)\.?\s+(\d{4})"
    r"(?:\s+(\d{1,2}):(\d{2}))?",
    re.IGNORECASE,
)
# "São Paulo | SP | Brasil"
_CARD_PLACE_RE = re.compile(r"^\s*(.+?)\s*\|\s*([A-Z]{2})\s*\|", re.MULTILINE)

# Distancias no texto renderizado: exige token k/km (evita anos/valores).
_TEXT_KM_RE = re.compile(r"\b(\d{1,3}(?:[.,]\d{1,2})?)\s*k(?:m)?\b", re.IGNORECASE)


# -- Passo: iguana-dates ------------------------------------------------------

def parse_iguana_cards(body_text: str) -> list[dict]:
    """Extrai cards {name, city, state, start_at} do texto renderizado da home.

    Estrutura observada (2026-07-19): nome na(s) linha(s) acima do local,
    local "Cidade | UF | Brasil", data "26 Jul 2026 05:15" logo abaixo.
    """
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]
    cards: list[dict] = []
    for i, line in enumerate(lines):
        place = _CARD_PLACE_RE.match(line)
        if not place:
            continue
        date_match = None
        for j in (i + 1, i + 2):
            if j < len(lines):
                date_match = _CARD_DATE_RE.search(lines[j])
                if date_match:
                    break
        if not date_match or i == 0:
            continue
        day, mon, year, hh, mm = date_match.groups()
        cards.append(
            {
                "name": lines[i - 1],
                "city": place.group(1),
                "state": place.group(2),
                "start_at": datetime(
                    int(year), _MONTHS_PT[mon.lower()], int(day),
                    int(hh or 0), int(mm or 0), tzinfo=TZ_BRT,
                ),
            }
        )
    return cards


def enrich_iguana_dates(conn: psycopg.Connection) -> int:
    events = conn.execute(
        """
        SELECT DISTINCT e.id, e.name FROM event e
        JOIN source_record sr ON sr.event_id = e.id AND sr.source = 'iguanasports'
        WHERE e.start_at IS NULL OR e.city IS NULL
        """
    ).fetchall()
    if not events:
        log.info("iguana-dates: nada a enriquecer")
        return 0

    body = pages_inner_text(["https://iguanasports.com.br/"]).get(
        "https://iguanasports.com.br/", ""
    )
    cards = parse_iguana_cards(body)
    log.info("iguana-dates: %d cards extraidos da homepage", len(cards))

    updated = 0
    for event_id, event_name in events:
        best, best_score = None, 0.0
        for card in cards:
            score = fuzz.token_set_ratio(
                normalize_name(event_name), normalize_name(card["name"])
            )
            if score > best_score:
                best, best_score = card, score
        if best is None or best_score < 85:
            log.info("  sem match p/ '%s' (melhor=%.0f)", event_name, best_score)
            continue
        conn.execute(
            """
            UPDATE event SET
                start_at = COALESCE(start_at, %s),
                city = COALESCE(city, %s),
                state = COALESCE(state, %s),
                updated_at = now()
            WHERE id = %s
            """,
            (best["start_at"], best["city"], best["state"], event_id),
        )
        log.info(
            "  '%s' <- %s, %s/%s (match %.0f)",
            event_name, best["start_at"].date(), best["city"], best["state"], best_score,
        )
        updated += 1
    return updated


# -- Passo: ticketsports-distances --------------------------------------------

def extract_distances_from_text(text: str) -> list[tuple[str, float]]:
    """Extrai distancias plausiveis (label, km) do texto renderizado."""
    found: dict[float, str] = {}
    for m in _TEXT_KM_RE.finditer(text):
        km = float(m.group(1).replace(",", "."))
        if 0.4 <= km <= 120 and km not in found:
            found[km] = m.group(0).strip()
    return [(label, km) for km, label in sorted(found.items())]


def enrich_ticketsports_distances(conn: psycopg.Connection, limit: int | None) -> int:
    rows = conn.execute(
        """
        SELECT e.id, e.name, e.official_url FROM event e
        JOIN source_record sr ON sr.event_id = e.id AND sr.source = 'ticketsports'
        WHERE e.official_url IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM event_distance d WHERE d.event_id = e.id)
        ORDER BY e.start_at NULLS LAST
        """
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        log.info("ticketsports-distances: nada a enriquecer")
        return 0

    log.info("ticketsports-distances: renderizando %d paginas", len(rows))
    texts = pages_inner_text([r[2] for r in rows])

    updated = 0
    for event_id, name, url in rows:
        distances = extract_distances_from_text(texts.get(url, ""))
        if not distances:
            log.info("  '%s': nenhuma distancia encontrada", name)
            continue
        for label, km in distances:
            conn.execute(
                """
                INSERT INTO event_distance (event_id, label, distance_km)
                VALUES (%s, %s, %s)
                ON CONFLICT (event_id, label) DO NOTHING
                """,
                (event_id, label, km),
            )
        log.info("  '%s': %s", name, [km for _, km in distances])
        updated += 1
    return updated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Enriquecimento de eventos (Fase 1)")
    parser.add_argument(
        "--step", required=True, choices=["iguana-dates", "ticketsports-distances"]
    )
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)

    from ..db import connect

    with connect() as conn:
        if args.step == "iguana-dates":
            n = enrich_iguana_dates(conn)
        else:
            n = enrich_ticketsports_distances(conn, args.limit)
    log.info("%s: %d eventos enriquecidos", args.step, n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
