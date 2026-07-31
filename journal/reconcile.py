"""Ворота шага 1: посделочная сверка нашей склейки с расчётом самой биржи.

Агрегатная сверка бесполезна — компенсирующие ошибки (пропущенный фандинг в одну
сторону, задвоенная комиссия в другую) дают идеальный итог при двух багах.
Поэтому критерий применяется к каждой сделке отдельно.

`closedPnl` у Bybit = gross − комиссии − фандинг, то есть сравнивается с нашим
`net_pnl` целиком. Проверено арифметикой на реальных сделках: на LABUSDT
их closedPnl минус наш (gross − fees) дал ровно наш funding, знак в знак,
до восьмого знака. Более раннее утверждение «closedPnl не включает фандинг»
пришло из веб-поиска и оказалось ложным — оно давало систематическое
расхождение, растущее со временем удержания позиции.
"""

import sqlite3
from decimal import ROUND_HALF_UP, Decimal

from .db import dec

TOLERANCE = Decimal("0.01")   # 1% на каждую сделку


def _candidates(conn: sqlite3.Connection, trade) -> list:
    """Записи closed-pnl биржи, относящиеся к нашей сделке.

    Сопоставление по ID закрывающего ордера, а не по времени. Временное окно
    здесь принципиально ненадёжно: при перевороте предыдущая позиция
    закрывается за миллисекунды до открытия следующей (наблюдалось 4 мс), и её
    P&L приписывался соседней сделке. Биржа дробит закрытие на несколько
    записей, поэтому их может быть больше одной.
    """
    return conn.execute(
        "SELECT p.* FROM exchange_pnl p"
        " JOIN trade_close_orders o ON o.order_id = p.order_id"
        " WHERE o.trade_id = ? ORDER BY p.updated_time",
        (trade["trade_id"],),
    ).fetchall()


def apply_exchange_fees(conn: sqlite3.Connection) -> dict:
    """Подставляет комиссию из closed-pnl там, где в fills она в чужой валюте.

    Комиссия в MNT (скидочный токен Bybit) не переводится в USDT средствами
    fills — курса в них нет. Биржа отдаёт готовый эквивалент в openFee/closeFee,
    и это единственный доступный точный источник. Такие сделки помечаются
    `fees_source='exchange'`: их комиссия при сверке уже не проверяется
    независимо, и прятать это нельзя.
    """
    patched, unresolved = 0, []
    rows = conn.execute(
        "SELECT * FROM round_trips WHERE fee_mixed = 1 AND closed_at IS NOT NULL"
    ).fetchall()

    for trade in rows:
        candidates = _candidates(conn, trade)
        if not candidates:
            unresolved.append(trade["trade_id"])
            continue
        fees = sum(dec(c["open_fee"]) + dec(c["close_fee"]) for c in candidates)
        net = dec(trade["gross_pnl"]) - fees - dec(trade["funding"])
        conn.execute(
            "UPDATE round_trips SET fees = ?, net_pnl = ?, fees_source = 'exchange'"
            " WHERE trade_id = ?",
            (str(fees), str(net), trade["trade_id"]),
        )
        patched += 1

    conn.commit()
    return {"patched": patched, "unresolved": unresolved}


def apply_leverage(conn: sqlite3.Connection) -> dict:
    """Проставляет сделкам плечо и объём входа из closed-pnl биржи.

    Из fills плечо не выводится — это настройка позиции, а не свойство
    исполнения. Единственный источник тут биржа, и там, где сделка с closed-pnl
    не сопоставилась, обе величины остаются пустыми: PnL% тогда честно
    недоступен, а не равен нулю.

    Объём входа складывается по всем закрывающим записям — биржа дробит
    закрытие на несколько, и каждая несёт свою часть позиции. Плечо берётся из
    первой: у одной позиции оно общее, потому что задаётся до входа.
    """
    filled, missing = 0, []
    rows = conn.execute(
        # Только свои: у сделок с MT5 пары в closed-pnl нет по построению, и
        # они попадали бы в «без данных биржи» каждым прогоном как шум.
        "SELECT * FROM round_trips WHERE closed_at IS NOT NULL AND source = 'fills'"
    ).fetchall()

    for trade in rows:
        candidates = _candidates(conn, trade)
        leverage = next((c["leverage"] for c in candidates if c["leverage"]), None)
        if not leverage:
            missing.append(trade["trade_id"])
            continue
        entry_value = sum(dec(c["entry_value"] or 0) for c in candidates)
        conn.execute(
            "UPDATE round_trips SET leverage = ?, entry_value = ? WHERE trade_id = ?",
            (leverage, str(entry_value), trade["trade_id"]),
        )
        filled += 1

    conn.commit()
    return {"filled": filled, "missing": missing}


def reconcile(conn: sqlite3.Connection) -> dict:
    """Сопоставляет наши round_trips с записями closed-pnl биржи (локальными).

    Сделки с MT5 в сверку не входят и войти не могут: второго источника по ним
    не существует — брокер отдаёт одну-единственную цифру, и сравнивать её не с
    чем. Это не «проверено и сошлось», а «проверить нечем», поэтому такие
    сделки считаются отдельно и названы в отчёте: молча пропустить их значило
    бы выдать непроверенное за проверенное.
    """
    ours = conn.execute(
        "SELECT * FROM round_trips WHERE closed_at IS NOT NULL"
        " AND source = 'fills' ORDER BY closed_at"
    ).fetchall()
    unverifiable = conn.execute(
        "SELECT COUNT(*) c FROM round_trips WHERE source <> 'fills'"
    ).fetchone()["c"]

    mismatches, unmatched, matched = [], [], 0
    from_exchange = 0

    for trade in ours:
        candidates = _candidates(conn, trade)
        if not candidates:
            unmatched.append(trade["trade_id"])
            continue
        if trade["fees_source"] == "exchange":
            from_exchange += 1

        matched += 1
        # Биржа может дробить закрытие позиции на несколько записей — суммируем.
        theirs = sum(dec(row["closed_pnl"]) for row in candidates)
        our_net = dec(trade["net_pnl"])

        denominator = abs(theirs)
        if denominator == 0:
            deviation = Decimal(0) if our_net == 0 else Decimal(1)
        else:
            deviation = abs(our_net - theirs) / denominator

        if deviation > TOLERANCE:
            # Цены входа сравниваются в точности биржи: наш Decimal хранит полный
            # период дроби, у Bybit 8 знаков. Без округления любое расхождение
            # P&L тянуло за собой ложное «вход не совпал».
            their_entry = dec(candidates[-1]["avg_entry"])
            our_entry = dec(trade["avg_entry"]).quantize(
                their_entry, rounding=ROUND_HALF_UP
            )
            mismatches.append(
                {
                    "trade_id": trade["trade_id"],
                    "symbol": trade["symbol"],
                    "ours": our_net,
                    "theirs": theirs,
                    "deviation": deviation,
                    "our_entry": our_entry,
                    "their_entry": their_entry,
                    "entry_differs": our_entry != their_entry,
                    "parts": len(candidates),
                }
            )

    return {
        "checked": len(ours),
        "matched": matched,
        "unmatched": unmatched,
        "mismatches": mismatches,
        "fees_from_exchange": from_exchange,
        "unverifiable": unverifiable,
        "passed": not mismatches and not unmatched and matched > 0,
    }


def format_report(result: dict) -> str:
    lines = [
        "=== Сверка с closed-pnl Bybit ===",
        f"Наших закрытых сделок: {result['checked']}",
        f"Сопоставлено: {result['matched']}",
    ]
    if result.get("fees_from_exchange"):
        lines.append(
            f"Из них с комиссией от биржи: {result['fees_from_exchange']}"
            " (комиссия в MNT — независимо не проверяется, склейка и gross проверены)"
        )
    if result.get("unverifiable"):
        lines.append(
            f"Вне сверки: {result['unverifiable']} сделок с MT5 — цифры от брокера,"
            " второго источника по ним нет. Не «сошлось», а «сравнить не с чем»."
        )
    if result["unmatched"]:
        lines.append(f"\nБЕЗ ПАРЫ у биржи ({len(result['unmatched'])}) — склейка выдумала сделку:")
        lines += [f"  {tid}" for tid in result["unmatched"][:20]]

    if result["mismatches"]:
        lines.append(f"\nРАСХОЖДЕНИЯ > 1% ({len(result['mismatches'])}):")
        for m in result["mismatches"][:20]:
            lines.append(
                f"  {m['symbol']:<12} наш {m['ours']:>12.4f} | биржа {m['theirs']:>12.4f}"
                f" | откл {m['deviation']:.2%} | частей: {m['parts']}"
            )
            if m["entry_differs"]:
                lines.append(
                    f"    вход не совпал: наш {m['our_entry']} vs {m['their_entry']}"
                    "  <- сначала чинить склейку, а не P&L"
                )

    lines.append("")
    lines.append("ВОРОТА ПРОЙДЕНЫ" if result["passed"] else "ВОРОТА НЕ ПРОЙДЕНЫ — "
                 "чинить docs/trade-definition.md, затем код. UI не начинать.")
    return "\n".join(lines)
