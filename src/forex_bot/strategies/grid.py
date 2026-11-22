"""Grid trading: profit from price oscillating inside a range.

The range ``lower`` to ``upper`` is split into ``levels`` equal steps.

- ``long``: when price falls through a level, buy, with the take profit one level up.
- ``short``: when price rises through a level, sell, with the take profit one level down.
- ``neutral``: buy below the middle of the range, sell above it.

Every trade's stop loss sits ``stop_pct`` percent beyond the edge of the grid, so a
breakout caps the loss. Without fixed ``lower``/``upper``, the grid is built
``range_pct`` percent either side of the current price and, if ``recenter`` is on,
rebuilt around the new price after a breakout. Grids lose money in strong one-way
trends: that is what the stop and the recentring are for.
"""

from __future__ import annotations

from typing import Any

from forex_bot.models import LONG, SHORT, CloseIntent, Intent, OpenIntent
from forex_bot.strategies.base import Strategy, StrategyContext

MODES = ("long", "short", "neutral")


def grid_levels(lower: float, upper: float, steps: int) -> list[float]:
    width = (upper - lower) / steps
    return [lower + width * index for index in range(steps + 1)]


class GridStrategy(Strategy):
    name = "grid"
    defaults = {
        "lower": None,
        "upper": None,
        "range_pct": 2.0,
        "levels": 10,
        "mode": "long",
        "stop_pct": 1.0,
        "recenter": True,
    }

    def validate(self) -> None:
        p = self.params
        self._require(p["mode"] in MODES, f"mode must be one of {MODES}")
        self._require(int(p["levels"]) >= 2, "levels must be at least 2")
        self._require(p["stop_pct"] > 0, "stop_pct must be positive")
        fixed = (p["lower"], p["upper"])
        self._require(
            fixed == (None, None) or None not in fixed,
            "set both lower and upper, or neither (auto range)",
        )
        if None not in fixed:
            self._require(0 < p["lower"] < p["upper"], "need 0 < lower < upper")
        else:
            self._require(0 < p["range_pct"] < 50, "range_pct must be between 0 and 50")

    @property
    def warmup(self) -> int:
        return 2

    @property
    def lookback(self) -> int:
        return 10

    @property
    def max_trades(self) -> int:
        return int(self.params["levels"])

    @property
    def fixed(self) -> bool:
        return self.params["lower"] is not None

    def _build(self, price: float, grid_id: int) -> dict[str, Any]:
        if self.fixed:
            lower, upper = float(self.params["lower"]), float(self.params["upper"])
        else:
            half = price * self.params["range_pct"] / 100
            lower, upper = price - half, price + half
        return {"id": grid_id, "lower": lower, "upper": upper}

    def on_candle(self, ctx: StrategyContext) -> list[Intent]:
        p = self.params
        state = ctx.state
        close = ctx.last.close
        grid = state.get("grid")
        if grid is None:
            state["grid"] = self._build(close, int(state.get("grid_count", 0)) + 1)
            state["grid_count"] = state["grid"]["id"]
            state["prev_close"] = close
            return []

        lower, upper, grid_id = grid["lower"], grid["upper"], grid["id"]
        stop_low = lower * (1 - p["stop_pct"] / 100)
        stop_high = upper * (1 + p["stop_pct"] / 100)
        mode = p["mode"]
        broke_out = (
            (mode == "long" and (close > upper or close <= stop_low))
            or (mode == "short" and (close < lower or close >= stop_high))
            or (mode == "neutral" and (close <= stop_low or close >= stop_high))
        )
        if broke_out and not self.fixed and p["recenter"]:
            intents: list[Intent] = [CloseIntent(t.id, "grid recentred") for t in ctx.trades]
            state["grid"] = self._build(close, grid_id + 1)
            state["grid_count"] = grid_id + 1
            state["prev_close"] = close
            return intents

        prev = float(state.get("prev_close", close))
        state["prev_close"] = close
        steps = int(p["levels"])
        levels = grid_levels(lower, upper, steps)
        center = steps // 2
        busy = {trade.comment for trade in ctx.trades}
        intents = []
        for index, level in enumerate(levels):
            buys = (mode == "long" and index < steps) or (mode == "neutral" and index < center)
            sells = (mode == "short" and index > 0) or (mode == "neutral" and index > center)
            if buys and prev > level >= close and close > stop_low:
                tag = f"g{grid_id}L{index}"
                if tag not in busy:
                    intents.append(
                        OpenIntent(
                            LONG,
                            stop_loss=stop_low,
                            take_profit=levels[index + 1],
                            comment=tag,
                            reason=f"price fell through grid level {level:.5g}",
                        )
                    )
            if sells and prev < level <= close and close < stop_high:
                tag = f"g{grid_id}S{index}"
                if tag not in busy:
                    intents.append(
                        OpenIntent(
                            SHORT,
                            stop_loss=stop_high,
                            take_profit=levels[index - 1],
                            comment=tag,
                            reason=f"price rose through grid level {level:.5g}",
                        )
                    )
        return intents
