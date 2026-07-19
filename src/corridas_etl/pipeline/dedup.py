"""Etapa de entity resolution (Fase 1): dedup de eventos canonicos no banco.

Roda APOS a carga dos conectores:
    python -m corridas_etl.pipeline.dedup            # aplica merges + fila revisao
    python -m corridas_etl.pipeline.dedup --dry-run  # so mostra o que faria
    python -m corridas_etl.pipeline.dedup --review   # lista pares pendentes
    python -m corridas_etl.pipeline.dedup --resolve <id> merge|distinct

Fluxo:
  1. Carrega os eventos do banco.
  2. Blocking barato: so compara pares com sobreposicao de tokens no nome.
  3. `resolution.matcher.match` pontua cada par (nome, data, local, distancias).
  4. MERGE      -> mescla automaticamente (sobrevive o mais completo).
     REVIEW     -> insere na fila `dedup_review` para decisao manual.
     DISTINCT   -> ignora.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import sys

import psycopg

from ..resolution.matcher import Decision, EventForMatch, match
from ..utils.text import normalize_name

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("corridas_etl.dedup")


# -- Carregamento -------------------------------------------------------------

def load_events(conn: psycopg.Connection) -> list[EventForMatch]:
    rows = conn.execute(
        """
        SELECT e.id, e.name, e.start_at, e.city, e.state, e.country,
               COALESCE(array_agg(d.distance_km) FILTER (WHERE d.distance_km IS NOT NULL), '{}')
        FROM event e
        LEFT JOIN event_distance d ON d.event_id = e.id
        GROUP BY e.id
        """
    ).fetchall()
    return [
        EventForMatch(
            id=r[0], name=r[1], start_at=r[2], city=r[3], state=r[4], country=r[5],
            distances_km=frozenset(float(km) for km in r[6]),
        )
        for r in rows
    ]


# -- Blocking + matching ------------------------------------------------------

def find_pairs(events: list[EventForMatch]):
    """Gera (a, b, MatchResult) para pares candidatos (com blocking por token)."""
    tokens = {e.id: set(normalize_name(e.name).split()) for e in events}
    for a, b in itertools.combinations(events, 2):
        # Blocking: sem nenhum token de nome em comum, nem vale pontuar.
        if not (tokens[a.id] & tokens[b.id]):
            continue
        result = match(a, b)
        if result.decision != Decision.DISTINCT:
            yield a, b, result


# -- Merge --------------------------------------------------------------------

def _completeness(conn: psycopg.Connection, event_id: int) -> int:
    row = conn.execute(
        """
        SELECT (start_at IS NOT NULL)::int + (city IS NOT NULL)::int
             + (state IS NOT NULL)::int + (description IS NOT NULL)::int
             + (image_url IS NOT NULL)::int + (latitude IS NOT NULL)::int
        FROM event WHERE id = %s
        """,
        (event_id,),
    ).fetchone()
    return row[0] if row else 0


def merge_events(conn: psycopg.Connection, survivor_id: int, absorbed_id: int) -> None:
    """Mescla `absorbed` em `survivor`: repointa fontes/distancias, completa
    campos nulos do sobrevivente, registra o alias e apaga o absorvido.

    O alias garante que a proxima carga da fonte do absorvido atualize o
    sobrevivente em vez de recriar o evento (ver db.upsert_event)."""
    with conn.cursor() as cur:
        # Aliases que apontavam para o absorvido migram para o sobrevivente,
        # e a chave do absorvido vira alias tambem (antes do DELETE, senao o
        # ON DELETE CASCADE os removeria).
        cur.execute(
            "UPDATE event_alias SET event_id = %s WHERE event_id = %s",
            (survivor_id, absorbed_id),
        )
        cur.execute(
            """
            INSERT INTO event_alias (canonical_key, event_id)
            SELECT canonical_key, %s FROM event WHERE id = %s
            ON CONFLICT (canonical_key) DO UPDATE SET event_id = EXCLUDED.event_id
            """,
            (survivor_id, absorbed_id),
        )
        # Completa campos que o sobrevivente nao tem com os do absorvido.
        cur.execute(
            """
            UPDATE event s SET
                description = COALESCE(s.description, a.description),
                start_at    = COALESCE(s.start_at, a.start_at),
                city        = COALESCE(s.city, a.city),
                state       = COALESCE(s.state, a.state),
                address     = COALESCE(s.address, a.address),
                latitude    = COALESCE(s.latitude, a.latitude),
                longitude   = COALESCE(s.longitude, a.longitude),
                image_url   = COALESCE(s.image_url, a.image_url),
                official_url= COALESCE(s.official_url, a.official_url),
                updated_at  = now()
            FROM event a
            WHERE s.id = %s AND a.id = %s
            """,
            (survivor_id, absorbed_id),
        )
        cur.execute(
            "UPDATE source_record SET event_id = %s WHERE event_id = %s",
            (survivor_id, absorbed_id),
        )
        cur.execute(
            """
            INSERT INTO event_distance (event_id, label, distance_km)
            SELECT %s, label, distance_km FROM event_distance WHERE event_id = %s
            ON CONFLICT (event_id, label) DO NOTHING
            """,
            (survivor_id, absorbed_id),
        )
        cur.execute("DELETE FROM event_distance WHERE event_id = %s", (absorbed_id,))
        cur.execute("DELETE FROM event WHERE id = %s", (absorbed_id,))


# -- Orquestracao -------------------------------------------------------------

def run_dedup(conn: psycopg.Connection, *, dry_run: bool = False) -> tuple[int, int]:
    events = load_events(conn)
    log.info("%d eventos carregados para dedup", len(events))

    # Pares ja decididos (auto ou manualmente) nao voltam para a fila.
    decided: set[tuple[int, int]] = {
        (r[0], r[1])
        for r in conn.execute("SELECT event_id_a, event_id_b FROM dedup_review").fetchall()
    }

    merged = queued = 0
    absorbed_ids: set[int] = set()

    for a, b, result in find_pairs(events):
        # Um evento ja absorvido nesta execucao nao participa de novos pares.
        if a.id in absorbed_ids or b.id in absorbed_ids:
            continue
        if (min(a.id, b.id), max(a.id, b.id)) in decided:
            continue

        if result.decision == Decision.MERGE:
            survivor, absorbed = _pick_survivor(conn, a, b)
            log.info(
                "MERGE  (%.2f) [%d]'%s'  <-  [%d]'%s'",
                result.score, survivor.id, survivor.name, absorbed.id, absorbed.name,
            )
            if not dry_run:
                merge_events(conn, survivor.id, absorbed.id)
                _record_decision(conn, survivor, absorbed, result, "merged", "auto")
            absorbed_ids.add(absorbed.id)
            merged += 1
        else:  # REVIEW
            log.info(
                "REVIEW (%.2f) [%d]'%s'  ~  [%d]'%s'%s",
                result.score, a.id, a.name, b.id, b.name,
                f"  (guard: {result.guard})" if result.guard else "",
            )
            if not dry_run:
                _record_decision(conn, a, b, result, "pending", None)
            queued += 1

    log.info("dedup: %d merges automaticos, %d pares na fila de revisao", merged, queued)
    return merged, queued


def _pick_survivor(conn, a: EventForMatch, b: EventForMatch):
    # Sobrevive o registro mais completo; empate -> menor id (mais antigo).
    ca, cb = _completeness(conn, a.id), _completeness(conn, b.id)
    return (a, b) if (ca, -a.id) >= (cb, -b.id) else (b, a)


def _record_decision(conn, a, b, result, status: str, decided_by: str | None) -> None:
    lo, hi = sorted((a, b), key=lambda e: e.id)
    conn.execute(
        """
        INSERT INTO dedup_review
            (event_id_a, event_id_b, event_name_a, event_name_b,
             score, features, guard, status, decided_by, resolved_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                CASE WHEN %s = 'pending' THEN NULL ELSE now() END)
        ON CONFLICT (event_id_a, event_id_b) DO NOTHING
        """,
        (lo.id, hi.id, lo.name, hi.name, result.score,
         json.dumps(result.features), result.guard, status, decided_by, status),
    )


# -- Revisao manual -----------------------------------------------------------

def list_pending(conn) -> None:
    rows = conn.execute(
        """SELECT id, score, event_name_a, event_name_b, guard
           FROM dedup_review WHERE status = 'pending' ORDER BY score DESC"""
    ).fetchall()
    if not rows:
        print("Fila de revisao vazia.")
        return
    for r in rows:
        guard = f"  [guard: {r[4]}]" if r[4] else ""
        print(f"  #{r[0]}  score={r[1]}  '{r[2]}'  ~  '{r[3]}'{guard}")


def resolve_pair(conn, review_id: int, verdict: str) -> None:
    row = conn.execute(
        "SELECT event_id_a, event_id_b FROM dedup_review WHERE id = %s AND status = 'pending'",
        (review_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"Revisao #{review_id} nao encontrada ou ja resolvida.")
    id_a, id_b = row

    if verdict == "merge":
        a = EventForMatch(id=id_a, name="")
        b = EventForMatch(id=id_b, name="")
        survivor, absorbed = _pick_survivor(conn, a, b)
        merge_events(conn, survivor.id, absorbed.id)

    conn.execute(
        """UPDATE dedup_review
           SET status = %s, decided_by = 'manual', resolved_at = now() WHERE id = %s""",
        ("merged" if verdict == "merge" else "distinct", review_id),
    )
    print(f"Revisao #{review_id}: {verdict} aplicado.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dedup de eventos (Fase 1)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--review", action="store_true", help="Lista pares pendentes")
    parser.add_argument(
        "--resolve", nargs=2, metavar=("ID", "VEREDITO"),
        help="Resolve um par pendente: --resolve 3 merge|distinct",
    )
    args = parser.parse_args(argv)

    from ..db import connect

    with connect() as conn:
        if args.review:
            list_pending(conn)
        elif args.resolve:
            review_id, verdict = args.resolve
            if verdict not in ("merge", "distinct"):
                raise SystemExit("veredito deve ser 'merge' ou 'distinct'")
            resolve_pair(conn, int(review_id), verdict)
        else:
            run_dedup(conn, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
