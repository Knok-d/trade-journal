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

from . import db, journal, stats

WEB_DIR = Path(__file__).parent / "web"

# Статика отдаётся по белому списку имён — никакого маппинга путей в
# файловую систему, а значит и path traversal невозможен по построению.
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'"
)


def _jsonable(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not serializable: {type(value)}")


class Handler(BaseHTTPRequestHandler):
    db_path = db.DB_PATH
    protocol_version = "HTTP/1.1"

    # --- инфраструктура ----------------------------------------------------

    def log_message(self, *args):  # тишина в терминале вместо access-лога
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

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

    # --- маршруты ------------------------------------------------------------

    def do_GET(self):
        route = urlparse(self.path).path
        if route in STATIC:
            name, content_type = STATIC[route]
            self._send(200, (WEB_DIR / name).read_bytes(), content_type)
        elif route == "/api/summary":
            self._api_summary()
        elif route == "/api/trades":
            self._api_trades()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        route = urlparse(self.path).path
        if route == "/api/note":
            self._api_note()
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
                " LEFT JOIN notes n ON n.trade_id = rt.trade_id"
                " LEFT JOIN intents i ON i.matched_trade_id = rt.trade_id"
                f" WHERE {where} ORDER BY rt.closed_at DESC",
                params,
            ).fetchall()
            trades = []
            for r in rows:
                trades.append({
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

    def _api_note(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            trade_id, body = data["trade_id"], data.get("body", "")
        except (ValueError, KeyError, json.JSONDecodeError):
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
            if body.strip():
                journal.add_note(conn, trade_id, body.strip())
            else:
                conn.execute("DELETE FROM notes WHERE trade_id = ?", (trade_id,))
                conn.commit()
            payload = {"ok": True, "coverage": journal.coverage(conn)}
        finally:
            conn.close()
        self._json(payload)


def serve(port: int = 8321, db_path: Path = db.DB_PATH) -> None:
    Handler.db_path = db_path
    # 127.0.0.1, не 0.0.0.0: дневник с историей счёта не должен быть виден
    # даже в локальной сети.
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Дневник: http://127.0.0.1:{port}/  (Ctrl+C — остановить)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        httpd.server_close()
