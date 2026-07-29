"""Журнал: намерения до входа, разбор после, покрытие и R-multiple.

Разделение pre/post — не удобство, а защита от ретроспективной рационализации:
комментарий, написанный после закрытия, всегда звучит как «я так и думал».
"""

import secrets
import sqlite3
import time
from decimal import Decimal

from .db import dec

# Насколько долго намерение ждёт свою сделку. Дольше суток — это уже другая идея,
# а не отложенный вход по той же.
MATCH_WINDOW_MS = 24 * 60 * 60 * 1000


def add_intent(conn, symbol, direction, thesis, *, entry_signal=None,
               planned_stop=None, planned_exit=None, tags=None, now_ms=None) -> int:
    if direction not in ("long", "short"):
        raise ValueError("direction: long | short")
    if not thesis or not thesis.strip():
        raise ValueError("Намерение без тезиса бессмысленно: писать нечего — не входи")

    cursor = conn.execute(
        "INSERT INTO intents (uid, symbol, direction, thesis, entry_signal, planned_stop,"
        " planned_exit, tags, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            new_id(), symbol.upper(), direction, thesis.strip(), entry_signal,
            str(planned_stop) if planned_stop is not None else None,
            str(planned_exit) if planned_exit is not None else None,
            tags, now_ms if now_ms is not None else int(time.time() * 1000),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def match_intents(conn: sqlite3.Connection) -> dict:
    """Привязывает намерения к сделкам. Идемпотентно, вызывается после rebuild.

    Правило: сделка того же символа и направления, открытая ПОСЛЕ намерения,
    в пределах окна, ещё никем не занятая. Берётся ближайшая по времени.
    """
    conn.execute(
        "UPDATE intents SET matched_trade_id = NULL, match_note = NULL"
        " WHERE matched_trade_id NOT IN (SELECT trade_id FROM round_trips)"
        "   AND matched_trade_id IS NOT NULL"
    )

    pending = conn.execute(
        "SELECT * FROM intents WHERE matched_trade_id IS NULL ORDER BY created_at"
    ).fetchall()

    matched, ambiguous = 0, []
    for intent in pending:
        candidates = conn.execute(
            "SELECT trade_id, opened_at FROM round_trips"
            " WHERE symbol = ? AND direction = ? AND opened_at >= ? AND opened_at <= ?"
            "   AND trade_id NOT IN (SELECT matched_trade_id FROM intents"
            "                        WHERE matched_trade_id IS NOT NULL)"
            " ORDER BY opened_at",
            (
                intent["symbol"], intent["direction"],
                intent["created_at"], intent["created_at"] + MATCH_WINDOW_MS,
            ),
        ).fetchall()

        if not candidates:
            continue

        note = None
        if len(candidates) > 1:
            # Несколько входов по одному символу подряд — привязка к ближайшему
            # может быть неверной. Помечаем, а не делаем вид, что всё однозначно.
            note = f"кандидатов было {len(candidates)}, взят ближайший"
            ambiguous.append(intent["intent_id"])

        conn.execute(
            "UPDATE intents SET matched_trade_id = ?, match_note = ? WHERE intent_id = ?",
            (candidates[0]["trade_id"], note, intent["intent_id"]),
        )
        matched += 1

    conn.commit()
    return {"matched": matched, "ambiguous": ambiguous, "pending": len(pending) - matched}


def r_multiple(conn: sqlite3.Connection, trade_id: str) -> Decimal | None:
    """R считается ТОЛЬКО если стоп был записан до входа. Иначе None, не ноль.

    Стоп, восстановленный по памяти после закрытия, — это не данные, и вся
    R-статистика, построенная на нём, была бы фикцией.
    """
    row = conn.execute(
        "SELECT rt.avg_entry, rt.qty, rt.net_pnl, i.planned_stop"
        " FROM round_trips rt JOIN intents i ON i.matched_trade_id = rt.trade_id"
        " WHERE rt.trade_id = ? AND rt.closed_at IS NOT NULL",
        (trade_id,),
    ).fetchone()

    if row is None or not row["planned_stop"] or row["net_pnl"] is None:
        return None

    risk_per_unit = abs(dec(row["avg_entry"]) - dec(row["planned_stop"]))
    risk = risk_per_unit * dec(row["qty"])
    if risk == 0:
        return None
    return dec(row["net_pnl"]) / risk


def coverage(conn: sqlite3.Connection, since_ms: int = 0) -> dict:
    """Разобранность сделок — заголовочная метрика продукта (решение C).

    «Разобрана» = есть хоть какое-то объяснение: заметка постфактум (основной
    режим) ИЛИ намерение до входа (высший тир). Pre-trade считается отдельно
    как подпоказатель — разрыв между ними и есть измеритель рационализации,
    а не повод бить по рукам.
    """
    total = conn.execute(
        "SELECT COUNT(*) c FROM round_trips WHERE opened_at >= ?", (since_ms,)
    ).fetchone()["c"]
    annotated = conn.execute(
        "SELECT COUNT(*) c FROM round_trips rt"
        " WHERE rt.opened_at >= ? AND ("
        "   rt.trade_id IN (SELECT trade_id FROM notes WHERE body <> '')"
        "   OR rt.trade_id IN (SELECT matched_trade_id FROM intents"
        "                      WHERE matched_trade_id IS NOT NULL))",
        (since_ms,),
    ).fetchone()["c"]
    with_intent = conn.execute(
        "SELECT COUNT(*) c FROM round_trips rt"
        " WHERE rt.opened_at >= ? AND rt.trade_id IN"
        "   (SELECT matched_trade_id FROM intents WHERE matched_trade_id IS NOT NULL)",
        (since_ms,),
    ).fetchone()["c"]
    with_stop = conn.execute(
        "SELECT COUNT(*) c FROM round_trips rt JOIN intents i"
        "   ON i.matched_trade_id = rt.trade_id"
        " WHERE rt.opened_at >= ? AND i.planned_stop IS NOT NULL",
        (since_ms,),
    ).fetchone()["c"]

    return {
        "trades": total,
        "annotated": annotated,
        "with_intent": with_intent,
        "with_planned_stop": with_stop,
        "missing": total - annotated,
        "share": (annotated / total) if total else None,
    }


def add_note(conn: sqlite3.Connection, trade_id: str, body: str) -> None:
    """Пишет разбор. Пустое тело — это стирание, но строка остаётся.

    Удалять нельзя: журнал синхронизируется в обе стороны, и удалённая строка
    вернулась бы с другой стороны первым же кругом. Поэтому «нет разбора»
    везде проверяется как `body <> ''`, а не как отсутствие записи.
    """
    conn.execute(
        "INSERT INTO notes (trade_id, body, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(trade_id) DO UPDATE SET body = excluded.body,"
        " updated_at = excluded.updated_at",
        (trade_id, body, int(time.time() * 1000)),
    )
    conn.commit()


def unjournaled(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    """Закрытые сделки без разбора — ни заметки, ни намерения (решение C)."""
    return conn.execute(
        "SELECT * FROM round_trips WHERE closed_at IS NOT NULL"
        " AND trade_id NOT IN (SELECT trade_id FROM notes WHERE body <> '')"
        " AND trade_id NOT IN (SELECT matched_trade_id FROM intents"
        "                      WHERE matched_trade_id IS NOT NULL)"
        " ORDER BY closed_at DESC LIMIT ?",
        (limit,),
    ).fetchall()


# --- правила торговли ------------------------------------------------------
#
# Правила пишет сам трейдер, а нарушения отмечаются при разборе — постфактум,
# как и всё остальное в продукте (решение C). Смысл не в дисциплине по чеклисту,
# а в том, чтобы «я нарушил своё правило» стало измеримым: без отметок это
# ощущение, с отметками — разница в P&L между двумя группами сделок.


def new_id() -> str:
    """Идентификатор, который не столкнётся с выданным на другой машине."""
    return secrets.token_hex(8)


def add_rule(conn: sqlite3.Connection, body: str) -> str:
    text = body.strip()
    if not text:
        raise ValueError("Пустое правило нечего соблюдать")
    rule_id, now = new_id(), int(time.time() * 1000)
    conn.execute(
        "INSERT INTO rules (rule_id, body, active, created_at, updated_at)"
        " VALUES (?,?,1,?,?)",
        (rule_id, text, now, now),
    )
    conn.commit()
    return rule_id


def edit_rule(conn: sqlite3.Connection, rule_id: str, *, body: str | None = None,
              active: bool | None = None) -> bool:
    """Правка текста и архивация. Возвращает False, если правила нет.

    Архивация вместо удаления: на правило ссылаются нарушения уже разобранных
    сделок, а `DELETE` вдобавок не пережил бы двусторонний синк.
    """
    assignments, params = [], []
    if body is not None:
        text = body.strip()
        if not text:
            raise ValueError("Пустое правило нечего соблюдать")
        assignments.append("body = ?")
        params.append(text)
    if active is not None:
        assignments.append("active = ?")
        params.append(1 if active else 0)
    if not assignments:
        return False

    assignments.append("updated_at = ?")
    params.extend([int(time.time() * 1000), rule_id])
    cursor = conn.execute(
        f"UPDATE rules SET {', '.join(assignments)} WHERE rule_id = ?", params
    )
    conn.commit()
    return cursor.rowcount > 0


def rules(conn: sqlite3.Connection, *, include_archived: bool = False) -> list[sqlite3.Row]:
    """Правила в порядке появления. Перестановок нет — порядок и так осмысленный."""
    query = "SELECT * FROM rules"
    if not include_archived:
        query += " WHERE active = 1"
    return conn.execute(query + " ORDER BY created_at, rule_id").fetchall()


def set_violation(conn: sqlite3.Connection, trade_id: str, rule_id: str,
                  broken: bool) -> None:
    conn.execute(
        "INSERT INTO rule_violations (trade_id, rule_id, broken, updated_at)"
        " VALUES (?,?,?,?) ON CONFLICT(trade_id, rule_id) DO UPDATE SET"
        " broken = excluded.broken, updated_at = excluded.updated_at",
        (trade_id, rule_id, 1 if broken else 0, int(time.time() * 1000)),
    )
    conn.commit()


def violations_by_trade(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Нарушенные правила по сделкам. Снятые (broken=0) не попадают."""
    result: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT trade_id, rule_id FROM rule_violations WHERE broken = 1"
    ):
        result.setdefault(row["trade_id"], []).append(row["rule_id"])
    return result
