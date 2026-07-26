"""Хранилище. SQLite, суммы — Decimal через TEXT.

Деньги во float считать нельзя: расхождение накопится ровно там, где мы его
измеряем (ворота сверки — 1% посделочно), и будет неотличимо от ошибки склейки.

Слой один: и UI, и будущий Telegram-бот читают отсюда. Логика расчётов живёт
в roundtrips.py, а не в потребителях, иначе их цифры разойдутся между собой.
"""

import sqlite3
from decimal import Decimal
from pathlib import Path

DB_PATH = Path.home() / ".trade-journal" / "journal.db"

SCHEMA = """
-- Сырые fills. Append-only: не редактируются и не удаляются никогда.
-- Всё производное пересчитывается отсюда, поэтому исправление определения
-- сделки не требует повторной выкачки истории.
CREATE TABLE IF NOT EXISTS raw_executions (
    exec_id      TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    position_idx INTEGER NOT NULL,
    side         TEXT NOT NULL,
    exec_type    TEXT NOT NULL,
    exec_price   TEXT NOT NULL,
    exec_qty     TEXT NOT NULL,
    exec_fee     TEXT NOT NULL,
    fee_currency TEXT,
    closed_size  TEXT,
    exec_time    INTEGER NOT NULL,
    -- Порядок исполнений внутри одной миллисекунды. Критично: 80% fills делят
    -- exec_time с соседями, а склейка — проход с состоянием. Сортировка по
    -- exec_id (UUID) давала случайный порядок и ломала разбиение на сделки.
    seq          INTEGER,
    order_id     TEXT,
    raw          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exec_group
    ON raw_executions (category, symbol, position_idx, exec_time, seq);

-- Расчёт закрытых сделок самой биржей. Append-only, как и fills.
-- Две роли: (1) независимый арбитр для сверки, (2) единственный источник
-- USDT-эквивалента комиссии, когда она списана в чужой валюте (MNT) — курса
-- в fills физически нет, восстановить его нам не из чего.
CREATE TABLE IF NOT EXISTS exchange_pnl (
    order_id     TEXT NOT NULL,
    updated_time INTEGER NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT,
    qty          TEXT,
    closed_size  TEXT,
    avg_entry    TEXT,
    avg_exit     TEXT,
    closed_pnl   TEXT NOT NULL,
    open_fee     TEXT NOT NULL,
    close_fee    TEXT NOT NULL,
    raw          TEXT NOT NULL,
    PRIMARY KEY (order_id, updated_time)
);
CREATE INDEX IF NOT EXISTS idx_pnl_lookup ON exchange_pnl (symbol, updated_time);

-- Производная таблица: пересобирается целиком из raw_executions.
CREATE TABLE IF NOT EXISTS round_trips (
    trade_id     TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    position_idx INTEGER NOT NULL,
    direction    TEXT NOT NULL,          -- long | short
    opened_at    INTEGER NOT NULL,
    closed_at    INTEGER,                -- NULL = позиция ещё открыта
    qty          TEXT NOT NULL,
    avg_entry    TEXT NOT NULL,
    avg_exit     TEXT,
    gross_pnl    TEXT,
    fees         TEXT NOT NULL,
    funding      TEXT NOT NULL,
    net_pnl      TEXT,
    liquidated   INTEGER NOT NULL DEFAULT 0,
    fee_mixed    INTEGER NOT NULL DEFAULT 0,  -- комиссия списана не в USDT (напр. в MNT)
    -- 'fills' — комиссия посчитана из сырых исполнений (обычный случай);
    -- 'exchange' — взята готовым USDT-эквивалентом из exchange_pnl, потому что
    -- в fills она в чужой валюте. Для таких сделок сверка проверяет склейку и
    -- gross, но не комиссию — это надо видеть, а не прятать.
    fees_source  TEXT NOT NULL DEFAULT 'fills'
);
CREATE INDEX IF NOT EXISTS idx_rt_closed ON round_trips (closed_at);

-- Связь сделка -> закрывающие ордера. Пересобирается вместе с round_trips.
-- Нужна для ТОЧНОГО сопоставления с closed-pnl: сопоставление по временным
-- окнам ломается на перевороте позиции, где предыдущая сделка закрывается за
-- миллисекунды до открытия следующей и её P&L приписывается не туда.
CREATE TABLE IF NOT EXISTS trade_close_orders (
    trade_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    PRIMARY KEY (trade_id, order_id)
);
CREATE INDEX IF NOT EXISTS idx_close_order ON trade_close_orders (order_id);

-- PRE-TRADE: намерение, записанное ДО входа.
--
-- Отдельная сущность, а не поле сделки: сделки в момент записи ещё не существует
-- (trade_id рождается из первого fill). Иммутабельность обеспечена конструкцией -
-- запись физически создана раньше сделки, и это доказывает created_at, а не
-- запрет на UPDATE. Правка после входа невозможна, потому что смысла в ней нет:
-- привязка уже состоялась, а исходный таймстамп остаётся.
CREATE TABLE IF NOT EXISTS intents (
    intent_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol       TEXT NOT NULL,
    direction    TEXT NOT NULL,          -- long | short
    thesis       TEXT NOT NULL,          -- почему вхожу
    entry_signal TEXT,                   -- что стало триггером
    planned_stop TEXT,                   -- цена стопа. Без неё R не считается никогда
    planned_exit TEXT,
    tags         TEXT,
    created_at   INTEGER NOT NULL,
    matched_trade_id TEXT,               -- проставляется импортом
    match_note   TEXT                    -- чем привязка подозрительна, если подозрительна
);
CREATE INDEX IF NOT EXISTS idx_intent_open
    ON intents (symbol, direction, created_at, matched_trade_id);

-- POST-TRADE: разбор после закрытия. Правится свободно - это и есть его роль.
CREATE TABLE IF NOT EXISTS notes (
    trade_id     TEXT PRIMARY KEY,
    body         TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);

-- Отметки о выкачанных периодах, чтобы бэкфилл не начинался каждый раз с нуля.
CREATE TABLE IF NOT EXISTS sync_state (
    category   TEXT PRIMARY KEY,
    synced_from INTEGER,
    synced_to   INTEGER
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Досоздаёт колонки, появившиеся после первых выкачек.

    Сырые fills хранятся целиком в `raw`, поэтому новое поле берётся оттуда —
    повторно ходить на биржу не нужно. Ради этого append-only и заводился.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(raw_executions)")}
    if "seq" not in columns:
        conn.execute("ALTER TABLE raw_executions ADD COLUMN seq INTEGER")
        conn.execute(
            "UPDATE raw_executions SET seq = CAST(json_extract(raw, '$.seq') AS INTEGER)"
        )
        conn.commit()

    rt_columns = {r["name"] for r in conn.execute("PRAGMA table_info(round_trips)")}
    if "fees_source" not in rt_columns:
        conn.execute(
            "ALTER TABLE round_trips ADD COLUMN fees_source TEXT NOT NULL DEFAULT 'fills'"
        )
        conn.commit()


def save_exchange_pnl(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Пишет записи closed-pnl биржи идемпотентно."""
    import json

    payload = [
        (
            row["orderId"], int(row["updatedTime"]), row["symbol"], row.get("side"),
            row.get("qty"), row.get("closedSize"), row.get("avgEntryPrice"),
            row.get("avgExitPrice"), row["closedPnl"],
            row.get("openFee") or "0", row.get("closeFee") or "0",
            json.dumps(row, ensure_ascii=False),
        )
        for row in rows
    ]
    cursor = conn.executemany(
        "INSERT OR IGNORE INTO exchange_pnl VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", payload
    )
    conn.commit()
    return cursor.rowcount


def dec(value) -> Decimal:
    """TEXT/число -> Decimal. Пустое и None -> 0."""
    if value is None or value == "":
        return Decimal(0)
    return Decimal(str(value))


def save_executions(conn: sqlite3.Connection, rows: list[dict]) -> int:
    """Пишет fills идемпотентно: повторный бэкфилл того же периода ничего не портит."""
    import json

    payload = [
        (
            row["execId"],
            row.get("_category", "linear"),
            row["symbol"],
            int(row.get("positionIdx") or 0),
            row["side"],
            row["execType"],
            row["execPrice"],
            row["execQty"],
            row.get("execFee") or "0",
            row.get("feeCurrency"),
            row.get("closedSize"),
            int(row["execTime"]),
            int(row["seq"]) if row.get("seq") is not None else None,
            row.get("orderId"),
            json.dumps(row, ensure_ascii=False),
        )
        for row in rows
    ]
    cursor = conn.executemany(
        "INSERT OR IGNORE INTO raw_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        payload,
    )
    conn.commit()
    return cursor.rowcount
