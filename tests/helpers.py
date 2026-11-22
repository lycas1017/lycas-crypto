"""Small builders shared by the tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from forex_bot.config import parse_config
from forex_bot.instruments import default_spec
from forex_bot.models import AccountState, Candle
from forex_bot.strategies import StrategyContext

T0 = datetime(2025, 3, 3, tzinfo=timezone.utc)  # a Monday


def candles_from_closes(
    closes: list[float], step_minutes: int = 60, start: datetime = T0, wick: float = 0.0
) -> list[Candle]:
    out = []
    previous = closes[0]
    for index, close in enumerate(closes):
        high = max(previous, close) + wick
        low = min(previous, close) - wick
        out.append(
            Candle(start + timedelta(minutes=step_minutes * index), previous, high, low, close)
        )
        previous = close
    return out


def context(
    candles: list[Candle],
    instrument: str = "EUR_USD",
    trades=None,
    state=None,
    step_seconds: int = 3600,
) -> StrategyContext:
    return StrategyContext(
        bot_id="test",
        instrument=instrument,
        granularity_seconds=step_seconds,
        candles=candles,
        trades=list(trades or []),
        state=state if state is not None else {},
        account=AccountState("USD", 10_000, 10_000, 0, 10_000),
        spec=default_spec(instrument),
    )


def make_config(bots: list[dict], **sections) -> object:
    raw = {"account_currency": "USD", "bots": bots, **sections}
    return parse_config(raw, env={})
