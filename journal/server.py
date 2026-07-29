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
        elif route == "/api/rules":
            if self._authorized():
                self._api_rules()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/note":
            if self._authorized():
                self._api_note()
        elif route == "/api/rule":
            if self._authorized():
                self._api_rule()
        elif route == "/api/violation":
            if self._authorized():
                self._api_violation()
        else:
            self._json({"error": "not found"}, 404)

    # --- ручки ---------------------------------------------------------------

    def _api_summary(self):
        days = self._days()
        conn = db.connect(self.db_path)
        try:
            payload = {
                "summary": stats.summary(conn, days),
                "top_trades": stats.top_trades(conn, days),
                "holding": stats.holding_time(conn, days),
                "r": {key: value for key, value in stats.r_multiples(conn, days).items()
                      if key != "values"},
                "coverage": journal.coverage(conn),
                "freshness": stats.freshness(conn),
                "rules": stats.rule_stats(conn, days),
                "sync": dict(self.sync_state) if self.sync_state else None,
                "sample_note": stats.sample_note(stats.summary(conn, days).get("n", 0)),
            }
        finally:
            conn.close()
        self._json(payload)

    def _api_trades(self):
        days = self._days()
        pending_only = self._query().get("pending", ["0"])[0] == "1"
        conn = db.connect(self.db_path)
        try:
            where, params = "rt.closed_at IS NOT NULL", []
            if days:
                where += (" AND rt.closed_at >="
                          " (SELECT MAX(closed_at) FROM round_trips) - ?")
                params.append(days * stats.DAY_MS)
            if pending_only:
                where += (" AND n.body IS NULL AND i.intent_id IS NULL")
            rows = conn.execute(
                "SELECT rt.trade_id, rt.symbol, rt.direction, rt.qty, rt.avg_entry,"
                "       rt.avg_exit, rt.gross_pnl, rt.fees, rt.funding, rt.net_pnl,"
                "       rt.opened_at, rt.closed_at, rt.liquidated, rt.fees_source,"
                "       n.body AS note, i.intent_id, i.thesis, i.planned_stop,"
                "       i.match_note"
                " FROM round_trips rt"
                " LEFT JOIN notes n ON n.trade_id = rt.trade_id AND n.body <> ''"
                " LEFT JOIN intents i ON i.matched_trade_id = rt.trade_id"
                f" WHERE {where} ORDER BY rt.closed_at DESC",
                params,
            ).fetchall()
            broken = journal.violations_by_trade(conn)
            trades = []
            for r in rows:
                trades.append({
                    "violations": broken.get(r["trade_id"], []),
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
                    "fees_source": r["fees_source"],
                    "note": r["note"],
                    "has_intent": r["intent_id"] is not None,
                    "thesis": r["thesis"],
                    "planned_stop": r["planned_stop"],
                    "match_note": r["match_note"],
                })
        finally:
            conn.close()
        self._json({"trades": trades})

    def _api_rules(self):
        conn = db.connect(self.db_path)
        try:
            payload = {"rules": [
                {
                    "rule_id": r["rule_id"],
                    "body": r["body"],
                    "active": bool(r["active"]),
                    "created_at": r["created_at"],
                }
                for r in journal.rules(conn, include_archived=True)
            ]}
        finally:
            conn.close()
        self._json(payload)

    def _api_rule(self):
        """Создать правило (без rule_id), поправить текст или сдать в архив."""
        data = self._payload()
        if data is None:
            return

        conn = db.connect(self.db_path)
        try:
            try:
                if data.get("rule_id"):
                    updated = journal.edit_rule(
                        conn, data["rule_id"],
                        body=data.get("body"), active=data.get("active"),
                    )
                    if not updated:
                        self._json({"error": "unknown rule"}, 404)
                        return
                    rule_id = data["rule_id"]
                else:
                    rule_id = journal.add_rule(conn, data.get("body", ""))
            except ValueError as exc:
                self._json({"error": str(exc)}, 400)
                return
            payload = {"ok": True, "rule_id": rule_id}
        finally:
            conn.close()
        self._json(payload)

    def _api_violation(self):
        data = self._payload()
        if data is None:
            return
        trade_id, rule_id = data.get("trade_id"), data.get("rule_id")
        if not trade_id or not rule_id:
            self._json({"error": "bad request"}, 400)
            return

        conn = db.connect(self.db_path)
        try:
            known = conn.execute(
                "SELECT 1 FROM round_trips WHERE trade_id = ?", (trade_id,)
            ).fetchone() and conn.execute(
                "SELECT 1 FROM rules WHERE rule_id = ?", (rule_id,)
            ).fetchone()
            if not known:
                self._json({"error": "unknown trade or rule"}, 404)
                return
            journal.set_violation(conn, trade_id, rule_id, bool(data.get("broken")))
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
            payload = {"ok": True, "coverage": journal.coverage(conn)}
        finally:
            conn.close()
        self._json(payload)


def serve(port: int = 8321, db_path: Path = db.DB_PATH, *, miniapp: bool = False,
          bot_token: str = "", owner_id: int = 0, host: str = "127.0.0.1") -> None:
    Handler.db_path = db_path
    Handler.miniapp = miniapp
    Handler.bot_token = bot_token
    Handler.owner_id = owner_id

    if miniapp and not (bot_token and owner_id):
        raise ValueError(
            "режим Mini App без токена и владельца открыл бы историю торговли всем"
        )

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
