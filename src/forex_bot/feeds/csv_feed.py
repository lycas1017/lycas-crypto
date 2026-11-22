"""Candles from CSV files, for offline backtests.

Files are named ``<INSTRUMENT>_<GRANULARITY>.csv`` (for example ``XAU_USD_H1.csv``) with
a header row. Required columns: ``time``, ``open``, ``high``, ``low``, ``close``;
``volume`` is optional. ``time`` may be ISO-8601 or unix seconds. ``forex-bot fetch``
writes files in exactly this format.
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from forex_bot.feeds.base import DataError, DataFeed
from forex_bot.models import Candle, format_time, parse_time

TIME_COLUMNS = ("time", "timestamp", "datetime", "date")


class CsvFeed(DataFeed):
    name = "csv"

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)

    def path(self, instrument: str, granularity: str) -> Path:
        return self.directory / f"{instrument}_{granularity}.csv"

    def history(
        self, instrument: str, granularity: str, start: datetime, end: datetime
    ) -> list[Candle]:
        path = self.path(instrument, granularity)
        if not path.exists():
            raise DataError(
                f"no CSV file at {path}. Create it with: "
                f"forex-bot fetch {instrument} {granularity}"
            )
        return [c for c in read_csv(path) if start <= c.time < end]


def read_csv(path: str | Path) -> list[Candle]:
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise DataError(f"{path} is empty")
        columns = {name.strip().lower(): name for name in reader.fieldnames}
        time_key = next((columns[c] for c in TIME_COLUMNS if c in columns), None)
        missing = [c for c in ("open", "high", "low", "close") if c not in columns]
        if time_key is None or missing:
            raise DataError(f"{path} needs columns time, open, high, low, close")
        candles = []
        for line, row in enumerate(reader, start=2):
            try:
                candles.append(
                    Candle(
                        time=parse_time(row[time_key]),
                        open=float(row[columns["open"]]),
                        high=float(row[columns["high"]]),
                        low=float(row[columns["low"]]),
                        close=float(row[columns["close"]]),
                        volume=float(row[columns["volume"]] or 0) if "volume" in columns else 0.0,
                    )
                )
            except (TypeError, ValueError) as exc:
                raise DataError(f"{path} line {line}: {exc}") from exc
    candles.sort(key=lambda c: c.time)
    return candles


def write_csv(path: str | Path, candles: Iterable[Candle]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "open", "high", "low", "close", "volume"])
        for candle in candles:
            writer.writerow(
                [
                    format_time(candle.time),
                    repr(candle.open),
                    repr(candle.high),
                    repr(candle.low),
                    repr(candle.close),
                    repr(candle.volume),
                ]
            )
            count += 1
    return count
