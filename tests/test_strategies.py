import math
from datetime import timedelta

import pytest
from helpers import T0, candles_from_closes, context

from forex_bot.models import LONG, SHORT, CloseIntent, OpenIntent, Trade
from forex_bot.strategies import (
    DcaStrategy,
    EmaRsiStrategy,
    GridStrategy,
    StrategyConfigError,
    create_strategy,
)


def trade(units: float, price: float, comment: str = "", trade_id: str = "1", hours: int = 0):
    return Trade(trade_id, "EUR_USD", units, price, T0 + timedelta(hours=hours), "test",
                 comment=comment)


# EMA + RSI ------------------------------------------------------------------------------


def sine_candles(n=400, period=60.0):
    closes = [1.10 + 0.01 * math.sin(i * 2 * math.pi / period) for i in range(n)]
    return candles_from_closes(closes, wick=0.0005)


def run_ema(strategy, candles, trades=()):
    signals = []
    for end in range(strategy.warmup, len(candles) + 1):
        intents = strategy.on_candle(context(candles[:end], trades=list(trades)))
        if intents:
            signals.append((end - 1, intents))
    return signals


def test_ema_rsi_signals_both_ways_with_atr_stops():
    strategy = EmaRsiStrategy({"fast": 5, "slow": 15, "trend": 0, "sl_atr": 2, "tp_atr": 3})
    signals = run_ema(strategy, sine_candles())
    opens = [i for _, intents in signals for i in intents if isinstance(i, OpenIntent)]
    assert {i.direction for i in opens} == {LONG, SHORT}
    for intent in opens:
        assert intent.sl_distance > 0
        assert intent.tp_distance == pytest.approx(intent.sl_distance * 1.5)


def test_ema_rsi_closes_on_opposite_cross_and_does_not_stack():
    strategy = EmaRsiStrategy({"fast": 5, "slow": 15, "trend": 0})
    candles = sine_candles()
    long_trade = trade(1000, 1.1)
    for _, intents in run_ema(strategy, candles, trades=[long_trade]):
        kinds = {type(i) for i in intents}
        if CloseIntent in kinds:
            # a cross down closes the long and may open a short in the same step
            assert all(i.direction == SHORT for i in intents if isinstance(i, OpenIntent))
        else:
            # a cross up while already long must not add a second long
            assert not any(isinstance(i, OpenIntent) for i in intents)


def test_ema_rsi_trend_filter_blocks_counter_trend():
    up = [1.0 + i * 0.001 for i in range(300)]
    dip = [up[-1] - i * 0.0015 for i in range(1, 25)]
    candles = candles_from_closes(up + dip, wick=0.0002)
    filtered = EmaRsiStrategy({"fast": 5, "slow": 15, "trend": 200})
    shorts = [i for _, its in run_ema(filtered, candles) for i in its
              if isinstance(i, OpenIntent) and i.direction == SHORT]
    assert shorts == []  # price is still above the 200 EMA


def test_ema_rsi_rejects_bad_params():
    with pytest.raises(StrategyConfigError):
        EmaRsiStrategy({"fast": 50, "slow": 20})
    with pytest.raises(StrategyConfigError):
        create_strategy("ema_rsi", {"speed": 3})
    with pytest.raises(StrategyConfigError):
        create_strategy("martingale")


# Grid -----------------------------------------------------------------------------------


def grid_step(strategy, state, prev, close, trades=()):
    """Build the grid (first candle), then move from ``prev`` to ``close``."""
    if "grid" not in state:
        strategy.on_candle(context(candles_from_closes([prev]), state=state))
    state["prev_close"] = prev
    return strategy.on_candle(context(candles_from_closes([close]), state=state, trades=trades))


def test_grid_buys_when_price_falls_through_a_level():
    strategy = GridStrategy({"lower": 1.0, "upper": 1.1, "levels": 10, "stop_pct": 1.0})
    state = {}
    intents = grid_step(strategy, state, 1.055, 1.045)
    assert len(intents) == 1
    buy = intents[0]
    assert buy.direction == LONG
    assert buy.take_profit == pytest.approx(1.06)
    assert buy.stop_loss == pytest.approx(0.99)
    assert buy.comment == "g1L5"
    # the same level is not bought twice while its trade is open
    again = grid_step(strategy, state, 1.055, 1.045, trades=[trade(1000, 1.045, "g1L5")])
    assert again == []


def test_grid_falling_through_several_levels_buys_each():
    strategy = GridStrategy({"lower": 1.0, "upper": 1.1, "levels": 10})
    intents = grid_step(strategy, {}, 1.075, 1.042)
    assert [i.comment for i in intents] == ["g1L5", "g1L6", "g1L7"]


def test_grid_rising_does_not_buy_in_long_mode():
    strategy = GridStrategy({"lower": 1.0, "upper": 1.1, "levels": 10})
    assert grid_step(strategy, {}, 1.045, 1.055) == []


def test_neutral_grid_sells_above_the_middle():
    strategy = GridStrategy(
        {"lower": 1.0, "upper": 1.1, "levels": 10, "mode": "neutral", "stop_pct": 1.0}
    )
    intents = grid_step(strategy, {}, 1.075, 1.085)
    assert len(intents) == 1
    sell = intents[0]
    assert sell.direction == SHORT
    assert sell.take_profit == pytest.approx(1.07)
    assert sell.stop_loss == pytest.approx(1.1 * 1.01)
    assert grid_step(strategy, {}, 1.045, 1.055) == []  # the middle level has no orders


def test_auto_grid_recentres_after_breakout_and_closes_leftovers():
    strategy = GridStrategy({"range_pct": 2.0, "levels": 4, "mode": "long"})
    state = {}
    strategy.on_candle(context(candles_from_closes([100.0]), state=state))
    assert state["grid"]["lower"] == pytest.approx(98.0)
    leftover = trade(1, 99.0, "g1L1", trade_id="7")
    intents = strategy.on_candle(
        context(candles_from_closes([103.0]), state=state, trades=[leftover])
    )
    assert intents == [CloseIntent("7", "grid recentred")]
    assert state["grid"]["id"] == 2
    assert state["grid"]["lower"] == pytest.approx(103.0 * 0.98)


def test_grid_validation():
    with pytest.raises(StrategyConfigError):
        GridStrategy({"lower": 1.0})
    with pytest.raises(StrategyConfigError):
        GridStrategy({"lower": 1.2, "upper": 1.1})
    with pytest.raises(StrategyConfigError):
        GridStrategy({"mode": "sideways"})


# DCA ------------------------------------------------------------------------------------


def dca_ctx(close, trades=(), state=None, hours=0):
    candles = candles_from_closes([close], start=T0 + timedelta(hours=hours))
    return context(candles, trades=list(trades), state=state, instrument="XAU_USD")


def test_dca_cycle_base_safety_and_take_profit():
    strategy = DcaStrategy(
        {"interval_candles": 24, "take_profit_pct": 2.0, "safety_orders": 2,
         "safety_step_pct": 1.0, "safety_scale": 1.5}
    )
    state = {}
    first = strategy.on_candle(dca_ctx(2000.0, state=state))
    assert len(first) == 1 and first[0].size_multiplier == 1.0
    held = [trade(1, 2000.0, "dca1", "1")]

    assert strategy.on_candle(dca_ctx(1995.0, held, state, hours=1)) == []  # small dip
    safety = strategy.on_candle(dca_ctx(1979.0, held, state, hours=2))  # -1.05%
    assert safety[0].size_multiplier == pytest.approx(1.5)
    held.append(trade(1.5, 1979.0, "dca1", "2", hours=2))

    second = strategy.on_candle(dca_ctx(1958.0, held, state, hours=3))  # -1.06% from 1979
    assert second[0].size_multiplier == pytest.approx(2.25)
    held.append(trade(2.25, 1958.0, "dca1", "3", hours=3))
    # safety orders exhausted: a further dip does nothing
    assert strategy.on_candle(dca_ctx(1900.0, held, state, hours=4)) == []

    average = sum(t.units * t.entry_price for t in held) / sum(t.units for t in held)
    exit_price = average * 1.021
    closes = strategy.on_candle(dca_ctx(exit_price, held, state, hours=5))
    assert {c.trade_id for c in closes} == {"1", "2", "3"}
    assert all(c.reason == "dca_take_profit" for c in closes)
    # next cycle starts on the next candle
    restart = strategy.on_candle(dca_ctx(exit_price, [], state, hours=6))
    assert restart and restart[0].comment == "dca2"


def test_dca_scheduled_buys_and_stop_loss():
    strategy = DcaStrategy({"interval_candles": 4, "stop_loss_pct": 5.0, "safety_orders": 0})
    state = {}
    strategy.on_candle(dca_ctx(2000.0, state=state))
    held = [trade(1, 2000.0, "dca1", "1")]
    assert strategy.on_candle(dca_ctx(2001.0, held, state, hours=3)) == []
    scheduled = strategy.on_candle(dca_ctx(2001.0, held, state, hours=4))
    assert scheduled[0].reason == "scheduled buy"
    held.append(trade(1, 2001.0, "dca1", "2", hours=4))
    stopped = strategy.on_candle(dca_ctx(1890.0, held, state, hours=5))
    assert {c.reason for c in stopped} == {"dca_stop_loss"}
    # after a stop the next cycle waits one interval
    assert strategy.on_candle(dca_ctx(1890.0, [], state, hours=6)) == []
    assert strategy.on_candle(dca_ctx(1890.0, [], state, hours=9))


def test_dca_retries_a_rejected_entry_on_the_next_candle():
    strategy = DcaStrategy({"interval_candles": 24})
    state = {}
    assert strategy.on_candle(dca_ctx(2000.0, state=state))  # rejected: nothing opened
    retry = strategy.on_candle(dca_ctx(2001.0, [], state, hours=1))
    assert retry and retry[0].reason == "base order"


def test_short_dca_mirrors_long():
    strategy = DcaStrategy({"direction": "short", "take_profit_pct": 1.0})
    state = {}
    assert strategy.on_candle(dca_ctx(2000.0, state=state))[0].direction == SHORT
    held = [trade(-1, 2000.0, "dca1", "1")]
    closes = strategy.on_candle(dca_ctx(1979.0, held, state, hours=1))
    assert closes and isinstance(closes[0], CloseIntent)
