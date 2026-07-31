"""Хранилище. SQLite, суммы — Decimal через TEXT.

Деньги во float считать нельзя: расхождение накопится ровно там, где мы его
измеряем (ворота сверки — 1% посделочно), и будет неотличимо от ошибки склейки.

Слой один: и UI, и будущий Telegram-бот читают отсюда. Логика расчётов живёт
в roundtrips.py, а не в потребителях, иначе их цифры разойдутся между собой.
"""

import os
import sqlite3
import time
from decimal import Decimal
from pathlib import Path

# Путь задаётся явно, а не только через $HOME: на сервере HOME берётся из passwd
# и указывает в /opt, который у юнита смонтирован только на чтение, а `sudo -u`
# при импорте оставляет HOME=/root — база уезжала бы мимо сервиса.
DB_PATH = Path(os.environ.get("TRADE_JOURNAL_DB")
               or Path.home() / ".trade-journal" / "journal.db")

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
-- Индекс по seq создаётся в _migrate, а не здесь: SCHEMA выполняется до
-- миграций, и на базе, заведённой раньше этой колонки, он падал бы с
-- «no such column: seq» при каждом подключении.

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
    leverage     TEXT,                   -- плечо позиции, как его видит биржа
    entry_value  TEXT,                   -- cumEntryValue: объём входа в USDT
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
    fees_source  TEXT NOT NULL DEFAULT 'fills',
    -- Откуда взялась сама сделка: 'fills' — склеена нами из исполнений Bybit,
    -- 'mt5' — принесена из выгрузки MT5 уже посчитанной. Пересборка стирает
    -- только первые: вторые собирать не из чего, кроме mt5_positions, и
    -- удалять их вместе с остальными значило бы терять их каждую минуту.
    source       TEXT NOT NULL DEFAULT 'fills',
    -- Плечо и объём входа приходят от биржи (closed-pnl), а не считаются нами:
    -- из fills плечо не выводится вовсе, это настройка позиции. Обе колонки
    -- пустые там, где сделка с closed-pnl не сопоставилась, — и тогда PnL%
    -- честно недоступен, а не равен нулю.
    leverage     TEXT,
    entry_value  TEXT
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

-- Какое сообщение бота про какую сделку. Нужно, чтобы разбор с телефона писался
-- ответом на сообщение, а не набором trade_id руками: на мобильном это разница
-- между «сделаю» и «не буду».
CREATE TABLE IF NOT EXISTS bot_messages (
    message_id INTEGER PRIMARY KEY,
    trade_id   TEXT NOT NULL,
    sent_at    INTEGER NOT NULL
);

-- PRE-TRADE: намерение, записанное ДО входа.
--
-- Отдельная сущность, а не поле сделки: сделки в момент записи ещё не существует
-- (trade_id рождается из первого fill). Иммутабельность обеспечена конструкцией -
-- запись физически создана раньше сделки, и это доказывает created_at, а не
-- запрет на UPDATE. Правка после входа невозможна, потому что смысла в ней нет:
-- привязка уже состоялась, а исходный таймстамп остаётся.
CREATE TABLE IF NOT EXISTS intents (
    intent_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    -- Опознаётся при синхронизации по uid, а не по intent_id: автоинкремент
    -- свой на каждой машине, и намерения с мака и с сервера столкнулись бы.
    uid          TEXT,
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
-- Уникальный индекс по uid создаётся в _migrate, а не здесь: SCHEMA
-- выполняется до миграций, и на базе, заведённой раньше этой колонки,
-- индекс падал бы с «no such column: uid» при каждом подключении.

-- POST-TRADE: разбор после закрытия. Правится свободно - это и есть его роль.
CREATE TABLE IF NOT EXISTS notes (
    trade_id     TEXT PRIMARY KEY,
    body         TEXT NOT NULL,
    updated_at   INTEGER NOT NULL
);

-- Правила торговли: пишутся руками и живут дольше отдельных сделок.
--
-- rule_id случайный, а не автоинкремент: правила заводятся и на маке, и с
-- телефона, а журнал синхронизируется в обе стороны. Два разных правила,
-- получивших id=1 на разных машинах, слияние съело бы молча. Та же причина,
-- по которой trade_id сделан детерминированным.
CREATE TABLE IF NOT EXISTS rules (
    rule_id    TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,   -- 0 = в архиве, строка остаётся
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

-- Какие правила нарушены в сделке. Отмечается при разборе, постфактум -
-- как и сам разбор (решение C).
--
-- Снятая галочка остаётся строкой с broken=0. Настоящее удаление не пережило
-- бы двусторонний синк: строка вернулась бы с другой стороны первым же кругом,
-- и снятое нарушение воскресло бы. По той же причине архивируются правила.
CREATE TABLE IF NOT EXISTS rule_violations (
    trade_id   TEXT NOT NULL,
    rule_id    TEXT NOT NULL,
    broken     INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (trade_id, rule_id)
);
CREATE INDEX IF NOT EXISTS idx_violation_rule ON rule_violations (rule_id, broken);

-- Основания входа: «сигнал», «отскок от уровня» и прочие заготовки, которые
-- иначе приходится печатать заново на каждой сделке.
--
-- Форма намеренно та же, что у правил: свой список, архивация вместо удаления,
-- случайный id. Разница только в смысле — правило нарушают, основание
-- применяют, — поэтому таблицы раздельные, а механика общая.
CREATE TABLE IF NOT EXISTS reasons (
    reason_id  TEXT PRIMARY KEY,
    body       TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_reasons (
    trade_id   TEXT NOT NULL,
    reason_id  TEXT NOT NULL,
    broken     INTEGER NOT NULL,   -- 1 = основание применено; имя общее с правилами
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (trade_id, reason_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_reason ON trade_reasons (reason_id, broken);

-- Открытые позиции: СНИМОК, а не история.
--
-- Единственная таблица, которую можно и нужно затирать целиком: закрытая
-- позиция должна исчезать, а не оставаться тумбстоном. Правило «в журнале
-- ничего не удаляем» сюда не относится — это не журнал, а сиюминутное
-- состояние биржи, и хранить его версии незачем.
--
-- Цифры (нереализованный P&L, цена ликвидации) считает биржа: своя оценка
-- разошлась бы с той, по которой позицию реально закрывают.
CREATE TABLE IF NOT EXISTS open_positions (
    symbol        TEXT NOT NULL,
    position_idx  INTEGER NOT NULL,
    direction     TEXT NOT NULL,
    qty           TEXT NOT NULL,
    avg_entry     TEXT NOT NULL,
    mark_price    TEXT,
    leverage      TEXT,
    unrealised    TEXT,
    liq_price     TEXT,
    position_value TEXT,
    opened_at     INTEGER,
    taken_at      INTEGER NOT NULL,   -- когда снимок сделан; возраст обязателен в UI
    PRIMARY KEY (symbol, position_idx)
);

-- Закрытые позиции с MT5 (Bybit TradFi CFD). Строки не удаляются, но, в
-- отличие от fills, правятся повторным импортом той же строки: набирают их
-- руками, а рука ошибается, и единственный способ исправить опечатку — ввести
-- заново. Ключ выдаёт брокер, поэтому повторный импорт того же файла
-- идемпотентен.
--
-- Второй источник данных, устроенный иначе первого, и это не лень, а свойство
-- источника. Через API Bybit счёт MT5 не отдаётся вовсе (`category` знает
-- только spot|linear|inverse|option), а веб-трейдер показывает не исполнения,
-- а уже закрытые позиции с посчитанным брокером P&L.
--
-- Поэтому P&L здесь НЕ вычисляется нами и вычислен быть не может: объём в MT5
-- указан в лотах, а прибыль равна разница_цен × размер_контракта × лоты, и
-- размера контракта в выгрузке нет. На реальных сделках он оказался равен 1
-- для акций и индекса и 10 для палладия — то есть наивная формула дала бы по
-- палладию число, которое выглядит правдоподобно и завышено вдесятеро.
-- Сверять эти сделки не с чем: второго независимого источника не существует.
-- Отсюда `round_trips.source = 'mt5'` — это должно быть видно, а не спрятано.
CREATE TABLE IF NOT EXISTS mt5_positions (
    position_id  TEXT PRIMARY KEY,       -- «ID позиции» из выгрузки
    symbol       TEXT NOT NULL,
    direction    TEXT NOT NULL,          -- long | short
    lots         TEXT NOT NULL,
    open_price   TEXT NOT NULL,
    close_price  TEXT NOT NULL,
    opened_at    INTEGER NOT NULL,
    closed_at    INTEGER NOT NULL,
    gross_pnl    TEXT NOT NULL,          -- «P&L ордера»
    fees         TEXT NOT NULL,          -- комиссия + налог
    swap         TEXT NOT NULL,          -- «Обмен»; в журнале это фандинг с обратным знаком
    net_pnl      TEXT NOT NULL,          -- «Общий P&L» = P&L ордера + обмен − комиссия
    comment      TEXT,                   -- напр. «sl 335.00»: выход по стопу
    entered_at   INTEGER NOT NULL
);

-- Справочник инструментов: чем торгуем — криптой, акцией или товаром.
--
-- Класс берётся у биржи (`symbolType` в /v5/market/instruments-info), а не
-- зашивается списком: список разъехался бы на первом же листинге, а на глаз
-- эти вещи не различаются вовсе — HEIUSDT читается как акция HEICO и является
-- криптой, MUUSDT читается как токен и является Micron.
--
-- Строки не удаляются: делистнутый инструмент пропадает из ответа биржи, но
-- сделки по нему остаются в истории, и класс им нужен по-прежнему.
CREATE TABLE IF NOT EXISTS symbols (
    symbol      TEXT PRIMARY KEY,
    symbol_type TEXT NOT NULL,      -- как отдала биржа: '', stock, commodity, innovation
    updated_at  INTEGER NOT NULL
);

-- Отметки о выкачанных периодах, чтобы бэкфилл не начинался каждый раз с нуля.
CREATE TABLE IF NOT EXISTS sync_state (
    category   TEXT PRIMARY KEY,
    synced_from INTEGER,
    synced_to   INTEGER
);

-- Служебные отметки. Главная — `synced_at`: когда данные последний раз
-- обновлялись с биржи. Без неё сломавшийся синк выглядит как исправный
-- дневник с позавчерашними цифрами: интерфейс показывает их так же
-- уверенно, как свежие.
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# Какие типы биржи означают крипту. Перечислена именно крипта, а не TradFi, и
# это сознательная инверсия: появись у Bybit `forex` или `index`, они попадут
# в TradFi сами. Перечисляй мы наоборот — новый тип молча уехал бы в крипту и
# растворился в её статистике, а такую ошибку не видно, пока её не ищешь.
#
# `innovation` — зона листинга новых токенов, то есть та же крипта, а не
# отдельный класс актива.
CRYPTO_SYMBOL_TYPES = ("", "innovation")

_CRYPTO_TYPES_SQL = ", ".join(f"'{kind}'" for kind in CRYPTO_SYMBOL_TYPES)

# Класс сделки одним выражением: crypto | stock | commodity. Запрос обязан
# присоединить справочник как `s` — LEFT JOIN symbols s ON s.symbol = <t>.symbol.
ASSET_CLASS_SQL = (
    f"CASE WHEN COALESCE(s.symbol_type, '') IN ({_CRYPTO_TYPES_SQL})"
    " THEN 'crypto' ELSE s.symbol_type END"
)


def asset_filter(asset: str | None) -> str:
    """Кусок WHERE для отбора по классу актива. Пусто — значит без отбора.

    Половин ровно две: крипта и всё остальное. Отдельных `stock` и `commodity`
    здесь нет намеренно — дробить статистику на четыре части, имея по девять
    сделок в каждой, значило бы искать закономерность в шуме (см. MIN_SLICE_N).
    """
    if asset == "crypto":
        return f" AND COALESCE(s.symbol_type, '') IN ({_CRYPTO_TYPES_SQL})"
    if asset == "tradfi":
        return f" AND COALESCE(s.symbol_type, '') NOT IN ({_CRYPTO_TYPES_SQL})"
    return ""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # WAL: приложение читает базу, пока фоновый синк её пишет. В обычном режиме
    # пересборка на секунду блокирует читателей, и дневник ловил бы «database is
    # locked» ровно в момент обновления данных. Ожидание блокировки — на случай
    # пересечения двух писателей (синк на маке, бот и Mini App на сервере).
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _reattach_journal(conn: sqlite3.Connection) -> None:
    """Возвращает разборы сделкам, у которых поменялся trade_id.

    `trade_id` = `символ:индекс:время_открытия:id_первого_исполнения`. Первые
    три части устойчивы, а четвёртая зависит от порядка fills внутри
    миллисекунды — и меняется, если этот порядок починили. Ровно так и вышло
    при развороте провёрнутых колонок: склейка осталась прежней (то же время
    открытия, то же закрытие, тот же P&L), а хвост идентификатора уехал, и
    четыре живых разбора повисли в воздухе.

    Поэтому сопоставление идёт по устойчивому префиксу: сделка того же
    инструмента, открытая в ту же миллисекунду, — это та же сделка. Старая
    строка остаётся пустой, а не удаляется: журнал ездит в обе стороны.
    """
    for table in ("notes", "rule_violations", "trade_reasons"):
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone():
            continue
        # Только строки, которые что-то несут. Пустой разбор и снятая отметка —
        # это тумбстоны, оставленные прошлым переносом; они тоже сироты, и без
        # этого условия следующий же вызов копировал бы пустоту поверх только
        # что восстановленной заметки и стирал её. Ровно так и случилось.
        carries = "body <> ''" if table == "notes" else "broken = 1"
        orphans = conn.execute(
            f"SELECT * FROM {table}"
            f" WHERE {carries}"
            "   AND trade_id NOT IN (SELECT trade_id FROM round_trips)"
        ).fetchall()
        for row in orphans:
            prefix = ":".join(row["trade_id"].split(":")[:3]) + ":"
            target = conn.execute(
                "SELECT trade_id FROM round_trips WHERE trade_id LIKE ? || '%'",
                (prefix,),
            ).fetchall()
            if len(target) != 1:
                continue          # неоднозначно — лучше оставить как есть
            now = int(time.time() * 1000)
            columns = [c for c in row.keys() if c != "trade_id"]
            values = [row[c] for c in columns]
            placeholders = ", ".join("?" for _ in columns)
            conn.execute(
                f"INSERT OR REPLACE INTO {table} (trade_id, {', '.join(columns)})"
                f" VALUES (?, {placeholders})",
                [target[0]["trade_id"], *values],
            )
            if table == "notes":
                conn.execute(
                    "UPDATE notes SET body = '', updated_at = ? WHERE trade_id = ?",
                    (now, row["trade_id"]),
                )
            else:
                conn.execute(
                    f"UPDATE {table} SET broken = 0, updated_at = ? WHERE trade_id = ?",
                    (now, row["trade_id"]),
                )
        if orphans:
            conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Досоздаёт колонки, появившиеся после первых выкачек.

    Сырые fills хранятся целиком в `raw`, поэтому новое поле берётся оттуда —
    повторно ходить на биржу не нужно. Ради этого append-only и заводился.
    """
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(raw_executions)")}
    if "seq" not in columns:
        conn.execute("ALTER TABLE raw_executions ADD COLUMN seq INTEGER")
        conn.commit()
        conn.execute(
            "UPDATE raw_executions SET seq = CAST(json_extract(raw, '$.seq') AS INTEGER)"
        )
        conn.commit()
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exec_group"
        " ON raw_executions (category, symbol, position_idx, exec_time, seq)"
    )
    conn.commit()

    rt_columns = {r["name"] for r in conn.execute("PRAGMA table_info(round_trips)")}
    if "fees_source" not in rt_columns:
        conn.execute(
            "ALTER TABLE round_trips ADD COLUMN fees_source TEXT NOT NULL DEFAULT 'fills'"
        )
        conn.commit()
    if "source" not in rt_columns:
        # Умолчание 'fills' верно для всего, что было до появления MT5: до этой
        # колонки других источников не существовало.
        conn.execute(
            "ALTER TABLE round_trips ADD COLUMN source TEXT NOT NULL DEFAULT 'fills'"
        )
        conn.commit()

    # Разовая починка провёрнутых колонок (история бага — у save_executions)
    # снята: она искала признак полным перебором на КАЖДОМ подключении, а
    # подключение — на каждый HTTP-запрос. Войти порче больше неоткуда:
    # и save_executions, и sync.merge перечисляют колонки поимённо. Проверить
    # руками: SELECT COUNT(*) FROM raw_executions WHERE typeof(seq) = 'text'.

    _reattach_journal(conn)

    # Плечо и объём входа. Повторно выкачивать историю не нужно: ответ биржи
    # с самого начала сохранялся целиком в raw, и обе величины достаются оттуда.
    pnl_columns = {r["name"] for r in conn.execute("PRAGMA table_info(exchange_pnl)")}
    if "leverage" not in pnl_columns:
        conn.execute("ALTER TABLE exchange_pnl ADD COLUMN leverage TEXT")
        conn.execute("ALTER TABLE exchange_pnl ADD COLUMN entry_value TEXT")
        conn.execute(
            "UPDATE exchange_pnl SET leverage = json_extract(raw, '$.leverage'),"
            " entry_value = json_extract(raw, '$.cumEntryValue')"
        )
        conn.commit()

    if "leverage" not in rt_columns:
        conn.execute("ALTER TABLE round_trips ADD COLUMN leverage TEXT")
        conn.execute("ALTER TABLE round_trips ADD COLUMN entry_value TEXT")
        conn.commit()

    # uid у намерений появился вместе с двусторонней синхронизацией. Старым
    # записям он выдаётся здесь: без него намерение не уедет на другую сторону.
    intent_columns = {r["name"] for r in conn.execute("PRAGMA table_info(intents)")}
    if "uid" not in intent_columns:
        conn.execute("ALTER TABLE intents ADD COLUMN uid TEXT")
        conn.execute("UPDATE intents SET uid = lower(hex(randomblob(8)))")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_intent_uid ON intents (uid)")
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
            row.get("leverage"), row.get("cumEntryValue"),
            json.dumps(row, ensure_ascii=False),
        )
        for row in rows
    ]
    # Колонки перечислены поимённо, а не VALUES по порядку: с безымянной формой
    # добавление любого поля в таблицу молча ломает вставку.
    cursor = conn.executemany(
        "INSERT OR IGNORE INTO exchange_pnl"
        " (order_id, updated_time, symbol, side, qty, closed_size, avg_entry,"
        "  avg_exit, closed_pnl, open_fee, close_fee, leverage, entry_value, raw)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", payload
    )
    conn.commit()
    return cursor.rowcount


def save_open_positions(conn: sqlite3.Connection, rows: list[dict],
                        taken_at: int | None = None) -> int:
    """Заменяет снимок открытых позиций целиком.

    Именно заменяет: позиция, которой в ответе биржи больше нет, закрыта, и
    оставлять её нельзя. Дописывание здесь дало бы вечно растущий список
    призраков с «текущим» P&L времён позапрошлой недели.
    """
    taken_at = taken_at if taken_at is not None else int(time.time() * 1000)
    payload = [
        (
            row["symbol"], int(row.get("positionIdx") or 0),
            "long" if row.get("side") == "Buy" else "short",
            row.get("size"), row.get("avgPrice"), row.get("markPrice"),
            row.get("leverage"), row.get("unrealisedPnl"), row.get("liqPrice"),
            row.get("positionValue"),
            int(row["createdTime"]) if row.get("createdTime") else None,
            taken_at,
        )
        for row in rows
    ]
    conn.execute("DELETE FROM open_positions")
    conn.executemany(
        "INSERT INTO open_positions (symbol, position_idx, direction, qty,"
        " avg_entry, mark_price, leverage, unrealised, liq_price, position_value,"
        " opened_at, taken_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        payload,
    )
    # Отметка ставится даже при пустом списке: «позиций нет» — тоже свежий факт,
    # и отличать его от «давно не спрашивали» интерфейс обязан.
    set_meta(conn, "positions_at", taken_at)
    conn.commit()
    return len(payload)


def save_symbols(conn: sqlite3.Connection, rows: list[dict],
                 updated_at: int | None = None) -> int:
    """Обновляет справочник инструментов. Ничего не удаляет.

    Перезапись, а не «вставить, если нет»: биржа может переклассифицировать
    инструмент, и оставить прежний тип молча значило бы показывать акцию
    криптой вечно. Инструменты, пропавшие из ответа биржи (делистнутые), в
    справочнике остаются — сделки по ним никуда не делись.
    """
    updated_at = updated_at if updated_at is not None else int(time.time() * 1000)
    conn.executemany(
        "INSERT INTO symbols (symbol, symbol_type, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(symbol) DO UPDATE SET symbol_type = excluded.symbol_type,"
        " updated_at = excluded.updated_at",
        [(row["symbol"], row.get("symbolType") or "", updated_at) for row in rows],
    )
    conn.commit()
    return len(rows)


def save_mt5_positions(conn: sqlite3.Connection, rows: list[dict],
                       entered_at: int | None = None) -> int:
    """Пишет закрытые позиции MT5. Повторный ввод той же строки её заменяет.

    Замена, а не «вставить, если нет»: строки набираются руками, и единственный
    способ исправить опечатку — ввести заново. Ключ выдаёт брокер, поэтому
    повторный импорт того же файла ничего не портит.
    """
    entered_at = entered_at if entered_at is not None else int(time.time() * 1000)
    payload = [
        (
            row["position_id"], row["symbol"], row["direction"], str(row["lots"]),
            str(row["open_price"]), str(row["close_price"]),
            int(row["opened_at"]), int(row["closed_at"]),
            str(row["gross_pnl"]), str(row["fees"]), str(row["swap"]),
            str(row["net_pnl"]), row.get("comment"), entered_at,
        )
        for row in rows
    ]
    # Колонки поимённо — по той же причине, что у fills: безымянный VALUES
    # опирается на физический порядок, а он расходится со SCHEMA после ALTER.
    conn.executemany(
        "INSERT INTO mt5_positions"
        " (position_id, symbol, direction, lots, open_price, close_price,"
        "  opened_at, closed_at, gross_pnl, fees, swap, net_pnl, comment, entered_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(position_id) DO UPDATE SET"
        "   symbol = excluded.symbol, direction = excluded.direction,"
        "   lots = excluded.lots, open_price = excluded.open_price,"
        "   close_price = excluded.close_price, opened_at = excluded.opened_at,"
        "   closed_at = excluded.closed_at, gross_pnl = excluded.gross_pnl,"
        "   fees = excluded.fees, swap = excluded.swap, net_pnl = excluded.net_pnl,"
        "   comment = excluded.comment, entered_at = excluded.entered_at",
        payload,
    )
    conn.commit()
    return len(payload)


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


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
    # Колонки поимённо. Безымянный VALUES опирается на физический порядок
    # колонок, а он расходится с SCHEMA на любой базе, пережившей ALTER TABLE:
    # SQLite дописывает новую колонку в конец, а не туда, где она объявлена.
    # Именно так `seq`, `order_id` и `raw` провернулись по кругу на 547 fills.
    cursor = conn.executemany(
        "INSERT OR IGNORE INTO raw_executions"
        " (exec_id, category, symbol, position_idx, side, exec_type, exec_price,"
        "  exec_qty, exec_fee, fee_currency, closed_size, exec_time, seq, order_id, raw)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        payload,
    )
    conn.commit()
    return cursor.rowcount
