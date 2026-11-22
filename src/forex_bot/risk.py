"""Position sizing and the account-wide safety rules.

- Daily loss limit: once equity falls ``daily_loss_limit_pct`` below the day's starting
  equity (UTC day), no new trades open until the next day.
- Max drawdown kill switch: once equity falls ``max_drawdown_pct`` below its peak, no new
  trades open (and, by default, every bot trade is closed) until someone runs
  ``forex-bot reset-risk``.
- Per-trade checks: total and per-bot trade limits, spread filter and a free-margin
  buffer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from forex_bot.config import RiskConfig
from forex_bot.instruments import InstrumentSpec
from forex_bot.models import AccountState, Quote, Sizing


class RiskManager:
    def __init__(self, config: RiskConfig, state: dict[str, Any] | None = None) -> None:
        self.config = config
        self.state: dict[str, Any] = state if state is not None else {}

    @property
    def kill_switch(self) -> bool:
        return bool(self.state.get("kill_switch"))

    @property
    def daily_halt(self) -> bool:
        return bool(self.state.get("daily_halt"))

    def update(self, now: datetime, equity: float) -> list[str]:
        """Track equity and return a message for each rule that has just triggered."""
        events: list[str] = []
        state = self.state
        day = now.date().isoformat()
        if state.get("day") != day:
            state["day"] = day
            state["day_start_equity"] = equity
            state["daily_halt"] = False
        peak = max(float(state.get("peak_equity", equity)), equity)
        state["peak_equity"] = peak
        limit = self.config.max_drawdown_pct
        if limit and not self.kill_switch and equity <= peak * (1 - limit / 100):
            state["kill_switch"] = True
            state["kill_reason"] = (
                f"equity {equity:.2f} is {limit:g}% below its peak of {peak:.2f}"
            )
            events.append(f"MAX DRAWDOWN kill switch: {state['kill_reason']}")
        daily = self.config.daily_loss_limit_pct
        start = float(state.get("day_start_equity", equity))
        if daily and not self.daily_halt and equity <= start * (1 - daily / 100):
            state["daily_halt"] = True
            events.append(
                f"DAILY LOSS LIMIT: equity {equity:.2f} is {daily:g}% below today's start "
                f"of {start:.2f}; no new trades until 00:00 UTC"
            )
        return events

    def reset(self, equity: float | None = None) -> None:
        self.state["kill_switch"] = False
        self.state["kill_reason"] = ""
        self.state["daily_halt"] = False
        if equity is not None:
            self.state["peak_equity"] = equity
            self.state["day_start_equity"] = equity

    def check_open(
        self,
        *,
        open_total: int,
        open_for_bot: int,
        bot_max: int,
        quote: Quote,
        spec: InstrumentSpec,
    ) -> str | None:
        """The reason a new trade may not open, or None if it may."""
        if self.kill_switch:
            return f"kill switch is on ({self.state.get('kill_reason')}); run forex-bot reset-risk"
        if self.daily_halt:
            return "daily loss limit reached; trading resumes at 00:00 UTC"
        if open_total >= self.config.max_open_trades:
            return f"account already has {open_total} open trades (max_open_trades)"
        if open_for_bot >= bot_max:
            return f"bot already has {open_for_bot} open trades (its limit is {bot_max})"
        limits = self.config.max_spread_pips
        limit = limits.get(spec.name, limits.get("default"))
        spread = spec.to_pips(quote.spread)
        if limit and spread > limit:
            return f"spread {spread:.1f} pips is above the {limit:g} pip limit"
        return None

    def units_for(
        self,
        sizing: Sizing,
        *,
        price: float,
        stop_distance: float | None,
        equity: float,
        conversion_rate: float,
        spec: InstrumentSpec,
    ) -> float:
        """Unsigned trade size in units, rounded down to what the instrument allows."""
        if sizing.mode == "units":
            units = sizing.value
        elif sizing.mode == "notional":
            units = sizing.value / (price * conversion_rate)
        else:
            if not stop_distance or stop_distance <= 0:
                raise ValueError("risk-based sizing needs a stop loss")
            units = equity * sizing.value / 100 / (stop_distance * conversion_rate)
        return abs(spec.round_units(units))

    def check_margin(
        self,
        units: float,
        *,
        price: float,
        conversion_rate: float,
        spec: InstrumentSpec,
        account: AccountState,
    ) -> str | None:
        required = abs(units) * price * conversion_rate * spec.margin_rate
        reserve = account.equity * self.config.margin_buffer_pct / 100
        free = account.margin_available - reserve
        if required > free:
            return (
                f"needs {required:.2f} {account.currency} margin but only {max(free, 0):.2f} "
                f"is free after keeping {self.config.margin_buffer_pct:g}% in reserve"
            )
        return None
