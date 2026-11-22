"""Reproducible made-up price data for demos and tests (no network needed).

Prices follow a random walk whose drift switches between trending and ranging regimes,
so every strategy meets conditions it is designed for and conditions it is not. Markets
are closed from Friday 22:00 to Sunday 22:00 UTC, as for real forex and gold.

Each instrument has one underlying 15-minute series; H1, H4 and D candles are built from
it, so bots on different timeframes of the same instrument see the same prices.
"""

from __future__ import annotations

import math
import random
import zlib
from datetime import datetime, timedelta, timezone

from forex_bot.feeds.base import DataFeed, granularity_seconds, resample
from forex_bot.models import Candle

START_PRICES = {
    "XAU_USD": 2350.0,
    "XAG_USD": 28.0,
    "EUR_USD": 1.085,
    "GBP_USD": 1.27,
    "AUD_USD": 0.66,
    "NZD_USD": 0.61,
    "USD_CHF": 0.90,
    "USD_CAD": 1.36,
    "USD_JPY": 150.0,
    "EUR_JPY": 162.0,
    "GBP_JPY": 190.0,
    "EUR_GBP": 0.855,
}
ANNUAL_VOLATILITY = {"XAU_USD": 0.16, "XAG_USD": 0.28}
SECONDS_PER_YEAR = 260 * 24 * 3600  # trading time only
BASE_SECONDS = 900
ANCHOR = datetime(2024, 1, 1, tzinfo=timezone.utc)


def market_open(when: datetime) -> bool:
    weekday, hour = when.weekday(), when.hour
    if weekday == 5:
        return False
    if weekday == 4 and hour >= 22:
        return False
    return not (weekday == 6 and hour < 22)


class SyntheticFeed(DataFeed):
    name = "synthetic"

    def __init__(self, seed: int = 7) -> None:
        self.seed = seed
        self._cache: dict[tuple[str, int, datetime], list[Candle]] = {}

    def history(
        self, instrument: str, granularity: str, start: datetime, end: datetime
    ) -> list[Candle]:
        step = granularity_seconds(granularity)
        base_step = min(step, BASE_SECONDS)
        anchor = min(ANCHOR, start.replace(hour=0, minute=0, second=0, microsecond=0))
        candles = self._series(instrument, base_step, anchor, end)
        if step != base_step:
            candles = resample(candles, step)
        span = timedelta(seconds=step)
        return [c for c in candles if start <= c.time and c.time + span <= end]

    def _series(
        self, instrument: str, step: int, anchor: datetime, end: datetime
    ) -> list[Candle]:
        key = (instrument, step, anchor)
        cached = self._cache.get(key)
        if cached and cached[-1].time + timedelta(seconds=step) >= end:
            return cached
        candles = self._generate(instrument, step, anchor, end)
        self._cache[key] = candles
        return candles

    def _generate(
        self, instrument: str, step: int, anchor: datetime, end: datetime
    ) -> list[Candle]:
        rng = random.Random(zlib.crc32(f"{self.seed}:{instrument}:{step}".encode()))
        price = START_PRICES.get(instrument, 1.0)
        volatility = ANNUAL_VOLATILITY.get(instrument, 0.08) * math.sqrt(step / SECONDS_PER_YEAR)
        when = anchor
        delta = timedelta(seconds=step)
        drift = 0.0
        regime_left = 0
        target = price
        candles: list[Candle] = []
        while when < end:
            if not market_open(when):
                when += delta
                continue
            if regime_left <= 0:
                regime_left = int(rng.randint(60, 400) * max(1.0, 3600 / step) / 2)
                choice = rng.random()
                strength = volatility * rng.uniform(0.04, 0.12)
                drift = strength if choice < 0.33 else -strength if choice < 0.66 else 0.0
                target = price
            regime_left -= 1
            open_ = price
            high = low = open_
            for _ in range(4):
                pull = 0.0 if drift else 0.01 * math.log(target / price)
                price *= math.exp(drift / 4 + pull + rng.gauss(0, volatility / 2))
                high = max(high, price)
                low = min(low, price)
            candles.append(Candle(when, open_, high, low, price, float(rng.randint(50, 500))))
            when += delta
        return candles
