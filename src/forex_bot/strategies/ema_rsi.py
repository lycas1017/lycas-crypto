"""Trend following: EMA crossover confirmed by RSI and an optional long-term trend filter.

- Long when the fast EMA crosses above the slow EMA, RSI is below ``rsi_long_max`` (not
  already overbought) and, if ``trend`` > 0, price is above the trend EMA.
- Short is the mirror image.
- Stop loss and take profit are ``sl_atr`` and ``tp_atr`` multiples of ATR from the fill.
- An opposite crossover closes the trade early when ``exit_on_cross`` is true.
"""

from __future__ import annotations

import math

from forex_bot.indicators import atr, ema, rsi
from forex_bot.models import LONG, SHORT, CloseIntent, Intent, OpenIntent
from forex_bot.strategies.base import Strategy, StrategyContext


class EmaRsiStrategy(Strategy):
    name = "ema_rsi"
    defaults = {
        "fast": 20,
        "slow": 50,
        "trend": 200,
        "rsi_period": 14,
        "rsi_long_max": 70.0,
        "rsi_short_min": 30.0,
        "atr_period": 14,
        "sl_atr": 2.0,
        "tp_atr": 3.0,
        "allow_long": True,
        "allow_short": True,
        "exit_on_cross": True,
    }

    def validate(self) -> None:
        p = self.params
        self._require(0 < p["fast"] < p["slow"], "need 0 < fast < slow")
        self._require(p["trend"] >= 0, "trend must be 0 (off) or a period")
        self._require(p["sl_atr"] > 0, "sl_atr must be positive")
        self._require(p["tp_atr"] >= 0, "tp_atr must be 0 (no target) or positive")
        self._require(p["allow_long"] or p["allow_short"], "allow at least one direction")
        self._require(0 <= p["rsi_short_min"] <= 100 and 0 <= p["rsi_long_max"] <= 100,
                      "RSI limits must be between 0 and 100")

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p["slow"], p["trend"], p["rsi_period"] + 1, p["atr_period"]) + 2

    def on_candle(self, ctx: StrategyContext) -> list[Intent]:
        p = self.params
        closes = [c.close for c in ctx.candles]
        fast = ema(closes, p["fast"])
        slow = ema(closes, p["slow"])
        if any(math.isnan(v) for v in (fast[-2], slow[-2], fast[-1], slow[-1])):
            return []
        crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
        if not (crossed_up or crossed_down):
            return []

        intents: list[Intent] = []
        remaining = list(ctx.trades)
        if p["exit_on_cross"]:
            for trade in ctx.trades:
                if (trade.is_long and crossed_down) or (not trade.is_long and crossed_up):
                    intents.append(CloseIntent(trade.id, "opposite EMA crossover"))
                    remaining.remove(trade)
        if remaining:
            return intents  # already in a trade in the signal direction

        strength = rsi(closes, p["rsi_period"])[-1]
        volatility = atr(ctx.candles, p["atr_period"])[-1]
        if math.isnan(strength) or math.isnan(volatility) or volatility <= 0:
            return intents
        close = closes[-1]
        trend = ema(closes, p["trend"])[-1] if p["trend"] else math.nan
        stop = p["sl_atr"] * volatility
        target = p["tp_atr"] * volatility or None

        long_ok = (
            crossed_up
            and p["allow_long"]
            and strength < p["rsi_long_max"]
            and (not p["trend"] or close > trend)
        )
        short_ok = (
            crossed_down
            and p["allow_short"]
            and strength > p["rsi_short_min"]
            and (not p["trend"] or close < trend)
        )
        if long_ok or short_ok:
            word = "above" if long_ok else "below"
            intents.append(
                OpenIntent(
                    LONG if long_ok else SHORT,
                    sl_distance=stop,
                    tp_distance=target,
                    reason=f"EMA{p['fast']} crossed {word} EMA{p['slow']}, RSI {strength:.0f}",
                )
            )
        return intents
