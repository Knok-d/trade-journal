"""API локального сервера: статика, сводка, сделки, сохранение разбора.

Сервер поднимается на 127.0.0.1:0 (случайный порт) с временной БД —
ни ключей, ни сети наружу.
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import db, roundtrips, server  # noqa: E402
from tests.test_roundtrips import fill  # noqa: E402

HOUR = 3_600_000


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "test.db"

        conn = db.connect(cls.db_path)
        db.save_executions(conn, [
            fill("e1", "BTCUSDT", "Buy", "100", "1", 10 * HOUR, fee="0.1"),
            fill("e2", "BTCUSDT", "Sell", "110", "1", 11 * HOUR, fee="0.1"),
            fill("s1", "SOLUSDT", "Sell", "10", "10", 20 * HOUR),
            fill("s2", "SOLUSDT", "Buy", "9", "10", 21 * HOUR),
        ])
        roundtrips.rebuild(conn)
        cls.trade_id = conn.execute(
            "SELECT trade_id FROM round_trips WHERE symbol='BTCUSDT'"
        ).fetchone()["trade_id"]
        conn.close()

        server.Handler.db_path = cls.db_path
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _get(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as r:
            return r.status, r.headers, r.read()

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_static_pages_have_csp(self):
        status, headers, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Trade Journal", body)
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])

    def test_unknown_path_is_404_not_file(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/../journal/keychain.py")
        self.assertEqual(ctx.exception.code, 404)
        ctx.exception.close()

    def test_summary_has_honesty_fields(self):
        _, _, body = self._get("/api/summary?days=0")
        data = json.loads(body)
        self.assertEqual(data["summary"]["n"], 2)
        self.assertIn("win_rate_ci", data["summary"])
        self.assertFalse(data["r"]["available"], "R без стопов должен быть недоступен")
        self.assertIn("annotated", data["coverage"])

    def test_trades_listing(self):
        _, _, body = self._get("/api/trades?days=0")
        trades = json.loads(body)["trades"]
        self.assertEqual(len(trades), 2)
        self.assertEqual({t["symbol"] for t in trades}, {"BTCUSDT", "SOLUSDT"})

    def test_note_roundtrip_updates_coverage_and_pending(self):
        status, payload = self._post(
            "/api/note", {"trade_id": self.trade_id, "body": "тест разбора"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["coverage"]["annotated"], 1)

        _, _, body = self._get("/api/trades?days=0&pending=1")
        pending = json.loads(body)["trades"]
        self.assertEqual([t["symbol"] for t in pending], ["SOLUSDT"],
                         "разобранная сделка должна уйти из pending")

        # очистка заметки возвращает сделку в pending
        status, payload = self._post("/api/note", {"trade_id": self.trade_id, "body": ""})
        self.assertEqual(payload["coverage"]["annotated"], 0)

    def test_note_for_unknown_trade_is_404(self):
        status, _ = self._post("/api/note", {"trade_id": "nope", "body": "x"})
        self.assertEqual(status, 404)

    def test_tag_lifecycle_over_api(self):
        status, payload = self._post(
            "/api/tag", {"kind": "rule", "body": "не усредняться в убыток"})
        self.assertEqual(status, 200)
        tag_id = payload["id"]

        _, _, body = self._get("/api/tags?kind=rule")
        self.assertIn(tag_id, [r["id"] for r in json.loads(body)["tags"]])

        self._post("/api/tag",
                   {"kind": "rule", "id": tag_id, "body": "не усредняться вообще"})
        self._post("/api/tag", {"kind": "rule", "id": tag_id, "active": False})
        _, _, body = self._get("/api/tags?kind=rule")
        archived = [r for r in json.loads(body)["tags"] if r["id"] == tag_id][0]
        self.assertEqual(archived["body"], "не усредняться вообще")
        self.assertFalse(archived["active"])

    def test_reasons_live_apart_from_rules(self):
        """Основания и правила не должны смешиваться в одном списке."""
        self._post("/api/tag", {"kind": "reason", "body": "отскок от уровня"})
        _, _, rules_body = self._get("/api/tags?kind=rule")
        _, _, reasons_body = self._get("/api/tags?kind=reason")

        bodies = lambda raw: [t["body"] for t in json.loads(raw)["tags"]]
        self.assertIn("отскок от уровня", bodies(reasons_body))
        self.assertNotIn("отскок от уровня", bodies(rules_body))

    def test_unknown_kind_is_rejected(self):
        status, _ = self._post("/api/tag", {"kind": "выдумка", "body": "x"})
        self.assertEqual(status, 400)

    def test_empty_tag_is_rejected_with_400(self):
        status, _ = self._post("/api/tag", {"kind": "rule", "body": "  "})
        self.assertEqual(status, 400)

    def test_unknown_tag_edit_is_404(self):
        status, _ = self._post("/api/tag", {"kind": "rule", "id": "нет", "body": "x"})
        self.assertEqual(status, 404)

    def test_mark_appears_on_the_trade_and_can_be_cleared(self):
        _, payload = self._post(
            "/api/tag", {"kind": "reason", "body": "сигнал"})
        tag_id = payload["id"]

        status, _ = self._post(
            "/api/mark",
            {"kind": "reason", "trade_id": self.trade_id, "id": tag_id, "on": True})
        self.assertEqual(status, 200)
        self.assertEqual(self._marks_of(self.trade_id, "reasons"), [tag_id])

        self._post("/api/mark",
                   {"kind": "reason", "trade_id": self.trade_id,
                    "id": tag_id, "on": False})
        self.assertEqual(self._marks_of(self.trade_id, "reasons"), [])

    def test_mark_for_unknown_tag_is_404(self):
        status, _ = self._post(
            "/api/mark",
            {"kind": "rule", "trade_id": self.trade_id, "id": "нет", "on": True})
        self.assertEqual(status, 404)

    def test_arbitrary_date_range_filters_trades(self):
        _, _, body = self._get("/api/trades?from=0&to=%d" % (15 * HOUR))
        symbols = [t["symbol"] for t in json.loads(body)["trades"]]
        self.assertEqual(symbols, ["BTCUSDT"],
                         "в отрезок попадает только сделка, закрытая внутри него")

        _, _, body = self._get("/api/summary?from=0&to=%d" % (15 * HOUR))
        self.assertEqual(json.loads(body)["summary"]["n"], 1)

    def _marks_of(self, trade_id, field):
        _, _, body = self._get("/api/trades?days=0")
        trade = [t for t in json.loads(body)["trades"]
                 if t["trade_id"] == trade_id][0]
        return trade[field]


class MiniAppAuthTest(unittest.TestCase):
    """Режим Mini App: страница публична, поэтому API обязан требовать подпись."""

    @classmethod
    def setUpClass(cls):
        from tests.test_webauth import OWNER, TOKEN, make_init_data
        cls.make_init_data = staticmethod(make_init_data)

        cls.tmp = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmp.name) / "mini.db"
        conn = db.connect(db_path)
        db.save_executions(conn, [
            fill("m1", "BTCUSDT", "Buy", "100", "1", 10 * HOUR),
            fill("m2", "BTCUSDT", "Sell", "110", "1", 11 * HOUR),
        ])
        roundtrips.rebuild(conn)
        conn.close()

        server.Handler.db_path = db_path
        server.Handler.miniapp = True
        server.Handler.bot_token = TOKEN
        server.Handler.owner_id = OWNER
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()
        server.Handler.miniapp = False
        server.Handler.bot_token = ""
        server.Handler.owner_id = 0

    def _get(self, path, init_data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}")
        if init_data is not None:
            req.add_header("X-Init-Data", init_data)
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            e.close()
            return e.code, body

    def test_api_without_signature_is_rejected(self):
        for path in ("/api/summary", "/api/trades"):
            status, _ = self._get(path)
            self.assertEqual(status, 401, f"{path} отдался без подписи")

    def test_api_with_garbage_signature_is_rejected(self):
        status, _ = self._get("/api/summary", "user=%7B%22id%22%3A1%7D&hash=deadbeef")
        self.assertEqual(status, 401)

    def test_api_with_valid_signature_works(self):
        status, body = self._get("/api/summary", self.make_init_data())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["summary"]["n"], 1)

    def test_post_note_requires_signature(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/note",
            data=json.dumps({"trade_id": "x", "body": "y"}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                self.fail(f"заметка сохранилась без подписи: {r.status}")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 401)
            e.close()

    def test_page_is_public_but_carries_csp(self):
        """Саму страницу отдаём всем — данных в ней нет, они за API."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/")
        with urllib.request.urlopen(req) as r:
            self.assertEqual(r.status, 200)
            self.assertIn(b"Trade Journal", r.read())
            self.assertIn("telegram.org", r.headers["Content-Security-Policy"])

    def test_miniapp_without_token_refuses_to_start(self):
        with self.assertRaises(ValueError):
            server.serve(port=0, miniapp=True, bot_token="", owner_id=0)


if __name__ == "__main__":
    unittest.main()
