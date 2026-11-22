import json
from datetime import datetime, timedelta, timezone

import pytest
import yaml
from helpers import make_config

from forex_bot.backtest import run_backtest
from forex_bot.brokers import PaperBroker
from forex_bot.cli import main
from forex_bot.engine import TradingEngine
from forex_bot.feeds import SyntheticFeed
from forex_bot.feeds.base import DataFeed
from forex_bot.models import Quote
from forex_bot.report import summary_text, write_report
from forex_bot.state import StateStore, state_path

START = datetime(2025, 1, 6, tzinfo=timezone.utc)
END = START + timedelta(days=90)
BOTS = [
    {"id": "gold-trend", "instrument": "XAU_USD", "granularity": "H1", "strategy": "ema_rsi",
     "sizing": {"mode": "risk", "value": 1.0}},
    {"id": "eur-grid", "instrument": "EUR_USD", "granularity": "M15", "strategy": "grid",
     "sizing": {"mode": "notional", "value": 5000}, "params": {"range_pct": 0.5, "levels": 5}},
    {"id": "gold-dca", "instrument": "XAU_USD", "granularity": "H4", "strategy": "dca",
     "sizing": {"mode": "units", "value": 1}, "params": {"interval_candles": 6}},
]


def synthetic_data(config):
    feed = SyntheticFeed(seed=3)
    return {b.id: feed.history(b.instrument, b.granularity, START, END) for b in config.bots}


def test_backtest_runs_every_strategy_and_is_deterministic(tmp_path):
    config = make_config(BOTS)
    first = run_backtest(config, synthetic_data(config))
    second = run_backtest(config, synthetic_data(config))
    assert [t.realized_pl for t in first.trades] == [t.realized_pl for t in second.trades]
    assert {t.bot_id for t in first.trades} == {"gold-trend", "eur-grid", "gold-dca"}
    metrics = first.metrics()
    final = first.equity_curve[-1][1]
    assert metrics["final_equity"] == pytest.approx(final)
    # every closed trade's P/L is accounted for in the final equity
    assert final == pytest.approx(10_000 + sum(t.realized_pl for t in first.trades))
    assert "Backtest" in summary_text(first)
    out = write_report(first, tmp_path / "r")
    assert (out / "report.html").read_text().startswith("<!doctype html>")
    summary = json.loads((out / "summary.json").read_text())
    assert summary["metrics"]["trades"] == len(first.trades)


def test_orders_fill_at_the_next_candle_open():
    config = make_config(BOTS[:1])
    data = synthetic_data(config)
    result = run_backtest(config, data)
    opens = {c.time for c in data["gold-trend"]}
    for trade in result.trades:
        assert trade.open_time in opens  # filled at a candle open, never mid-candle


class FakeFeed(DataFeed):
    """Synthetic candles plus a mid-price quote at the latest close."""

    name = "fake"
    live = True

    def __init__(self, now):
        self.now = now
        self.inner = SyntheticFeed(seed=5)

    def history(self, instrument, granularity, start, end):
        return self.inner.history(instrument, granularity, start, min(end, self.now))

    def recent(self, instrument, granularity, count):
        start = self.now - timedelta(days=120)
        return self.history(instrument, granularity, start, self.now)[-count:]

    def quotes(self, instruments):
        out = {}
        for name in instruments:
            last = self.history(name, "M15", self.now - timedelta(days=3), self.now)[-1]
            out[name] = Quote(name, last.close, last.close, self.now)
        return out


def test_paper_engine_trades_saves_and_resumes(tmp_path):
    config = make_config(BOTS)
    now = datetime(2025, 5, 7, 12, 1, tzinfo=timezone.utc)
    store = StateStore(state_path(tmp_path / "state.json", "paper"))
    engine = TradingEngine(config, config.bots, PaperBroker(), FakeFeed(now), store, mode="paper")
    engine.tick(now)
    assert store.path.exists()
    saved = json.loads(store.path.read_text())
    assert saved["mode"] == "paper"
    assert set(saved["bots"]) == {"gold-trend", "eur-grid", "gold-dca"}
    opened = len(engine.broker.open_trades())
    assert opened >= 1  # at least the DCA base order

    # Same candle again after a restart: nothing new happens, the account is restored.
    again = TradingEngine(config, config.bots, PaperBroker(), FakeFeed(now), store, mode="paper")
    assert len(again.broker.open_trades()) == opened
    again.tick(now + timedelta(seconds=30))
    assert len(again.broker.open_trades()) == opened

    with pytest.raises(ValueError, match="paper mode"):
        TradingEngine(config, config.bots, PaperBroker(), FakeFeed(now), store, mode="live")


def test_cli_init_demo_backtest_check_and_live_guard(tmp_path, capsys):
    assert main(["init", str(tmp_path)]) == 0
    assert (tmp_path / "config.yaml").exists() and (tmp_path / ".env").exists()

    assert main(["demo", "--days", "20", "--out", str(tmp_path / "reports")]) == 0
    assert list((tmp_path / "reports").glob("demo-*/report.html"))

    raw = yaml.safe_load((tmp_path / "config.yaml").read_text())
    raw["data"]["source"] = "synthetic"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw))
    assert main(["backtest", "-c", str(config_path), "--bot", "gold-trend",
                 "--start", "2025-01-06", "--end", "2025-03-01",
                 "--out", str(tmp_path / "bt"), "--no-html"]) == 0
    assert list((tmp_path / "bt").glob("backtest-*/trades.csv"))
    assert main(["check", "-c", str(config_path)]) == 0

    # live trading refuses to start without the LIVE_TRADING switch
    assert main(["live", "-c", str(config_path)]) == 1
    assert "live trading is switched off" in capsys.readouterr().err

    assert main(["status", "-c", str(config_path)]) == 0
    assert main(["reset-risk", "-c", str(config_path)]) == 0
    assert main(["backtest", "-c", str(tmp_path / "nope.yaml")]) == 1
