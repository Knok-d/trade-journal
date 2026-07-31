"""API локального сервера: статика, сводка, сделки, сохранение разбора.

Сервер поднимается на 127.0.0.1:0 (случайный порт) с временной БД —
ни ключей, ни сети наружу.
"""

import json
import sys
import tempfile
import threading
import time
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
        self.assertNotIn("coverage", data,
                         "разобранность из дашборда убрана — не должна и ездить")

    def test_trades_listing(self):
        _, _, body = self._get("/api/trades?days=0")
        trades = json.loads(body)["trades"]
        self.assertEqual(len(trades), 2)
        self.assertEqual({t["symbol"] for t in trades}, {"BTCUSDT", "SOLUSDT"})

    def test_note_roundtrip_moves_the_trade_out_of_pending_and_back(self):
        status, _ = self._post(
            "/api/note", {"trade_id": self.trade_id, "body": "тест разбора"})
        self.assertEqual(status, 200)

        _, _, body = self._get("/api/trades?days=0&pending=1")
        pending = json.loads(body)["trades"]
        self.assertEqual([t["symbol"] for t in pending], ["SOLUSDT"],
                         "разобранная сделка должна уйти из pending")

        # Очистка разбора возвращает сделку в pending: пустое тело — это
        # стирание, а не «разобрано пустотой».
        self._post("/api/note", {"trade_id": self.trade_id, "body": ""})
        _, _, body = self._get("/api/trades?days=0&pending=1")
        self.assertEqual(
            {t["symbol"] for t in json.loads(body)["trades"]},
            {"BTCUSDT", "SOLUSDT"})

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

    def test_reason_alone_marks_the_trade_reviewed(self):
        """Основание без текста — тоже разбор: сделка уходит из «без разбора»."""
        _, payload = self._post("/api/tag", {"kind": "reason", "body": "пробой"})
        tag_id = payload["id"]
        mark = {"kind": "reason", "trade_id": self.trade_id, "id": tag_id}
        self._post("/api/mark", {**mark, "on": True})
        try:
            _, _, body = self._get("/api/trades?days=0")
            trade = [t for t in json.loads(body)["trades"]
                     if t["trade_id"] == self.trade_id][0]
            self.assertTrue(trade["reviewed"])
            self.assertIsNone(trade["note"], "текста нет — разбор только галочкой")

            _, _, body = self._get("/api/trades?days=0&pending=1")
            self.assertNotIn(self.trade_id,
                             [t["trade_id"] for t in json.loads(body)["trades"]])
        finally:
            # Фикстура одна на класс: снятая галочка возвращает состояние назад.
            self._post("/api/mark", {**mark, "on": False})

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


class OpenTradeAndAssetTest(unittest.TestCase):
    """Разбор на живой позиции и разделение крипты с TradFi.

    Открытая сделка — единственная строка в дневнике, которую нельзя прятать
    за фильтром периода: разбирать её надо, пока она открыта, а не когда стало
    поздно.
    """

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = Path(cls.tmp.name) / "open.db"

        conn = db.connect(db_path)
        db.save_executions(conn, [
            # Закрытая крипта — свежая, задаёт край окна для относительного периода.
            fill("c1", "BTCUSDT", "Buy", "100", "1", 199 * HOUR),
            fill("c2", "BTCUSDT", "Sell", "110", "1", 200 * HOUR),
            # Открытое золото — вход давно, выхода нет вовсе.
            fill("o1", "XAUUSDT", "Buy", "3000", "1", 10 * HOUR),
        ])
        roundtrips.rebuild(conn)
        db.save_symbols(conn, [
            {"symbol": "BTCUSDT", "symbolType": ""},
            {"symbol": "XAUUSDT", "symbolType": "commodity"},
        ])
        cls.open_id = conn.execute(
            "SELECT trade_id FROM round_trips WHERE closed_at IS NULL"
        ).fetchone()["trade_id"]
        cls.closed_id = conn.execute(
            "SELECT trade_id FROM round_trips WHERE closed_at IS NOT NULL"
        ).fetchone()["trade_id"]
        conn.close()

        server.Handler.db_path = db_path
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.db_path = db_path
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def _trades(self, query=""):
        with urllib.request.urlopen(
                f"http://127.0.0.1:{self.port}/api/trades{query}") as r:
            return json.loads(r.read())["trades"]

    def _note(self, trade_id, body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/note",
            data=json.dumps({"trade_id": trade_id, "body": body}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as r:
            return r.status

    def test_open_trade_comes_first(self):
        trades = self._trades("?days=0")
        self.assertIsNone(trades[0]["closed_at"], "открытая обязана быть сверху")
        self.assertEqual(trades[0]["symbol"], "XAUUSDT")

    def test_open_trade_survives_a_narrow_period(self):
        """Позиция, открытая месяц назад и живая сейчас, не должна пропадать."""
        symbols = [t["symbol"] for t in self._trades("?days=1")]
        self.assertIn("XAUUSDT", symbols)
        self.assertIn("BTCUSDT", symbols)

    def test_open_trade_survives_an_explicit_range(self):
        window = "?from=%d&to=%d" % (198 * HOUR, 201 * HOUR)
        symbols = [t["symbol"] for t in self._trades(window)]
        self.assertEqual(symbols, ["XAUUSDT", "BTCUSDT"])

    def test_open_trade_has_no_invented_numbers(self):
        """Чего нет — того нет: у живой позиции ни выхода, ни итога."""
        live = self._trades("?days=0")[0]
        self.assertIsNone(live["net_pnl"])
        self.assertIsNone(live["avg_exit"])
        self.assertIsNone(live["roi"])

    def test_note_on_an_open_trade_is_marked_as_written_before_the_outcome(self):
        self.assertEqual(self._note(self.open_id, "жду 3100, стоп под 2950"), 200)
        live = [t for t in self._trades("?days=0") if t["trade_id"] == self.open_id][0]
        self.assertEqual(live["note"], "жду 3100, стоп под 2950")
        self.assertTrue(live["note_before_close"])

    def test_note_written_after_the_close_is_not_marked(self):
        self._note(self.closed_id, "задним числом")
        closed = [t for t in self._trades("?days=0")
                  if t["trade_id"] == self.closed_id][0]
        self.assertFalse(closed["note_before_close"],
                         "иначе поздний текст выдавался бы за ранний")

    def _set_closed_at(self, at_ms):
        conn = db.connect(self.db_path)
        try:
            conn.execute("UPDATE round_trips SET closed_at = ? WHERE trade_id = ?",
                         (at_ms, self.closed_id))
            conn.commit()
        finally:
            conn.close()

    def test_mark_survives_the_close(self):
        """Разбор написан на живой позиции, потом сделка закрылась — метка остаётся."""
        self._note(self.closed_id, "написано до выхода")
        # Закрытие сдвигается в будущее относительно заметки: ровно так выглядит
        # сделка, разобранная до того, как стал известен исход. Состояние
        # возвращается назад — фикстура на весь класс одна, и следующий тест
        # получил бы сделку, закрытую завтра.
        self._set_closed_at(int(time.time() * 1000) + HOUR)
        try:
            closed = [t for t in self._trades("?days=0")
                      if t["trade_id"] == self.closed_id][0]
            self.assertTrue(closed["note_before_close"])
        finally:
            self._set_closed_at(200 * HOUR)

    def test_search_by_ticker_matches_a_substring(self):
        """Набрал «xau» — получил XAUUSDT: точного совпадения от поиска не ждут."""
        self.assertEqual([t["symbol"] for t in self._trades("?days=0&symbol=xau")],
                         ["XAUUSDT"])
        self.assertEqual([t["symbol"] for t in self._trades("?days=0&symbol=USDT")],
                         ["XAUUSDT", "BTCUSDT"])
        self.assertEqual(self._trades("?days=0&symbol=zzz"), [])

    def test_sort_by_profit_and_by_loss_are_mirror_images(self):
        best = self._trades("?days=0&sort=profit")
        worst = self._trades("?days=0&sort=loss")
        closed = [t for t in best if t["closed_at"] is not None]
        self.assertEqual([t["trade_id"] for t in closed],
                         list(reversed([t["trade_id"] for t in worst
                                        if t["closed_at"] is not None])))

    def test_open_trade_sinks_in_a_money_sort(self):
        """Итога у неё нет, и держать её сверху значило бы отвечать прочерком
        на вопрос «где мой лучший результат»."""
        for sort in ("profit", "loss"):
            with self.subTest(sort=sort):
                rows = self._trades(f"?days=0&sort={sort}")
                self.assertIsNone(rows[-1]["closed_at"])
        self.assertIsNone(self._trades("?days=0")[0]["closed_at"],
                          "а по времени она, наоборот, закреплена сверху")

    def test_asset_filter_splits_without_losing_anything(self):
        everything = self._trades("?days=0")
        crypto = self._trades("?days=0&asset=crypto")
        tradfi = self._trades("?days=0&asset=tradfi")
        self.assertEqual(len(everything), len(crypto) + len(tradfi))
        self.assertEqual([t["symbol"] for t in tradfi], ["XAUUSDT"])
        self.assertEqual([t["symbol"] for t in crypto], ["BTCUSDT"])

    def test_asset_class_travels_with_every_trade(self):
        by_symbol = {t["symbol"]: t["asset_class"] for t in self._trades("?days=0")}
        self.assertEqual(by_symbol, {"XAUUSDT": "commodity", "BTCUSDT": "crypto"})

    def test_unknown_asset_value_shows_everything(self):
        """Пустой дневник читался бы как «сделок нет», а не как опечатка в адресе."""
        self.assertEqual(len(self._trades("?days=0&asset=nonsense")), 2)


class MoneyOrderTest(unittest.TestCase):
    """Сортировка по деньгам сравнивает числа, а не строки.

    `net_pnl` хранится текстом (Decimal через TEXT — деньги во float считать
    нельзя), и без CAST сортировка идёт лексикографически.

    Ломается при этом ПРИБЫЛЬ, а не убыток, и это стоит знать, прежде чем
    какой-нибудь из этих тестов покажется лишним: у отрицательных чисел
    лексический порядок случайно совпадает с числовым, поэтому «худший убыток
    первым» проходит и без CAST. А вот строкой «9» больше «100» — и лучшей
    сделкой периода объявлялась бы девятидолларовая. Проверено: без CAST
    падает ровно `test_best_profit_comes_first`.

    Проверяется сам кусок SQL, без сервера: он тут и есть предмет.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "order.db")
        for symbol, net in (("A", "-9"), ("B", "-58"), ("C", "100"),
                            ("D", "9"), ("E", None)):
            self.conn.execute(
                "INSERT INTO round_trips (trade_id, category, symbol, position_idx,"
                " direction, opened_at, closed_at, qty, avg_entry, fees, funding,"
                " net_pnl) VALUES (?,'linear',?,0,'long',1,?,'1','1','0','0',?)",
                (symbol, symbol, None if net is None else 2, net))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _order(self, key):
        return [r["symbol"] for r in self.conn.execute(
            "SELECT rt.symbol FROM round_trips rt"
            f" ORDER BY {server.Handler.ORDERS[key]}")]

    def test_worst_loss_comes_first(self):
        self.assertEqual(self._order("loss")[0], "B", "-58 хуже, чем -9")

    def test_best_profit_comes_first(self):
        self.assertEqual(self._order("profit")[0], "C", "100 больше, чем 9")

    def test_open_trade_is_last_in_both_money_orders(self):
        for key in ("loss", "profit"):
            with self.subTest(key=key):
                self.assertEqual(self._order(key)[-1], "E")


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
