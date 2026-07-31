"""Локальный веб-интерфейс. Только stdlib, только 127.0.0.1.

План предлагал FastAPI, но это было до того, как «ноль внешних зависимостей»
стал принципом проекта (см. README). Одному локальному пользователю фреймворк
не нужен: http.server из stdlib отдаёт три статических файла и четыре
JSON-ручки. Ключи биржи серверу не нужны вообще — он читает только SQLite.
"""

import json
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, journal, stats, webauth

WEB_DIR = Path(__file__).parent / "web"

# Статика отдаётся по белому списку имён — никакого маппинга путей в
# файловую систему, а значит и path traversal невозможен по построению.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/miniapp.css": ("miniapp.css", "text/css; charset=utf-8"),
    "/miniapp.js": ("miniapp.js", "text/javascript; charset=utf-8"),
}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'"
)

# В режиме Mini App приходится пустить один внешний скрипт — SDK Telegram
# грузится только с telegram.org и даёт разворот на весь экран, нативную
# кнопку «назад» и тему клиента. Обойтись без него можно (initData лежит в
# хеше URL), но интерфейс тогда открывается вполовину экрана.
CSP_MINIAPP = (
    "default-src 'none'; script-src 'self' https://telegram.org; "
    "style-src 'self'; connect-src 'self'; img-src 'self'"
)


def _open_positions(conn) -> dict:
    """Открытые позиции плюс возраст снимка.

    Возраст обязателен и отдаётся всегда: нереализованный P&L, показанный как
    текущий, но снятый десять минут назад, — худшее, что может показать
    дневник. Пустой список при свежей отметке значит «позиций нет», при
    отсутствующей — «мы не спрашивали», и это разные вещи.
    """
    rows = conn.execute(
        "SELECT * FROM open_positions ORDER BY symbol, position_idx"
    ).fetchall()
    taken = db.get_meta(conn, "positions_at")
    return {
        "taken_at": int(taken) if taken else None,
        "positions": [
            {
                "symbol": r["symbol"],
                "direction": r["direction"],
                "qty": float(db.dec(r["qty"])),
                "avg_entry": float(db.dec(r["avg_entry"])),
                "mark_price": float(db.dec(r["mark_price"])) if r["mark_price"] else None,
                "leverage": float(db.dec(r["leverage"])) if r["leverage"] else None,
                "unrealised": float(db.dec(r["unrealised"])) if r["unrealised"] else None,
                "liq_price": float(db.dec(r["liq_price"])) if r["liq_price"] else None,
                "position_value": (float(db.dec(r["position_value"]))
                                   if r["position_value"] else None),
                "opened_at": r["opened_at"],
            }
            for r in rows
        ],
    }


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not serializable: {type(value)}")


class Handler(BaseHTTPRequestHandler):
    db_path = db.DB_PATH
    protocol_version = "HTTP/1.1"

    # Режим Mini App: страница доступна публично, поэтому каждый вызов API
    # обязан предъявить подпись Telegram. Пустой токен = локальный режим.
    miniapp = False
    bot_token = ""
    owner_id = 0

    # Состояние фоновой синхронизации, если она живёт в этом же процессе
    # (режим приложения на маке). None — синка здесь нет, и интерфейс судит
    # о свежести по отметке в базе, как на сервере.
    sync_state = None

    # --- инфраструктура ----------------------------------------------------

    def log_message(self, *args):  # тишина в терминале вместо access-лога
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         CSP_MINIAPP if self.miniapp else CSP)
        if self.miniapp:
            self.send_header("Strict-Transport-Security", "max-age=31536000")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """В режиме Mini App пускает только владельца с валидной подписью."""
        if not self.miniapp:
            return True
        try:
            webauth.validate(
                self.headers.get("X-Init-Data", ""), self.bot_token, self.owner_id
            )
            return True
        except webauth.AuthError as exc:
            self._json({"error": str(exc)}, 401)
            return False

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=_jsonable, ensure_ascii=False).encode()
        self._send(status, body, "application/json; charset=utf-8")

    def _query(self) -> dict:
        return parse_qs(urlparse(self.path).query)

    def _days(self) -> int:
        try:
            return max(0, int(self._query().get("days", ["0"])[0]))
        except ValueError:
            return 0

    def _asset(self) -> str | None:
        """Класс актива из query-строки. Незнакомое значение = без отбора.

        Молча показать всё честнее, чем показать пусто: пустой дневник читается
        как «сделок нет», а не как «в адресе опечатка».
        """
        asset = self._query().get("asset", [""])[0]
        return asset if asset in ("crypto", "tradfi") else None

    # Порядок строк в таблице сделок. Ключи приходят от интерфейса, а SQL берётся
    # отсюда: подставлять присланное в ORDER BY нельзя ни при каких проверках.
    #
    # `net_pnl` хранится текстом (Decimal), поэтому CAST обязателен. Ломается
    # при этом не убыток, а прибыль: у отрицательных чисел лексический порядок
    # случайно совпадает с числовым («-58» < «-9» и как строки, и как числа), а
    # у положительных нет — строкой «9» больше «100», и лучшей сделкой периода
    # объявлялась бы девятидолларовая вместо стодолларовой.
    #
    # В сортировке по деньгам открытые сделки уходят вниз (`net_pnl IS NULL`
    # первым ключом): итога у них нет, и держать их сверху значило бы отвечать
    # прочерком на вопрос «где мой лучший результат». В порядке по времени они,
    # наоборот, закреплены сверху — там они самые важные.
    ORDERS = {
        "date": ("rt.closed_at IS NULL DESC,"
                 " COALESCE(rt.closed_at, rt.opened_at) DESC"),
        "profit": "rt.net_pnl IS NULL, CAST(rt.net_pnl AS REAL) DESC",
        "loss": "rt.net_pnl IS NULL, CAST(rt.net_pnl AS REAL) ASC",
    }

    def _order(self) -> str:
        return self.ORDERS.get(self._query().get("sort", [""])[0], self.ORDERS["date"])

    def _range(self) -> tuple[int | None, int | None]:
        """Произвольный отрезок в миллисекундах. Пусто — значит период из days."""
        def bound(name):
            raw = self._query().get(name, [""])[0]
            try:
                return int(raw) if raw else None
            except ValueError:
                return None
        return bound("from"), bound("to")

    def _payload(self) -> dict | None:
        """Тело POST как словарь. None — значит уже ответили 400."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("ожидался объект")
            return data
        except (ValueError, json.JSONDecodeError):
            self._json({"error": "bad request"}, 400)
            return None

    # --- маршруты ------------------------------------------------------------

    def do_GET(self):
        route = urlparse(self.path).path
        if route == "/" and self.miniapp:
            self._send(200, (WEB_DIR / "miniapp.html").read_bytes(),
                       "text/html; charset=utf-8")
        elif route in STATIC:
            name, content_type = STATIC[route]
            self._send(200, (WEB_DIR / name).read_bytes(), content_type)
        elif route == "/api/summary":
            if self._authorized():
                self._api_summary()
        elif route == "/api/trades":
            if self._authorized():
                self._api_trades()
        elif route == "/api/tags":
            if self._authorized():
                self._api_tags()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/note":
            if self._authorized():
                self._api_note()
        elif route == "/api/tag":
            if self._authorized():
                self._api_tag()
        elif route == "/api/mark":
            if self._authorized():
                self._api_mark()
        else:
            self._json({"error": "not found"}, 404)

    # --- ручки ---------------------------------------------------------------

    def _api_summary(self):
        days = self._days()
        since, until = self._range()
        # Класс актива едет вместе с границами периода: у всех функций
        # статистики он такой же именованный параметр, и держать его отдельно
        # значило бы забыть про него ровно в одной из десяти строк ниже.
        bounds = {"since": since, "until": until, "asset": self._asset()}
        conn = db.connect(self.db_path)
        try:
            overall = stats.summary(conn, days, **bounds)
            payload = {
                "summary": overall,
                "top_trades": stats.top_trades(conn, days, **bounds),
                "holding": stats.holding_time(conn, days, **bounds),
                "r": {key: value
                      for key, value in stats.r_multiples(conn, days, **bounds).items()
                      if key != "values"},
                # Разобранность из дашборда убрана по просьбе владельца: цифра
                # висела заголовочной и ни на что не влияла. Сама метрика жива
                # и считается в `journal coverage`, отчёте и боте.
                "freshness": stats.freshness(conn),
                "rules": stats.tag_stats(conn, "rule", days, **bounds),
                "reasons": stats.tag_stats(conn, "reason", days, **bounds),
                "series": stats.series(conn, days, **bounds),
                "open": _open_positions(conn),
                "sync": dict(self.sync_state) if self.sync_state else None,
                "sample_note": stats.sample_note(overall.get("n", 0)),
            }
        finally:
            conn.close()
        self._json(payload)

    def _api_trades(self):
        days = self._days()
        since, until = self._range()
        query = self._query()
        pending_only = query.get("pending", ["0"])[0] == "1"
        conn = db.connect(self.db_path)
        try:
            # То же условие, что и у остальных видов: одна формулировка на всех.
            where, params = stats.trade_scope(
                days, since=since, until=until, asset=self._asset())
            if pending_only:
                where += f" AND NOT {journal.reviewed_sql('rt')}"

            # Поиск по тикеру. Подстрокой, а не по точному совпадению: набрать
            # «btc» и получить BTCUSDT — то, чего ждёшь от поиска. LIKE у SQLite
            # для латиницы регистронезависим, а тикеры латиницей и записаны.
            if symbol := query.get("symbol", [""])[0].strip():
                where += " AND rt.symbol LIKE ?"
                params.append(f"%{symbol}%")

            # Отбор по отметке: «покажи сделки, где я нарушил вот это правило».
            # broken = 1 обязателен — снятая галочка остаётся строкой (журнал
            # ездит в обе стороны, физически ничего не удаляется).
            for kind, table, column in (("rule", "rule_violations", "rule_id"),
                                        ("reason", "trade_reasons", "reason_id")):
                if tag_id := query.get(kind, [""])[0].strip():
                    where += (f" AND rt.trade_id IN (SELECT trade_id FROM {table}"
                              f" WHERE {column} = ? AND broken = 1)")
                    params.append(tag_id)
            rows = conn.execute(
                "SELECT rt.trade_id, rt.symbol, rt.direction, rt.qty, rt.avg_entry,"
                "       rt.avg_exit, rt.gross_pnl, rt.fees, rt.funding, rt.net_pnl,"
                "       rt.opened_at, rt.closed_at, rt.liquidated, rt.fees_source,"
                "       rt.source,"
                "       rt.leverage, rt.entry_value,"
                "       n.body AS note, i.intent_id, i.thesis, i.planned_stop,"
                "       i.match_note,"
                f"      {db.ASSET_CLASS_SQL} AS asset_class,"
                # Разобранность считает SQL, а не JS: определение одно на весь
                # проект (journal.reviewed_sql), и вторая копия в браузере
                # разошлась бы с ним незаметно — обе выглядели бы исправными.
                f"      {journal.reviewed_sql('rt')} AS reviewed,"
                # Разбор, написанный до того, как стал известен исход. Считается
                # здесь, а не в JS: правило одно, а оболочек две, и вторая копия
                # разъехалась бы с первой на первой же правке.
                "       CASE WHEN n.body IS NULL THEN 0"
                "            WHEN rt.closed_at IS NULL THEN 1"
                "            WHEN n.updated_at < rt.closed_at THEN 1"
                "            ELSE 0 END AS note_before_close"
                " FROM round_trips rt"
                " LEFT JOIN notes n ON n.trade_id = rt.trade_id AND n.body <> ''"
                " LEFT JOIN intents i ON i.matched_trade_id = rt.trade_id"
                " LEFT JOIN symbols s ON s.symbol = rt.symbol"
                f" WHERE {where} ORDER BY {self._order()}",
                params,
            ).fetchall()
            broken = journal.marks_by_trade(conn, "rule")
            applied = journal.marks_by_trade(conn, "reason")
            trades = []
            for r in rows:
                trades.append({
                    "violations": broken.get(r["trade_id"], []),
                    "reasons": applied.get(r["trade_id"], []),
                    "trade_id": r["trade_id"],
                    "symbol": r["symbol"],
                    "direction": r["direction"],
                    "qty": float(db.dec(r["qty"])),
                    "avg_entry": float(db.dec(r["avg_entry"])),
                    "avg_exit": float(db.dec(r["avg_exit"])) if r["avg_exit"] else None,
                    "fees": float(db.dec(r["fees"])),
                    "funding": float(db.dec(r["funding"])),
                    "net_pnl": float(db.dec(r["net_pnl"])) if r["net_pnl"] else None,
                    "opened_at": r["opened_at"],
                    "closed_at": r["closed_at"],
                    "liquidated": bool(r["liquidated"]),
                    "asset_class": r["asset_class"],
                    "source": r["source"],
                    "reviewed": bool(r["reviewed"]),
                    "note_before_close": bool(r["note_before_close"]),
                    "fees_source": r["fees_source"],
                    "leverage": float(db.dec(r["leverage"])) if r["leverage"] else None,
                    "roi": stats.roi(r["net_pnl"], r["entry_value"], r["leverage"]),
                    "note": r["note"],
                    "has_intent": r["intent_id"] is not None,
                    "thesis": r["thesis"],
                    "planned_stop": r["planned_stop"],
                    "match_note": r["match_note"],
                })
        finally:
            conn.close()
        self._json({"trades": trades})

    def _kind(self, data=None) -> str | None:
        """Правило или основание. Вид приходит явно, умолчания нет."""
        source = data if data is not None else {
            k: v[0] for k, v in self._query().items()
        }
        kind = source.get("kind")
        if kind not in journal.KINDS:
            self._json({"error": "unknown kind"}, 400)
            return None
        return kind

    def _api_tags(self):
        kind = self._kind()
        if kind is None:
            return
        _, id_column, _, _ = journal.KINDS[kind]
        conn = db.connect(self.db_path)
        try:
            payload = {"tags": [
                {
                    "id": r[id_column],
                    "body": r["body"],
                    "active": bool(r["active"]),
                    "created_at": r["created_at"],
                }
                for r in journal.tags(conn, kind, include_archived=True)
            ]}
        finally:
            conn.close()
        self._json(payload)

    def _api_tag(self):
        """Создать (без id), поправить текст или сдать в архив."""
        data = self._payload()
        if data is None:
            return
        kind = self._kind(data)
        if kind is None:
            return

        conn = db.connect(self.db_path)
        try:
            try:
                if data.get("id"):
                    updated = journal.edit_tag(
                        conn, kind, data["id"],
                        body=data.get("body"), active=data.get("active"),
                    )
                    if not updated:
                        self._json({"error": "unknown tag"}, 404)
                        return
                    tag_id = data["id"]
                else:
                    tag_id = journal.add_tag(conn, kind, data.get("body", ""))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            payload = {"ok": True, "id": tag_id}
        finally:
            conn.close()
        self._json(payload)

    def _api_mark(self):
        data = self._payload()
        if data is None:
            return
        kind = self._kind(data)
        if kind is None:
            return
        trade_id, tag_id = data.get("trade_id"), data.get("id")
        if not trade_id or not tag_id:
            self._json({"error": "bad request"}, 400)
            return

        table, id_column, _, _ = journal.KINDS[kind]
        conn = db.connect(self.db_path)
        try:
            known = conn.execute(
                "SELECT 1 FROM round_trips WHERE trade_id = ?", (trade_id,)
            ).fetchone() and conn.execute(
                f"SELECT 1 FROM {table} WHERE {id_column} = ?", (tag_id,)
            ).fetchone()
            if not known:
                self._json({"error": "unknown trade or tag"}, 404)
                return
            journal.set_mark(conn, kind, trade_id, tag_id, bool(data.get("on")))
            payload = {"ok": True}
        finally:
            conn.close()
        self._json(payload)

    def _api_note(self):
        data = self._payload()
        if data is None:
            return
        try:
            trade_id, body = data["trade_id"], data.get("body", "")
        except KeyError:
            self._json({"error": "bad request"}, 400)
            return

        conn = db.connect(self.db_path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM round_trips WHERE trade_id = ?", (trade_id,)
            ).fetchone()
            if not exists:
                self._json({"error": "unknown trade"}, 404)
                return
            # Очистка разбора пишет пустое тело, а не удаляет строку: журнал
            # синхронизируется в обе стороны, и удалённая строка вернулась бы
            # с другой стороны первым же кругом.
            journal.add_note(conn, trade_id, body.strip())
            payload = {"ok": True}
        finally:
            conn.close()
        self._json(payload)


def serve(port: int = 8321, db_path: Path = db.DB_PATH, *, miniapp: bool = False,
          bot_token: str = "", owner_id: int = 0, host: str = "127.0.0.1") -> None:
    # Ошибка настройки ловится до того, как что-то настроено: иначе Handler уже
    # переведён в режим Mini App, а исключение летит следом.
    if miniapp and not (bot_token and owner_id):
        raise ValueError(
            "режим Mini App без токена и владельца открыл бы историю торговли всем"
        )

    Handler.db_path = db_path
    Handler.miniapp = miniapp
    Handler.bot_token = bot_token
    Handler.owner_id = owner_id

    # По умолчанию 127.0.0.1, а не 0.0.0.0: дневник с историей счёта не должен
    # быть виден даже в локальной сети. Mini App слушает шире, но за HTTPS-прокси
    # и с обязательной проверкой подписи на каждом вызове API.
    httpd = ThreadingHTTPServer((host, port), Handler)
    where = "Mini App" if miniapp else "Дневник"
    print(f"{where}: http://{host}:{port}/  (Ctrl+C — остановить)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.", flush=True)
    finally:
        httpd.server_close()
