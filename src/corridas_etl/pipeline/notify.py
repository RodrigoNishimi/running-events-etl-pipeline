"""Feed de notificações de mudança de preço/status (Fase 3).

    python -m corridas_etl.pipeline.notify              # lista o feed pendente
    python -m corridas_etl.pipeline.notify --json        # feed como JSON (p/ o app)
    python -m corridas_etl.pipeline.notify --mark-sent    # marca como despachado

O ETL apenas DETECTA e REGISTRA mudanças (tabela event_change, alimentada pelo
trigger). A entrega ao usuário (e-mail/push) é responsabilidade do serviço de
notificação do app, que consome este feed — tipicamente via `--json`, e depois
`--mark-sent` para não reenviar. Este módulo não envia nada.

Cada mudança vira uma mensagem pronta: "Inscrições abriram para X",
"Y esgotou", "Preço de Z caiu de R$A para R$B".
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("corridas_etl.notify")

# Mensagem por transição de status (old -> new). Só as transições que valem
# uma notificação; as demais viram um texto genérico.
_STATUS_MESSAGES = {
    ("closed", "open"): "Inscrições abriram para {name}",
    ("coming_soon", "open"): "Inscrições abriram para {name}",
    ("unknown", "open"): "Inscrições abriram para {name}",
    ("sold_out", "open"): "Vagas liberadas: inscrições reabriram para {name}",
    ("open", "sold_out"): "{name} esgotou",
    ("open", "closed"): "Inscrições encerraram para {name}",
}


def _status_message(name: str, old: str, new: str) -> str:
    tpl = _STATUS_MESSAGES.get((old, new))
    if tpl:
        return tpl.format(name=name)
    return f"Status de inscrição de {name} mudou de {old} para {new}"


def _price_message(name: str, old: str, new: str) -> str:
    old_v, new_v = float(old), float(new)
    verbo = "caiu" if new_v < old_v else "subiu"
    return f"Preço de {name} {verbo} de R$ {old_v:.2f} para R$ {new_v:.2f}"


def build_feed(conn: psycopg.Connection, *, only_pending: bool = True) -> list[dict]:
    where = "WHERE c.notified_at IS NULL" if only_pending else ""
    rows = conn.execute(
        f"""
        SELECT c.id, c.event_id, e.name, e.slug, e.official_url,
               c.field, c.old_value, c.new_value, c.detected_at
        FROM event_change c JOIN event e ON e.id = c.event_id
        {where}
        ORDER BY c.detected_at, c.id
        """
    ).fetchall()

    feed = []
    for cid, event_id, name, slug, url, field, old, new, detected in rows:
        if field == "registration_status":
            kind, message = "status", _status_message(name, old, new)
        else:
            kind, message = "price", _price_message(name, old, new)
        feed.append(
            {
                "change_id": cid,
                "event_id": event_id,
                "event_name": name,
                "event_slug": slug,
                "official_url": url,
                "kind": kind,
                "field": field,
                "old_value": old,
                "new_value": new,
                "message": message,
                "detected_at": detected.isoformat(),
            }
        )
    return feed


def mark_sent(conn: psycopg.Connection, change_ids: list[int]) -> int:
    if not change_ids:
        return 0
    return conn.execute(
        "UPDATE event_change SET notified_at = now() WHERE id = ANY(%s) AND notified_at IS NULL",
        (change_ids,),
    ).rowcount


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Feed de notificações de mudança")
    parser.add_argument("--json", action="store_true", help="Saída JSON (p/ o serviço de notificação)")
    parser.add_argument("--all", action="store_true", help="Inclui as já despachadas")
    parser.add_argument("--mark-sent", action="store_true", help="Marca as pendentes como despachadas")
    args = parser.parse_args(argv)

    from ..db import connect

    with connect() as conn:
        feed = build_feed(conn, only_pending=not args.all)

        if args.json:
            print(json.dumps(feed, ensure_ascii=False, indent=2))
        else:
            if not feed:
                log.info("Nenhuma mudança pendente no feed.")
            for item in feed:
                icon = "🔔" if item["kind"] == "status" else "💰"
                log.info("%s %s", icon, item["message"])
            log.info("%d mudança(s) no feed", len(feed))

        if args.mark_sent:
            n = mark_sent(conn, [i["change_id"] for i in feed])
            log.info("%d mudança(s) marcadas como despachadas", n)

    return 0


if __name__ == "__main__":
    sys.exit(main())
