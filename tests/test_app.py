"""Приложение: фоновая синхронизация и инкрементальный забор.

Главное, что здесь защищается, — провал синка обязан быть виден. Дневник со
сломанным обновлением выглядит исправным: он показывает вчерашние цифры с той
же уверенностью, что и сегодняшние. Поэтому неудачный круг оставляет причину,
а не молча уходит на следующий.
"""

import argparse
import io
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import app, cli, db  # noqa: E402

HOUR = 3_600_000


def script(body: str, root: Path) -> Path:
    path = root / "fake-sync.sh"
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)
    return path


class SyncLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_successful_round_is_recorded(self):
        loop = app.SyncLoop(script('echo "готово"; exit 0', self.root))
        self.assertTrue(loop.run_once())
        self.assertIsNotNone(loop.state["last_ok"])
        self.assertIsNone(loop.state["last_error"])

    def test_skipped_round_is_neither_success_nor_failure(self):
        """Нет сети — круг пропущен, а не пройден.

        С кодом 0 приложение засчитывало пропуск успехом и рисовало «Данные
        свежие» поверх цифр любой давности: мак за captive-порталом врал бы
        уверенно. Штамп успеха не двигается, но и жаловаться не на что —
        интерфейс сам покажет растущее «обновлено N мин назад».
        """
        loop = app.SyncLoop(script('echo "сети нет, пропускаю"; exit 75', self.root))
        self.assertFalse(loop.run_once())
        self.assertIsNone(loop.state["last_ok"], "пропуск не должен выглядеть успехом")
        self.assertIsNone(loop.state["last_error"], "пропуск не поломка")

    def test_skip_does_not_erase_an_earlier_success(self):
        loop = app.SyncLoop(script('exit 0', self.root))
        loop.run_once()
        stamp = loop.state["last_ok"]

        loop.script = script('echo "сети нет, пропускаю"; exit 75', self.root)
        loop.run_once()
        self.assertEqual(loop.state["last_ok"], stamp,
                         "штамп остаётся прежним: данные и правда этой давности")
        self.assertIsNone(loop.state["last_error"],
                          "пропуск не должен выглядеть поломкой синхронизации")

    def test_failed_round_keeps_the_reason(self):
        loop = app.SyncLoop(script('echo "ПРОВАЛ: сервер не принял"; exit 3', self.root))
        self.assertFalse(loop.run_once())
        self.assertIn("сервер не принял", loop.state["last_error"])
        self.assertIsNone(loop.state["last_ok"], "провал не должен выглядеть успехом")

    def test_failure_goes_to_the_log_whole(self):
        """Успех молчит, провал пишет весь вывод: лог отвечает на «почему»."""
        quiet = app.SyncLoop(script('echo "бэкфилл"; echo "готово"; exit 0', self.root))
        with unittest.mock.patch("sys.stdout", io.StringIO()) as out:
            quiet.run_once()
            self.assertEqual(out.getvalue(), "", "удачный круг не должен засорять лог")

        loud = app.SyncLoop(
            script('echo "бэкфилл"; echo "ПРОВАЛ: нет связи"; exit 2', self.root))
        with unittest.mock.patch("sys.stdout", io.StringIO()) as out:
            loud.run_once()
            written = out.getvalue()
        self.assertIn("бэкфилл", written, "в логе должен быть весь вывод, не хвост")
        self.assertIn("ПРОВАЛ: нет связи", written)

    def test_earlier_success_does_not_mask_a_later_failure(self):
        ok = app.SyncLoop(script('exit 0', self.root))
        ok.run_once()
        stamp = ok.state["last_ok"]

        ok.script = script('echo "сеть отвалилась"; exit 1', self.root)
        ok.run_once()
        self.assertEqual(ok.state["last_ok"], stamp, "штамп успеха не обновляется")
        self.assertIn("сеть отвалилась", ok.state["last_error"])

    def test_hung_round_is_cut_off(self):
        loop = app.SyncLoop(script('sleep 5', self.root))
        with unittest.mock.patch.object(app, "TIMEOUT_SEC", 1):
            self.assertFalse(loop.run_once())
        self.assertIn("не уложился", loop.state["last_error"])

    def test_missing_script_fails_loudly(self):
        with unittest.mock.patch.object(app, "SYNC_SCRIPT", self.root / "нет.sh"):
            with self.assertRaises(FileNotFoundError):
                app.run(sync=True)


class FakeBybit:
    """Клиент, который ничего не приносит, но запоминает запрошенное окно."""

    def __init__(self):
        self.windows = []

    def assert_read_only(self):
        return {"readOnly": 1}

    def executions(self, start, end, category="linear"):
        self.windows.append((start, end))
        return iter(())

    def closed_pnl(self, start, end, category="linear"):
        return iter(())

    def positions(self, category="linear"):
        return []


class IncrementalBackfillTest(unittest.TestCase):
    """Круг раз в минуту не должен тянуть неделю истории заново."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        self.client = FakeBybit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _backfill(self, since_last):
        args = argparse.Namespace(days=90, category="linear", since_last=since_last)
        with unittest.mock.patch.object(cli.bybit, "Bybit", lambda: self.client), \
             unittest.mock.patch.object(cli.db, "connect", lambda: self.conn):
            return cli.cmd_backfill(args)

    def test_first_run_takes_the_full_period(self):
        self._backfill(since_last=True)
        start, end = self.client.windows[0]
        self.assertGreater(end - start, 89 * 24 * HOUR,
                           "без отметки края первый заход обязан взять весь период")

    def test_second_run_asks_only_for_the_new_part(self):
        self._backfill(since_last=True)
        first_end = self.client.windows[0][1]

        self._backfill(since_last=True)
        start, end = self.client.windows[1]
        self.assertLess(end - start, 2 * HOUR, "второй круг тянет всё заново")
        self.assertLess(start, first_end,
                        "окна обязаны перекрываться: опоздавший fill не должен"
                        " провалиться в дыру между ними")

    def test_full_backfill_ignores_the_edge(self):
        self._backfill(since_last=True)
        self._backfill(since_last=False)
        start, end = self.client.windows[1]
        self.assertGreater(end - start, 89 * 24 * HOUR)

    def test_edge_is_stored_per_category(self):
        self._backfill(since_last=True)
        row = self.conn.execute(
            "SELECT category, synced_to FROM sync_state").fetchone()
        self.assertEqual(row["category"], "linear")
        self.assertIsNotNone(row["synced_to"])


class WalModeTest(unittest.TestCase):
    """Читатель не должен получать «database is locked» во время записи."""

    def test_database_is_in_wal_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = db.connect(Path(tmp) / "test.db")
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "wal")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
