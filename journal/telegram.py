"""Telegram-бот: чтение журнала и разбор сделок с телефона.

Роль бота задана в grilling-сессии (решение C): он читалка-аннотатор, а не
тренажёр дисциплины. Никаких «тапни перед входом» — с телефона смотрят
статистику и дописывают «почему заходил» к уже закрытым сделкам.

Границы, принятые осознанно и один раз:

* Всё отправленное проходит через серверы Telegram и оседает в облачной истории
  чата. Поэтому бот отвечает ТОЛЬКО своему chat_id, а чужие сообщения
  игнорирует молча: ответ «доступ запрещён» подтверждает, что бот существует.
* Бот **не отдаёт баланс, размер депозита и открытые позиции**. Это не только
  правило: в базе таких данных нет вовсе — хранятся закрытые сделки, — так что
  утечка невозможна структурно, а не по договорённости.
* Ключей биржи бот не касается: он читает SQLite и больше ничего.

Транспорт (`Bot`) отделён от логики (`handle_update`), поэтому команды
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
MAX_TRADES = 15

HELP = (
    "Команды:\n"
    "/stats [3d|7d|30d] — статистика за период\n"
    "/trades [3d|7d|30d] — последние закрытые сделки\n"
    "/pending — сделки без разбора; ответь на сообщение текстом, "
    "и он сохранится как разбор\n"
    "/help — эта справка"
)

PERIODS = {"3d": 3, "7d": 7, "30d": 30, "90d": 90, "all": 0}


def _pre(text: str) -> str:
    return f"<pre>{html.escape(text)}</pre>"


def _period(args: list[str]) -> tuple[int, str | None]:
    """Разбирает аргумент периода. Возвращает (дни, текст ошибки)."""
    if not args:
        return 30, None
    key = args[0].lower()
    if key in PERIODS:
        return PERIODS[key], None
    return 0, f"Не понял период «{args[0]}». Доступно: " + ", ".join(PERIODS)


def _fmt_trade_line(row) -> str:
    pnl = dec(row["net_pnl"] or 0)
    mark = "+" if pnl > 0 else ""
    return (f"{row['symbol']:<13}{row['direction']:<6}{mark}{pnl:>10.2f}")


def _reply(text: str, trade_id: str | None = None) -> dict:
    """Одно исходящее сообщение. trade_id — чтобы привязать будущий ответ."""
    return {"text": text, "trade_id": trade_id}


# --- команды ---------------------------------------------------------------


def cmd_stats(conn, args) -> list[dict]:
    days, error = _period(args)
    if error:
        return [_reply(error)]
    return [_reply(_pre(report.render(conn, days)))]


def cmd_trades(conn, args) -> list[dict]:
    days, error = _period(args)
    if error:
        return [_reply(error)]

    query = "SELECT * FROM round_trips WHERE closed_at IS NOT NULL"
    params: tuple = ()
    if days:
        query += (" AND closed_at >= (SELECT MAX(closed_at) FROM round_trips) - ?")
        params = (days * stats.DAY_MS,)
    rows = conn.execute(
        query + " ORDER BY closed_at DESC LIMIT ?", params + (MAX_TRADES,)
    ).fetchall()

    if not rows:
        return [_reply("За период закрытых сделок нет.")]

    total = sum(dec(r["net_pnl"] or 0) for r in rows)
    header = f"Последние {len(rows)} сделок, сумма {total:+.2f} USDT\n\n"
    body = "\n".join(_fmt_trade_line(r) for r in rows)
    return [_reply(_pre(header + body))]


def cmd_pending(conn, args) -> list[dict]:
    """Сделки без разбора — каждая отдельным сообщением, чтобы на неё ответить."""
    rows = journal.unjournaled(conn, limit=MAX_PENDING)
    if not rows:
        cov = journal.coverage(conn)
        return [_reply(
            f"Все закрытые сделки разобраны ({cov['annotated']} из {cov['trades']})."
        )]

    cov = journal.coverage(conn)
    replies = [_reply(
        f"Без разбора: {cov['missing']} из {cov['trades']}. "
        f"Показываю {len(rows)} — ответь на сообщение текстом, "
        "и он сохранится как разбор этой сделки."
    )]
    for row in rows:
        pnl = dec(row["net_pnl"] or 0)
        when = time.strftime("%d.%m %H:%M", time.localtime(row["closed_at"] / 1000))
        replies.append(_reply(
            f"{row['symbol']} {row['direction']} · {pnl:+.2f} USDT · {when}",
            trade_id=row["trade_id"],
        ))
    return replies


COMMANDS = {
    "/stats": cmd_stats,
    "/trades": cmd_trades,
    "/pending": cmd_pending,
}


# --- обработчик ------------------------------------------------------------


def handle_update(conn, update: dict, allowed_chat_id: int) -> list[dict]:
    """Чистая логика: апдейт -> список сообщений в ответ.

    Пустой список означает «промолчать». Именно это происходит с чужими
    чатами: подтверждать существование бота не нужно.
    """
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
            return [_reply(
                f"Разбор сохранён. Разобрано {cov['annotated']} из {cov['trades']}."
            )]
        return [_reply(
            "Не понял, к какой сделке это относится — отвечай на сообщение из /pending."
        )]

    if not text.startswith("/"):
        return [_reply(HELP)]

    # /stats@my_bot 7d -> ("/stats", ["7d"])
    parts = text.split()
    command = parts[0].split("@")[0].lower()
    handler = COMMANDS.get(command)
    if not handler:
        return [_reply(HELP)]
    return handler(conn, parts[1:])


# --- транспорт -------------------------------------------------------------


class Bot:
    def __init__(self, token: str, chat_id: int):
        self.token = token
        self.chat_id = chat_id
        self._offset = None

    def _call(self, method: str, **params) -> dict:
        data = urllib.parse.urlencode(params).encode()
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

    def send(self, text: str) -> int:
        result = self._call(
            "sendMessage", chat_id=self.chat_id, text=text,
            parse_mode="HTML", disable_web_page_preview="true",
        )
        return result["message_id"]

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
                replies = handle_update(conn, update, chat_id)
                for reply in replies:
                    message_id = bot.send(reply["text"])
                    if reply["trade_id"]:
                        conn.execute(
                            "INSERT OR REPLACE INTO bot_messages VALUES (?,?,?)",
                            (message_id, reply["trade_id"], int(time.time() * 1000)),
                        )
                        conn.commit()
            except RuntimeError as exc:
                print(f"  {exc}", flush=True)
            finally:
                conn.close()
