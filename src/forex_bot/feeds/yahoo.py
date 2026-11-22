"""Free candles from Yahoo Finance, so backtests and paper trading work without an account.

Yahoo's chart API is unofficial and limits how far back intraday data goes (1m: 7 days,
5m to 30m: 60 days, 1h: 730 days). Forex pairs use symbols like ``EURUSD=X``. Gold and
silver use COMEX futures (``GC=F``, ``SI=F``), which track spot closely but not exactly.
Prices are mid prices only, so the paper broker adds a modelled spread.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from forex_bot.feeds.base import (
    DataError,
    DataFeed,
    drop_incomplete,
    granularity_seconds,
    resample,
)
from forex_bot.models import Candle, Quote, utcnow

log = logging.getLogger(__name__)

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
USER_AGENT = "Mozilla/5.0 (compatible; forex-bot/1.0)"

# Our granularity -> (Yahoo interval, seconds to resample to or None).
INTERVALS: dict[str, tuple[str, int | None]] = {
    "M1": ("1m", None),
    "M5": ("5m", None),
    "M15": ("15m", None),
    "M30": ("30m", None),
    "H1": ("60m", None),
    "H4": ("60m", 4 * 3600),
    "D": ("1d", None),
}
MAX_LOOKBACK_DAYS = {"1m": 7, "5m": 59, "15m": 59, "30m": 59, "60m": 729, "1d": 365 * 30}
DEFAULT_SYMBOLS = {"XAU_USD": "GC=F", "XAG_USD": "SI=F"}
STALE_AFTER = timedelta(minutes=15)


def yahoo_symbol(instrument: str, overrides: Mapping[str, str] | None = None) -> str:
    if overrides and instrument in overrides:
        return overrides[instrument]
    if instrument in DEFAULT_SYMBOLS:
        return DEFAULT_SYMBOLS[instrument]
    base, quote = instrument.split("_")
    return f"{base}{quote}=X"


class YahooFeed(DataFeed):
    name = "yahoo"
    has_bid_ask = False
    live = True

    def __init__(
        self,
        symbols: Mapping[str, str] | None = None,
        session: requests.Session | None = None,
        timeout: float = 20.0,
        retries: int = 3,
    ) -> None:
        self.symbols = dict(symbols or {})
        self.session = session or requests.Session()
        # Must replace requests' own "python-requests/x" agent, which Yahoo answers with 429.
        self.session.headers["User-Agent"] = USER_AGENT
        self.timeout = timeout
        self.retries = retries

    def history(
        self, instrument: str, granularity: str, start: datetime, end: datetime
    ) -> list[Candle]:
        interval, resample_to = _interval(granularity)
        now = utcnow()
        earliest = now - timedelta(days=MAX_LOOKBACK_DAYS[interval])
        if start < earliest:
            log.warning(
                "Yahoo keeps only %d days of %s data; starting %s instead of %s",
                MAX_LOOKBACK_DAYS[interval],
                interval,
                earliest.date(),
                start.date(),
            )
            start = earliest
        end = min(end, now)
        payload = self._chart(
            yahoo_symbol(instrument, self.symbols),
            {
                "interval": interval,
                "period1": int(start.timestamp()),
                "period2": int(end.timestamp()),
                "includePrePost": "false",
            },
        )
        # Yahoo sometimes stamps the last bar of a session off the grid (21:59:57 instead
        # of 21:00); snapping every bar onto the interval grid merges such stragglers.
        base_seconds = 3600 if resample_to else granularity_seconds(granularity)
        candles = resample(parse_chart(payload), base_seconds)
        if resample_to:
            candles = resample(candles, resample_to)
        candles = drop_incomplete(candles, granularity, now)
        return [c for c in candles if start <= c.time < end]

    def quotes(self, instruments: Sequence[str]) -> dict[str, Quote]:
        out: dict[str, Quote] = {}
        now = utcnow()
        for instrument in instruments:
            payload = self._chart(
                yahoo_symbol(instrument, self.symbols), {"interval": "1m", "range": "1d"}
            )
            meta = payload["chart"]["result"][0]["meta"]
            price = meta.get("regularMarketPrice")
            stamp = meta.get("regularMarketTime")
            if price is None or stamp is None:
                raise DataError(f"Yahoo returned no price for {instrument}")
            when = datetime.fromtimestamp(int(stamp), tz=timezone.utc)
            price = _clean(price)
            out[instrument] = Quote(
                instrument, price, price, when, tradeable=now - when < STALE_AFTER
            )
        return out

    def _chart(self, symbol: str, params: dict[str, Any]) -> dict[str, Any]:
        url = CHART_URL.format(symbol=symbol)
        for attempt in range(self.retries):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt == self.retries - 1:
                    raise DataError(f"could not reach Yahoo Finance: {exc}") from exc
                time.sleep(2**attempt)
                continue
            if response.status_code in (429, 500, 502, 503) and attempt < self.retries - 1:
                log.info("Yahoo returned HTTP %s, retrying", response.status_code)
                time.sleep(2 ** (attempt + 1))
                continue
            break
        try:
            payload = response.json()
        except ValueError as exc:
            raise DataError(f"Yahoo returned HTTP {response.status_code} without JSON") from exc
        error = (payload.get("chart") or {}).get("error")
        if error or response.status_code >= 400:
            detail = (error or {}).get("description") or response.reason
            raise DataError(f"Yahoo Finance {symbol}: {detail}")
        return payload


def parse_chart(payload: dict[str, Any]) -> list[Candle]:
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return []
    data = result[0]
    stamps = data.get("timestamp") or []
    quote = ((data.get("indicators") or {}).get("quote") or [{}])[0]
    columns = [quote.get(key) or [] for key in ("open", "high", "low", "close", "volume")]
    candles: list[Candle] = []
    for index, stamp in enumerate(stamps):
        values = [col[index] if index < len(col) else None for col in columns]
        open_, high, low, close, volume = values
        if None in (open_, high, low, close):
            continue  # Yahoo leaves gaps as nulls
        candles.append(
            Candle(
                time=datetime.fromtimestamp(int(stamp), tz=timezone.utc),
                open=_clean(open_),
                high=_clean(high),
                low=_clean(low),
                close=_clean(close),
                volume=float(volume or 0),
            )
        )
    return candles


def _clean(value: float) -> float:
    """Yahoo sends 32-bit floats (3515.39990234375); 7 significant digits restore 3515.4."""
    return float(f"{float(value):.7g}")


def _interval(granularity: str) -> tuple[str, int | None]:
    granularity_seconds(granularity)  # validates the name
    return INTERVALS[granularity]
