"""Статистика: проверяется не столько арифметика, сколько отказы считать.

Главное, что тут защищается, — правила, которые легко «смягчить» ради красивого
отчёта: R без стопа, срезы на малых ячейках, метрика без n.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import db, journal, report, roundtrips, stats  # noqa: E402
from tests.test_roundtrips import fill  # noqa: E402

HOUR = 3_600_000


class StatsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trade(self, tag, symbol, entry, exit_, qty="1", at=10 * HOUR):
        db.save_executions(self.conn, [
            fill(f"{tag}a", symbol, "Buy", entry, qty, at),
            fill(f"{tag}b", symbol, "Sell", exit_, qty, at + HOUR),
        ])

    def test_metrics_carry_sample_size(self):
        for i in range(3):
            self._trade(f"t{i}", "BTCUSDT", "100", "110", at=(10 + i) * HOUR)
        roundtrips.rebuild(self.conn)

        s = stats.summary(self.conn)
        self.assertEqual(s["n"], 3)
        self.assertIn("win_rate_ci", s, "доля обязана идти с интервалом")
        self.assertIn("expectancy_ci", s)

    def test_small_sample_is_labelled(self):
        self.assertIn("выводы делать рано", stats.sample_note(20))
        self.assertIn("гипотезы", stats.sample_note(150))
        self.assertIn("осторожные выводы", stats.sample_note(500))

    def test_wilson_interval_widens_on_small_n(self):
        narrow = stats.wilson_interval(60, 100)
        wide = stats.wilson_interval(6, 10)
        self.assertLess(narrow[1] - narrow[0], wide[1] - wide[0])

    def test_r_multiple_unavailable_without_planned_stop(self):
        self._trade("x", "BTCUSDT", "100", "110")
        roundtrips.rebuild(self.conn)
        journal.add_intent(self.conn, "BTCUSDT", "long", "без стопа", now_ms=9 * HOUR)
        journal.match_intents(self.conn)

        r = stats.r_multiples(self.conn)
        self.assertFalse(r["available"])
        self.assertEqual(r["n"], 0)
        self.assertIn("НЕДОСТУПЕН", report.render(self.conn))

    def test_r_multiple_appears_with_planned_stop(self):
        journal.add_intent(self.conn, "BTCUSDT", "long", "с планом",
                           planned_stop="90", now_ms=9 * HOUR)
        self._trade("x", "BTCUSDT", "100", "110")
        roundtrips.rebuild(self.conn)
        journal.match_intents(self.conn)

        r = stats.r_multiples(self.conn)
        self.assertTrue(r["available"])
        self.assertEqual(r["values"], [1])   # прибыль 10 при риске 10

    def test_symbol_slice_hidden_below_threshold(self):
        for i in range(5):
            self._trade(f"s{i}", "BTCUSDT", "100", "110", at=(10 + i) * HOUR)
        roundtrips.rebuild(self.conn)

        sliced = stats.by_symbol(self.conn)
        self.assertEqual(sliced["shown"], [], "5 сделок не должны давать срез")
        self.assertEqual(sliced["hidden"], 1)
        self.assertEqual(sliced["tested"], 1)

    def test_symbol_slice_shown_above_threshold(self):
        for i in range(stats.MIN_SLICE_N):
            self._trade(f"s{i}", "ETHUSDT", "100", "110", at=(10 + i) * HOUR)
        roundtrips.rebuild(self.conn)

        sliced = stats.by_symbol(self.conn)
        self.assertEqual(len(sliced["shown"]), 1)
        self.assertEqual(sliced["shown"][0]["n"], stats.MIN_SLICE_N)

    def test_top_trades_only_winners_sorted(self):
        self._trade("w1", "BTCUSDT", "100", "110")            # +10
        self._trade("w2", "ETHUSDT", "100", "130", at=12 * HOUR)  # +30
        self._trade("l1", "SOLUSDT", "100", "90", at=14 * HOUR)   # -10
        roundtrips.rebuild(self.conn)

        top = stats.top_trades(self.conn, limit=10)
        self.assertEqual([t["symbol"] for t in top["trades"]],
                         ["ETHUSDT", "BTCUSDT"], "убыточные не попадают, сортировка по прибыли")
        self.assertEqual(top["winners_total"], 2)
        self.assertEqual(float(top["share_of_wins"]), 1.0)

    def test_top_trades_limit_and_share(self):
        for i in range(4):
            self._trade(f"t{i}", f"C{i}USDT", "100", str(110 + i * 10),
                        at=(10 + 2 * i) * HOUR)
        roundtrips.rebuild(self.conn)

        top = stats.top_trades(self.conn, limit=2)
        self.assertEqual(len(top["trades"]), 2)
        self.assertLess(float(top["share_of_wins"]), 1.0,
                        "доля топа обязана честно показывать, что он не вся прибыль")

    def test_bootstrap_is_reproducible(self):
        from decimal import Decimal
        values = [Decimal(v) for v in (10, -5, 30, -40, 7, 22, -3)]
        self.assertEqual(stats.bootstrap_mean_ci(values),
                         stats.bootstrap_mean_ci(values))

    def test_report_survives_empty_database(self):
        self.assertIn("нет", report.render(self.conn).lower())


if __name__ == "__main__":
    unittest.main()
