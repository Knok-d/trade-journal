"""Статистика по сверенным сделкам.

Три принципа, заданные советом и не подлежащие смягчению ради красоты отчёта:

1. **Метрика без n — дизайн-баг.** Любое число показывается вместе с размером
   выборки, а где возможно — с интервалом. 63% побед на 20 сделках и на 500 —
   разные утверждения, и отчёт обязан их различать.
2. **Средние по тяжёлым хвостам лгут.** Распределение показывается гистограммой;
   среднее идёт рядом с ним, а не вместо.
3. **Чего нет — того нет.** R-multiple без заранее записанного стопа не считается
   и помечается недоступным, а не подменяется расчётом задним числом.
"""

import random
import sqlite3
import time
from decimal import Decimal

from .db import dec

DAY_MS = 24 * 60 * 60 * 1000

# Через сколько часов без обновления данные считаются несвежими. Синк ходит
# раз в полчаса, так что три часа — это уже не «просто спал», а поломка.
STALE_AFTER_HOURS = 3

# Порог, ниже которого срез не показывается: на меньших ячейках «лучший сетап»
# находится всегда, потому что ищется в шуме.
MIN_SLICE_N = 30

# Границы интерпретации выборки (методолог совета):
#   <100  — только описание, никаких выводов
#   100-300 — гипотезы
#   300+  — осторожные выводы
SAMPLE_TIERS = ((100, "только описание, выводы делать рано"),
                (300, "хватает на гипотезы, не на выводы"),
                (10**9, "осторожные выводы допустимы"))


def sample_note(n: int) -> str:
    for threshold, note in SAMPLE_TIERS:
        if n < threshold:
            return note
    return SAMPLE_TIERS[-1][1]


def freshness(conn: sqlite3.Connection, now_ms: int | None = None) -> dict:
    """Когда данные последний раз брали с биржи и насколько это давно.

    Отдельно от «времени последней сделки»: если не торговал два дня, свежих
    сделок нет, а синк при этом исправен. Молчаливое устаревание — худший
    вид поломки для дневника, потому что интерфейс выглядит рабочим.
    """
    from .db import get_meta

    raw = get_meta(conn, "synced_at")
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    if raw is None:
        return {"synced_at": None, "age_hours": None, "stale": True}
    synced_at = int(raw)
    age_hours = (now - synced_at) / 3_600_000
    return {
        "synced_at": synced_at,
        "age_hours": age_hours,
        "stale": age_hours > STALE_AFTER_HOURS,
    }


def _closed(conn: sqlite3.Connection, days: int = 0) -> list[sqlite3.Row]:
    query = "SELECT * FROM round_trips WHERE closed_at IS NOT NULL"
    params: tuple = ()
    if days:
        query += " AND closed_at >= (SELECT MAX(closed_at) FROM round_trips) - ?"
        params = (days * DAY_MS,)
    return conn.execute(query + " ORDER BY closed_at", params).fetchall()


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Интервал Уилсона для доли — честнее нормального приближения на малых n."""
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z**2 / n
    centre = (phat + z**2 / (2 * n)) / denom
    margin = z * ((phat * (1 - phat) / n + z**2 / (4 * n**2)) ** 0.5) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def bootstrap_mean_ci(values: list[Decimal], iterations: int = 2000,
                      seed: int = 20260726) -> tuple[float, float]:
    """Перцентильный бутстрап для среднего.

    P&L имеет тяжёлые хвосты, поэтому t-интервал вокруг среднего вводит в
    заблуждение. Сид фиксирован: отчёт должен быть воспроизводимым.
    """
    if len(values) < 2:
        return (0.0, 0.0)
    rng = random.Random(seed)
    pool = [float(v) for v in values]
    n = len(pool)
    means = sorted(sum(rng.choices(pool, k=n)) / n for _ in range(iterations))
    return (means[int(0.025 * iterations)], means[int(0.975 * iterations)])


def summary(conn: sqlite3.Connection, days: int = 0) -> dict:
    rows = _closed(conn, days)
    if not rows:
        return {"n": 0}

    net = [dec(r["net_pnl"]) for r in rows]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x < 0]
    gross = sum(dec(r["gross_pnl"]) for r in rows)
    fees = sum(dec(r["fees"]) for r in rows)
    funding = sum(dec(r["funding"]) for r in rows)

    avg_win = sum(wins) / len(wins) if wins else Decimal(0)
    avg_loss = sum(losses) / len(losses) if losses else Decimal(0)

    return {
        "n": len(rows),
        "total": sum(net),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(rows),
        "win_rate_ci": wilson_interval(len(wins), len(rows)),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        # Во сколько раз средний выигрыш больше среднего проигрыша. Вместе с
        # win rate определяет знак матожидания: одного win rate недостаточно.
        "payoff": (avg_win / abs(avg_loss)) if avg_loss else None,
        "expectancy": sum(net) / len(rows),
        "expectancy_ci": bootstrap_mean_ci(net),
        "gross": gross,
        "fees": fees,
        "funding": funding,
        # Издержки в абсолюте. Доля от gross сознательно не считается: при
        # отрицательном gross это деление на модуль убытка — число, которое
        # выглядит осмысленным и им не является.
        "costs": fees + funding,
        "fees_from_exchange": sum(1 for r in rows if r["fees_source"] == "exchange"),
        "liquidated": sum(1 for r in rows if r["liquidated"]),
    }


def pnl_histogram(conn: sqlite3.Connection, days: int = 0, buckets: int = 9) -> dict:
    """Распределение P&L. Именно оно, а не среднее, устойчиво на малой выборке."""
    rows = _closed(conn, days)
    if not rows:
        return {"n": 0, "bins": []}

    values = sorted(float(dec(r["net_pnl"])) for r in rows)
    lo, hi = values[0], values[-1]
    if lo == hi:
        return {"n": len(values), "bins": [(lo, hi, len(values))]}

    width = (hi - lo) / buckets
    bins = []
    for i in range(buckets):
        start = lo + i * width
        end = start + width
        last = i == buckets - 1
        count = sum(1 for v in values if start <= v < end or (last and v == hi))
        bins.append((start, end, count))
    return {"n": len(values), "bins": bins, "min": lo, "max": hi}


def top_trades(conn: sqlite3.Connection, days: int = 0, limit: int = 10) -> dict:
    """Топ прибыльных сделок: монета, направление, сколько взято.

    Рядом отдаётся доля топа во всей валовой прибыли — концентрация результата
    в паре сделок сама по себе факт, который стоит видеть, а не выяснять.
    """
    rows = _closed(conn, days)
    winners = sorted(
        (r for r in rows if dec(r["net_pnl"] or 0) > 0),
        key=lambda r: dec(r["net_pnl"]),
        reverse=True,
    )
    total_wins = sum(dec(r["net_pnl"]) for r in winners)
    top = winners[:limit]
    top_sum = sum(dec(r["net_pnl"]) for r in top)
    return {
        "trades": [
            {
                "trade_id": r["trade_id"],
                "symbol": r["symbol"],
                "direction": r["direction"],
                "net_pnl": dec(r["net_pnl"]),
                "closed_at": r["closed_at"],
            }
            for r in top
        ],
        "winners_total": len(winners),
        "top_sum": top_sum,
        "share_of_wins": (top_sum / total_wins) if total_wins else None,
    }


def r_multiples(conn: sqlite3.Connection, days: int = 0) -> dict:
    """R-multiple по сделкам с заранее записанным стопом.

    Пусто — значит статистика недоступна, а не нулевая. Единственный способ её
    получить — записывать стоп ДО входа (`intent --stop`).
    """
    rows = conn.execute(
        "SELECT rt.trade_id, rt.avg_entry, rt.qty, rt.net_pnl, i.planned_stop"
        " FROM round_trips rt JOIN intents i ON i.matched_trade_id = rt.trade_id"
        " WHERE rt.closed_at IS NOT NULL AND i.planned_stop IS NOT NULL"
    ).fetchall()

    values = []
    for row in rows:
        risk = abs(dec(row["avg_entry"]) - dec(row["planned_stop"])) * dec(row["qty"])
        if risk:
            values.append(dec(row["net_pnl"]) / risk)

    total_closed = len(_closed(conn, days))
    return {
        "n": len(values),
        "of_total": total_closed,
        "values": sorted(values),
        "available": bool(values),
    }


def by_symbol(conn: sqlite3.Connection, days: int = 0,
              min_n: int = MIN_SLICE_N) -> dict:
    """Разрез по символам. Показываются только ячейки, где n достаточен.

    Возвращает и число скрытых срезов, и общее число проверенных — без этого
    читатель не видит, что «лучший символ» выбран из множества сравнений.
    """
    rows = _closed(conn, days)
    grouped: dict[str, list[Decimal]] = {}
    for row in rows:
        grouped.setdefault(row["symbol"], []).append(dec(row["net_pnl"]))

    shown = []
    for symbol, values in grouped.items():
        if len(values) < min_n:
            continue
        wins = sum(1 for v in values if v > 0)
        shown.append({
            "symbol": symbol,
            "n": len(values),
            "total": sum(values),
            "win_rate": wins / len(values),
            "win_rate_ci": wilson_interval(wins, len(values)),
        })
    shown.sort(key=lambda item: item["total"], reverse=True)
    return {"shown": shown, "hidden": len(grouped) - len(shown), "tested": len(grouped)}


def holding_time(conn: sqlite3.Connection, days: int = 0) -> dict:
    """Связь длительности удержания с результатом.

    Проверяет ходовую гипотезу «убыточные сделки держатся дольше» — то есть
    прибыль режется рано, а убыток пересиживается.
    """
    rows = _closed(conn, days)
    win_h, loss_h = [], []
    for row in rows:
        hours = (row["closed_at"] - row["opened_at"]) / 3_600_000
        (win_h if dec(row["net_pnl"]) > 0 else loss_h).append(hours)

    def median(xs):
        if not xs:
            return None
        s = sorted(xs)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    return {
        "median_win_hours": median(win_h),
        "median_loss_hours": median(loss_h),
        "n_wins": len(win_h),
        "n_losses": len(loss_h),
    }


def rule_stats(conn: sqlite3.Connection, days: int = 0,
               min_n: int = MIN_SLICE_N) -> dict:
    """Во что нарушение собственных правил обходится в деньгах.

    Считается только по РАЗОБРАННЫМ сделкам — тем, где есть непустая заметка.
    Галочки правил ставятся при разборе, поэтому у неразобранной сделки
    нарушений нет не потому, что их не было, а потому, что никто не смотрел.
    Попав в «чистые», такие сделки задрали бы их результат, и вывод получился
    бы про разобранность, а не про правила.

    `enough` отвечает на вопрос, можно ли вообще сравнивать: пока хоть одна из
    двух групп меньше `min_n`, разница между ними — шум. Цифры при этом
    показываются: скрывать их значило бы врать в другую сторону.
    """
    rows = _closed(conn, days)
    reviewed_ids = {
        r["trade_id"] for r in
        conn.execute("SELECT trade_id FROM notes WHERE body <> ''")
    }
    broken: dict[str, list[str]] = {}
    for row in conn.execute(
        "SELECT trade_id, rule_id FROM rule_violations WHERE broken = 1"
    ):
        broken.setdefault(row["trade_id"], []).append(row["rule_id"])

    reviewed = [r for r in rows if r["trade_id"] in reviewed_ids]
    violated = [r for r in reviewed if broken.get(r["trade_id"])]
    clean = [r for r in reviewed if not broken.get(r["trade_id"])]

    def group(items) -> dict:
        values = [dec(r["net_pnl"]) for r in items]
        return {
            "n": len(values),
            "total": sum(values) if values else Decimal(0),
            "avg": (sum(values) / len(values)) if values else None,
        }

    per_rule = []
    for rule in conn.execute(
        "SELECT rule_id, body, active FROM rules ORDER BY created_at, rule_id"
    ):
        hits = [r for r in reviewed if rule["rule_id"] in broken.get(r["trade_id"], ())]
        if not hits and not rule["active"]:
            continue          # архивное правило без нарушений показывать незачем
        measured = group(hits)
        measured.update({
            "rule_id": rule["rule_id"],
            "body": rule["body"],
            "active": bool(rule["active"]),
        })
        per_rule.append(measured)

    return {
        "reviewed": len(reviewed),
        "of_total": len(rows),
        "violated": group(violated),
        "clean": group(clean),
        "enough": min(len(violated), len(clean)) >= min_n,
        "min_n": min_n,
        "rules": per_rule,
    }
