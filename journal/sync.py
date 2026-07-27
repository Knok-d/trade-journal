"""Перенос сделок с Мака на сервер без потери разборов.

Разделение ролей после решения выносить Mini App на VPS:

* **Мак** — единственное место, где есть ключи биржи. Делает backfill и
  отдаёт наружу только данные о сделках.
* **VPS** — дом журнала. Заметки и намерения пишутся с телефона и живут
  только здесь; заливка с Мака их не трогает.

Почему слияние безопасно: `raw_executions` и `exchange_pnl` — append-only
таблицы с первичными ключами от биржи, поэтому `INSERT OR IGNORE`
идемпотентен. Производные (`round_trips`) пересобираются на месте, а
`trade_id` детерминирован — привязка заметок переживает пересборку.
"""

import sqlite3
import time
from pathlib import Path

from . import db, journal, reconcile, roundtrips

# Данные биржи: едут с Мака, на сервере только дополняются.
TRADE_TABLES = ("raw_executions", "exchange_pnl")
# Журнал: живёт на сервере, с Мака едет только при первичном заполнении.
JOURNAL_TABLES = ("notes", "intents")


def export(conn: sqlite3.Connection, path: Path, *, with_journal: bool = False) -> dict:
    """Складывает данные о сделках в отдельный файл для переноса."""
    path = Path(path)
    path.unlink(missing_ok=True)

    # Момент выгрузки едет вместе с данными: сервер сам к бирже не ходит и
    # иначе не знает, насколько он отстал.
    db.set_meta(conn, "synced_at", int(time.time() * 1000))

    tables = TRADE_TABLES + (JOURNAL_TABLES if with_journal else ()) + ("meta",)
    counts = {}
    conn.execute("ATTACH DATABASE ? AS out", (str(path),))
    try:
        for table in tables:
            columns = ", ".join(
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            )
            conn.execute(
                f"CREATE TABLE out.{table} AS SELECT {columns} FROM main.{table}"
            )
            counts[table] = conn.execute(
                f"SELECT COUNT(*) c FROM out.{table}"
            ).fetchone()["c"]
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE out")
    return counts


def merge(conn: sqlite3.Connection, path: Path) -> dict:
    """Вливает файл переноса в базу. Заметки и намерения не затираются.

    Возвращает, сколько строк реально добавилось: ноль по всем таблицам —
    признак, что залили то же самое ещё раз, а не что перенос сломался.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"нет файла переноса: {path}")

    added = {}
    conn.execute("ATTACH DATABASE ? AS src", (str(path),))
    try:
        present = {
            row["name"] for row in
            conn.execute("SELECT name FROM src.sqlite_master WHERE type='table'")
        }
        for table in TRADE_TABLES + JOURNAL_TABLES:
            if table not in present:
                continue
            before = conn.execute(f"SELECT COUNT(*) c FROM main.{table}").fetchone()["c"]
            columns = ", ".join(
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            )
            # OR IGNORE, а не OR REPLACE: заметка, написанная на сервере, важнее
            # той же строки из переноса — иначе разбор с телефона молча пропал бы.
            conn.execute(
                f"INSERT OR IGNORE INTO main.{table} ({columns})"
                f" SELECT {columns} FROM src.{table}"
            )
            after = conn.execute(f"SELECT COUNT(*) c FROM main.{table}").fetchone()["c"]
            added[table] = after - before

        # Отметка свежести, наоборот, всегда перезаписывается: она про то,
        # когда данные в последний раз брали с биржи.
        if "meta" in present:
            conn.execute(
                "INSERT INTO main.meta SELECT key, value FROM src.meta WHERE key='synced_at'"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        conn.commit()
    finally:
        conn.execute("DETACH DATABASE src")

    # Производные пересобираются здесь же: без этого сервер отдавал бы старые
    # сделки при свежих fills.
    stats = roundtrips.rebuild(conn)
    fees = reconcile.apply_exchange_fees(conn)
    matched = journal.match_intents(conn)
    return {
        "added": added,
        "round_trips": stats["round_trips"],
        "fees_from_exchange": fees["patched"],
        "intents_matched": matched["matched"],
        "orphan_funding": len(stats["orphan_funding"]),
    }
