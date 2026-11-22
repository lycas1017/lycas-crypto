import math

import pytest
from helpers import candles_from_closes

from forex_bot.indicators import atr, ema, rsi, sma


def test_sma_and_ema_basics():
    values = [1, 2, 3, 4, 5, 6]
    assert sma(values, 3)[2:] == [2, 3, 4, 5]
    assert math.isnan(sma(values, 3)[1])
    out = ema(values, 3)
    assert out[2] == pytest.approx(2.0)  # seeded with the SMA
    assert out[3] == pytest.approx(3.0)  # 2 + 0.5 * (4 - 2)
    assert out[5] == pytest.approx(5.0)


def test_ema_shorter_than_period_is_all_nan():
    assert all(math.isnan(v) for v in ema([1, 2], 5))


def test_rsi_extremes_and_range():
    rising = [float(i) for i in range(30)]
    assert rsi(rising, 14)[-1] == pytest.approx(100.0)
    falling = rising[::-1]
    assert rsi(falling, 14)[-1] == pytest.approx(0.0)
    zigzag = [1.0 + (0.01 if i % 2 else 0.0) for i in range(40)]
    value = rsi(zigzag, 14)[-1]
    assert 40 < value < 60


def test_atr_matches_constant_range():
    candles = candles_from_closes([1.0] * 30, wick=0.5)  # every bar spans exactly 1.0
    assert atr(candles, 14)[-1] == pytest.approx(1.0)


def test_bad_period_is_rejected():
    with pytest.raises(ValueError):
        ema([1, 2, 3], 0)
