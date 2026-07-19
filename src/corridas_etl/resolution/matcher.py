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

# O nome e a ancora de identidade; data/local apenas corroboram. Abaixo do
# piso, mesmo dia+cidade iguais nao tornam dois nomes diferentes o mesmo
# evento (calibrado com a triagem real: 59 pares "mesmo dia/cidade, nomes
# distintos" eram todos eventos diferentes). Auto-merge exige nome quase
# identico.
NAME_FLOOR = 0.70
AUTO_MERGE_NAME_MIN = 0.85

# Pesos por feature (aplicados so quando a evidencia existe dos dois lados).
_W_NAME = 0.60
_W_DATE = 0.20
_W_PLACE = 0.10
_W_DIST = 0.10

# Tokens que indicam sub-evento/publico distinto dentro do mesmo "guarda-chuva".
# Inclui diminutivos de prova kids ("Circuitinho das Estacoes", "Corridinha").
_MARKER_TOKENS = {"kids", "infantil", "caminhada", "walk", "virtual", "circuitinho", "corridinha", "maratoninha"}

_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Tokens de edicao que sao ruido para o matching de identidade: ordinais
# ("35a", "10o"), anos soltos (o guard de anos ja cuida de edicoes) e numerais
# romanos validos ("xxi"). NAO usar no canonical_key — so no score fuzzy.
_ORDINAL_TOKEN_RE = re.compile(r"^\d{1,3}[ao]?$")
_ROMAN_TOKEN_RE = re.compile(r"^m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$")


_YEAR_TOKEN_RE = re.compile(r"^20\d{2}$")


def _match_name(name: str, drop_tokens: frozenset[str] = frozenset()) -> str:
    """Nome normalizado para o score fuzzy, sem marcadores de edicao.

    Alem de ordinais/romanos, remove anos e os `drop_tokens` (tokens da cidade
    do evento): ambos ja sao evidencias pontuadas separadamente, e mante-los no
    nome infla a similaridade de eventos distintos na mesma praca — caso real
    'TECH RUN CURITIBA 2026' vs 'Eco Run - Curitiba 2026' (falso merge 0.94 em
    2026-07-19): o token distintivo (tech/eco) e minoria no nome.
    """
    tokens = normalize_name(name).split()
    kept = [
        t for t in tokens
        if t not in drop_tokens
        and not _ORDINAL_TOKEN_RE.match(t)
        and not _YEAR_TOKEN_RE.match(t)
        and not _ROMAN_TOKEN_RE.fullmatch(t)
    ]
    return " ".join(kept) if kept else " ".join(tokens)


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
    country: str = "BR"
    distances_km: frozenset[float] = frozenset()


@dataclass(frozen=True)
class MatchResult:
    score: float
    decision: Decision
    features: dict[str, float | None]
    guard: str | None = None  # motivo pelo qual o auto-merge foi vetado


def match(a: EventForMatch, b: EventForMatch) -> MatchResult:
    # Paises diferentes: eventos distintos, sem necessidade de pontuar.
    if a.country != b.country:
        return MatchResult(0.0, Decision.DISTINCT, {"country": 0.0})

    guard = _guard_reason(a, b)

    num = 0.0
    den = 0.0
    features: dict[str, float | None] = {}

    # -- Nome (sempre disponivel) -----------------------------------------
    # Quando a cidade e A MESMA dos dois lados, os tokens dela saem do nome:
    # sao redundantes com a evidencia de local e inflam a similaridade de
    # marcas diferentes na mesma praca. Quando as cidades DIFEREM, ficam —
    # sao justamente o que distingue "Meia de Jundiai" de "Meia de SBC".
    city_tokens: frozenset[str] = frozenset()
    if a.city and b.city and _same_city(a.city, b.city):
        city_tokens = frozenset(normalize_name(a.city).split())
    name_a = _match_name(a.name, city_tokens)
    name_b = _match_name(b.name, city_tokens)
    # token_set e generoso com subconjuntos ("brooks run the bridge" ⊇ "run the
    # bridge" — padrao de prefixo de patrocinador); token_sort pune insercoes.
    # Peso maior no set: os subconjuntos perigosos (kids/caminhada/virtual) ja
    # sao vetados pelos guards.
    name_score = (
        0.7 * fuzz.token_set_ratio(name_a, name_b)
        + 0.3 * fuzz.token_sort_ratio(name_a, name_b)
    ) / 100.0
    features["name"] = round(name_score, 3)
    if name_score < NAME_FLOOR:
        return MatchResult(0.0, Decision.DISTINCT, features, guard)
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
        if a.city and b.city:
            place_score = 1.0 if _same_city(a.city, b.city) else 0.0
        else:
            place_score = 0.5  # so a UF bate; evidencia fraca
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

    # Cidades conhecidas e DIFERENTES: penalidade multiplicativa. Triagem real
    # (2026-07-19, 39 pares) mostrou que "mesma UF, cidades distintas" e quase
    # sempre outro evento, mesmo com data e distancias iguais ("Meia de Jundiai"
    # vs "Meia de SBC"). Nao e DISTINCT absoluto porque cidades vizinhas podem
    # nomear o mesmo evento ("Dez Milhas Garoto": Vitoria vs Vila Velha) — com
    # nome quase identico o par ainda alcanca a fila de revisao.
    if features.get("place") == 0.0:
        score *= 0.85

    if score >= AUTO_MERGE_THRESHOLD and name_score >= AUTO_MERGE_NAME_MIN and guard is None:
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
