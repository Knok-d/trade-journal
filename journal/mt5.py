"""Разбор выгрузки закрытых позиций с MT5 (Bybit TradFi CFD).

Второй источник данных, и он принципиально слабее первого. Через API Bybit
счёт MT5 не отдаётся вовсе: `category` знает только `spot | linear | inverse |
option`. Остаётся то, что человек видит на экране веб-трейдера, — таблица уже
закрытых позиций с посчитанным брокером P&L.

Отсюда два следствия, которые определяют весь модуль:

1. **P&L берётся готовым.** Считать его самим нельзя: объём указан в лотах, а
   прибыль равна `разница_цен × размер_контракта × лоты`, и размера контракта
   в выгрузке нет. На реальных сделках он оказался равен 1 для акций и индекса
   и 10 для палладия — наивная формула завысила бы палладий вдесятеро, оставив
   правдоподобное на вид число.
2. **Проверять приходится тем, что есть.** Двух вещей достаточно, чтобы
   поймать почти любую опечатку при вводе: тождество
   `общий = P&L ордера + обмен − комиссия` и выведенный из строки размер
   контракта — он обязан быть одинаковым у всех строк одного инструмента.
   Ошибка в цифре ломает одно из двух почти наверняка.
"""

import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

# Порядок колонок — как в таблице истории веб-трейдера, слева направо.
# Строка берётся оттуда целиком, поэтому порядок задан один раз и здесь.
COLUMNS = (
    "symbol", "order_type", "order_side", "lots", "close_lots",
    "net_pnl", "gross_pnl", "fees", "tax", "swap",
    "open_price", "opened_at", "close_price", "closed_at",
    "position_id", "comment",
)
REQUIRED = len(COLUMNS) - 1      # комментарий бывает пустым и может отсутствовать

# «Закрыть шорт» значит, что позиция БЫЛА шортом. Направление ордера в соседней
# колонке говорит о том же (продать = открывали шорт), и это не дублирование,
# а независимая проверка: расхождение означает, что строку разобрали не так.
DIRECTIONS = {"закрыть шорт": "short", "закрыть лонг": "long"}
SIDES = {"продать": "short", "купить": "long"}

# Время в выгрузке — по серверу MT5. У Bybit TradFi он идёт по UTC без сдвига.
TIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M")


class Mt5Error(ValueError):
    """Строка не разобрана. Текст всегда называет строку и что именно не так."""


def _number(raw: str) -> Decimal:
    """«1,272.16», «-4.35», «0.00 USDx», «--» -> Decimal.

    Валюта в ячейке отбрасывается сознательно: в выгрузке она одна на весь
    счёт, и хранить её у каждого числа значило бы делать вид, что бывает иначе.
    """
    text = re.sub(r"[^\d.,+-]", "", (raw or "").strip())
    text = text.replace(",", "")          # разделитель тысяч, дробный — точка
    if text in ("", "-", "+", "."):
        return Decimal(0)
    try:
        return Decimal(text)
    except InvalidOperation:
        raise Mt5Error(f"не число: {raw!r}") from None


def _moment(raw: str) -> int:
    text = (raw or "").strip()
    for fmt in TIME_FORMATS:
        try:
            naive = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return int(naive.replace(tzinfo=timezone.utc).timestamp() * 1000)
    raise Mt5Error(f"не время: {raw!r} (ожидается 2025-03-14 23:02:04)")


def _cells(line: str) -> list[str]:
    """Ячейки строки. Табуляция — основной разделитель, точка с запятой — запасной.

    Табуляция потому, что таблица со страницы копируется именно так. Пробел
    разделителем быть не может: он есть внутри «Закрыть шорт» и внутри времени.
    """
    parts = line.split("\t") if "\t" in line else line.split(";")
    return [cell.strip() for cell in parts]


def parse_line(line: str) -> dict:
    """Одна строка выгрузки -> одна закрытая позиция.

    Проверок ровно две, и обе про то, что строку разобрали правильно, а не про
    то, что брокер посчитал верно: его расчёт мы принимаем как есть, потому что
    другого нет.
    """
    cells = _cells(line)
    if len(cells) < REQUIRED:
        raise Mt5Error(
            f"колонок {len(cells)}, нужно минимум {REQUIRED}."
            " Ожидается строка таблицы истории целиком, разделители — табуляции"
        )
    row = dict(zip(COLUMNS, cells))

    direction = DIRECTIONS.get(row["order_type"].lower())
    if direction is None:
        raise Mt5Error(
            f"неизвестный тип ордера {row['order_type']!r}:"
            " ожидается «Закрыть шорт» или «Закрыть Лонг»"
        )
    by_side = SIDES.get(row["order_side"].lower())
    if by_side is not None and by_side != direction:
        # Две колонки говорят об одном и том же разными словами. Разошлись —
        # значит колонки разъехались при копировании, и дальше считать нельзя.
        raise Mt5Error(
            f"тип ордера ({row['order_type']}) и направление ({row['order_side']})"
            " противоречат друг другу — похоже, колонки сдвинулись"
        )

    gross = _number(row["gross_pnl"])
    fees = _number(row["fees"]) + _number(row["tax"])
    swap = _number(row["swap"])
    net = _number(row["net_pnl"])

    # Тождество из самой выгрузки: своп прибавляется к P&L, комиссия вычитается.
    # Сошлось — значит четыре числа прочитаны верно; разошлось — верно не все.
    if gross + swap - fees != net:
        raise Mt5Error(
            f"общий P&L {net} не равен {gross} + {swap} − {fees}"
            f" = {gross + swap - fees}: похоже, число прочитано неверно"
        )

    position_id = row["position_id"].strip()
    if not position_id:
        raise Mt5Error("пустой ID позиции — по нему сделка опознаётся при повторе")

    comment = (row.get("comment") or "").strip().strip("[]")
    return {
        "position_id": position_id,
        "symbol": row["symbol"],
        "direction": direction,
        "lots": _number(row["lots"]),
        "open_price": _number(row["open_price"]),
        "close_price": _number(row["close_price"]),
        "opened_at": _moment(row["opened_at"]),
        "closed_at": _moment(row["closed_at"]),
        "gross_pnl": gross,
        "fees": fees,
        "swap": swap,
        "net_pnl": net,
        "comment": comment if comment not in ("", "--") else None,
    }


def parse(text: str) -> tuple[list[dict], list[str]]:
    """Весь файл. Возвращает разобранные позиции и жалобы по строкам.

    Плохая строка не роняет импорт целиком: при ручном вводе одна опечатка
    среди десяти строк — обычное дело, и заставлять перенабирать всё незачем.
    Но и молча пропускать её нельзя, поэтому жалобы возвращаются наверх и
    печатаются рядом с результатом.
    """
    rows, problems = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        # Шапка таблицы, если её скопировали вместе со строками.
        if line.split("\t")[0].strip().lower() in ("торговые пары", "symbol"):
            continue
        try:
            rows.append(parse_line(line))
        except Mt5Error as exc:
            problems.append(f"строка {number}: {exc}")
    return rows, problems


def contract_sizes(rows: list[dict]) -> dict[str, list[Decimal]]:
    """Размер контракта, выведенный из каждой строки: сколько единиц в лоте.

    Считается не ради самого числа, а ради проверки. У одного инструмента он
    один и тот же, поэтому два разных значения по одному символу означают, что
    в какой-то строке ошиблись ценой, объёмом или P&L. Величина при этом
    нигде не используется в расчётах: P&L берётся у брокера готовым.
    """
    sizes: dict[str, list[Decimal]] = {}
    for row in rows:
        move = row["close_price"] - row["open_price"]
        sign = 1 if row["direction"] == "long" else -1
        divisor = move * row["lots"] * sign
        size = (row["gross_pnl"] / divisor) if divisor else None
        seen = sizes.setdefault(row["symbol"], [])
        if size is not None and size not in seen:
            seen.append(size)
    return sizes
