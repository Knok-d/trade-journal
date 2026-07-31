"""Импорт закрытых позиций с MT5.

Здесь защищается вещь, которой нет ни у одного другого источника в проекте:
эти цифры набирает человек, и проверить их не с чем — второго источника по
счёту MT5 не существует. Поэтому вся ценность модуля в двух проверках самого
ввода: тождество P&L из выгрузки и постоянство размера контракта. Ослабить их
значит остаться вообще без контроля.
"""

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import db, mt5, roundtrips  # noqa: E402

# Строка ровно того вида, что даёт таблица истории веб-трейдера.
AAPL = ("AAPL.s\tЗакрыть шорт\tПродать\t5.0\t5.0\t-4.35\t-4.35\t0.00 USDx\t"
        "0.0 USDx\t0.00 USDx\t334.30\t2025-03-14 23:02:04\t335.17\t"
        "2025-03-14 23:06:46\t900000001\t[sl 335.00]")
# Палладий: тот самый случай, где лот не равен единице.
XPD = ("XPDUSD.s\tЗакрыть шорт\tПродать\t0.5\t0.5\t94.55\t94.55\t0.00 USDx\t"
       "0.0 USDx\t0.00 USDx\t1,272.16\t2025-03-12 09:53:24\t1,253.25\t"
       "2025-03-12 13:00:37\t900000003\t--")
# Своп ненулевой: он прибавляется к P&L, а в журнале станет фандингом наоборот.
SP500 = ("SP500.s\tЗакрыть шорт\tПродать\t4.0\t4.0\t43.42\t42.40\t0.00 USDx\t"
         "0.0 USDx\t1.02 USDx\t7,414.10\t2025-03-11 21:53:29\t7,403.50\t"
         "2025-03-12 04:15:50\t900000004\t--")


class ParseTest(unittest.TestCase):
    def test_row_from_the_screen_is_read_whole(self):
        row = mt5.parse_line(AAPL)
        self.assertEqual(row["position_id"], "900000001")
        self.assertEqual(row["symbol"], "AAPL.s")
        self.assertEqual(row["direction"], "short")
        self.assertEqual(row["open_price"], Decimal("334.30"))
        self.assertEqual(row["close_price"], Decimal("335.17"))
        self.assertEqual(row["net_pnl"], Decimal("-4.35"))
        self.assertEqual(row["comment"], "sl 335.00", "выход по стопу стоит помнить")

    def test_thousands_separator_and_currency_are_stripped(self):
        row = mt5.parse_line(XPD)
        self.assertEqual(row["open_price"], Decimal("1272.16"))
        self.assertEqual(row["fees"], Decimal(0), "«0.00 USDx» — это ноль")

    def test_time_is_read_as_utc(self):
        """Сервер Bybit TradFi идёт по UTC без сдвига.

        Значение посчитано отдельно, а не тем же кодом, что проверяется:
        сдвиг на пару часов иначе прошёл бы незамеченным и растащил бы
        сделки по соседним дням на графике «по дням».
        """
        expected = int(datetime(2025, 3, 14, 23, 6, 46,
                                tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(expected, 1741993606000, "контроль самой константы")
        self.assertEqual(mt5.parse_line(AAPL)["closed_at"], expected)

    def test_empty_comment_becomes_nothing(self):
        self.assertIsNone(mt5.parse_line(XPD)["comment"])

    def test_broken_identity_is_refused(self):
        """Общий P&L обязан равняться P&L ордера плюс своп минус комиссия."""
        with self.assertRaises(mt5.Mt5Error) as ctx:
            mt5.parse_line(SP500.replace("43.42", "44.42", 1))
        self.assertIn("не равен", str(ctx.exception))

    def test_contradicting_direction_columns_are_refused(self):
        """Две колонки говорят об одном; разошлись — значит колонки сдвинулись."""
        with self.assertRaises(mt5.Mt5Error):
            mt5.parse_line(AAPL.replace("Закрыть шорт", "Закрыть Лонг", 1))

    def test_short_row_is_refused_with_a_countable_reason(self):
        with self.assertRaises(mt5.Mt5Error) as ctx:
            mt5.parse_line("AAPL.s\tЗакрыть шорт\tПродать")
        self.assertIn("колонок 3", str(ctx.exception))

    def test_one_bad_line_does_not_sink_the_rest(self):
        rows, problems = mt5.parse("\n".join([AAPL, "мусор\tещё мусор", XPD]))
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(problems), 1)
        self.assertIn("строка 2", problems[0])

    def test_header_row_is_skipped(self):
        rows, problems = mt5.parse("Торговые пары\tТип ордера\n" + AAPL)
        self.assertEqual(len(rows), 1)
        self.assertEqual(problems, [])

    def test_contract_size_is_derived_per_instrument(self):
        """Единственная проверка цен и объёмов: у инструмента лот один."""
        rows, _ = mt5.parse("\n".join([AAPL, XPD, SP500]))
        sizes = mt5.contract_sizes(rows)
        self.assertEqual(sizes["AAPL.s"], [Decimal(1)])
        self.assertEqual(sizes["SP500.s"], [Decimal(1)])
        self.assertEqual(sizes["XPDUSD.s"], [Decimal(10)],
                         "лот палладия — десять единиц, а не одна")

    def test_wrong_price_shows_up_as_a_second_contract_size(self):
        """Опечатку в цене тождество P&L не ловит — ловит размер контракта."""
        rows, problems = mt5.parse("\n".join([AAPL, AAPL.replace("334.30", "333.30")
                                              .replace("900000001", "900000002")]))
        self.assertEqual(problems, [])
        self.assertEqual(len(mt5.contract_sizes(rows)["AAPL.s"]), 2)


class Mt5InJournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        rows, _ = mt5.parse("\n".join([AAPL, XPD, SP500]))
        db.save_mt5_positions(self.conn, rows)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _trades(self):
        return self.conn.execute(
            "SELECT * FROM round_trips ORDER BY closed_at").fetchall()

    def test_positions_become_trades_with_the_brokers_numbers(self):
        roundtrips.rebuild(self.conn)
        by_symbol = {t["symbol"]: t for t in self._trades()}
        self.assertEqual(len(by_symbol), 3)

        sp = by_symbol["SP500.s"]
        self.assertEqual(sp["source"], "mt5", "происхождение обязано быть видно")
        self.assertEqual(db.dec(sp["net_pnl"]), Decimal("43.42"))
        self.assertEqual(db.dec(sp["gross_pnl"]), Decimal("42.40"))
        self.assertEqual(db.dec(sp["funding"]), Decimal("-1.02"),
                         "своп прибавляется к P&L, а фандинг вычитается")

    def test_rebuild_does_not_wipe_them(self):
        """Пересборка идёт раз в минуту: пропажа была бы почти мгновенной."""
        roundtrips.rebuild(self.conn)
        roundtrips.rebuild(self.conn)
        roundtrips.rebuild(self.conn)
        self.assertEqual(len(self._trades()), 3)

    def test_trade_id_survives_reimport_so_the_note_stays(self):
        roundtrips.rebuild(self.conn)
        trade_id = self._trades()[0]["trade_id"]

        rows, _ = mt5.parse("\n".join([AAPL, XPD, SP500]))
        db.save_mt5_positions(self.conn, rows)
        roundtrips.rebuild(self.conn)

        self.assertEqual(self._trades()[0]["trade_id"], trade_id)
        self.assertEqual(len(self._trades()), 3, "повторный ввод не задваивает")

    def test_hedged_positions_stay_separate(self):
        """Три позиции по одному символу — три сделки, а не одна склеенная."""
        second = SP500.replace("900000004", "900000005").replace(
            "7,414.10", "7,420.10")
        # Тождество после подмены цены не ломается: P&L взят из выгрузки как есть.
        rows, problems = mt5.parse(second)
        self.assertEqual(problems, [])
        db.save_mt5_positions(self.conn, rows)
        roundtrips.rebuild(self.conn)

        sp = [t for t in self._trades() if t["symbol"] == "SP500.s"]
        self.assertEqual(len(sp), 2)
        self.assertEqual(len({t["trade_id"] for t in sp}), 2)

    def test_full_rebuild_restores_what_the_glue_wipes(self):
        """Пересборка стирает round_trips целиком, а комиссия в MNT и плечо
        приходят отдельным проходом и в fills отсутствуют. Вызвать одну склейку
        и остановиться — значит оставить дневник с заниженным P&L; ровно так и
        вышло при первом импорте MT5."""
        from tests.test_roundtrips import fill

        db.save_executions(self.conn, [
            fill("a", "BTCUSDT", "Buy", "100", "1", 10 * 3_600_000, fee="1"),
            fill("b", "BTCUSDT", "Sell", "110", "1", 11 * 3_600_000, fee="1"),
        ])
        # Позиция закрыта ордером o-b, комиссия у биржи вдвое больше нашей.
        db.save_exchange_pnl(self.conn, [{
            "orderId": "o-b", "updatedTime": 11 * 3_600_000, "symbol": "BTCUSDT",
            "closedPnl": "6", "openFee": "2", "closeFee": "2",
            "leverage": "10", "cumEntryValue": "100",
        }])
        self.conn.execute("UPDATE raw_executions SET fee_currency = 'MNT'")
        self.conn.commit()

        roundtrips.rebuild_all(self.conn)
        btc = self.conn.execute(
            "SELECT * FROM round_trips WHERE symbol = 'BTCUSDT'").fetchone()
        self.assertEqual(btc["fees_source"], "exchange")
        self.assertEqual(db.dec(btc["fees"]), Decimal(4), "комиссия должна быть от биржи")
        self.assertEqual(btc["leverage"], "10", "плечо в fills отсутствует")

    def test_typo_is_fixed_by_entering_the_line_again(self):
        roundtrips.rebuild(self.conn)
        fixed, _ = mt5.parse(AAPL.replace("-4.35", "-5.35"))
        db.save_mt5_positions(self.conn, fixed)
        roundtrips.rebuild(self.conn)

        aapl = [t for t in self._trades() if t["symbol"] == "AAPL.s"]
        self.assertEqual(len(aapl), 1, "исправление не должно создавать вторую")
        self.assertEqual(db.dec(aapl[0]["net_pnl"]), Decimal("-5.35"))


if __name__ == "__main__":
    unittest.main()
