"""Technical indicators in plain Python. Each returns a list aligned with its input.

Values before an indicator has enough data are ``math.nan``.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from forex_bot.models import Candle


def sma(values: Sequence[float], period: int) -> list[float]:
    _check_period(period)
    out = [math.nan] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            out[index] = running / period
    return out


def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average seeded with the simple average of the first ``period``."""
    _check_period(period)
    out = [math.nan] * len(values)
    if len(values) < period:
        return out
    alpha = 2.0 / (period + 1)
    current = sum(values[:period]) / period
    out[period - 1] = current
    for index in range(period, len(values)):
        current += alpha * (values[index] - current)
        out[index] = current
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float]:
    """Wilder's relative strength index, 0 to 100."""
    _check_period(period)
    out = [math.nan] * len(values)
    if len(values) <= period:
        return out
    gains = 0.0
    losses = 0.0
    for index in range(1, period + 1):
        change = values[index] - values[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)
    for index in range(period + 1, len(values)):
        change = values[index] - values[index - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[index] = _rsi_value(avg_gain, avg_loss)
    return out


def true_range(candles: Sequence[Candle]) -> list[float]:
    out: list[float] = []
    for index, candle in enumerate(candles):
        if index == 0:
            out.append(candle.high - candle.low)
            continue
        prev_close = candles[index - 1].close
        out.append(
            max(
                candle.high - candle.low,
                abs(candle.high - prev_close),
                abs(candle.low - prev_close),
            )
        )
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> list[float]:
    """Wilder's average true range."""
    _check_period(period)
    ranges = true_range(candles)
    out = [math.nan] * len(ranges)
    if len(ranges) < period:
        return out
    current = sum(ranges[:period]) / period
    out[period - 1] = current
    for index in range(period, len(ranges)):
        current = (current * (period - 1) + ranges[index]) / period
        out[index] = current
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _check_period(period: int) -> None:
    if period < 1:
        raise ValueError(f"indicator period must be at least 1, got {period}")
