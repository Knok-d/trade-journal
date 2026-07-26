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


if __name__ == "__main__":
    unittest.main()
