"""A broker that refuses orders the way Alpaca refuses them.

Every bug that reached production this fortnight lived in the seam between
our code and the broker, and every one of them was invisible to a unit test:

  MARKET ORDERS DO NOT FILL OUTSIDE 09:30-16:00. A stop at 07:00 sat queued
  until the open. SDST's shares left at 0.2150 against a stop of 0.2818 —
  23% below — and the journal recorded the intended price.

  SHARES HELD FOR AN OPEN ORDER CANNOT BE SOLD AGAIN. The second close came
  back "insufficient qty available", which our code treated as permanent and
  stopped managing the position entirely.

  last_equity ROLLS AT THE SESSION BOUNDARY. Comparing equity against it
  outside market hours showed a 3.8% drawdown that never happened, and the
  loss cap flattened the book at 00:17.

  POSITIONS DO NOT APPEAR THE INSTANT AN ORDER IS SUBMITTED. Several bars
  closing in the same second each saw two positions and each opened a third.

This reproduces all four. A replay against it runs a full session in seconds
and fails loudly on any of them, instead of the trader finding it at 4am with
money-shaped things on the line.

It is deliberately pessimistic: where the real broker's behaviour is
uncertain, this assumes the harsher outcome. A harness that is kinder than
reality is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class BrokerError(Exception):
    """What alpaca-py raises. Message shaped like the real one."""


@dataclass
class FakePosition:
    symbol: str
    qty: float
    avg_entry_price: float
    current_price: float
    held_for_orders: float = 0.0

    @property
    def unrealized_plpc(self) -> float:
        if self.avg_entry_price <= 0:
            return 0.0
        return self.current_price / self.avg_entry_price - 1

    @property
    def unrealized_pl(self) -> float:
        return (self.current_price - self.avg_entry_price) * self.qty


@dataclass
class FakeAccount:
    equity: float
    last_equity: float


@dataclass
class FakeOrder:
    symbol: str
    qty: float
    side: str
    limit_price: float | None
    extended_hours: bool
    submitted_at: datetime
    filled_at: datetime | None = None
    filled_avg_price: float | None = None

    @property
    def is_open(self) -> bool:
        return self.filled_at is None


class FakeBroker:
    """Stands in for alpaca-py's TradingClient during a replay."""

    def __init__(self, equity: float = 100_000.0) -> None:
        self.starting_equity = equity
        self.cash = equity
        self.realised = 0.0
        self.positions: dict = {}
        self.orders: list = []
        self.now = datetime(2026, 9, 4, 4, 0, tzinfo=ET)
        self.prices: dict = {}
        self.rejections: list = []      # every refusal, for the report

    # ------------------------------------------------------------ clock

    def set_time(self, when: datetime) -> None:
        self.now = when
        self._try_fills()

    def set_price(self, symbol: str, price: float) -> None:
        self.prices[symbol] = price
        if symbol in self.positions:
            self.positions[symbol].current_price = price

    @property
    def regular_hours(self) -> bool:
        return time(9, 30) <= self.now.time() < time(16, 0)

    # ------------------------------------- the TradingClient interface

    def get_account(self) -> FakeAccount:
        equity = self.cash + sum(p.qty * p.current_price
                                 for p in self.positions.values())
        return FakeAccount(equity=equity, last_equity=self.starting_equity)

    def get_all_positions(self) -> list:
        return list(self.positions.values())

    def get_orders(self, request=None) -> list:
        return list(self.orders)

    def submit_order(self, order_request) -> FakeOrder:
        symbol = order_request.symbol
        qty = float(order_request.qty)
        side = getattr(order_request.side, "value", str(order_request.side))
        limit = getattr(order_request, "limit_price", None)
        extended = bool(getattr(order_request, "extended_hours", False))

        if side.startswith("sell"):
            pos = self.positions.get(symbol)
            available = (pos.qty - pos.held_for_orders) if pos else 0
            if available < qty:
                msg = (f'{{"available":"{available:.0f}","code":40310000,'
                       f'"message":"insufficient qty available for order '
                       f'(requested: {qty:.0f}, available: {available:.0f})",'
                       f'"symbol":"{symbol}"}}')
                self.rejections.append((self.now, symbol, "insufficient qty"))
                raise BrokerError(msg)
            pos.held_for_orders += qty

        if not self.regular_hours and not extended:
            # The real broker accepts it and simply does not fill until the
            # open, which is exactly what made this invisible.
            self.rejections.append(
                (self.now, symbol, "market order outside session hours"))

        order = FakeOrder(symbol=symbol, qty=qty, side=side,
                          limit_price=float(limit) if limit else None,
                          extended_hours=extended, submitted_at=self.now)
        self.orders.append(order)
        self._try_fills()
        return order

    def close_position(self, symbol: str):
        """A MARKET sell. Cannot fill outside regular hours."""
        pos = self.positions.get(symbol)
        if pos is None:
            raise BrokerError(f'{{"message":"position not found","symbol":"{symbol}"}}')
        available = pos.qty - pos.held_for_orders
        if available <= 0:
            self.rejections.append((self.now, symbol, "insufficient qty"))
            raise BrokerError(
                f'{{"available":"0","code":40310000,"message":"insufficient '
                f'qty available for order (requested: {pos.qty:.0f}, '
                f'available: 0)","symbol":"{symbol}"}}')

        pos.held_for_orders += available
        order = FakeOrder(symbol=symbol, qty=available, side="sell",
                          limit_price=None, extended_hours=False,
                          submitted_at=self.now)
        self.orders.append(order)
        if not self.regular_hours:
            self.rejections.append(
                (self.now, symbol, "market close outside session hours"))
        self._try_fills()
        return order

    def close_all_positions(self, cancel_orders: bool = False):
        if cancel_orders:
            self.cancel_orders()
        for symbol in list(self.positions):
            try:
                self.close_position(symbol)
            except BrokerError:
                pass

    def cancel_orders(self) -> None:
        for order in self.orders:
            if order.is_open:
                order.filled_at = None
                pos = self.positions.get(order.symbol)
                if pos and order.side.startswith("sell"):
                    pos.held_for_orders = max(0.0,
                                              pos.held_for_orders - order.qty)
        self.orders = [o for o in self.orders if not o.is_open]

    # --------------------------------------------------------- filling

    def _try_fills(self) -> None:
        for order in self.orders:
            if not order.is_open:
                continue

            price = self.prices.get(order.symbol)
            if price is None:
                continue

            # A market order simply waits for the open. This is the whole
            # premarket-stop failure, reproduced.
            if order.limit_price is None and not self.regular_hours:
                continue
            if order.extended_hours and order.limit_price is None:
                continue

            if order.limit_price is not None:
                if order.side.startswith("buy") and price > order.limit_price:
                    continue
                if order.side.startswith("sell") and price < order.limit_price:
                    continue

            self._fill(order, price)

    def _fill(self, order: FakeOrder, price: float) -> None:
        order.filled_at = self.now
        order.filled_avg_price = price

        if order.side.startswith("buy"):
            self.cash -= order.qty * price
            pos = self.positions.get(order.symbol)
            if pos:
                total = pos.qty + order.qty
                pos.avg_entry_price = (
                    (pos.avg_entry_price * pos.qty + price * order.qty) / total)
                pos.qty = total
            else:
                self.positions[order.symbol] = FakePosition(
                    symbol=order.symbol, qty=order.qty,
                    avg_entry_price=price, current_price=price)
        else:
            pos = self.positions.get(order.symbol)
            if pos is None:
                return
            self.cash += order.qty * price
            self.realised += (price - pos.avg_entry_price) * order.qty
            pos.held_for_orders = max(0.0, pos.held_for_orders - order.qty)
            pos.qty -= order.qty
            if pos.qty <= 0:
                del self.positions[order.symbol]
