"""Telegram-бот: экраны, кнопки и границы. Без сети и без токена.

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


def press(data, chat_id=MY_CHAT, message_id=500):
    return {"update_id": 2, "callback_query": {
        "id": "cb1", "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat_id}},
    }}


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

    def texts(self, actions):
        return " ".join(a.get("text", "") for a in actions)

    # --- границы ---------------------------------------------------------

    def test_foreign_chat_gets_silence(self):
        """Чужому чату — ни слова: ответ подтвердил бы существование бота."""
        for text in ("/stats", "/pending", "/trades 7d", "привет", "/start"):
            self.assertEqual(self.handle(update(text, chat_id=OTHER_CHAT)), [],
                             f"на «{text}» из чужого чата был ответ")

    def test_foreign_chat_button_press_ignored(self):
        """Кнопку тоже может нажать чужой — проверяется отдельно от сообщений."""
        self.assertEqual(self.handle(press("stats:30", chat_id=OTHER_CHAT)), [])

    def test_no_balance_or_positions_in_any_screen(self):
        forbidden = ("баланс", "депозит", "equity", "wallet", "открытая позиция")
        screens = ["/menu", "/stats", "/stats 7d", "/trades", "/pending"]
        presses = ["stats:30", "trades:7", "top:30", "menu:0", "report:30"]
        for text in screens:
            joined = self.texts(self.handle(update(text))).lower()
            for word in forbidden:
                self.assertNotIn(word, joined, f"«{word}» просочилось в {text}")
        for data in presses:
            joined = self.texts(self.handle(press(data))).lower()
            for word in forbidden:
                self.assertNotIn(word, joined, f"«{word}» просочилось в кнопку {data}")

    def test_open_trades_never_listed(self):
        db.save_executions(self.conn, [
            fill("o1", "ETHUSDT", "Buy", "100", "1", 30 * HOUR),  # осталась открытой
        ])
        roundtrips.rebuild(self.conn)
        self.assertNotIn("ETHUSDT", self.texts(self.handle(update("/trades"))))

    # --- экраны и навигация ----------------------------------------------

    def test_start_shows_menu_with_buttons(self):
        actions = self.handle(update("/start"))
        self.assertEqual(len(actions), 1)
        self.assertIn("Trade Journal", actions[0]["text"])
        self.assertTrue(actions[0]["keyboard"], "у меню должны быть кнопки")

    def test_stats_screen_has_period_switcher(self):
        actions = self.handle(update("/stats"))
        labels = [b[0] for row in actions[0]["keyboard"] for b in row]
        self.assertIn("7д", labels)
        self.assertIn("· 30д ·", labels, "текущий период должен быть помечен")

    def test_losing_period_does_not_crash_the_stats_screen(self):
        """Период без единой прибыли ронял бота насмерть.

        Рядом с отношением прибыль/убыток печатается «сколько нужно для
        безубытка» = (1 − win_rate) / win_rate. При нулевом win rate это
        деление на ноль, а ZeroDivisionError не ловится циклом опроса —
        процесс бота просто умирал до перезапуска systemd.
        """
        self.conn.execute("DELETE FROM raw_executions")
        db.save_executions(self.conn, [
            fill("l1", "BTCUSDT", "Buy", "100", "1", 10 * HOUR),
            fill("l2", "BTCUSDT", "Sell", "90", "1", 11 * HOUR),
        ])
        roundtrips.rebuild(self.conn)

        text = self.texts(self.handle(update("/stats")))
        self.assertIn("Побед", text, "экран должен строиться, а не падать")
        self.assertNotIn("П/У", text,
                         "без побед отношения прибыль/убыток не существует")

    def test_button_edits_message_instead_of_spamming(self):
        """Переключение периода правит сообщение на месте, а не шлёт новое."""
        actions = self.handle(press("stats:7", message_id=777))
        self.assertEqual(actions[0]["kind"], "answer", "спиннер кнопки надо погасить")
        edits = [a for a in actions if a["kind"] == "edit"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["message_id"], 777)
        self.assertIn("7 дней", edits[0]["text"])

    def test_unknown_button_still_answers_callback(self):
        actions = self.handle(press("nonsense:1"))
        self.assertEqual([a["kind"] for a in actions], ["answer"])

    def test_top_and_report_screens_reachable(self):
        self.assertIn("Топ прибыльных", self.texts(self.handle(press("top:30"))))
        self.assertIn("Статистика", self.texts(self.handle(press("report:30"))))

    def test_command_with_bot_username(self):
        self.assertIn("Статистика", self.texts(self.handle(update("/stats@journal_bot"))))

    def test_period_aliases(self):
        for arg, label in (("7d", "7 дней"), ("7д", "7 дней"), ("all", "всё время")):
            self.assertIn(label, self.texts(self.handle(update(f"/stats {arg}"))),
                          f"период «{arg}» не распознан")

    def test_bad_period_is_explained(self):
        self.assertIn("Не понял период", self.texts(self.handle(update("/stats вчера"))))

    def test_unknown_command_and_chatter_show_menu(self):
        for text in ("/whatever", "как дела"):
            self.assertIn("Trade Journal", self.texts(self.handle(update(text))))

    # --- разбор ----------------------------------------------------------

    def test_pending_sends_message_per_trade(self):
        actions = self.handle(update("/pending"))
        self.assertGreater(len(actions), 1)
        self.assertIsNone(actions[0]["trade_id"])
        self.assertTrue(all(a["trade_id"] for a in actions[1:]))

    def test_pending_empty_when_all_annotated(self):
        for row in self.conn.execute("SELECT trade_id FROM round_trips").fetchall():
            journal.add_note(self.conn, row["trade_id"], "разобрано")
        self.assertIn("разобраны", self.texts(self.handle(update("/pending"))))

    def test_reply_saves_note(self):
        trade_id = self.conn.execute(
            "SELECT trade_id FROM round_trips LIMIT 1").fetchone()["trade_id"]
        self.conn.execute("INSERT INTO bot_messages VALUES (?,?,?)", (77, trade_id, 0))
        self.conn.commit()

        actions = self.handle(update("вошёл на импульсе, плана не было", reply_to=77))
        self.assertIn("Разбор сохранён", actions[0]["text"])
        saved = self.conn.execute(
            "SELECT body FROM notes WHERE trade_id = ?", (trade_id,)).fetchone()
        self.assertEqual(saved["body"], "вошёл на импульсе, плана не было")

    def test_reply_to_unknown_message_is_explained(self):
        text = self.texts(self.handle(update("разбор", reply_to=404)))
        self.assertIn("Разобрать", text)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"], 0)

    def test_command_in_reply_stays_command(self):
        trade_id = self.conn.execute(
            "SELECT trade_id FROM round_trips LIMIT 1").fetchone()["trade_id"]
        self.conn.execute("INSERT INTO bot_messages VALUES (?,?,?)", (78, trade_id, 0))
        self.conn.commit()

        self.assertIn("Статистика", self.texts(self.handle(update("/stats", reply_to=78))))
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) c FROM notes").fetchone()["c"], 0)

    # --- вёрстка ---------------------------------------------------------

    def test_trades_are_not_code_blocks(self):
        """Список сделок — обычный текст: моноширинный блок выглядит выгрузкой."""
        for data in ("trades:30", "top:30"):
            text = self.texts(self.handle(press(data)))
            self.assertNotIn("<pre>", text, f"{data} всё ещё отдаёт код")
            self.assertIn("<b>", text, f"{data} потерял выделение тикера")

    def test_trades_paginate_through_everything(self):
        """Листалка доходит до последней сделки, а не обрывается на первой странице."""
        base = 40 * HOUR
        for i in range(telegram.MAX_TRADES + 4):
            db.save_executions(self.conn, [
                fill(f"p{i}a", "ADAUSDT", "Buy", "10", "1", base + i * 2 * HOUR),
                fill(f"p{i}b", "ADAUSDT", "Sell", "11", "1", base + (i * 2 + 1) * HOUR),
            ])
        roundtrips.rebuild(self.conn)
        total = self.conn.execute(
            "SELECT COUNT(*) c FROM round_trips WHERE closed_at IS NOT NULL"
        ).fetchone()["c"]

        first = self.handle(press("trades:0"))
        text = self.texts(first)
        self.assertIn(f"из {total}", text)
        labels = [b[0] for a in first if a["kind"] == "edit"
                  for row in a["keyboard"] for b in row]
        self.assertTrue(any("Ещё" in l for l in labels), "нет кнопки «Ещё»")

        # вторая страница показывает остаток и умеет вернуться в начало
        second = self.texts(self.handle(press(f"trades:0:{telegram.MAX_TRADES}")))
        self.assertIn(f"{telegram.MAX_TRADES + 1}–{total} из {total}", second)
        labels2 = [b[0] for a in self.handle(press(f"trades:0:{telegram.MAX_TRADES}"))
                   if a["kind"] == "edit" for row in a["keyboard"] for b in row]
        self.assertTrue(any("В начало" in l for l in labels2))

    def test_annotated_trade_is_marked_in_list(self):
        trade_id = self.conn.execute(
            "SELECT trade_id FROM round_trips WHERE symbol='BTCUSDT'").fetchone()["trade_id"]
        journal.add_note(self.conn, trade_id, "разобрано")
        self.assertIn("📝", self.texts(self.handle(press("trades:30"))))

    def test_symbol_shortened(self):
        """USDT-суффикс не несёт информации и съедает ширину на узком экране."""
        self.assertEqual(telegram.short_symbol("BTCUSDT"), "BTC")
        self.assertEqual(telegram.short_symbol("FARTCOINUSDT"), "FARTCOIN")
        self.assertEqual(telegram.short_symbol("XAGUSD"), "XAGUSD", "не-USDT не режем")
        text = self.texts(self.handle(press("trades:30")))
        self.assertIn("<b>BTC</b>", text)
        self.assertNotIn("BTCUSDT", text)

    def test_money_formatting(self):
        self.assertEqual(telegram.money(231.05), "+231.05")
        self.assertEqual(telegram.money(-80.2), "−80.20")
        self.assertTrue(telegram.money(1234.5).endswith("1 234.50"))

    def test_monospace_block_fits_phone_width(self):
        """Строки моноширинных блоков не шире 30 знаков — иначе ломается на экране."""
        import re
        for data in ("stats:30", "trades:30", "top:30"):
            text = self.texts(self.handle(press(data)))
            for block in re.findall(r"<pre>(.*?)</pre>", text, re.S):
                for line in block.splitlines():
                    self.assertLessEqual(len(line), 30, f"длинная строка в {data}: {line!r}")

    def test_html_is_escaped_in_full_report(self):
        edit = next(a for a in self.handle(press("report:30")) if a["kind"] == "edit")
        text = edit["text"]
        self.assertTrue(text.startswith("<pre>") and text.endswith("</pre>"))
        self.assertNotIn("<pre>", text[5:-6], "содержимое отчёта должно быть экранировано")


if __name__ == "__main__":
    unittest.main()
