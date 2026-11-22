"""Dollar-cost averaging in cycles ("deals").

- A cycle starts with a base order and adds one more base order every
  ``interval_candles`` candles while it is open.
- When price moves ``safety_step_pct`` percent against the last entry, it adds a safety
  order ``safety_scale`` times bigger than the previous one (up to ``safety_orders``).
- When price is ``take_profit_pct`` percent beyond the average entry, it closes the whole
  cycle; the next cycle starts on the following candle.
- With ``stop_loss_pct`` > 0, the whole cycle is closed once price is that far past the
  average entry, and the next cycle waits ``interval_candles``.

Exits are checked on candle closes, not tick by tick.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from forex_bot.models import LONG, SHORT, CloseIntent, Intent, OpenIntent, format_time, parse_time
from forex_bot.strategies.base import Strategy, StrategyContext


class DcaStrategy(Strategy):
    name = "dca"
    defaults = {
        "direction": "long",
        "interval_candles": 24,
        "take_profit_pct": 1.5,
        "stop_loss_pct": 0.0,
        "safety_orders": 3,
        "safety_step_pct": 1.0,
        "safety_scale": 1.5,
        "max_buys": 10,
    }

    def validate(self) -> None:
        p = self.params
        self._require(p["direction"] in ("long", "short"), "direction must be long or short")
        self._require(int(p["interval_candles"]) >= 1, "interval_candles must be at least 1")
        self._require(p["take_profit_pct"] > 0, "take_profit_pct must be positive")
        self._require(p["stop_loss_pct"] >= 0, "stop_loss_pct must be 0 (off) or positive")
        self._require(int(p["safety_orders"]) >= 0, "safety_orders cannot be negative")
        self._require(p["safety_step_pct"] > 0, "safety_step_pct must be positive")
        self._require(p["safety_scale"] >= 1, "safety_scale must be at least 1")
        self._require(int(p["max_buys"]) >= 1, "max_buys must be at least 1")

    @property
    def warmup(self) -> int:
        return 1

    @property
    def lookback(self) -> int:
        return 5

    @property
    def max_trades(self) -> int:
        return int(self.params["max_buys"])

    def on_candle(self, ctx: StrategyContext) -> list[Intent]:
        p = self.params
        state = ctx.state
        side = LONG if p["direction"] == "long" else SHORT
        close = ctx.last.close
        now = ctx.last.time

        if not ctx.trades:
            # An entry was requested but nothing is open: it was rejected (closed market,
            # risk rule) or closed by hand. Retry now instead of waiting a whole interval.
            failed_entry = bool(state.get("cycle_open_trades"))
            state["safety_used"] = 0
            state["cycle_open_trades"] = False
            if failed_entry or self._interval_passed(state, now, ctx.granularity_seconds):
                return [self._entry(state, side, close, now, 1.0, "base order")]
            return []

        units = sum(t.units for t in ctx.trades)
        average = sum(t.units * t.entry_price for t in ctx.trades) / units
        gain = (close - average) / average * side
        if gain >= p["take_profit_pct"] / 100:
            state["last_entry_time"] = None  # start the next cycle right away
            return self._close_all(ctx, "dca_take_profit")
        if p["stop_loss_pct"] and gain <= -p["stop_loss_pct"] / 100:
            state["last_entry_time"] = format_time(now)  # cool down one interval
            return self._close_all(ctx, "dca_stop_loss")
        if len(ctx.trades) >= int(p["max_buys"]):
            return []

        last_price = float(
            state.get("last_entry_price")
            or max(ctx.trades, key=lambda t: t.open_time).entry_price
        )
        used = int(state.get("safety_used", 0))
        against = (close - last_price) / last_price * side
        if used < int(p["safety_orders"]) and against <= -p["safety_step_pct"] / 100:
            state["safety_used"] = used + 1
            scale = float(p["safety_scale"]) ** (used + 1)
            return [self._entry(state, side, close, now, scale, f"safety order {used + 1}")]
        if self._interval_passed(state, now, ctx.granularity_seconds):
            return [self._entry(state, side, close, now, 1.0, "scheduled buy")]
        return []

    def _entry(
        self, state: dict[str, Any], side: int, price: float, now: datetime, scale: float, why: str
    ) -> OpenIntent:
        if not state.get("cycle_open_trades"):
            state["cycle"] = int(state.get("cycle", 0)) + 1
        state["last_entry_time"] = format_time(now)
        state["last_entry_price"] = price
        state["cycle_open_trades"] = True
        return OpenIntent(
            side, size_multiplier=scale, comment=f"dca{state['cycle']}", reason=why
        )

    def _close_all(self, ctx: StrategyContext, why: str) -> list[Intent]:
        ctx.state["safety_used"] = 0
        ctx.state["last_entry_price"] = None
        ctx.state["cycle_open_trades"] = False
        return [CloseIntent(trade.id, why) for trade in ctx.trades]

    def _interval_passed(self, state: dict[str, Any], now: datetime, step: int) -> bool:
        last = state.get("last_entry_time")
        if not last:
            return True
        elapsed = (now - parse_time(last)).total_seconds() / step
        return elapsed >= int(self.params["interval_candles"])
