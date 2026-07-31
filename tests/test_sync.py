"""Перенос сделок Мак → сервер.

Главное, что здесь защищается: заливка новых сделок НЕ затирает разборы,
написанные на сервере с телефона. Это единственный способ потерять то, ради
чего продукт существует.
"""

import importlib
import os
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import db, journal, roundtrips, sync  # noqa: E402
from tests.test_roundtrips import fill  # noqa: E402

HOUR = 3_600_000


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mac = db.connect(self.root / "mac.db")
        self.vps = db.connect(self.root / "vps.db")

    def tearDown(self):
        self.mac.close()
        self.vps.close()
        self.tmp.cleanup()

    def _add_trade(self, conn, tag, symbol, at, entry="100", exit_="110"):
        db.save_executions(conn, [
            fill(f"{tag}a", symbol, "Buy", entry, "1", at),
            fill(f"{tag}b", symbol, "Sell", exit_, "1", at + HOUR),
        ])
        roundtrips.rebuild(conn)

    def _transfer(self, **kwargs):
        path = self.root / "transfer.db"
        sync.export(self.mac, path, **kwargs)
        return sync.merge(self.vps, path)

    def test_trades_travel_to_server(self):
        self._add_trade(self.mac, "t1", "BTCUSDT", 10 * HOUR)
        result = self._transfer()

        self.assertEqual(result["added"]["raw_executions"], 2)
        self.assertEqual(result["round_trips"], 1)
        row = self.vps.execute("SELECT symbol, net_pnl FROM round_trips").fetchone()
        self.assertEqual(row["symbol"], "BTCUSDT")

    def test_server_notes_survive_new_upload(self):
        """Разбор, написанный с телефона, переживает заливку свежих сделок."""
        self._add_trade(self.mac, "t1", "BTCUSDT", 10 * HOUR)
        self._transfer()

        trade_id = self.vps.execute("SELECT trade_id FROM round_trips").fetchone()["trade_id"]
        journal.add_note(self.vps, trade_id, "вошёл против тренда, зря")

        # на Маке появилась новая сделка, заливаем ещё раз
        self._add_trade(self.mac, "t2", "SOLUSDT", 30 * HOUR)
        result = self._transfer()

        self.assertEqual(result["round_trips"], 2, "новая сделка должна доехать")
        note = self.vps.execute(
            "SELECT body FROM notes WHERE trade_id = ?", (trade_id,)).fetchone()
        self.assertIsNotNone(note, "разбор с сервера пропал при заливке")
        self.assertEqual(note["body"], "вошёл против тренда, зря")

    def test_note_stays_attached_after_rebuild(self):
        """trade_id детерминирован, поэтому привязка переживает пересборку."""
        self._add_trade(self.mac, "t1", "BTCUSDT", 10 * HOUR)
        self._transfer()
        trade_id = self.vps.execute("SELECT trade_id FROM round_trips").fetchone()["trade_id"]
        journal.add_note(self.vps, trade_id, "разбор")

        self._transfer()   # повторная заливка тех же данных
        linked = self.vps.execute(
            "SELECT COUNT(*) c FROM notes n JOIN round_trips rt"
            " ON rt.trade_id = n.trade_id").fetchone()["c"]
        self.assertEqual(linked, 1, "заметка отвязалась от сделки")

    def test_repeated_upload_adds_nothing(self):
        """Идемпотентность: повторная заливка не плодит дубли."""
        self._add_trade(self.mac, "t1", "BTCUSDT", 10 * HOUR)
        self._transfer()
        second = self._transfer()

        self.assertEqual(second["added"]["raw_executions"], 0)
        self.assertEqual(second["round_trips"], 1)

    def test_journal_always_travels(self):
        """Обычная заливка везёт журнал: отдельного флага для этого нет.

        Флаг был, и ровно там его забывали — заметка с мака тихо не доезжала
        до телефона, а заметить это можно было только случайно.
        """
        self._add_trade(self.mac, "t1", "BTCUSDT", 10 * HOUR)
        mac_trade = self.mac.execute("SELECT trade_id FROM round_trips").fetchone()["trade_id"]
        journal.add_note(self.mac, mac_trade, "локальная заметка")
        journal.add_tag(self.mac, "rule", "не усредняться в убыток")

        self._transfer()
        self.assertEqual(
            self.vps.execute("SELECT body FROM notes").fetchone()["body"],
            "локальная заметка")
        self.assertEqual(
            self.vps.execute("SELECT COUNT(*) c FROM rules").fetchone()["c"], 1)

    def test_server_note_wins_over_uploaded_one(self):
        """Если разбор есть с обеих сторон, побеждает более свежий."""
        self._add_trade(self.mac, "t1", "BTCUSDT", 10 * HOUR)
        trade_id = self.mac.execute("SELECT trade_id FROM round_trips").fetchone()["trade_id"]
        journal.add_note(self.mac, trade_id, "старая версия с Мака")
        self._transfer()

        time.sleep(0.002)
        journal.add_note(self.vps, trade_id, "свежая версия с телефона")
        self._transfer()

        self.assertEqual(
            self.vps.execute("SELECT body FROM notes").fetchone()["body"],
            "свежая версия с телефона")

    def test_missing_transfer_file_is_explicit(self):
        with self.assertRaises(FileNotFoundError):
            sync.merge(self.vps, self.root / "нет-такого.db")


class TwoWayJournalTest(unittest.TestCase):
    """Журнал ездит в обе стороны: пишут и на маке, и с телефона.

    Проверяется не «чья сторона правее», а «чья правка свежее» — и то, что
    ничто не воскресает: снятое нарушение, стёртый разбор и убранное правило
    обязаны пережить круг синхронизации в снятом виде.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mac = db.connect(self.root / "mac.db")
        self.vps = db.connect(self.root / "vps.db")

        db.save_executions(self.mac, [
            fill("a", "BTCUSDT", "Buy", "100", "1", 10 * HOUR),
            fill("b", "BTCUSDT", "Sell", "90", "1", 11 * HOUR),
        ])
        roundtrips.rebuild(self.mac)
        self.trade_id = self.mac.execute(
            "SELECT trade_id FROM round_trips").fetchone()["trade_id"]
        self._push()

    def tearDown(self):
        self.mac.close()
        self.vps.close()
        self.tmp.cleanup()

    def _push(self):
        """Мак → сервер: сделки плюс журнал. Ровно то, что делает sync.sh."""
        path = self.root / "push.db"
        sync.export(self.mac, path)
        return sync.merge(self.vps, path)

    def _pull(self):
        """Сервер → мак: только журнал."""
        path = self.root / "pull.db"
        sync.export(self.vps, path, journal_only=True)
        return sync.merge(self.mac, path)

    def test_rule_created_on_mac_reaches_the_server(self):
        rule_id = journal.add_tag(self.mac, "rule", "не усредняться в убыток")
        self._push()
        self.assertEqual(
            self.vps.execute("SELECT body FROM rules WHERE rule_id = ?",
                             (rule_id,)).fetchone()["body"],
            "не усредняться в убыток")

    def test_violation_marked_on_phone_reaches_the_mac(self):
        rule_id = journal.add_tag(self.mac, "rule", "не входить против тренда")
        self._push()

        journal.add_note(self.vps, self.trade_id, "разобрал с телефона")
        journal.set_mark(self.vps, "rule", self.trade_id, rule_id, True)
        self._pull()

        self.assertEqual(journal.marks_by_trade(self.mac, "rule"),
                         {self.trade_id: [rule_id]})
        self.assertEqual(
            self.mac.execute("SELECT body FROM notes").fetchone()["body"],
            "разобрал с телефона")

    def test_fresher_edit_wins_in_both_directions(self):
        journal.add_note(self.vps, self.trade_id, "версия с телефона")
        self._pull()
        time.sleep(0.002)
        journal.add_note(self.mac, self.trade_id, "правка на маке")
        self._push()
        self.assertEqual(
            self.vps.execute("SELECT body FROM notes").fetchone()["body"],
            "правка на маке", "свежая маковская правка не доехала")

        time.sleep(0.002)
        journal.add_note(self.vps, self.trade_id, "снова с телефона")
        self._pull()
        self.assertEqual(
            self.mac.execute("SELECT body FROM notes").fetchone()["body"],
            "снова с телефона", "свежая правка с телефона не доехала")

    def test_stale_side_does_not_overwrite_fresher_one(self):
        """Мак заливает сделки, не тронув заметку, написанную позже на телефоне."""
        journal.add_note(self.mac, self.trade_id, "старое с мака")
        self._push()
        time.sleep(0.002)
        journal.add_note(self.vps, self.trade_id, "новое с телефона")

        self._push()          # мак не знает о правке и везёт свою старую версию
        self.assertEqual(
            self.vps.execute("SELECT body FROM notes").fetchone()["body"],
            "новое с телефона")

    def test_cleared_violation_does_not_come_back(self):
        rule_id = journal.add_tag(self.mac, "rule", "не докупать на проливе")
        journal.set_mark(self.mac, "rule", self.trade_id, rule_id, True)
        self._push()

        time.sleep(0.002)
        journal.set_mark(self.vps, "rule", self.trade_id, rule_id, False)
        self._pull()
        self._push()          # круг целиком: снятое не должно воскреснуть

        self.assertEqual(journal.marks_by_trade(self.mac, "rule"), {})
        self.assertEqual(journal.marks_by_trade(self.vps, "rule"), {})

    def test_cleared_note_does_not_come_back(self):
        journal.add_note(self.mac, self.trade_id, "было")
        self._push()

        time.sleep(0.002)
        journal.add_note(self.mac, self.trade_id, "")
        self._push()
        self._pull()

        self.assertEqual(
            self.mac.execute("SELECT body FROM notes").fetchone()["body"], "")
        self.assertEqual(journal.coverage(self.mac)["annotated"], 0)

    def test_archived_rule_does_not_come_back(self):
        rule_id = journal.add_tag(self.mac, "rule", "передумал")
        self._push()

        time.sleep(0.002)
        journal.edit_tag(self.vps, "rule", rule_id, active=False)
        self._pull()
        self._push()

        self.assertEqual(journal.tags(self.mac, "rule"), [])
        self.assertEqual(journal.tags(self.vps, "rule"), [])

    def test_intent_travels_once_and_keeps_its_own_id(self):
        journal.add_intent(self.mac, "BTCUSDT", "long", "пробой уровня")
        self._push()
        self._push()

        rows = self.vps.execute("SELECT uid, thesis FROM intents").fetchall()
        self.assertEqual(len(rows), 1, "намерение задвоилось при повторной заливке")
        self.assertEqual(rows[0]["thesis"], "пробой уровня")
        self.assertIsNotNone(rows[0]["uid"])

    def test_open_positions_are_replaced_not_accumulated(self):
        """Закрытая позиция обязана исчезнуть с сервера, а не остаться призраком."""
        db.save_open_positions(self.mac, [
            {"symbol": "BTCUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
             "unrealisedPnl": "5", "leverage": "10"},
            {"symbol": "ETHUSDT", "side": "Sell", "size": "2", "avgPrice": "50",
             "unrealisedPnl": "-3", "leverage": "20"},
        ])
        self._push()
        self.assertEqual(
            {r["symbol"] for r in self.vps.execute("SELECT symbol FROM open_positions")},
            {"BTCUSDT", "ETHUSDT"})

        # ETH закрыт: биржа его больше не отдаёт
        db.save_open_positions(self.mac, [
            {"symbol": "BTCUSDT", "side": "Buy", "size": "1", "avgPrice": "100",
             "unrealisedPnl": "9", "leverage": "10"},
        ])
        self._push()

        rows = self.vps.execute(
            "SELECT symbol, unrealised FROM open_positions").fetchall()
        self.assertEqual([r["symbol"] for r in rows], ["BTCUSDT"],
                         "закрытая позиция осталась на сервере")
        self.assertEqual(rows[0]["unrealised"], "9", "цифры обязаны обновиться")

    def test_symbol_directory_travels_and_reclassification_wins(self):
        """К бирже ходит только мак, поэтому его справочник на сервере главный."""
        db.save_symbols(self.mac, [{"symbol": "XAUUSDT", "symbolType": "commodity"}])
        self._push()
        self.assertEqual(
            self.vps.execute(
                "SELECT symbol_type FROM symbols WHERE symbol='XAUUSDT'"
            ).fetchone()["symbol_type"], "commodity")

        # Биржа переклассифицировала инструмент — сервер обязан это принять,
        # а не оставить прежний тип и показывать акцию товаром.
        db.save_symbols(self.mac, [{"symbol": "XAUUSDT", "symbolType": "stock"}])
        self._push()
        self.assertEqual(
            self.vps.execute(
                "SELECT symbol_type FROM symbols WHERE symbol='XAUUSDT'"
            ).fetchone()["symbol_type"], "stock")

    def test_mt5_positions_reach_the_server_and_become_trades(self):
        """Вводятся они только на маке, а смотреть на них надо и с телефона."""
        from journal import mt5
        rows, problems = mt5.parse(
            "AAPL.s\tЗакрыть шорт\tПродать\t5.0\t5.0\t-4.35\t-4.35\t0.00 USDx\t"
            "0.0 USDx\t0.00 USDx\t334.30\t2025-03-14 23:02:04\t335.17\t"
            "2025-03-14 23:06:46\t900000001\t--")
        self.assertEqual(problems, [])
        db.save_mt5_positions(self.mac, rows)
        self._push()

        trade = self.vps.execute(
            "SELECT * FROM round_trips WHERE source = 'mt5'").fetchone()
        self.assertEqual(trade["symbol"], "AAPL.s")
        self.assertEqual(db.dec(trade["net_pnl"]), db.dec("-4.35"))

    def test_corrected_mt5_typo_reaches_the_server(self):
        """Дописывание не донесло бы правку, а при ручном вводе правка — норма."""
        from journal import mt5
        line = ("AAPL.s\tЗакрыть шорт\tПродать\t5.0\t5.0\t-4.35\t-4.35\t0.00 USDx\t"
                "0.0 USDx\t0.00 USDx\t334.30\t2025-03-14 23:02:04\t335.17\t"
                "2025-03-14 23:06:46\t900000001\t--")
        db.save_mt5_positions(self.mac, mt5.parse(line)[0])
        self._push()

        db.save_mt5_positions(self.mac, mt5.parse(line.replace("-4.35", "-5.35"))[0])
        self._push()

        rows = self.vps.execute(
            "SELECT net_pnl FROM round_trips WHERE source = 'mt5'").fetchall()
        self.assertEqual(len(rows), 1, "исправление не должно задваивать сделку")
        self.assertEqual(db.dec(rows[0]["net_pnl"]), db.dec("-5.35"))

    def test_empty_snapshot_is_a_fact_not_a_gap(self):
        """«Позиций нет» отличается от «давно не спрашивали» — по отметке."""
        db.save_open_positions(self.mac, [])
        self._push()

        self.assertEqual(
            self.vps.execute("SELECT COUNT(*) c FROM open_positions").fetchone()["c"], 0)
        self.assertIsNotNone(db.get_meta(self.vps, "positions_at"))

    def test_pull_does_not_touch_the_freshness_stamp(self):
        """Сервер к бирже не ходит — его штамп не должен затирать маковский."""
        from journal import stats
        self._push()
        mac_stamp = stats.freshness(self.mac)["synced_at"]

        time.sleep(0.002)
        self._pull()
        self.assertEqual(stats.freshness(self.mac)["synced_at"], mac_stamp)


class DbPathTest(unittest.TestCase):
    """На сервере путь к базе задаёт юнит, а не $HOME.

    HOME у сервиса указывает в /opt (только чтение у ProtectSystem=strict),
    а у `sudo -u` при импорте остаётся /root. Без явной переменной база
    расходилась на два файла, и сервис читал не тот.
    """

    def _reload(self, **env):
        with unittest.mock.patch.dict(os.environ, env, clear=False):
            if "TRADE_JOURNAL_DB" not in env:
                os.environ.pop("TRADE_JOURNAL_DB", None)
            return importlib.reload(db).DB_PATH

    def tearDown(self):
        os.environ.pop("TRADE_JOURNAL_DB", None)
        importlib.reload(db)

    def test_env_overrides_home(self):
        self.assertEqual(
            self._reload(TRADE_JOURNAL_DB="/var/lib/trade-journal/journal.db"),
            Path("/var/lib/trade-journal/journal.db"))

    def test_falls_back_to_home(self):
        self.assertEqual(self._reload(),
                         Path.home() / ".trade-journal" / "journal.db")


if __name__ == "__main__":
    unittest.main()


class FreshnessTest(unittest.TestCase):
    """Отметка свежести: сломавшийся синк обязан быть виден.

    Молчаливое устаревание — худший вид поломки для дневника: интерфейс
    показывает позавчерашние цифры с той же уверенностью, что и сегодняшние.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mac = db.connect(self.root / "mac.db")
        self.vps = db.connect(self.root / "vps.db")

    def tearDown(self):
        self.mac.close()
        self.vps.close()
        self.tmp.cleanup()

    def test_never_synced_counts_as_stale(self):
        from journal import stats
        f = stats.freshness(self.vps)
        self.assertIsNone(f["synced_at"])
        self.assertTrue(f["stale"], "база без отметки обязана считаться устаревшей")

    def test_export_stamps_and_merge_carries_it(self):
        from journal import stats
        path = self.root / "t.db"
        sync.export(self.mac, path)
        sync.merge(self.vps, path)

        f = stats.freshness(self.vps)
        self.assertIsNotNone(f["synced_at"], "отметка не доехала до сервера")
        self.assertFalse(f["stale"])
        self.assertLess(f["age_hours"], 0.1)

    def test_old_stamp_is_stale(self):
        from journal import stats
        old = int(time.time() * 1000) - int(stats.STALE_AFTER_HOURS + 1) * 3_600_000
        db.set_meta(self.vps, "synced_at", old)
        f = stats.freshness(self.vps)
        self.assertTrue(f["stale"])
        self.assertGreater(f["age_hours"], stats.STALE_AFTER_HOURS)

    def test_stamp_refreshes_on_repeat_sync(self):
        """Повторный синк обновляет отметку, а не оставляет первую."""
        from journal import stats
        path = self.root / "t.db"
        sync.export(self.mac, path)
        sync.merge(self.vps, path)
        first = stats.freshness(self.vps)["synced_at"]

        time.sleep(0.01)
        sync.export(self.mac, path)
        sync.merge(self.vps, path)
        second = stats.freshness(self.vps)["synced_at"]
        self.assertGreater(second, first)
