"""Перенос сделок Мак → сервер.

Главное, что здесь защищается: заливка новых сделок НЕ затирает разборы,
написанные на сервере с телефона. Это единственный способ потерять то, ради
чего продукт существует.
"""

import importlib
import os
import sys
import tempfile
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

    def test_journal_not_exported_by_default(self):
        """Обычная заливка не тащит журнал с Мака — он живёт на сервере."""
        self._add_trade(self.mac, "t1", "BTCUSDT", 10 * HOUR)
        mac_trade = self.mac.execute("SELECT trade_id FROM round_trips").fetchone()["trade_id"]
        journal.add_note(self.mac, mac_trade, "локальная заметка")

        self._transfer()
        self.assertEqual(
            self.vps.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"], 0)

    def test_initial_seed_carries_journal(self):
        """Первичное заполнение переносит уже накопленные разборы."""
        self._add_trade(self.mac, "t1", "BTCUSDT", 10 * HOUR)
        mac_trade = self.mac.execute("SELECT trade_id FROM round_trips").fetchone()["trade_id"]
        journal.add_note(self.mac, mac_trade, "перенесённый разбор")

        self._transfer(with_journal=True)
        note = self.vps.execute("SELECT body FROM notes").fetchone()
        self.assertEqual(note["body"], "перенесённый разбор")

    def test_server_note_wins_over_uploaded_one(self):
        """Если разбор есть с обеих сторон, серверный не перезаписывается."""
        self._add_trade(self.mac, "t1", "BTCUSDT", 10 * HOUR)
        trade_id = self.mac.execute("SELECT trade_id FROM round_trips").fetchone()["trade_id"]
        journal.add_note(self.mac, trade_id, "старая версия с Мака")
        self._transfer(with_journal=True)

        journal.add_note(self.vps, trade_id, "свежая версия с телефона")
        self._transfer(with_journal=True)

        self.assertEqual(
            self.vps.execute("SELECT body FROM notes").fetchone()["body"],
            "свежая версия с телефона")

    def test_missing_transfer_file_is_explicit(self):
        with self.assertRaises(FileNotFoundError):
            sync.merge(self.vps, self.root / "нет-такого.db")


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
