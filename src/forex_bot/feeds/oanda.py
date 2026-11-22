"""Candles and live bid/ask prices from OANDA (a free practice account is enough)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from forex_bot.brokers.oanda import OandaClient, parse_price
from forex_bot.feeds.base import DataFeed
from forex_bot.models import Candle, Quote


class OandaFeed(DataFeed):
    name = "oanda"
    has_bid_ask = True
    live = True

    def __init__(self, client: OandaClient) -> None:
        self.client = client

    def history(
        self, instrument: str, granularity: str, start: datetime, end: datetime
    ) -> list[Candle]:
        return self.client.candle_history(instrument, granularity, start, end)

    def recent(self, instrument: str, granularity: str, count: int) -> list[Candle]:
        # Ask for one extra: the newest candle is usually still forming and gets dropped.
        return self.client.candles(instrument, granularity, count=count + 1)[-count:]

    def quotes(self, instruments: Sequence[str]) -> dict[str, Quote]:
        prices = self.client.pricing(list(instruments)).get("prices", [])
        return {item["instrument"]: parse_price(item) for item in prices}
