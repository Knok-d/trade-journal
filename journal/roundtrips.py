"""Склейка fills в сделки — реализация docs/trade-definition.md.

Единственное место, где решается, что такое «сделка». Меняется только вместе
с документом.
"""

import sqlite3
from decimal import Decimal

from .db import dec

LIQUIDATION_TYPES = {"BustTrade", "AdlTrade"}
SETTLEMENT_VALUTA = "USDT"


class _Position:
    """Открытая позиция внутри одного ключа группировки."""

    def __init__(self, category, symbol, position_idx, direction, opened_at, first_exec_id):
        self.category = category
        self.symbol = symbol
        self.position_idx = position_idx
        self.direction = direction              # long | short
        self.opened_at = opened_at
        # trade_id детерминирован: пересборка из сырых fills даёт те же id,
        # поэтому записи журнала не отвязываются.
        self.trade_id = f"{symbol}:{position_idx}:{opened_at}:{first_exec_id}"

        self.qty = Decimal(0)                   # текущий открытый объём
        self.entry_cost = Decimal(0)            # себестоимость ОСТАТКА позиции
        self.entry_qty = Decimal(0)             # объём остатка (= self.qty)
        # Средняя цена входа закрытого объёма: Σ(средняя_на_момент_выхода × объём).
        # Ведётся отдельно от базы, потому что база обнуляется при полном выходе,
        # а показать надо именно то, что показывает биржа в closed-pnl.
        self.closed_entry_cost = Decimal(0)
        self.exit_value = Decimal(0)            # Σ(цена × объём) по выходам
        self.exit_qty = Decimal(0)
        self.gross_pnl = Decimal(0)
        self.fees = Decimal(0)
        self.funding = Decimal(0)
        self.liquidated = False
        self.fee_mixed = False
        self.closed_at = None
        self.close_orders: set[str] = set()   # ордера, которыми позиция закрывалась

    @property
    def avg_entry(self) -> Decimal:
        """Средняя цена входа остатка — по ней считается P&L следующего выхода."""
        return self.entry_cost / self.entry_qty if self.entry_qty else Decimal(0)

    @property
    def reported_entry(self) -> Decimal:
        """Средняя цена входа закрытого объёма — сопоставима с avgEntryPrice биржи."""
        if self.exit_qty:
            return self.closed_entry_cost / self.exit_qty
        return self.avg_entry

    @property
    def avg_exit(self) -> Decimal | None:
        return self.exit_value / self.exit_qty if self.exit_qty else None

    @property
    def sign(self) -> int:
        return 1 if self.direction == "long" else -1

    def add_entry(self, price: Decimal, qty: Decimal) -> None:
        """Вход или долив: меняет среднюю цену, реализованный P&L не трогает."""
        self.entry_cost += price * qty
        self.entry_qty += qty
        self.qty += qty

    def add_exit(self, price: Decimal, qty: Decimal) -> None:
        """Выход: реализует P&L по текущей средней цене. Среднюю цену не меняет."""
        entry = self.avg_entry
        self.gross_pnl += (price - entry) * qty * self.sign
        self.exit_value += price * qty
        self.exit_qty += qty
        self.closed_entry_cost += entry * qty
        self.qty -= qty

        # Себестоимость вышедшей части снимается с базы. Средняя цена от этого не
        # меняется (снимаем ровно по ней), но последующий долив пересчитается от
        # остатка, а не от всех входов за жизнь позиции. Без этого долив ПОСЛЕ
        # частичного выхода загрязняет среднюю уже проданной частью.
        self.entry_cost -= entry * qty
        self.entry_qty -= qty

    def row(self) -> tuple:
        net = self.gross_pnl - self.fees - self.funding if self.closed_at else None
        return (
            self.trade_id, self.category, self.symbol, self.position_idx,
            self.direction, self.opened_at, self.closed_at,
            str(self.exit_qty if self.closed_at else self.qty),
            str(self.reported_entry),
            str(self.avg_exit) if self.avg_exit is not None else None,
            str(self.gross_pnl) if self.closed_at else None,
            str(self.fees), str(self.funding),
            str(net) if net is not None else None,
            int(self.liquidated), int(self.fee_mixed), "fills",
        )


def rebuild_all(conn: sqlite3.Connection) -> dict:
    """Полная пересборка: склейка и всё, что она стирает.

    `rebuild` удаляет round_trips целиком, а вместе с ними — комиссию, взятую
    у биржи (когда она списана в MNT), плечо и объём входа. Все три в fills
    отсутствуют и проставляются отдельными проходами, поэтому вызвать одну
    склейку и на этом остановиться значит оставить дневник с заниженным P&L
    у сорока пяти сделок и провалить сверку.

    Ровно это и случилось при первом импорте MT5: последовательность была
    переписана в трёх местах, и в новом, четвёртом, три шага из четырёх просто
    забылись. Поэтому теперь она одна и вызывается целиком.
    """
    from . import journal, reconcile

    stats = rebuild(conn)
    fees = reconcile.apply_exchange_fees(conn)
    leverage = reconcile.apply_leverage(conn)
    intents = journal.match_intents(conn)
    return {**stats, "fees": fees, "leverage": leverage, "intents": intents}


def _rebuild_mt5(conn: sqlite3.Connection) -> int:
    """Переносит закрытые позиции MT5 в round_trips как есть.

    Склейки здесь нет и быть не может: брокер отдаёт уже закрытые позиции с
    посчитанным P&L, а исполнений не отдаёт вовсе. Считать P&L самим нельзя —
    объём в лотах, а размер контракта в выгрузке отсутствует (см. комментарий
    к `mt5_positions`). Поэтому цифры переносятся, а не выводятся, и сделка
    помечается `source='mt5'`, чтобы это было видно.

    `trade_id` детерминирован, как и у своих сделок: он выводится из ID
    позиции, который выдал брокер, поэтому переживает и пересборку, и
    повторный ввод строки — разбор не отвязывается.

    Счёт в режиме хеджирования — на одном символе одновременно живут несколько
    независимых позиций (три SP500, закрытых одной секундой). Поэтому каждая
    позиция становится отдельной сделкой, а не склеивается с соседями по
    символу: иначе три сделки схлопнулись бы в одну и разбор было бы не к чему
    привязать.
    """
    rows = conn.execute("SELECT * FROM mt5_positions").fetchall()
    conn.executemany(
        "INSERT INTO round_trips"
        " (trade_id, category, symbol, position_idx, direction, opened_at, closed_at,"
        "  qty, avg_entry, avg_exit, gross_pnl, fees, funding, net_pnl,"
        "  liquidated, fee_mixed, fees_source, source)"
        " VALUES (?,'mt5',?,0,?,?,?,?,?,?,?,?,?,?,0,0,'mt5','mt5')",
        [
            (
                f"mt5:{row['position_id']}", row["symbol"], row["direction"],
                row["opened_at"], row["closed_at"], row["lots"],
                row["open_price"], row["close_price"], row["gross_pnl"],
                row["fees"],
                # Своп прибавляется к P&L, а фандинг в журнале вычитается —
                # значит это одна и та же величина с обратным знаком.
                str(-dec(row["swap"])),
                row["net_pnl"],
            )
            for row in rows
        ],
    )
    return len(rows)


def rebuild(conn: sqlite3.Connection) -> dict:
    """Пересобирает round_trips из сырых fills. Идемпотентно."""
    # Целиком, включая MT5: это производная таблица, и обе её половины
    # пересобираются здесь же — своя из fills, привозная из mt5_positions.
    # Щадить вторую не нужно, у неё есть свой источник (и именно поэтому
    # исправленная опечатка доезжает до дневника).
    conn.execute("DELETE FROM round_trips")

    # Порядок — по seq внутри миллисекунды: 80% fills делят exec_time с соседями,
    # а это проход с состоянием. exec_id как тайбрейк (UUID) давал случайный
    # порядок и рвал позиции не там, где они рвались на бирже.
    fills = conn.execute(
        "SELECT * FROM raw_executions ORDER BY category, symbol, position_idx,"
        " exec_time, seq, exec_id"
    ).fetchall()

    completed: list[_Position] = []
    orphan_funding: list[str] = []
    open_position: _Position | None = None
    current_key = None

    for fill in fills:
        key = (fill["category"], fill["symbol"], fill["position_idx"])
        if key != current_key:
            if open_position is not None:
                completed.append(open_position)   # осталась открытой — closed_at = None
            open_position, current_key = None, key

        # Фандинг не меняет размер позиции — только начисляется на неё.
        if fill["exec_type"] == "Funding":
            if open_position is None:
                orphan_funding.append(fill["exec_id"])
            else:
                open_position.funding += dec(fill["exec_fee"])
            continue

        qty = dec(fill["exec_qty"])
        price = dec(fill["exec_price"])
        fee = dec(fill["exec_fee"])
        signed = qty if fill["side"] == "Buy" else -qty

        # Комиссия разрезанного fill делится между сделками по объёму.
        # Доли считаются как fee*portion/total (умножение раньше деления), а
        # последняя часть получает точный остаток: иначе периодические дроби
        # (1/3) дают дрейф, который на дистанции съедает допуск сверки.
        total_qty, fee_left = qty, fee

        # Decimal, а не float: суммы приходят от биржи десятичными строками,
        # поэтому возврат позиции в ноль определяется точно, без эпсилонов.
        while qty > 0:
            if open_position is None:
                open_position = _Position(
                    fill["category"], fill["symbol"], fill["position_idx"],
                    "long" if signed > 0 else "short",
                    fill["exec_time"], fill["exec_id"],
                )

            closing = open_position.sign * signed < 0
            portion = min(qty, open_position.qty) if closing else qty

            if closing:
                open_position.add_exit(price, portion)
                if fill["order_id"]:
                    open_position.close_orders.add(fill["order_id"])
            else:
                open_position.add_entry(price, portion)

            is_last_portion = portion == qty
            portion_fee = fee_left if is_last_portion else fee * portion / total_qty
            fee_left -= portion_fee
            open_position.fees += portion_fee
            if fill["fee_currency"] and fill["fee_currency"] != SETTLEMENT_VALUTA:
                open_position.fee_mixed = True
            if fill["exec_type"] in LIQUIDATION_TYPES:
                open_position.liquidated = True

            qty -= portion

            if closing and open_position.qty == 0:
                open_position.closed_at = fill["exec_time"]
                completed.append(open_position)
                open_position = None
                # qty > 0 здесь означает переворот через ноль: остаток
                # следующей итерацией откроет новую сделку в другую сторону.

    if open_position is not None:
        completed.append(open_position)

    conn.executemany(
        # Колонки поимённо: плечо и объём входа приходят не отсюда, а от биржи
        # отдельным проходом, и безымянный VALUES ломался бы от их появления.
        "INSERT INTO round_trips"
        " (trade_id, category, symbol, position_idx, direction, opened_at, closed_at,"
        "  qty, avg_entry, avg_exit, gross_pnl, fees, funding, net_pnl,"
        "  liquidated, fee_mixed, fees_source)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [position.row() for position in completed],
    )
    mt5 = _rebuild_mt5(conn)
    conn.execute("DELETE FROM trade_close_orders")
    conn.executemany(
        "INSERT OR IGNORE INTO trade_close_orders VALUES (?,?)",
        [
            (position.trade_id, order_id)
            for position in completed
            for order_id in position.close_orders
        ],
    )
    conn.commit()

    closed = [p for p in completed if p.closed_at]
    return {
        "fills": len(fills),
        "round_trips": len(completed) + mt5,
        "closed": len(closed) + mt5,      # с MT5 приходят только закрытые
        "open": len(completed) - len(closed),
        "mt5": mt5,
        "orphan_funding": orphan_funding,
    }
