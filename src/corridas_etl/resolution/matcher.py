"""Matching de eventos duplicados (entity resolution) — logica pura, sem I/O.

O mesmo evento do mundo real aparece com nomes diferentes entre fontes (ex.:
"Run The Bridge 2026" vs "Brooks Run The Bridge 2026" — prefixo de patrocinador)
e com evidencias parciais (uma fonte tem data e cidade, outra nao).

Estrategia: score composto normalizado PELA EVIDENCIA DISPONIVEL. Cada feature
(nome, data, local, distancias) so entra no denominador quando ambos os lados
tem o dado. Assim um par sem data nao e penalizado pela ausencia — mas tambem
nao ganha pontos por ela.

Guards (nunca auto-mesclar, independente do score):
  - anos explicitos diferentes no nome (edicoes distintas: "X 2025" vs "X 2026");
  - marcadores de sub-evento divergentes (kids/infantil/caminhada/virtual):
    "Athenas Run Longer" e "Athenas KIDS Run Longer" sao produtos distintos.

Decisao:
  score >= AUTO_MERGE_THRESHOLD e sem guard  -> merge automatico
  score >= REVIEW_THRESHOLD (ou guard ativo) -> fila de revisao manual
  abaixo                                     -> eventos distintos
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from rapidfuzz import fuzz

from ..utils.text import normalize_name

AUTO_MERGE_THRESHOLD = 0.88
REVIEW_THRESHOLD = 0.70

# Pesos por feature (aplicados so quando a evidencia existe dos dois lados).
_W_NAME = 0.60
_W_DATE = 0.20
_W_PLACE = 0.10
_W_DIST = 0.10

# Tokens que indicam sub-evento/publico distinto dentro do mesmo "guarda-chuva".
_MARKER_TOKENS = {"kids", "infantil", "caminhada", "walk", "virtual"}

_YEAR_RE = re.compile(r"\b(20\d{2})\b")


class Decision(str, Enum):
    MERGE = "merge"
    REVIEW = "review"
    DISTINCT = "distinct"


@dataclass(frozen=True)
class EventForMatch:
    """Projecao minima de um evento para o matching (independe de DB/Pydantic)."""

    id: int
    name: str
    start_at: datetime | None = None
    city: str | None = None
    state: str | None = None
    distances_km: frozenset[float] = frozenset()


@dataclass(frozen=True)
class MatchResult:
    score: float
    decision: Decision
    features: dict[str, float | None]
    guard: str | None = None  # motivo pelo qual o auto-merge foi vetado


def match(a: EventForMatch, b: EventForMatch) -> MatchResult:
    guard = _guard_reason(a, b)

    num = 0.0
    den = 0.0
    features: dict[str, float | None] = {}

    # -- Nome (sempre disponivel) -----------------------------------------
    name_a, name_b = normalize_name(a.name), normalize_name(b.name)
    # token_set e generoso com subconjuntos ("brooks run the bridge" ⊇ "run the
    # bridge"); token_sort pune reordenacoes/insercoes. A media equilibra.
    name_score = (
        fuzz.token_set_ratio(name_a, name_b) + fuzz.token_sort_ratio(name_a, name_b)
    ) / 200.0
    features["name"] = round(name_score, 3)
    num += name_score * _W_NAME
    den += _W_NAME

    # -- Data ---------------------------------------------------------------
    if a.start_at and b.start_at:
        delta = abs((a.start_at.date() - b.start_at.date()).days)
        if delta == 0:
            date_score = 1.0
        elif delta == 1:
            date_score = 0.6
        elif delta <= 3:
            date_score = 0.25
        else:
            # Datas distantes com ambos os lados conhecidos: eventos distintos.
            return MatchResult(0.0, Decision.DISTINCT, {**features, "date": 0.0})
        features["date"] = date_score
        num += date_score * _W_DATE
        den += _W_DATE

    # -- Local ----------------------------------------------------------------
    if a.state and b.state:
        if a.state != b.state:
            # UFs conflitantes: quase certamente eventos distintos.
            return MatchResult(0.0, Decision.DISTINCT, {**features, "place": 0.0})
        place_score = 1.0 if (a.city and b.city and _same_city(a.city, b.city)) else 0.5
        features["place"] = place_score
        num += place_score * _W_PLACE
        den += _W_PLACE

    # -- Distancias -----------------------------------------------------------
    if a.distances_km and b.distances_km:
        inter = len(a.distances_km & b.distances_km)
        union = len(a.distances_km | b.distances_km)
        dist_score = inter / union
        features["distances"] = round(dist_score, 3)
        num += dist_score * _W_DIST
        den += _W_DIST

    score = num / den if den else 0.0

    if score >= AUTO_MERGE_THRESHOLD and guard is None:
        decision = Decision.MERGE
    elif score >= REVIEW_THRESHOLD or (guard is not None and score >= REVIEW_THRESHOLD * 0.9):
        decision = Decision.REVIEW
    else:
        decision = Decision.DISTINCT

    return MatchResult(round(score, 4), decision, features, guard)


def _guard_reason(a: EventForMatch, b: EventForMatch) -> str | None:
    years_a = set(_YEAR_RE.findall(a.name))
    years_b = set(_YEAR_RE.findall(b.name))
    if years_a and years_b and years_a.isdisjoint(years_b):
        return f"anos diferentes no nome ({years_a} vs {years_b})"

    tokens_a = set(normalize_name(a.name).split()) & _MARKER_TOKENS
    tokens_b = set(normalize_name(b.name).split()) & _MARKER_TOKENS
    if tokens_a != tokens_b:
        return f"marcadores de sub-evento divergem ({tokens_a or '{}'} vs {tokens_b or '{}'})"

    return None


def _same_city(a: str, b: str) -> bool:
    return normalize_name(a) == normalize_name(b)
