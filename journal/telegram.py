"""Telegram-бот: журнал и статистика с телефона.

Роль бота задана в grilling-сессии (решение C): он читалка-аннотатор, а не
тренажёр дисциплины. С телефона смотрят цифры и дописывают «почему заходил»
к уже закрытым сделкам.

Границы, принятые осознанно и один раз:

* Всё отправленное проходит через серверы Telegram и оседает в облачной истории
  чата. Поэтому бот отвечает ТОЛЬКО своему chat_id, а чужие сообщения
  игнорирует молча: ответ «доступ запрещён» подтверждает, что бот существует.
* Бот **не отдаёт баланс, размер депозита и открытые позиции**. Это не только
  правило: в базе таких данных нет вовсе — хранятся закрытые сделки, — так что
  утечка невозможна структурно, а не по договорённости.
* Ключей биржи бот не касается: он читает SQLite и больше ничего.

Вёрстка мобильная, а не переиспользованная из терминала: на экране телефона
помещается ~30 моноширинных знаков, и отчёт шириной в 80 ломается в кашу.
Полный терминальный отчёт остаётся доступен отдельной кнопкой.

Транспорт (`Bot`) отделён от логики (`handle_update`), поэтому экраны
тестируются без сети и без токена.
"""

import html
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import journal, report, stats
from .db import dec

API = "https://api.telegram.org/bot"
POLL_TIMEOUT = 30
MAX_PENDING = 5          # сколько неразобранных сделок показывать за раз
MAX_TRADES = 15          # строк на страницу списка сделок

MINUS = "−"         # настоящий минус: в моноширинном тексте весомее дефиса

PERIODS = {"3d": 3, "7d": 7, "30d": 30, "90d": 90, "all": 0}
PERIOD_LABEL = {3: "3 дня", 7: "7 дней", 30: "30 дней", 90: "90 дней", 0: "всё время"}

COMMAND_MENU = [
    ("menu", "главное меню"),
    ("stats", "статистика"),
    ("trades", "последние сделки"),
    ("pending", "сделки без разбора"),
]


# --- форматирование --------------------------------------------------------


def money(value, width: int = 0) -> str:
    """Сумма со знаком и настоящим минусом, опционально по ширине колонки."""
    number = float(value)
    text = f"{abs(number):,.2f}".replace(",", " ")
    sign = "+" if number > 0 else (MINUS if number < 0 else " ")
    return f"{sign}{text}".rjust(width)


def short_symbol(symbol: str, width: int = 0) -> str:
    """BTCUSDT -> BTC. Все пары здесь USDT-маржинальные, суффикс не несёт
    информации и съедает 4 знака из тридцати на каждой строке."""
    name = symbol[:-4] if symbol.endswith("USDT") else symbol
    return name[:width].ljust(width) if width else name


def _pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def _b(text: str) -> str:
    return f"<b>{html.escape(text)}</b>"


def _period_keyboard(screen: str, days: int) -> list[list[tuple[str, str]]]:
    """Переключатель периода: текущий помечен, чтобы было видно, где ты."""
    row = []
    for label, value in (("3д", 3), ("7д", 7), ("30д", 30), ("Всё", 0)):
        row.append((f"· {label} ·" if value == days else label, f"{screen}:{value}"))
    return [row]


def _nav_row(exclude: str) -> list[tuple[str, str]]:
    buttons = [
        ("📊 Статистика", "stats:30"),
        ("📋 Сделки", "trades:30"),
        ("✍️ Разобрать", "pending:0"),
    ]
    return [b for b in buttons if not b[1].startswith(exclude)]


# --- экраны ----------------------------------------------------------------


def screen_menu(conn) -> tuple[str, list]:
    cov = journal.coverage(conn)
    summary = stats.summary(conn, 30)

    lines = [_b("📓 Trade Journal"), "Bybit · linear · сверено с биржей", ""]
    if summary["n"]:
        lines.append(
            f"За 30 дней: {money(summary['total'])} USDT · {summary['n']} сделок"
        )
    lines.append(f"Разобрано: {cov['annotated']} из {cov['trades']}")
    if cov["missing"]:
        lines.append(f"Ждут разбора: {_b(str(cov['missing']))}")

    keyboard = [
        [("📊 Статистика", "stats:30"), ("📋 Сделки", "trades:30")],
        [("✍️ Разобрать сделки", "pending:0")],
    ]
    return "\n".join(lines), keyboard


def screen_stats(conn, days: int) -> tuple[str, list]:
    s = stats.summary(conn, days)
    title = f"📊 {_b('Статистика')} · {PERIOD_LABEL.get(days, f'{days} дн.')}"
    if not s["n"]:
        return f"{title}\n\nЗакрытых сделок за период нет.", _period_keyboard("stats", days)

    lo, hi = s["win_rate_ci"]
    block = [
        f"Итог     {money(s['total'], 11)}",
        f"Сделок   {s['n']:>11}",
        f"Побед    {s['win_rate']:>11.0%}",
        f"  95%    {f'{lo:.0%}{MINUS}{hi:.0%}':>11}",
        "",
        f"Ср. плюс {money(s['avg_win'], 11)}",
        f"Ср. минус{money(s['avg_loss'], 11)}",
    ]
    if s["payoff"] is not None:
        needed = (1 - s["win_rate"]) / s["win_rate"]
        mark = "✓" if s["payoff"] >= needed else "✗"
        block.append(f"П/У      {float(s['payoff']):>11.2f} {mark}")
        block.append(f"  нужно  {needed:>11.2f}")

    elo, ehi = s["expectancy_ci"]
    block += [
        "",
        f"На сделку{money(s['expectancy'], 11)}",
        f"Издержки {money(-s['costs'], 11)}",
    ]

    parts = [title, "", _pre("\n".join(block))]

    if elo < 0 < ehi:
        parts.append(
            f"⚠️ Интервал матожидания {money(elo)}…{money(ehi)} накрывает ноль: "
            "знак статистически не установлен."
        )
    parts.append(f"<i>{html.escape(stats.sample_note(s['n']))}</i>")

    hold = stats.holding_time(conn, days)
    if hold["median_win_hours"] is not None and hold["median_loss_hours"] is not None:
        if hold["median_loss_hours"] > hold["median_win_hours"] * 1.5:
            parts.append(
                f"⏱ Убытки держатся {hold['median_loss_hours']:.1f} ч против "
                f"{hold['median_win_hours']:.1f} ч у прибыли — пересиживаются."
            )

    keyboard = _period_keyboard("stats", days)
    keyboard.append([("🏆 Топ прибыльных", f"top:{days}"),
                     ("📄 Полный отчёт", f"report:{days}")])
    keyboard.append(_nav_row("stats"))
    return "\n".join(parts), keyboard


def trade_line(row) -> str:
    """Строка сделки обычным текстом, а не кодом.

    Моноширинный блок держит колонки, но выглядит выгрузкой из терминала.
    Здесь смысл несут кружок (плюс/минус — цвета в тексте Telegram нет),
    стрелка направления и жирный тикер; выравнивание не нужно.
    """
    pnl = dec(row["net_pnl"] or 0)
    dot = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪️")
    arrow = "▲" if row["direction"] == "long" else "▼"
    when = time.strftime("%d.%m", time.localtime(row["closed_at"] / 1000))
    mark = " 📝" if row["annotated"] else ""
    return (f"{dot} {_b(short_symbol(row['symbol']))} {arrow} "
            f"{money(pnl)} · <i>{when}</i>{mark}")


def _trades_page(conn, days: int, offset: int, limit: int):
    """Страница закрытых сделок + признак разбора и общее число за период."""
    where = "closed_at IS NOT NULL"
    params: tuple = ()
    if days:
        where += " AND closed_at >= (SELECT MAX(closed_at) FROM round_trips) - ?"
        params = (days * stats.DAY_MS,)

    total = conn.execute(
        f"SELECT COUNT(*) c, COALESCE(SUM(CAST(net_pnl AS REAL)), 0) s"
        f" FROM round_trips WHERE {where}", params
    ).fetchone()
    rows = conn.execute(
        "SELECT rt.*, (n.trade_id IS NOT NULL OR i.intent_id IS NOT NULL) AS annotated"
        " FROM round_trips rt"
        " LEFT JOIN notes n ON n.trade_id = rt.trade_id"
        " LEFT JOIN intents i ON i.matched_trade_id = rt.trade_id"
        f" WHERE rt.{where} ORDER BY rt.closed_at DESC LIMIT ? OFFSET ?",
        params + (limit, offset),
    ).fetchall()
    return rows, total["c"], total["s"]


def screen_top(conn, days: int) -> tuple[str, list]:
    top = stats.top_trades(conn, days, limit=8)
    title = f"🏆 {_b('Топ прибыльных')} · {PERIOD_LABEL.get(days, f'{days} дн.')}"
    if not top["trades"]:
        return f"{title}\n\nПрибыльных сделок за период нет.", _period_keyboard("top", days)

    lines = [
        f"🟢 {_b(short_symbol(t['symbol']))} · {money(t['net_pnl'])}"
        for t in top["trades"]
    ]
    parts = [title, ""] + lines
    if top["share_of_wins"] is not None:
        parts += ["", (
            f"Эти {len(top['trades'])} из {top['winners_total']} прибыльных дали "
            f"{_b(f'{float(top['share_of_wins']):.0%}')} всей прибыли."
        )]
    keyboard = _period_keyboard("top", days)
    keyboard.append(_nav_row("top"))
    return "\n".join(parts), keyboard


def screen_trades(conn, days: int, offset: int = 0) -> tuple[str, list]:
    rows, total, total_pnl = _trades_page(conn, days, offset, MAX_TRADES)
    title = f"📋 {_b('Сделки')} · {PERIOD_LABEL.get(days, f'{days} дн.')}"
    if not rows:
        return f"{title}\n\nЗакрытых сделок за период нет.", _period_keyboard("trades", days)

    shown_to = offset + len(rows)
    parts = [title, ""] + [trade_line(r) for r in rows]
    parts += ["", (f"{offset + 1}–{shown_to} из {total} · "
                   f"итог за период {_b(money(total_pnl) + ' USDT')}")]

    keyboard = _period_keyboard("trades", days)
    # Листалка появляется только когда есть куда листать.
    page_row = []
    if offset:
        page_row.append(("↑ В начало", f"trades:{days}:0"))
    if shown_to < total:
        page_row.append((f"Ещё {min(MAX_TRADES, total - shown_to)} ↓",
                         f"trades:{days}:{shown_to}"))
    if page_row:
        keyboard.append(page_row)
    keyboard.append(_nav_row("trades"))
    return "\n".join(parts), keyboard


def screen_report(conn, days: int) -> tuple[str, list]:
    """Полный терминальный отчёт — на телефоне широкий, поэтому по запросу."""
    return _pre(report.render(conn, days)), [[("← Назад", f"stats:{days}")]]


def messages_pending(conn) -> list[dict]:
    """Сделки без разбора — каждая отдельным сообщением, чтобы на неё ответить."""
    cov = journal.coverage(conn)
    rows = journal.unjournaled(conn, limit=MAX_PENDING)
    if not rows:
        return [_send(
            f"✅ Все закрытые сделки разобраны ({cov['annotated']} из {cov['trades']}).",
            keyboard=[_nav_row("pending")],
        )]

    head = _send(
        f"✍️ {_b('Без разбора')}: {cov['missing']} из {cov['trades']}\n"
        f"Показываю {len(rows)}. Ответь на сообщение текстом — он сохранится "
        "как разбор этой сделки.",
    )
    out = [head]
    for row in rows:
        arrow = "▲" if row["direction"] == "long" else "▼"
        when = time.strftime("%d.%m %H:%M", time.localtime(row["closed_at"] / 1000))
        out.append(_send(
            f"{arrow} {_b(short_symbol(row['symbol']))}  "
            f"{money(row['net_pnl'] or 0)} USDT\n<i>{when}</i>",
            trade_id=row["trade_id"],
        ))
    return out


# --- действия --------------------------------------------------------------


def _send(text: str, keyboard=None, trade_id: str | None = None) -> dict:
    return {"kind": "send", "text": text, "keyboard": keyboard, "trade_id": trade_id}


def _edit(message_id: int, text: str, keyboard=None) -> dict:
    return {"kind": "edit", "message_id": message_id, "text": text,
            "keyboard": keyboard}


def _answer(callback_id: str, text: str = "") -> dict:
    return {"kind": "answer", "callback_id": callback_id, "text": text}


SCREENS = {
    "stats": screen_stats,
    "trades": screen_trades,
    "top": screen_top,
    "report": screen_report,
}


def _screen_actions(conn, name: str, days: int, edit_id: int | None,
                    offset: int = 0) -> list[dict]:
    if name == "menu":
        text, keyboard = screen_menu(conn)
    elif name == "pending":
        return messages_pending(conn)
    elif name == "trades":
        text, keyboard = screen_trades(conn, days, offset)
    else:
        text, keyboard = SCREENS[name](conn, days)
    if edit_id is not None:
        return [_edit(edit_id, text, keyboard)]
    return [_send(text, keyboard)]


def _parse_period(args: list[str]) -> tuple[int, str | None]:
    if not args:
        return 30, None
    key = args[0].lower().rstrip("д")
    for alias, value in PERIODS.items():
        if key == alias.rstrip("d") or key == alias:
            return value, None
    return 0, f"Не понял период «{args[0]}». Доступно: " + ", ".join(PERIODS)


def handle_update(conn, update: dict, allowed_chat_id: int) -> list[dict]:
    """Чистая логика: апдейт -> список действий.

    Пустой список означает «промолчать». Именно это происходит с чужими
    чатами: подтверждать существование бота не нужно.
    """
    callback = update.get("callback_query")
    if callback:
        return _handle_callback(conn, callback, allowed_chat_id)

    message = update.get("message") or update.get("edited_message")
    if not message:
        return []
    if message.get("chat", {}).get("id") != allowed_chat_id:
        return []

    text = (message.get("text") or "").strip()
    if not text:
        return []

    # Ответ на сообщение о сделке — это разбор, а не команда.
    replied = message.get("reply_to_message")
    if replied and not text.startswith("/"):
        row = conn.execute(
            "SELECT trade_id FROM bot_messages WHERE message_id = ?",
            (replied.get("message_id"),),
        ).fetchone()
        if row:
            journal.add_note(conn, row["trade_id"], text)
            cov = journal.coverage(conn)
            return [_send(
                f"✅ Разбор сохранён. Разобрано {cov['annotated']} из {cov['trades']}.",
                keyboard=[[("✍️ Ещё", "pending:0"), ("📊 Статистика", "stats:30")]],
            )]
        return [_send(
            "Не понял, к какой сделке это относится — отвечай на сообщение из ✍️ Разобрать."
        )]

    if not text.startswith("/"):
        return _screen_actions(conn, "menu", 0, None)

    parts = text.split()
    command = parts[0].split("@")[0].lower().lstrip("/")
    if command in ("start", "menu", "help"):
        return _screen_actions(conn, "menu", 0, None)
    if command == "pending":
        return messages_pending(conn)
    if command in SCREENS:
        days, error = _parse_period(parts[1:])
        if error:
            return [_send(error)]
        return _screen_actions(conn, command, days, None)
    return _screen_actions(conn, "menu", 0, None)


def _handle_callback(conn, callback: dict, allowed_chat_id: int) -> list[dict]:
    message = callback.get("message") or {}
    if message.get("chat", {}).get("id") != allowed_chat_id:
        return []

    # Формат: имя:период[:смещение]
    parts = (callback.get("data") or "").split(":")
    name = parts[0]
    days = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 30
    offset = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0

    actions = [_answer(callback["id"])]
    if name == "pending":
        # Отдельные сообщения на сделку — редактировать нечего.
        return actions + messages_pending(conn)
    if name == "menu" or name in SCREENS:
        return actions + _screen_actions(
            conn, name, days, message.get("message_id"), offset)
    return actions


# --- транспорт -------------------------------------------------------------


def _keyboard_json(keyboard) -> str | None:
    if not keyboard:
        return None
    return json.dumps({"inline_keyboard": [
        [{"text": label, "callback_data": data} for label, data in row]
        for row in keyboard
    ]})


class Bot:
    def __init__(self, token: str, chat_id: int):
        self.token = token
        self.chat_id = chat_id
        self._offset = None

    def _call(self, method: str, **params) -> dict:
        payload = {k: v for k, v in params.items() if v is not None}
        data = urllib.parse.urlencode(payload).encode()
        request = urllib.request.Request(f"{API}{self.token}/{method}", data=data)
        try:
            with urllib.request.urlopen(request, timeout=POLL_TIMEOUT + 15) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            # Токен не должен утечь в текст ошибки — в URL он есть всегда.
            raise RuntimeError(f"Telegram {method}: HTTP {exc.code}") from None
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method}: {body.get('description')}")
        return body["result"]

    def send(self, text: str, keyboard=None) -> int:
        result = self._call(
            "sendMessage", chat_id=self.chat_id, text=text,
            parse_mode="HTML", disable_web_page_preview="true",
            reply_markup=_keyboard_json(keyboard),
        )
        return result["message_id"]

    def edit(self, message_id: int, text: str, keyboard=None) -> None:
        try:
            self._call(
                "editMessageText", chat_id=self.chat_id, message_id=message_id,
                text=text, parse_mode="HTML", disable_web_page_preview="true",
                reply_markup=_keyboard_json(keyboard),
            )
        except RuntimeError as exc:
            # Повторное нажатие той же кнопки не меняет текст — Telegram отвечает
            # ошибкой, и это не повод шуметь в логе.
            if "not modified" not in str(exc):
                raise

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", callback_query_id=callback_id, text=text or None)

    def set_commands(self) -> None:
        """Нативное меню по «/» — команды видно, не надо помнить."""
        self._call("setMyCommands", commands=json.dumps(
            [{"command": name, "description": desc} for name, desc in COMMAND_MENU]
        ))

    def poll(self) -> list[dict]:
        params = {"timeout": POLL_TIMEOUT}
        if self._offset is not None:
            params["offset"] = self._offset
        updates = self._call("getUpdates", **params)
        if updates:
            self._offset = updates[-1]["update_id"] + 1
        return updates


def run(conn_factory, token: str, chat_id: int) -> None:
    bot = Bot(token, chat_id)
    try:
        bot.set_commands()
    except RuntimeError as exc:
        print(f"  меню команд не обновилось: {exc}", flush=True)

    # flush=True: без него сообщения долгоживущей команды застревают в буфере,
    # когда вывод идёт не в терминал, и запуск выглядит как зависание.
    print(f"Бот запущен, отвечает только chat_id {chat_id}. Ctrl+C — остановить.",
          flush=True)

    while True:
        try:
            updates = bot.poll()
        except KeyboardInterrupt:
            print("\nОстановлено.", flush=True)
            return
        except RuntimeError as exc:
            print(f"  {exc}; повтор через 15 с", flush=True)
            time.sleep(15)
            continue

        for update in updates:
            conn = conn_factory()
            try:
                for action in handle_update(conn, update, chat_id):
                    _perform(bot, conn, action)
            except RuntimeError as exc:
                print(f"  {exc}", flush=True)
            finally:
                conn.close()


def _perform(bot: Bot, conn, action: dict) -> None:
    kind = action["kind"]
    if kind == "answer":
        bot.answer_callback(action["callback_id"], action["text"])
    elif kind == "edit":
        bot.edit(action["message_id"], action["text"], action["keyboard"])
    elif kind == "send":
        message_id = bot.send(action["text"], action["keyboard"])
        if action["trade_id"]:
            conn.execute(
                "INSERT OR REPLACE INTO bot_messages VALUES (?,?,?)",
                (message_id, action["trade_id"], int(time.time() * 1000)),
            )
            conn.commit()
