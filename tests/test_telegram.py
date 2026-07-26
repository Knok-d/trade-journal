"""Telegram-бот: команды и границы. Без сети и без токена.

Проверяются в первую очередь не команды, а инварианты: чужой чат остаётся без
ответа, баланс и открытые позиции не утекают, разбор с телефона доходит до базы.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from journal import db, journal, roundtrips, telegram  # noqa: E402
from tests.test_roundtrips import fill  # noqa: E402

HOUR = 3_600_000
MY_CHAT = 12345
OTHER_CHAT = 99999


def update(text, chat_id=MY_CHAT, reply_to=None, message_id=1):
    message = {"message_id": message_id, "chat": {"id": chat_id}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": message}


class TelegramTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "test.db")
        db.save_executions(self.conn, [
            fill("e1", "BTCUSDT", "Buy", "100", "1", 10 * HOUR, fee="0.1"),
            fill("e2", "BTCUSDT", "Sell", "110", "1", 11 * HOUR, fee="0.1"),
            fill("s1", "SOLUSDT", "Sell", "10", "10", 20 * HOUR),
            fill("s2", "SOLUSDT", "Buy", "9", "10", 21 * HOUR),
        ])
        roundtrips.rebuild(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def handle(self, upd):
        return telegram.handle_update(self.conn, upd, MY_CHAT)

    # --- границы ---------------------------------------------------------

    def test_foreign_chat_gets_silence(self):
        """Чужому чату — ни слова: ответ подтвердил бы существование бота."""
        for text in ("/stats", "/pending", "/trades 7d", "привет"):
            self.assertEqual(self.handle(update(text, chat_id=OTHER_CHAT)), [],
                             f"на «{text}» из чужого чата был ответ")

    def test_no_balance_or_positions_in_any_reply(self):
        """Ни одна команда не отдаёт баланс, депозит и открытые позиции."""
        forbidden = ("баланс", "депозит", "equity", "wallet", "открытая позиция")
        for text in ("/stats", "/stats 7d", "/trades", "/pending", "/help"):
            joined = " ".join(r["text"] for r in self.handle(update(text))).lower()
            for word in forbidden:
                self.assertNotIn(word, joined, f"«{word}» просочилось в ответ на {text}")

    def test_open_trades_never_listed(self):
        """В выдачу попадают только закрытые сделки."""
        db.save_executions(self.conn, [
            fill("o1", "ETHUSDT", "Buy", "100", "1", 30 * HOUR),  # осталась открытой
        ])
        roundtrips.rebuild(self.conn)
        joined = " ".join(r["text"] for r in self.handle(update("/trades")))
        self.assertNotIn("ETHUSDT", joined)

    # --- команды ---------------------------------------------------------

    def test_stats_returns_report(self):
        replies = self.handle(update("/stats"))
        self.assertEqual(len(replies), 1)
        self.assertIn("Статистика", replies[0]["text"])

    def test_command_with_bot_username(self):
        """/stats@my_bot из группового чата — та же команда."""
        self.assertIn("Статистика", self.handle(update("/stats@journal_bot"))[0]["text"])

    def test_bad_period_is_explained(self):
        text = self.handle(update("/stats вчера"))[0]["text"]
        self.assertIn("Не понял период", text)

    def test_unknown_command_and_chatter_get_help(self):
        for text in ("/whatever", "как дела"):
            self.assertIn("/pending", self.handle(update(text))[0]["text"])

    def test_pending_sends_message_per_trade(self):
        replies = self.handle(update("/pending"))
        # первое — сводка, дальше по сообщению на сделку с привязкой
        self.assertGreater(len(replies), 1)
        self.assertIsNone(replies[0]["trade_id"])
        self.assertTrue(all(r["trade_id"] for r in replies[1:]))

    def test_pending_empty_when_all_annotated(self):
        for row in self.conn.execute("SELECT trade_id FROM round_trips").fetchall():
            journal.add_note(self.conn, row["trade_id"], "разобрано")
        self.assertIn("разобраны", self.handle(update("/pending"))[0]["text"])

    # --- разбор ответом --------------------------------------------------

    def test_reply_saves_note(self):
        trade_id = self.conn.execute(
            "SELECT trade_id FROM round_trips LIMIT 1").fetchone()["trade_id"]
        self.conn.execute("INSERT INTO bot_messages VALUES (?,?,?)", (77, trade_id, 0))
        self.conn.commit()

        replies = self.handle(update("вошёл на импульсе, плана не было", reply_to=77))
        self.assertIn("Разбор сохранён", replies[0]["text"])
        saved = self.conn.execute(
            "SELECT body FROM notes WHERE trade_id = ?", (trade_id,)).fetchone()
        self.assertEqual(saved["body"], "вошёл на импульсе, плана не было")

    def test_reply_to_unknown_message_is_explained(self):
        text = self.handle(update("разбор", reply_to=404))[0]["text"]
        self.assertIn("/pending", text)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"], 0)

    def test_command_in_reply_stays_command(self):
        """Ответ, начинающийся со слэша, — команда, а не текст разбора."""
        trade_id = self.conn.execute(
            "SELECT trade_id FROM round_trips LIMIT 1").fetchone()["trade_id"]
        self.conn.execute("INSERT INTO bot_messages VALUES (?,?,?)", (78, trade_id, 0))
        self.conn.commit()

        replies = self.handle(update("/stats", reply_to=78))
        self.assertIn("Статистика", replies[0]["text"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"], 0)

    def test_html_is_escaped_in_report(self):
        """Отчёт уходит в <pre>, содержимое экранируется — вёрстка не ломается."""
        text = self.handle(update("/stats"))[0]["text"]
        self.assertTrue(text.startswith("<pre>") and text.endswith("</pre>"))
        self.assertNotIn("<pre>", text[5:-6])


if __name__ == "__main__":
    unittest.main()
