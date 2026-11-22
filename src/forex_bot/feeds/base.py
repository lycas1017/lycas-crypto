"""Where candles and prices come from."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from forex_bot.instruments import GRANULARITY_SECONDS
from forex_bot.models import Candle, Quote, utcnow


class DataError(RuntimeError):
    """A data source could not deliver what was asked for."""


class DataFeed(ABC):
    name: str = "feed"
    #: True when ``quotes`` returns real bid/ask; False when it returns a mid price only
    #: (the paper broker then adds a modelled spread).
    has_bid_ask: bool = False
    #: True when the feed can serve live prices for paper or live trading.
    live: bool = False

    @abstractmethod
    def history(
        self, instrument: str, granularity: str, start: datetime, end: datetime
    ) -> list[Candle]:
        """Complete candles with ``start <= time < end``, oldest first."""

    def recent(self, instrument: str, granularity: str, count: int) -> list[Candle]:
        """The latest ``count`` complete candles."""
        span = timedelta(seconds=granularity_seconds(granularity) * count)
        # Markets close at weekends, so look back further than ``count`` bars.
        end = utcnow()
        start = end - span * 2 - timedelta(days=4)
        return self.history(instrument, granularity, start, end)[-count:]

    def quotes(self, instruments: Sequence[str]) -> dict[str, Quote]:
        raise DataError(f"the {self.name} data source cannot provide live prices")


def granularity_seconds(granularity: str) -> int:
    try:
        return GRANULARITY_SECONDS[granularity]
    except KeyError as exc:
        raise ValueError(
            f"granularity must be one of {', '.join(GRANULARITY_SECONDS)}, got {granularity!r}"
        ) from exc


def drop_incomplete(candles: Sequence[Candle], granularity: str, now: datetime) -> list[Candle]:
    step = timedelta(seconds=granularity_seconds(granularity))
    return [c for c in candles if c.time + step <= now]


def resample(candles: Sequence[Candle], seconds: int) -> list[Candle]:
    """Merge candles into larger buckets aligned to multiples of ``seconds`` since 1970 UTC."""
    out: list[Candle] = []
    bucket_start: int | None = None
    current: Candle | None = None
    for candle in candles:
        stamp = int(candle.time.timestamp())
        start = stamp - stamp % seconds
        if current is None or start != bucket_start:
            if current is not None:
                out.append(current)
            bucket_start = start
            current = Candle(
                time=datetime.fromtimestamp(start, tz=timezone.utc),
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
        else:
            current = Candle(
                time=current.time,
                open=current.open,
                high=max(current.high, candle.high),
                low=min(current.low, candle.low),
                close=candle.close,
                volume=current.volume + candle.volume,
            )
    if current is not None:
        out.append(current)
    return out
