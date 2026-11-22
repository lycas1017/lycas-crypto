from datetime import datetime, timedelta, timezone

import pytest
import requests
from helpers import T0, candles_from_closes

from forex_bot.feeds import CsvFeed, DataError, SyntheticFeed, read_csv, write_csv
from forex_bot.feeds.base import drop_incomplete, resample
from forex_bot.feeds.yahoo import YahooFeed, parse_chart, yahoo_symbol


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.reason = "OK" if status == 200 else "Error"
        self.content = b"x"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload):
        self.payload = payload
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return FakeResponse(self.payload)


def chart(stamps, closes, meta=None):
    return {"chart": {"error": None, "result": [{
        "meta": meta or {},
        "timestamp": stamps,
        "indicators": {"quote": [{
            "open": closes, "high": [c + 1 if c else None for c in closes],
            "low": [c - 1 if c else None for c in closes], "close": closes,
            "volume": [0] * len(closes),
        }]},
    }]}}


def test_yahoo_symbols():
    assert yahoo_symbol("XAU_USD") == "GC=F"
    assert yahoo_symbol("EUR_USD") == "EURUSD=X"
    assert yahoo_symbol("EUR_USD", {"EUR_USD": "6E=F"}) == "6E=F"


def test_yahoo_replaces_the_default_requests_user_agent():
    # Yahoo answers HTTP 429 to "python-requests/x", the agent a plain Session sends.
    feed = YahooFeed(session=requests.Session())
    assert "python-requests" not in feed.session.headers["User-Agent"]


def test_parse_chart_skips_null_rows():
    stamp = int(T0.timestamp())
    candles = parse_chart(chart([stamp, stamp + 3600, stamp + 7200], [10.0, None, 12.0]))
    assert [c.close for c in candles] == [10.0, 12.0]
    assert candles[0].time == T0


def test_yahoo_history_resamples_h4_and_drops_the_forming_candle():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=12)
    stamps = [int((start + timedelta(hours=i)).timestamp()) for i in range(12)]
    feed = YahooFeed(session=FakeSession(chart(stamps, [float(i + 100) for i in range(12)])))
    candles = feed.history("XAU_USD", "H4", start - timedelta(hours=8), now)
    assert all(c.time.hour % 4 == 0 for c in candles)
    assert all(c.time + timedelta(hours=4) <= now for c in candles)
    url, params = feed.session.calls[0]
    assert "GC=F" in url and params["interval"] == "60m"


def test_yahoo_snaps_off_grid_bars_and_cleans_float32_noise():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(hours=6)
    stamps = [int((start + timedelta(hours=i)).timestamp()) for i in range(3)]
    stamps.append(stamps[-1] + 3597)  # Yahoo's "21:59:57" session-end bar
    closes = [3510.0, 3512.0, 3515.39990234375, 3516.0]
    feed = YahooFeed(session=FakeSession(chart(stamps, closes)))
    candles = feed.history("XAU_USD", "H1", start, now)
    assert [c.time.minute for c in candles] == [0, 0, 0]
    assert candles[2].open == 3515.4  # 32-bit noise removed
    assert candles[2].close == 3516.0  # the straggler merged into the 3rd hour


def test_yahoo_quotes_mark_stale_prices_untradeable():
    old = int((datetime.now(timezone.utc) - timedelta(days=2)).timestamp())
    feed = YahooFeed(session=FakeSession(
        chart([], [], meta={"regularMarketPrice": 1.08, "regularMarketTime": old})
    ))
    quote = feed.quotes(["EUR_USD"])["EUR_USD"]
    assert quote.mid == pytest.approx(1.08)
    assert not quote.tradeable


def test_yahoo_error_is_a_data_error():
    payload = {"chart": {"result": None, "error": {"description": "No data found"}}}
    feed = YahooFeed(session=FakeSession(payload))
    with pytest.raises(DataError, match="No data found"):
        feed.quotes(["EUR_USD"])


def test_resample_and_drop_incomplete():
    hourly = candles_from_closes([1, 2, 3, 4, 5, 6, 7, 8], step_minutes=60)
    four = resample(hourly, 4 * 3600)
    assert len(four) == 2
    assert four[0].open == 1 and four[0].close == 4 and four[0].high == max(1, 2, 3, 4)
    now = hourly[-1].time + timedelta(minutes=30)
    assert drop_incomplete(hourly, "H1", now) == hourly[:-1]


def test_csv_round_trip_and_errors(tmp_path):
    candles = candles_from_closes([1.1, 1.2, 1.15])
    write_csv(tmp_path / "EUR_USD_H1.csv", candles)
    assert read_csv(tmp_path / "EUR_USD_H1.csv") == candles
    feed = CsvFeed(tmp_path)
    assert feed.history("EUR_USD", "H1", T0, T0 + timedelta(hours=2)) == candles[:2]
    with pytest.raises(DataError, match="forex-bot fetch"):
        feed.history("XAU_USD", "H1", T0, T0 + timedelta(days=1))
    (tmp_path / "bad.csv").write_text("date,price\n2025-01-01,1\n")
    with pytest.raises(DataError):
        read_csv(tmp_path / "bad.csv")


def test_synthetic_is_reproducible_and_consistent_across_timeframes():
    start = datetime(2025, 3, 3, tzinfo=timezone.utc)
    end = start + timedelta(days=14)
    a = SyntheticFeed(seed=1).history("XAU_USD", "H1", start, end)
    b = SyntheticFeed(seed=1).history("XAU_USD", "H1", start, end)
    assert a == b and len(a) > 200
    four = SyntheticFeed(seed=1).history("XAU_USD", "H4", start, end)
    by_time = {c.time: c for c in a}
    for candle in four[:10]:
        assert candle.open == pytest.approx(by_time[candle.time].open)
    assert all(c.time.weekday() != 5 for c in a)  # closed on Saturday
