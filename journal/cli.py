"""CLI шага 1: проверить ключ -> выкачать fills -> собрать сделки -> сверить.

Запуск: python3 -m journal.cli <команда>
"""

import argparse
import sys
import time

from . import bybit, db, journal, reconcile, report, roundtrips
from .keychain import KeychainError

DAY_MS = 24 * 60 * 60 * 1000


def _period(days: int) -> tuple[int, int]:
    now = int(time.time() * 1000)
    return now - days * DAY_MS, now


def cmd_check_key(_args) -> int:
    client = bybit.Bybit()
    info = client.assert_read_only()
    print("Ключ пригоден: readOnly=1, записывать не может.")
    print(f"  id: {info.get('id')}  создан: {info.get('createdAt')}")
    for warning in client.key_warnings(info):
        print(f"  ! {warning}")
    return 0


def cmd_backfill(args) -> int:
    client = bybit.Bybit()
    client.assert_read_only()      # каждый запуск, а не только при добавлении ключа

    start, end = _period(args.days)
    conn = db.connect()
    total_new = 0
    batch: list[dict] = []

    for fill in client.executions(start, end, category=args.category):
        fill["_category"] = args.category
        batch.append(fill)
        if len(batch) >= 500:
            total_new += db.save_executions(conn, batch)
            print(f"  ...{total_new} новых fills", end="\r", flush=True)
            batch = []
    if batch:
        total_new += db.save_executions(conn, batch)

    print(f"Выкачано за {args.days} дн.: новых fills {total_new}")

    # closed-pnl биржи тянется тем же проходом: он и арбитр для сверки, и
    # единственный источник USDT-комиссии там, где она списана в MNT.
    pnl_rows = list(client.closed_pnl(start, end, category=args.category))
    new_pnl = db.save_exchange_pnl(conn, pnl_rows)
    print(f"Записей closed-pnl биржи: {len(pnl_rows)} (новых {new_pnl})")
    return 0


def cmd_rebuild(_args) -> int:
    conn = db.connect()
    stats = roundtrips.rebuild(conn)
    print(
        f"fills: {stats['fills']} -> сделок: {stats['round_trips']}"
        f" (закрытых {stats['closed']}, открытых {stats['open']})"
    )
    if stats["orphan_funding"]:
        print(
            f"ВНИМАНИЕ: фандинг без открытой позиции — {len(stats['orphan_funding'])} шт."
            "\n  Это значит, что склейка потеряла позицию. Разобрать до сверки."
        )

    fees = reconcile.apply_exchange_fees(conn)
    if fees["patched"]:
        print(f"комиссия взята у биржи (списана не в USDT): {fees['patched']} сделок")
    if fees["unresolved"]:
        print(
            f"  не нашлось записи closed-pnl для {len(fees['unresolved'])} таких сделок"
            " — их P&L занижен на величину комиссии"
        )

    matching = journal.match_intents(conn)
    print(f"намерений привязано: {matching['matched']}, ждут сделку: {matching['pending']}")
    if matching["ambiguous"]:
        print(
            f"  спорных привязок: {len(matching['ambiguous'])}"
            " — несколько входов подряд по одному символу, проверить вручную"
        )
    return 0


def cmd_intent(args) -> int:
    """Записать обоснование ДО входа. В этом весь смысл: потом будет поздно."""
    conn = db.connect()
    intent_id = journal.add_intent(
        conn, args.symbol, args.direction, args.thesis,
        entry_signal=args.signal, planned_stop=args.stop,
        planned_exit=args.target, tags=args.tags,
    )
    print(f"Намерение #{intent_id} записано: {args.symbol.upper()} {args.direction}")
    if not args.stop:
        print("  без планового стопа — R по этой сделке считаться не будет")
    return 0


def cmd_note(args) -> int:
    conn = db.connect()
    journal.add_note(conn, args.trade_id, args.text)
    print(f"Разбор сохранён: {args.trade_id}")
    return 0


def cmd_coverage(args) -> int:
    conn = db.connect()
    since = 0 if not args.days else int(time.time() * 1000) - args.days * DAY_MS
    stats = journal.coverage(conn, since)

    if not stats["trades"]:
        print("Сделок за период нет.")
        return 0

    print(f"Сделок: {stats['trades']}")
    print(f"Разобрано (заметка или намерение): {stats['annotated']} ({stats['share']:.0%})")
    print(f"  из них с намерением до входа: {stats['with_intent']}")
    print(f"  с плановым стопом: {stats['with_planned_stop']}")
    print(f"Без разбора: {stats['missing']}")
    return 0


def cmd_stats(args) -> int:
    conn = db.connect()
    print(report.render(conn, args.days))
    return 0


def cmd_serve(args) -> int:
    from . import server
    server.serve(port=args.port)
    return 0


def cmd_pending(args) -> int:
    conn = db.connect()
    rows = journal.unjournaled(conn, limit=args.limit)
    if not rows:
        print("Все закрытые сделки имеют обоснование.")
        return 0
    print("Закрытые сделки без обоснования (самые свежие):")
    for row in rows:
        pnl = f"{db.dec(row['net_pnl']):+.2f}" if row["net_pnl"] else "?"
        print(f"  {row['symbol']:<12} {row['direction']:<6} P&L {pnl:>10}"
              f"  {row['trade_id']}")
    return 0


def cmd_reconcile(args) -> int:
    """Сверка идёт по локальным данным — сеть не нужна, обе стороны уже выкачаны."""
    conn = db.connect()
    have = conn.execute("SELECT COUNT(*) c FROM exchange_pnl").fetchone()["c"]
    if not have:
        print("Нет записей closed-pnl. Сначала: backfill, затем rebuild.", file=sys.stderr)
        return 5

    result = reconcile.reconcile(conn)
    print(reconcile.format_report(result))
    return 0 if result["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="journal")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check-key", help="проверить, что ключ только на чтение")

    for name, handler, help_text in (
        ("backfill", cmd_backfill, "выкачать fills за период"),
        ("reconcile", cmd_reconcile, "сверить наши сделки с closed-pnl биржи"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--days", type=int, default=90)
        p.add_argument("--category", default="linear")
        p.set_defaults(func=handler)

    sub.add_parser("rebuild", help="пересобрать сделки и привязать намерения")

    intent = sub.add_parser("intent", help="записать обоснование ДО входа")
    intent.add_argument("symbol")
    intent.add_argument("direction", choices=["long", "short"])
    intent.add_argument("thesis", help="почему вхожу")
    intent.add_argument("--signal", help="что стало триггером")
    intent.add_argument("--stop", help="плановая цена стопа (без неё R не считается)")
    intent.add_argument("--target", help="план выхода")
    intent.add_argument("--tags", help="теги через запятую")
    intent.set_defaults(func=cmd_intent)

    note = sub.add_parser("note", help="разбор после закрытия")
    note.add_argument("trade_id")
    note.add_argument("text")
    note.set_defaults(func=cmd_note)

    st = sub.add_parser("stats", help="статистика по сверенным сделкам")
    st.add_argument("--days", type=int, default=0, help="0 = за всю историю")
    st.set_defaults(func=cmd_stats)

    srv = sub.add_parser("serve", help="локальный веб-интерфейс (127.0.0.1)")
    srv.add_argument("--port", type=int, default=8321)
    srv.set_defaults(func=cmd_serve)

    cov = sub.add_parser("coverage", help="сколько сделок без обоснования")
    cov.add_argument("--days", type=int, default=0, help="0 = за всё время")
    cov.set_defaults(func=cmd_coverage)

    pending = sub.add_parser("pending", help="закрытые сделки без обоснования")
    pending.add_argument("--limit", type=int, default=20)
    pending.set_defaults(func=cmd_pending)

    args = parser.parse_args()
    handler = {
        "check-key": cmd_check_key,
        "rebuild": cmd_rebuild,
    }.get(args.command) or args.func

    try:
        return handler(args)
    except KeychainError as exc:
        print(exc, file=sys.stderr)
        return 2
    except bybit.UnsafeKeyError as exc:
        print(f"\nОТКАЗ: {exc}", file=sys.stderr)
        return 3
    except bybit.BybitError as exc:
        print(f"Ошибка API: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
