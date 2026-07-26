"""Журнал: привязка намерений, R-multiple, покрытие.

Ключевое, что здесь проверяется, — намерение нельзя привязать к сделке,
открытой ДО него. Иначе поле «обоснование до входа» ничего не гарантирует.
"""

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import db, journal, roundtrips  # noqa: E402
from tests.test_roundtrips import fill  # noqa: E402

HOUR = 60 * 60 * 1000


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _closed_long(self, symbol="BTCUSDT", entry="100", exit_="110", opened=10 * HOUR):
        db.save_executions(self.conn, [
            fill("e1", symbol, "Buy", entry, "1", opened),
            fill("e2", symbol, "Sell", exit_, "1", opened + HOUR),
        ])
        roundtrips.rebuild(self.conn)
        return self.conn.execute("SELECT * FROM round_trips").fetchone()

    def test_intent_before_entry_is_matched(self):
        journal.add_intent(
            self.conn, "BTCUSDT", "long", "пробой уровня",
            planned_stop="95", now_ms=9 * HOUR,
        )
        trade = self._closed_long()
        result = journal.match_intents(self.conn)

        self.assertEqual(result["matched"], 1)
        intent = self.conn.execute("SELECT * FROM intents").fetchone()
        self.assertEqual(intent["matched_trade_id"], trade["trade_id"])

    def test_intent_written_after_entry_is_never_matched(self):
        """Обоснование, написанное после входа, не должно засчитываться."""
        self._closed_long(opened=10 * HOUR)
        journal.add_intent(
            self.conn, "BTCUSDT", "long", "я так и думал",
            planned_stop="95", now_ms=12 * HOUR,      # уже после входа
        )
        result = journal.match_intents(self.conn)

        self.assertEqual(result["matched"], 0)
        intent = self.conn.execute("SELECT * FROM intents").fetchone()
        self.assertIsNone(intent["matched_trade_id"])

    def test_intent_outside_window_is_not_matched(self):
        journal.add_intent(self.conn, "BTCUSDT", "long", "старая идея", now_ms=0)
        self._closed_long(opened=48 * HOUR)          # спустя двое суток
        self.assertEqual(journal.match_intents(self.conn)["matched"], 0)

    def test_wrong_direction_is_not_matched(self):
        journal.add_intent(self.conn, "BTCUSDT", "short", "жду разворот", now_ms=9 * HOUR)
        self._closed_long()
        self.assertEqual(journal.match_intents(self.conn)["matched"], 0)

    def test_ambiguous_match_is_flagged(self):
        """Два входа подряд по одному символу — привязка помечается как спорная."""
        journal.add_intent(self.conn, "ETHUSDT", "long", "лонг эфира", now_ms=1 * HOUR)
        db.save_executions(self.conn, [
            fill("a1", "ETHUSDT", "Buy", "100", "1", 2 * HOUR),
            fill("a2", "ETHUSDT", "Sell", "110", "1", 3 * HOUR),
            fill("b1", "ETHUSDT", "Buy", "120", "1", 4 * HOUR),
            fill("b2", "ETHUSDT", "Sell", "130", "1", 5 * HOUR),
        ])
        roundtrips.rebuild(self.conn)
        result = journal.match_intents(self.conn)

        self.assertEqual(result["matched"], 1)
        self.assertEqual(len(result["ambiguous"]), 1)
        intent = self.conn.execute("SELECT * FROM intents").fetchone()
        self.assertIn("кандидатов было 2", intent["match_note"])

    def test_r_multiple_requires_planned_stop(self):
        journal.add_intent(
            self.conn, "BTCUSDT", "long", "с планом", planned_stop="90", now_ms=9 * HOUR
        )
        trade = self._closed_long(entry="100", exit_="110")
        journal.match_intents(self.conn)

        # риск = |100 - 90| * 1 = 10, прибыль 10 -> R = 1
        self.assertEqual(journal.r_multiple(self.conn, trade["trade_id"]), Decimal("1"))

    def test_r_multiple_is_none_without_stop(self):
        journal.add_intent(self.conn, "BTCUSDT", "long", "без стопа", now_ms=9 * HOUR)
        trade = self._closed_long()
        journal.match_intents(self.conn)

        self.assertIsNone(
            journal.r_multiple(self.conn, trade["trade_id"]),
            "без записанного заранее стопа R не существует",
        )

    def test_coverage_counts_notes_and_intents(self):
        """Решение C: «разобрана» = заметка ИЛИ намерение; pre-trade — подпоказатель."""
        journal.add_intent(self.conn, "BTCUSDT", "long", "есть", planned_stop="95",
                           now_ms=9 * HOUR)
        self._closed_long()
        db.save_executions(self.conn, [
            fill("x1", "SOLUSDT", "Buy", "10", "1", 20 * HOUR),
            fill("x2", "SOLUSDT", "Sell", "11", "1", 21 * HOUR),
            fill("y1", "ADAUSDT", "Buy", "5", "1", 30 * HOUR),
            fill("y2", "ADAUSDT", "Sell", "6", "1", 31 * HOUR),
        ])
        roundtrips.rebuild(self.conn)
        journal.match_intents(self.conn)

        # SOLUSDT разбирается постфактум — основной режим продукта
        sol = self.conn.execute(
            "SELECT trade_id FROM round_trips WHERE symbol='SOLUSDT'"
        ).fetchone()["trade_id"]
        journal.add_note(self.conn, sol, "вошёл на импульсе, без плана")

        stats = journal.coverage(self.conn)
        self.assertEqual(stats["trades"], 3)
        self.assertEqual(stats["annotated"], 2, "заметка и намерение оба засчитаны")
        self.assertEqual(stats["with_intent"], 1, "pre-trade — отдельный подпоказатель")
        self.assertEqual(stats["with_planned_stop"], 1)
        self.assertEqual(stats["missing"], 1)
        self.assertAlmostEqual(stats["share"], 2 / 3)

        # без разбора осталась только ADAUSDT
        pending = journal.unjournaled(self.conn)
        self.assertEqual([r["symbol"] for r in pending], ["ADAUSDT"])

    def test_match_survives_rebuild(self):
        """Пересборка сделок не должна рвать связь с журналом."""
        journal.add_intent(self.conn, "BTCUSDT", "long", "тезис", now_ms=9 * HOUR)
        self._closed_long()
        journal.match_intents(self.conn)

        roundtrips.rebuild(self.conn)
        journal.match_intents(self.conn)

        intent = self.conn.execute("SELECT * FROM intents").fetchone()
        self.assertIsNotNone(intent["matched_trade_id"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM intents").fetchone()["c"], 1
        )

    def test_intent_requires_thesis(self):
        with self.assertRaises(ValueError):
            journal.add_intent(self.conn, "BTCUSDT", "long", "   ")


if __name__ == "__main__":
    unittest.main()
