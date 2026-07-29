"""Схема и миграции.

Здесь защищается вещь, которую тесты на свежей базе поймать не могут в
принципе: физический порядок колонок у мигрировавшей базы отличается от
порядка в SCHEMA. `ALTER TABLE ... ADD COLUMN` дописывает колонку в конец,
а не туда, где она объявлена, поэтому безымянный `INSERT ... VALUES` пишет
значения не в те колонки — молча и только в проде.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import db  # noqa: E402

# Таблица fills до того, как появилась колонка seq. Ровно в таком виде база
# была заведена в июле, и миграция дописала seq пятнадцатой, а не тринадцатой.
LEGACY_EXECUTIONS = """
CREATE TABLE raw_executions (
    exec_id      TEXT PRIMARY KEY,
    category     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    position_idx INTEGER NOT NULL,
    side         TEXT NOT NULL,
    exec_type    TEXT NOT NULL,
    exec_price   TEXT NOT NULL,
    exec_qty     TEXT NOT NULL,
    exec_fee     TEXT NOT NULL,
    fee_currency TEXT,
    closed_size  TEXT,
    exec_time    INTEGER NOT NULL,
    order_id     TEXT,
    raw          TEXT NOT NULL
);
"""

EXECUTION = {
    "execId": "e-1", "symbol": "BTCUSDT", "side": "Buy", "execType": "Trade",
    "execPrice": "100", "execQty": "1", "execFee": "0.1", "execTime": "1000",
    "seq": 42, "orderId": "3df08a07-c88f-4c86-9f0a-09bfadb274e0",
    "positionIdx": 0, "feeCurrency": "USDT", "closedSize": "0",
}


class LegacyColumnOrderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "legacy.db"
        # База, заведённая до появления seq.
        old = sqlite3.connect(self.path)
        old.executescript(LEGACY_EXECUTIONS)
        old.commit()
        old.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_values_land_in_the_right_columns_after_migration(self):
        conn = db.connect(self.path)
        try:
            columns = [r[1] for r in conn.execute("PRAGMA table_info(raw_executions)")]
            self.assertEqual(columns[-1], "seq",
                             "миграция обязана дописать seq в конец — иначе тест не о том")

            db.save_executions(conn, [EXECUTION])
            row = conn.execute("SELECT * FROM raw_executions").fetchone()

            self.assertEqual(row["order_id"], EXECUTION["orderId"],
                             "в order_id уехало не то: по нему сделка сопоставляется"
                             " с closed-pnl биржи, и сверка перестанет находить пару")
            self.assertEqual(row["seq"], 42,
                             "в seq уехало не то: по нему сортируются fills внутри"
                             " миллисекунды, и склейка порвёт позиции не там")
            self.assertEqual(json.loads(row["raw"])["execId"], "e-1")
        finally:
            conn.close()

    def test_already_shifted_rows_are_repaired(self):
        """База, куда успели записать провёрнутые значения, чинится сама."""
        conn = db.connect(self.path)
        try:
            conn.execute(
                "INSERT INTO raw_executions (exec_id, category, symbol, position_idx,"
                " side, exec_type, exec_price, exec_qty, exec_fee, fee_currency,"
                " closed_size, exec_time, order_id, raw, seq)"
                " VALUES ('e-2','linear','BTCUSDT',0,'Buy','Trade','100','1','0.1',"
                " 'USDT','0',1000, '42', ?, ?)",
                (EXECUTION["orderId"], json.dumps(EXECUTION)),
            )
            conn.commit()
        finally:
            conn.close()

        conn = db.connect(self.path)          # миграция ловит сдвиг на подключении
        try:
            row = conn.execute(
                "SELECT * FROM raw_executions WHERE exec_id = 'e-2'").fetchone()
            self.assertEqual(row["order_id"], EXECUTION["orderId"])
            self.assertEqual(row["seq"], 42)
            self.assertEqual(json.loads(row["raw"])["execId"], "e-1")
        finally:
            conn.close()


class ReattachJournalTest(unittest.TestCase):
    """Разбор не должен теряться, если у сделки поменялся хвост trade_id.

    Хвост — это id первого исполнения, а он зависит от порядка fills внутри
    миллисекунды. Починка этого порядка оставила склейку прежней, но сдвинула
    идентификаторы, и живые разборы повисли в воздухе.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        self.conn.execute(
            "INSERT INTO round_trips (trade_id, category, symbol, position_idx,"
            " direction, opened_at, closed_at, qty, avg_entry, fees, funding)"
            " VALUES ('BTCUSDT:0:1000:новый', 'linear', 'BTCUSDT', 0, 'long',"
            " 1000, 2000, '1', '100', '0', '0')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_note_follows_the_trade(self):
        from journal import journal
        journal.add_note(self.conn, "BTCUSDT:0:1000:старый", "вошёл по сигналу")

        db._reattach_journal(self.conn)

        moved = self.conn.execute(
            "SELECT body FROM notes WHERE trade_id = 'BTCUSDT:0:1000:новый'").fetchone()
        self.assertEqual(moved["body"], "вошёл по сигналу")
        old = self.conn.execute(
            "SELECT body FROM notes WHERE trade_id = 'BTCUSDT:0:1000:старый'").fetchone()
        self.assertEqual(old["body"], "", "старая строка обязана остаться тумбстоном")

    def test_ambiguous_match_is_left_alone(self):
        """Две сделки под один префикс — молча привязывать нельзя."""
        from journal import journal
        self.conn.execute(
            "INSERT INTO round_trips (trade_id, category, symbol, position_idx,"
            " direction, opened_at, closed_at, qty, avg_entry, fees, funding)"
            " VALUES ('BTCUSDT:0:1000:второй', 'linear', 'BTCUSDT', 0, 'long',"
            " 1000, 3000, '1', '100', '0', '0')"
        )
        journal.add_note(self.conn, "BTCUSDT:0:1000:старый", "разбор")

        db._reattach_journal(self.conn)

        self.assertEqual(
            self.conn.execute(
                "SELECT body FROM notes WHERE trade_id = 'BTCUSDT:0:1000:старый'"
            ).fetchone()["body"], "разбор")

    def test_unrelated_orphan_is_not_grabbed(self):
        """Заметка чужого инструмента не должна прилипнуть к соседней сделке."""
        from journal import journal
        journal.add_note(self.conn, "ETHUSDT:0:1000:старый", "чужой разбор")

        db._reattach_journal(self.conn)

        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM notes WHERE trade_id = 'BTCUSDT:0:1000:новый'").fetchone())


if __name__ == "__main__":
    unittest.main()
