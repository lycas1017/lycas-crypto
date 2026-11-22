"""A simulated broker used for backtests and paper trading.

It fills market orders at the ask (buys) or bid (sells), applies a modelled spread when
only a mid price is known, checks stop loss and take profit against each price update
or candle, and tracks balance, equity and margin in the account currency.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from forex_bot.brokers.base import Broker, OrderRejected
from forex_bot.instruments import (
    DEFAULT_SPREAD_PIPS,
    InstrumentSpec,
    default_spec,
    quote_to_account_rate,
)
from forex_bot.models import AccountState, Candle, ClosedTrade, Quote, Trade, utcnow

MAX_CLOSED_HISTORY = 1000


class PaperBroker(Broker):
    def __init__(
        self,
        currency: str = "USD",
        balance: float = 10_000.0,
        spread_pips: Mapping[str, float] | None = None,
        commission_per_trade: float = 0.0,
        slippage_pips: float = 0.0,
        conversion_rates: Mapping[str, float] | None = None,
        specs: Mapping[str, InstrumentSpec] | None = None,
        keep_all_closed: bool = False,
    ) -> None:
        if balance <= 0:
            raise ValueError("starting balance must be positive")
        self.currency = currency
        self.balance = float(balance)
        self.commission_per_trade = float(commission_per_trade)
        self.slippage_pips = float(slippage_pips)
        self.keep_all_closed = keep_all_closed
        self.closed: list[ClosedTrade] = []
        self._spreads = dict(spread_pips or {})
        self._rates: dict[str, float] = dict(conversion_rates or {})
        self._specs = dict(specs or {})
        self._quotes: dict[str, Quote] = {}
        self._trades: dict[str, Trade] = {}
        self._commissions: dict[str, float] = {}
        self._next_id = 1

    # Prices ---------------------------------------------------------------------------

    def spec(self, instrument: str) -> InstrumentSpec:
        if instrument not in self._specs:
            self._specs[instrument] = default_spec(instrument)
        return self._specs[instrument]

    def spread_price(self, instrument: str) -> float:
        if instrument in self._spreads:
            pips = self._spreads[instrument]
        elif instrument in DEFAULT_SPREAD_PIPS:
            pips = DEFAULT_SPREAD_PIPS[instrument]
        else:
            pips = self._spreads.get("default", 2.0)
        return pips * self.spec(instrument).pip

    def set_mid(
        self, instrument: str, mid: float, time: datetime | None = None, tradeable: bool = True
    ) -> list[ClosedTrade]:
        """Update the price from a mid quote, adding the modelled spread."""
        half = self.spread_price(instrument) / 2
        quote = Quote(instrument, mid - half, mid + half, time or utcnow(), tradeable)
        return self.update_quote(quote)

    def update_quote(self, quote: Quote) -> list[ClosedTrade]:
        """Store a bid/ask quote and close any trade whose stop or target it touched."""
        self._quotes[quote.instrument] = quote
        self._rates[quote.instrument] = quote.mid
        closed: list[ClosedTrade] = []
        for trade in self._trades_for(quote.instrument):
            exit_price = quote.bid if trade.is_long else quote.ask
            hit = _stop_hit(trade, exit_price, exit_price, exit_price)
            if hit:
                price, reason = hit
                closed.append(self._close(trade, price, reason, quote.time))
        return closed

    def process_candle(
        self, instrument: str, candle: Candle, close_time: datetime
    ) -> list[ClosedTrade]:
        """Check stops against the candle's full range, then mark the price at its close.

        If one candle touches both the stop loss and the take profit, the stop loss is
        assumed to have been hit first. That is the conservative choice for a backtest.
        Trades opened after the candle opened are skipped: its high or low may have come
        before they existed (a bot on a shorter timeframe checks them with its own candles).
        """
        half = self.spread_price(instrument) / 2
        closed: list[ClosedTrade] = []
        for trade in self._trades_for(instrument):
            if trade.open_time > candle.time:
                continue
            offset = -half if trade.is_long else half
            hit = _stop_hit(
                trade, candle.open + offset, candle.high + offset, candle.low + offset
            )
            if hit:
                price, reason = hit
                closed.append(self._close(trade, price, reason, close_time))
        closed.extend(self.set_mid(instrument, candle.close, close_time))
        return closed

    def quote(self, instrument: str) -> Quote:
        if instrument not in self._quotes:
            raise OrderRejected(f"no price for {instrument} yet")
        return self._quotes[instrument]

    def conversion_rate(self, instrument: str) -> float:
        return quote_to_account_rate(
            instrument, self.quote(instrument).mid, self.currency, self._rates
        )

    # Account --------------------------------------------------------------------------

    def account(self) -> AccountState:
        unrealized = 0.0
        margin = 0.0
        for trade in self._trades.values():
            unrealized += self._unrealized(trade)
            margin += self._margin(trade.instrument, trade.units)
        equity = self.balance + unrealized
        return AccountState(
            currency=self.currency,
            balance=self.balance,
            equity=equity,
            margin_used=margin,
            margin_available=equity - margin,
            open_trade_count=len(self._trades),
        )

    def open_trades(self, bot_id: str | None = None) -> list[Trade]:
        trades = [t for t in self._trades.values() if bot_id is None or t.bot_id == bot_id]
        for trade in trades:
            trade.unrealized_pl = self._unrealized(trade)
        return trades

    # Orders ---------------------------------------------------------------------------

    def open_trade(
        self,
        bot_id: str,
        instrument: str,
        units: float,
        stop_loss: float | None,
        take_profit: float | None,
        comment: str = "",
        reason: str = "",
    ) -> Trade:
        quote = self.quote(instrument)
        if not quote.tradeable:
            raise OrderRejected(f"{instrument} market is closed")
        spec = self.spec(instrument)
        units = spec.round_units(units)
        if abs(units) < spec.min_units:
            raise OrderRejected(f"{units} units is below the {spec.min_units} minimum")
        slip = self.slippage_pips * spec.pip
        price = quote.ask + slip if units > 0 else quote.bid - slip
        stop_loss = spec.round_price(stop_loss) if stop_loss is not None else None
        take_profit = spec.round_price(take_profit) if take_profit is not None else None
        _check_stops(units, price, stop_loss, take_profit)
        required = self._margin(instrument, units)
        available = self.account().margin_available
        if required > available:
            raise OrderRejected(
                f"insufficient margin: need {required:.2f} {self.currency}, "
                f"have {available:.2f}"
            )
        trade = Trade(
            id=f"P{self._next_id}",
            instrument=instrument,
            units=units,
            entry_price=price,
            open_time=quote.time,
            bot_id=bot_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
            comment=comment,
        )
        self._next_id += 1
        self._trades[trade.id] = trade
        if self.commission_per_trade:
            self.balance -= self.commission_per_trade
            self._commissions[trade.id] = self.commission_per_trade
        return trade

    def close_trade(self, trade_id: str, reason: str = "closed by strategy") -> ClosedTrade:
        trade = self._trades.get(trade_id)
        if trade is None:
            raise OrderRejected(f"trade {trade_id} is not open")
        quote = self.quote(trade.instrument)
        slip = self.slippage_pips * self.spec(trade.instrument).pip
        price = quote.bid - slip if trade.is_long else quote.ask + slip
        return self._close(trade, price, reason, quote.time)

    def close_all(self, reason: str) -> list[ClosedTrade]:
        return [self.close_trade(trade_id, reason) for trade_id in list(self._trades)]

    def get_closed_trade(self, trade_id: str) -> ClosedTrade | None:
        for closed in reversed(self.closed):
            if closed.id == trade_id:
                return closed
        return None

    # Persistence ----------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "balance": self.balance,
            "next_id": self._next_id,
            "rates": self._rates,
            "commissions": self._commissions,
            "trades": [trade.to_dict() for trade in self._trades.values()],
            "closed": [closed.to_dict() for closed in self.closed[-MAX_CLOSED_HISTORY:]],
        }

    def restore(self, data: Mapping[str, Any]) -> None:
        if data.get("currency", self.currency) != self.currency:
            raise ValueError(
                f"saved paper account is in {data['currency']}, config says {self.currency}"
            )
        self.balance = float(data["balance"])
        self._next_id = int(data.get("next_id", 1))
        self._rates.update(data.get("rates", {}))
        self._commissions = dict(data.get("commissions", {}))
        self._trades = {t["id"]: Trade.from_dict(t) for t in data.get("trades", [])}
        self.closed = [ClosedTrade.from_dict(c) for c in data.get("closed", [])]

    # Internals ------------------------------------------------------------------------

    def _trades_for(self, instrument: str) -> list[Trade]:
        return [t for t in self._trades.values() if t.instrument == instrument]

    def _rate(self, instrument: str, mid: float) -> float:
        return quote_to_account_rate(instrument, mid, self.currency, self._rates)

    def _unrealized(self, trade: Trade) -> float:
        quote = self._quotes.get(trade.instrument)
        if quote is None:
            return 0.0
        exit_price = quote.bid if trade.is_long else quote.ask
        return (exit_price - trade.entry_price) * trade.units * self._rate(
            trade.instrument, quote.mid
        )

    def _margin(self, instrument: str, units: float) -> float:
        quote = self._quotes.get(instrument)
        if quote is None:
            return 0.0
        rate = self._rate(instrument, quote.mid)
        return abs(units) * quote.mid * rate * self.spec(instrument).margin_rate

    def _close(self, trade: Trade, price: float, reason: str, time: datetime) -> ClosedTrade:
        self._trades.pop(trade.id, None)
        quote = self._quotes.get(trade.instrument)
        mid = quote.mid if quote else price
        pl = (price - trade.entry_price) * trade.units * self._rate(trade.instrument, mid)
        self.balance += pl
        commission = self._commissions.pop(trade.id, 0.0)
        closed = ClosedTrade(
            id=trade.id,
            instrument=trade.instrument,
            bot_id=trade.bot_id,
            units=trade.units,
            entry_price=trade.entry_price,
            exit_price=price,
            open_time=trade.open_time,
            close_time=time,
            realized_pl=pl - commission,
            reason=reason,
            comment=trade.comment,
        )
        self.closed.append(closed)
        if not self.keep_all_closed and len(self.closed) > MAX_CLOSED_HISTORY:
            del self.closed[: len(self.closed) - MAX_CLOSED_HISTORY]
        return closed


def _stop_hit(
    trade: Trade, open_: float, high: float, low: float
) -> tuple[float, str] | None:
    """Fill price and reason if a stop or target was touched, given exit-side prices."""
    sl, tp = trade.stop_loss, trade.take_profit
    if trade.is_long:
        if sl is not None and open_ <= sl:
            return open_, "stop_loss"
        if tp is not None and open_ >= tp:
            return open_, "take_profit"
        if sl is not None and low <= sl:
            return sl, "stop_loss"
        if tp is not None and high >= tp:
            return tp, "take_profit"
    else:
        if sl is not None and open_ >= sl:
            return open_, "stop_loss"
        if tp is not None and open_ <= tp:
            return open_, "take_profit"
        if sl is not None and high >= sl:
            return sl, "stop_loss"
        if tp is not None and low <= tp:
            return tp, "take_profit"
    return None


def _check_stops(
    units: float, price: float, stop_loss: float | None, take_profit: float | None
) -> None:
    if units > 0:
        if stop_loss is not None and stop_loss >= price:
            raise OrderRejected(f"long stop loss {stop_loss} must be below entry {price}")
        if take_profit is not None and take_profit <= price:
            raise OrderRejected(f"long take profit {take_profit} must be above entry {price}")
    else:
        if stop_loss is not None and stop_loss <= price:
            raise OrderRejected(f"short stop loss {stop_loss} must be above entry {price}")
        if take_profit is not None and take_profit >= price:
            raise OrderRejected(f"short take profit {take_profit} must be below entry {price}")
