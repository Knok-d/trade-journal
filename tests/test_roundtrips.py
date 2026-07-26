"""Проверка склейки на трёх грязных кейсах из docs/trade-definition.md.

Не требует ключей и сети: синтетические fills в том же формате, что отдаёт Bybit.
Запуск: python3 -m unittest discover tests
"""

import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import db, roundtrips  # noqa: E402


_seq = iter(range(1, 10_000))


def fill(exec_id, symbol, side, price, qty, time_ms, *,
         position_idx=0, fee="0", exec_type="Trade", closed_size=None, seq=None):
    return {
        "execId": exec_id, "symbol": symbol, "side": side,
        "execPrice": price, "execQty": qty, "execFee": fee,
        "feeCurrency": "USDT", "execType": exec_type,
        "closedSize": closed_size, "execTime": time_ms,
        "seq": seq if seq is not None else next(_seq),
        "orderId": f"o-{exec_id}", "positionIdx": position_idx,
        "_category": "linear",
    }


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def build(self, fills):
        db.save_executions(self.conn, fills)
        stats = roundtrips.rebuild(self.conn)
        trades = self.conn.execute(
            "SELECT * FROM round_trips ORDER BY opened_at, trade_id"
        ).fetchall()
        return trades, stats

    def test_reversal_through_zero(self):
        """Кейс 1: лонг 1.0, затем продажа 3.0 -> закрытие лонга + шорт 2.0."""
        trades, _ = self.build([
            fill("1", "BTCUSDT", "Buy", "100", "1", 1_000),
            fill("2", "BTCUSDT", "Sell", "110", "3", 2_000, fee="3"),
        ])

        self.assertEqual(len(trades), 2, "переворот должен дать две сделки")

        closed_long, new_short = trades
        self.assertEqual(closed_long["direction"], "long")
        self.assertIsNotNone(closed_long["closed_at"])
        # (110 - 100) * 1
        self.assertEqual(Decimal(closed_long["gross_pnl"]), Decimal("10"))
        # комиссия 3 делится пропорционально: 1 из 3 закрыли -> 1.0
        self.assertEqual(Decimal(closed_long["fees"]), Decimal("1"))

        self.assertEqual(new_short["direction"], "short")
        self.assertIsNone(new_short["closed_at"], "остаток остаётся открытой позицией")
        self.assertEqual(Decimal(new_short["qty"]), Decimal("2"))
        self.assertEqual(Decimal(new_short["avg_entry"]), Decimal("110"))
        self.assertEqual(Decimal(new_short["fees"]), Decimal("2"))

    def test_partial_exit_does_not_move_average_entry(self):
        """Кейс 2: долив меняет среднюю цену, частичный выход — нет."""
        trades, _ = self.build([
            fill("1", "ETHUSDT", "Buy", "100", "1", 1_000),
            fill("2", "ETHUSDT", "Buy", "200", "1", 2_000),      # средняя -> 150
            fill("3", "ETHUSDT", "Sell", "300", "1", 3_000),     # частичный выход
            fill("4", "ETHUSDT", "Sell", "400", "1", 4_000),     # закрытие
        ])

        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(Decimal(trade["avg_entry"]), Decimal("150"))
        # (300-150)*1 + (400-150)*1 = 150 + 250
        self.assertEqual(Decimal(trade["gross_pnl"]), Decimal("400"))
        self.assertEqual(Decimal(trade["avg_exit"]), Decimal("350"))

    def test_add_after_partial_exit_rebases_average(self):
        """Долив ПОСЛЕ частичного выхода считается от остатка, а не от всех входов.

        Ловит баг, невидимый прежним тестам: если при выходе не снимать
        себестоимость с базы, средняя цена загрязняется уже проданной частью.
        """
        trades, _ = self.build([
            fill("1", "ADAUSDT", "Buy", "100", "2", 1_000),   # 2 шт по 100
            fill("2", "ADAUSDT", "Sell", "150", "1", 2_000),  # вышли 1 -> остаток 1 по 100
            fill("3", "ADAUSDT", "Buy", "200", "1", 3_000),   # долив -> средняя (100+200)/2=150
            fill("4", "ADAUSDT", "Sell", "150", "2", 4_000),  # закрыли 2 по 150 -> P&L 0
        ])

        self.assertEqual(len(trades), 1)
        # 1-й выход: (150-100)*1 = +50. 2-й выход: (150-150)*2 = 0. Итого +50.
        # При старом (ошибочном) расчёте средняя после долива была бы
        # (100*2+200)/3 = 133.33 и P&L вышел бы иным.
        self.assertEqual(Decimal(trades[0]["gross_pnl"]), Decimal("50"))

    def test_fills_in_same_millisecond_use_seq_order(self):
        """Порядок внутри миллисекунды берётся из seq, а не из exec_id.

        80% реальных fills делят exec_time с соседями; сортировка по UUID
        рвала позиции не там, где они рвались на бирже.
        """
        same_ms = 5_000
        # exec_id намеренно в порядке, обратном seq: 'z' открывает, 'a' закрывает
        rows = [
            fill("z-open", "XRPUSDT", "Buy", "100", "1", same_ms, seq=1),
            fill("a-close", "XRPUSDT", "Sell", "110", "1", same_ms, seq=2),
        ]
        trades, _ = self.build(rows)

        self.assertEqual(len(trades), 1, "по seq это одна открытая-и-закрытая сделка")
        self.assertEqual(trades[0]["direction"], "long")
        self.assertEqual(Decimal(trades[0]["gross_pnl"]), Decimal("10"))

    def test_hedge_mode_keeps_sides_separate(self):
        """Кейс 3: две стороны хеджа по одному символу — две независимые сделки."""
        trades, _ = self.build([
            fill("1", "SOLUSDT", "Buy", "100", "1", 1_000, position_idx=1),
            fill("2", "SOLUSDT", "Sell", "100", "1", 1_100, position_idx=2),
            fill("3", "SOLUSDT", "Sell", "120", "1", 2_000, position_idx=1),
            fill("4", "SOLUSDT", "Buy", "120", "1", 2_100, position_idx=2),
        ])

        self.assertEqual(len(trades), 2, "хедж не должен схлопнуться в одну сделку")
        long_side = next(t for t in trades if t["position_idx"] == 1)
        short_side = next(t for t in trades if t["position_idx"] == 2)

        self.assertEqual(Decimal(long_side["gross_pnl"]), Decimal("20"))
        self.assertEqual(Decimal(short_side["gross_pnl"]), Decimal("-20"))

    def test_funding_attaches_to_open_position(self):
        """Фандинг не меняет объём позиции и вычитается из чистого P&L."""
        trades, stats = self.build([
            fill("1", "BTCUSDT", "Buy", "100", "1", 1_000),
            fill("f", "BTCUSDT", "Buy", "0", "0", 1_500, exec_type="Funding", fee="0.5"),
            fill("2", "BTCUSDT", "Sell", "110", "1", 2_000),
        ])

        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(Decimal(trade["qty"]), Decimal("1"), "фандинг не меняет объём")
        self.assertEqual(Decimal(trade["funding"]), Decimal("0.5"))
        self.assertEqual(Decimal(trade["net_pnl"]), Decimal("9.5"))
        self.assertEqual(stats["orphan_funding"], [])

    def test_rebuild_is_idempotent_and_ids_are_stable(self):
        """Пересборка не плодит дубли и сохраняет trade_id — иначе отвяжется журнал."""
        fills = [
            fill("1", "BTCUSDT", "Buy", "100", "1", 1_000),
            fill("2", "BTCUSDT", "Sell", "110", "1", 2_000),
        ]
        first, _ = self.build(fills)
        second, _ = self.build(fills)

        self.assertEqual(len(second), 1, "повторный импорт не должен удваивать сделки")
        self.assertEqual(first[0]["trade_id"], second[0]["trade_id"])


if __name__ == "__main__":
    unittest.main()
