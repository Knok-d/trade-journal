"""Текстовый отчёт по статистике.

Формат подчинён одному правилу: читатель не должен уметь вынести из отчёта
вывод, который данные не поддерживают. Отсюда n рядом с каждым числом,
интервалы, явное «недоступно» и предупреждение о числе сравнений.
"""

from . import journal, stats


def _bar(count: int, peak: int, width: int = 28) -> str:
    return "█" * max(1, round(count / peak * width)) if count else ""


def render(conn, days: int = 0) -> str:
    s = stats.summary(conn, days)
    if not s["n"]:
        print_period = f"за {days} дн." if days else "за всё время"
        return f"Закрытых сделок {print_period} нет. Сначала backfill и rebuild."

    period = f"за последние {days} дн." if days else "за всю историю"
    out = [f"=== Статистика {period} ==="]

    lo, hi = s["win_rate_ci"]
    exp_lo, exp_hi = s["expectancy_ci"]
    out += [
        "",
        f"Сделок: {s['n']}   ({stats.sample_note(s['n'])})",
        f"Итог: {s['total']:+.2f} USDT",
        "",
        f"Прибыльных: {s['wins']} из {s['n']} = {s['win_rate']:.0%}"
        f"   (95% интервал {lo:.0%}–{hi:.0%})",
        f"Средняя прибыль: {s['avg_win']:+.2f}      средний убыток: {s['avg_loss']:+.2f}",
    ]

    if s["payoff"] is not None:
        out.append(
            f"Отношение прибыль/убыток: {s['payoff']:.2f}"
            f"   (для безубыточности при {s['win_rate']:.0%} побед нужно "
            f"{(1 - s['win_rate']) / s['win_rate']:.2f})"
        )

    out += [
        f"Матожидание на сделку: {s['expectancy']:+.2f}"
        f"   (95% интервал {exp_lo:+.2f}…{exp_hi:+.2f})",
    ]
    if exp_lo < 0 < exp_hi:
        out.append("  интервал накрывает ноль — знак матожидания статистически не установлен")

    out += [
        "",
        f"Валовой P&L: {s['gross']:+.2f}   комиссии: {s['fees']:.2f}"
        f"   фандинг: {s['funding']:+.2f}",
    ]
    out.append(
        f"Без издержек итог был бы {s['gross']:+.2f}; издержки изменили его"
        f" на {-s['costs']:+.2f}"
    )
    if s["fees_from_exchange"]:
        out.append(
            f"  у {s['fees_from_exchange']} сделок комиссия взята у биржи (списана в MNT)"
        )
    if s["liquidated"]:
        out.append(f"  ликвидаций: {s['liquidated']} — исключены из метрик процесса")

    # --- распределение ---
    hist = stats.pnl_histogram(conn, days)
    if hist["bins"]:
        peak = max(count for _, _, count in hist["bins"]) or 1
        out += ["", "Распределение P&L (устойчивее среднего на такой выборке):"]
        for start, end, count in hist["bins"]:
            out.append(f"  {start:>9.0f}…{end:<9.0f} {count:>4}  {_bar(count, peak)}")

    # --- длительность ---
    hold = stats.holding_time(conn, days)
    if hold["median_win_hours"] is not None and hold["median_loss_hours"] is not None:
        out += [
            "",
            f"Медиана удержания: прибыльные {hold['median_win_hours']:.1f} ч"
            f" (n={hold['n_wins']})   убыточные {hold['median_loss_hours']:.1f} ч"
            f" (n={hold['n_losses']})",
        ]
        if hold["median_loss_hours"] > hold["median_win_hours"] * 1.5:
            out.append("  убытки пересиживаются дольше, чем держится прибыль")

    # --- R-multiple ---
    r = stats.r_multiples(conn, days)
    out += ["", "R-multiple:"]
    if not r["available"]:
        out += [
            f"  НЕДОСТУПЕН — ни у одной из {r['of_total']} сделок нет стопа,",
            "  записанного ДО входа. Стоп, восстановленный по памяти, сделал бы",
            "  всю R-статистику фикцией, поэтому она не считается вовсе.",
            "  Появится сама, как только начнёшь: journal intent SYMBOL long \"тезис\" --stop X",
        ]
    else:
        out.append(f"  посчитан по {r['n']} сделкам из {r['of_total']}")
        out.append(f"  медиана {r['values'][len(r['values']) // 2]:.2f}")

    # --- разобранность (заголовок продукта, см. решение C) ---
    cov = journal.coverage(conn)
    if cov["trades"] and cov["share"] is not None:
        out += [
            "",
            f"Разобрано сделок: {cov['annotated']} из {cov['trades']}"
            f" = {cov['share']:.0%}   (включая открытые)",
            f"  из них с намерением, записанным до входа: {cov['with_intent']}",
        ]
        if not cov["annotated"]:
            out.append(
                "  журнал пуст: цифры выше описывают ЧТО происходило,"
                " но не отвечают почему"
            )

    # --- разрез по символам ---
    sym = stats.by_symbol(conn, days)
    out += ["", "По символам:"]
    if not sym["shown"]:
        out.append(
            f"  ни один символ не набрал {stats.MIN_SLICE_N} сделок"
            f" (проверено символов: {sym['tested']}) — срезы не показаны,"
            " на меньших ячейках «лучший» находится всегда"
        )
        if s["n"] and sym["tested"] > s["n"] / 4:
            out.append(
                f"  {sym['tested']} символов на {s['n']} сделок"
                f" = {s['n'] / sym['tested']:.1f} сделки на символ."
                " Это само по себе наблюдение: торговля размазана, и разрез"
                " по символам не наберёт выборку никогда"
            )
    else:
        for item in sym["shown"]:
            slo, shi = item["win_rate_ci"]
            out.append(
                f"  {item['symbol']:<14} n={item['n']:<4} итог {item['total']:+9.2f}"
                f"   побед {item['win_rate']:.0%} ({slo:.0%}–{shi:.0%})"
            )
        out.append(
            f"  показано {len(sym['shown'])} из {sym['tested']} символов;"
            f" скрыто {sym['hidden']} с n<{stats.MIN_SLICE_N}."
        )
        out.append(
            "  помни о числе сравнений: чем больше срезов просмотрено,"
            " тем вероятнее случайный «лидер»"
        )

    return "\n".join(out)
