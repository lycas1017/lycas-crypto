"""Replay history through the strategies on a simulated account.

All bots share one paper account, exactly as they would live. Candles from every bot
are merged into one timeline ordered by close time. For each candle:

1. Orders the bot decided on at the previous candle's close are filled at this candle's
   open (no look-ahead: a decision never uses a price it could not have seen).
2. Stop losses and take profits are checked against the candle's high and low.
3. The daily loss limit and drawdown kill switch are updated.
4. The strategy sees the closed candle and may queue new orders.

Open trades are closed at the last price when the data ends.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from forex_bot.brokers.paper import PaperBroker
from forex_bot.config import AppConfig, BotConfig
from forex_bot.execution import BotRuntime, Executor
from forex_bot.feeds.base import granularity_seconds
from forex_bot.metrics import compute_metrics
from forex_bot.models import Candle, ClosedTrade, Intent
from forex_bot.risk import RiskManager

log = logging.getLogger(__name__)
CLOSE_EVENT = 0
OPEN_EVENT = 1


@dataclass
class BacktestResult:
    currency: str
    initial_balance: float
    bots: list[BotConfig]
    trades: list[ClosedTrade]
    equity_curve: list[tuple[datetime, float]]
    rejections: Counter[str] = field(default_factory=Counter)
    risk_events: list[tuple[datetime, str]] = field(default_factory=list)
    candles: dict[str, int] = field(default_factory=dict)

    @property
    def start(self) -> datetime | None:
        return self.equity_curve[0][0] if self.equity_curve else None

    @property
    def end(self) -> datetime | None:
        return self.equity_curve[-1][0] if self.equity_curve else None

    def metrics(self) -> dict[str, Any]:
        return compute_metrics(self.initial_balance, self.trades, self.equity_curve)


def make_broker(
    config: AppConfig, balance: float | None = None, keep_all: bool = False
) -> PaperBroker:
    paper = config.paper
    return PaperBroker(
        currency=config.account_currency,
        balance=balance if balance is not None else paper.initial_balance,
        spread_pips=paper.spread_pips,
        commission_per_trade=paper.commission_per_trade,
        slippage_pips=paper.slippage_pips,
        conversion_rates=paper.conversion_rates,
        keep_all_closed=keep_all,
    )


def run_backtest(
    config: AppConfig,
    data: Mapping[str, Sequence[Candle]],
    bots: Sequence[BotConfig] | None = None,
    initial_balance: float | None = None,
) -> BacktestResult:
    """``data`` maps each bot id to its candles (oldest first)."""
    bot_configs = list(bots if bots is not None else config.enabled_bots())
    broker = make_broker(config, initial_balance, keep_all=True)
    risk = RiskManager(config.risk)
    executor = Executor(broker, risk)
    runtimes = [BotRuntime.build(bot) for bot in bot_configs]

    # Two events per candle on one timeline: its open (fill queued orders) and its close
    # (stops, risk, strategy). At equal times closes sort first (kind 0 before 1), so a
    # candle closing at 10:00 is handled before the next one opens at 10:00.
    events: list[tuple[datetime, int, int, int]] = []
    for index, bot in enumerate(runtimes):
        step = timedelta(seconds=granularity_seconds(bot.granularity))
        for i, candle in enumerate(data.get(bot.id) or []):
            events.append((candle.time, OPEN_EVENT, index, i))
            events.append((candle.time + step, CLOSE_EVENT, index, i))
    events.sort()

    result = BacktestResult(
        currency=config.account_currency,
        initial_balance=broker.balance,
        bots=bot_configs,
        trades=[],
        equity_curve=[],
        candles={bot.id: len(data.get(bot.id) or []) for bot in runtimes},
    )
    pending: dict[str, list[Intent]] = {}
    for when, kind, bot_index, candle_index in events:
        bot = runtimes[bot_index]
        series = data[bot.id]
        candle = series[candle_index]

        if kind == OPEN_EVENT:
            queued = pending.pop(bot.id, None)
            if queued:
                broker.set_mid(bot.instrument, candle.open, candle.time)
                report = executor.execute(bot, queued)
                for message in report.rejected:
                    result.rejections[_short_reason(message)] += 1
            continue

        close_time = when
        broker.process_candle(bot.instrument, candle, close_time)
        account = broker.account()
        for event in risk.update(close_time, account.equity):
            result.risk_events.append((close_time, event))
            log.warning("%s %s", close_time, event)
            if risk.kill_switch and config.risk.close_all_on_max_drawdown:
                broker.close_all("max drawdown kill switch")

        if candle_index + 1 >= bot.strategy.warmup:
            window = series[max(0, candle_index + 1 - bot.strategy.lookback) : candle_index + 1]
            context = bot.context(
                window, broker.open_trades(bot.id), account, broker.spec(bot.instrument)
            )
            intents = bot.strategy.on_candle(context)
            if intents:
                pending[bot.id] = intents
        result.equity_curve.append((close_time, broker.account().equity))

    if broker.open_trades():
        broker.close_all("end of backtest")
        if result.equity_curve:
            result.equity_curve.append((result.equity_curve[-1][0], broker.account().equity))
    result.trades = list(broker.closed)
    return result


def _short_reason(message: str) -> str:
    """Group rejection messages by cause for the summary table."""
    text = message.split("): ", 1)[-1]
    for marker in (" (", ": need", " needs", ";"):
        text = text.split(marker, 1)[0]
    return text[:90]
