"""Перенос данных между Маком и сервером без потери разборов.

Разделение ролей:

* **Мак** — единственное место, где есть ключи биржи. Ходит на Bybit и
  отдаёт наружу посчитанные сделки.
* **VPS** — то же приложение для телефона: Mini App и бот.

Данные биржи едут в одну сторону, мак → сервер: `raw_executions` и
`exchange_pnl` — append-only таблицы с первичными ключами от биржи, поэтому
`INSERT OR IGNORE` идемпотентен. Производные (`round_trips`) пересобираются
на месте, а `trade_id` детерминирован — привязка заметок переживает пересборку.

**Журнал ездит в обе стороны.** Разбор пишется и на маке, и с телефона,
поэтому для журнальных таблиц побеждает более свежая запись по `updated_at`,
а не сторона. Отсюда требование ко всему журналу: ничего не удаляется
физически. Очищенный разбор — пустое тело, снятое нарушение — `broken=0`,
убранное правило — `active=0`. Удалённая строка вернулась бы с другой стороны
первым же кругом, и удаление выглядело бы как призрак.

Цена решения: last-write-wins опирается на согласованные часы мака и сервера.
Оба под NTP, а одну и ту же заметку не правят с двух устройств одновременно,
поэтому расхождение в секунды роли не играет.
"""

import sqlite3
import time
from pathlib import Path

from . import db, roundtrips

# Данные биржи: едут только с Мака, на сервере лишь дополняются.
TRADE_TABLES = ("raw_executions", "exchange_pnl")
# Снимки состояния биржи: заменяются целиком, а не дополняются. Закрытая
# позиция обязана исчезнуть — тумбстоны здесь были бы призраками с «текущим»
# P&L недельной давности. Правило «ничего не удаляем» — про журнал, который
# пишет человек, и на снимки не распространяется.
#
# Справочник инструментов здесь же, хотя и не сиюминутен: на биржу ходит только
# мак, поэтому его версия авторитетна целиком, а дописывание оставило бы на
# сервере устаревший класс актива после переклассификации.
#
# Сделки с MT5 здесь же, и по той же причине: вводят и правят их только на
# маке (команда `mt5-import`), поэтому его версия авторитетна целиком.
# Дописывание не донесло бы до сервера исправленную опечатку — а при ручном
# вводе исправление опечатки и есть основной способ правки.
SNAPSHOT_TABLES = ("open_positions", "symbols", "mt5_positions")
# Журнал: ездит в обе стороны, побеждает более свежая правка.
JOURNAL_TABLES = ("notes", "rules", "rule_violations", "reasons", "trade_reasons")
# Намерение правкам не подлежит по замыслу (иммутабельность — вся его ценность),
# поэтому едет отдельным правилом: добавляется по новому uid и не переписывается.
IMMUTABLE_JOURNAL_TABLES = ("intents",)
ALL_JOURNAL_TABLES = JOURNAL_TABLES + IMMUTABLE_JOURNAL_TABLES


def export(conn: sqlite3.Connection, path: Path, *, journal_only: bool = False) -> dict:
    """Складывает данные в отдельный файл для переноса.

    Журнал едет всегда: он синхронизируется в обе стороны, и отдельный флаг
    «взять ещё и заметки» был бы ровно тем местом, где его забывают — заметка
    с мака тихо не доезжала бы до телефона, и заметить это можно было бы только
    случайно.

    `journal_only` — обратный рейс с сервера на мак: везёт только журнал, без
    семи мегабайт сырых fills. Отметку свежести при этом не трогает и не
    отдаёт: она про поход на биржу, а сервер туда не ходит, и его штамп затёр
    бы честный маковский.
    """
    path = Path(path)
    path.unlink(missing_ok=True)

    if journal_only:
        tables = ALL_JOURNAL_TABLES
    else:
        # Момент выгрузки едет вместе с данными: сервер сам к бирже не ходит и
        # иначе не знает, насколько он отстал.
        db.set_meta(conn, "synced_at", int(time.time() * 1000))
        tables = TRADE_TABLES + SNAPSHOT_TABLES + ALL_JOURNAL_TABLES + ("meta",)
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
        conn.rollback()      # см. комментарий в merge: иначе ошибка маскируется
        conn.execute("DETACH DATABASE out")
    return counts


def _key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            if row["pk"]]


def merge(conn: sqlite3.Connection, path: Path) -> dict:
    """Вливает файл переноса в базу.

    Данные биржи добавляются, журнал сливается по «свежее — значит правее».
    Возвращает, сколько строк добавилось: ноль по всем таблицам — признак,
    что залили то же самое ещё раз, а не что перенос сломался. Обновления
    существующих строк в это число не входят.
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
            columns = [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]
            names = ", ".join(columns)

            if table in TRADE_TABLES:
                # Append-only с ключами от биржи: повтор просто игнорируется.
                conn.execute(f"INSERT OR IGNORE INTO main.{table} ({names})"
                             f" SELECT {names} FROM src.{table}")
            else:
                # Журнал: выигрывает более свежая правка, откуда бы она ни
                # пришла. Сторона значения не имеет — разбор пишется и на маке,
                # и с телефона, и «серверная всегда правее» потеряла бы маковскую.
                keys = _key_columns(conn, table)
                updates = ", ".join(f"{c} = excluded.{c}" for c in columns
                                    if c not in keys)
                # `WHERE true` обязателен: без него SQLite не понимает, где
                # кончается SELECT и начинается ON CONFLICT, и падает на «DO».
                conn.execute(
                    f"INSERT INTO main.{table} ({names})"
                    f" SELECT {names} FROM src.{table} WHERE true"
                    f" ON CONFLICT({', '.join(keys)}) DO UPDATE SET {updates}"
                    f" WHERE excluded.updated_at > main.{table}.updated_at"
                )
            after = conn.execute(f"SELECT COUNT(*) c FROM main.{table}").fetchone()["c"]
            added[table] = after - before

        # Намерения: только новые. Правки у них нет по замыслу, а intent_id —
        # автоинкремент, свой на каждой машине, поэтому сверка идёт по uid.
        if "intents" in present:
            before = conn.execute("SELECT COUNT(*) c FROM main.intents").fetchone()["c"]
            carried = [row["name"] for row in conn.execute("PRAGMA table_info(intents)")
                       if row["name"] not in ("intent_id", "matched_trade_id",
                                              "match_note")]
            names = ", ".join(carried)
            conn.execute(
                f"INSERT INTO main.intents ({names}) SELECT {names} FROM src.intents"
                " WHERE uid IS NOT NULL AND uid NOT IN"
                " (SELECT uid FROM main.intents WHERE uid IS NOT NULL)"
            )
            after = conn.execute("SELECT COUNT(*) c FROM main.intents").fetchone()["c"]
            added["intents"] = after - before

        # Снимок открытых позиций заменяется целиком: позиция, которой в
        # переносе нет, закрыта, и оставлять её на сервере значило бы врать
        # текущим P&L. Первая таблица в проекте, где DELETE — это правильно.
        for table in SNAPSHOT_TABLES:
            if table not in present:
                continue
            columns = ", ".join(
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            )
            conn.execute(f"DELETE FROM main.{table}")
            conn.execute(f"INSERT INTO main.{table} ({columns})"
                         f" SELECT {columns} FROM src.{table}")

        # Отметки свежести всегда перезаписываются: они про то, когда данные
        # в последний раз брали с биржи, а ходит туда только мак.
        if "meta" in present:
            conn.execute(
                "INSERT INTO main.meta SELECT key, value FROM src.meta"
                " WHERE key IN ('synced_at', 'positions_at')"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            )
        conn.commit()
    finally:
        # Откат перед отсоединением: иначе любая ошибка выше превращается в
        # «database src is locked» на DETACH и прячет собственную причину.
        conn.rollback()
        conn.execute("DETACH DATABASE src")

    # Производные пересобираются здесь же: без этого сервер отдавал бы старые
    # сделки при свежих fills. Целиком, а не одной склейкой — она стирает
    # комиссию в MNT и плечо (см. roundtrips.rebuild_all).
    stats = roundtrips.rebuild_all(conn)
    return {
        "added": added,
        "round_trips": stats["round_trips"],
        "fees_from_exchange": stats["fees"]["patched"],
        "intents_matched": stats["intents"]["matched"],
        "orphan_funding": len(stats["orphan_funding"]),
    }
