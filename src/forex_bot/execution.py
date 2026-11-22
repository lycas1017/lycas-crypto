"""Turns strategy intents into sized, risk-checked orders on any broker."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from forex_bot.brokers.base import Broker, BrokerError, OrderRejected
from forex_bot.config import BotConfig
from forex_bot.feeds.base import granularity_seconds
from forex_bot.instruments import ConversionError, InstrumentSpec
from forex_bot.models import (
    AccountState,
    Candle,
    CloseIntent,
    ExecutionReport,
    Intent,
    OpenIntent,
    Trade,
)
from forex_bot.risk import RiskManager
from forex_bot.strategies import Strategy, StrategyContext, create_strategy

log = logging.getLogger(__name__)


@dataclass
class BotRuntime:
    """A configured bot: its settings, strategy instance and saved state."""

    config: BotConfig
    strategy: Strategy
    state: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(cls, config: BotConfig, state: dict[str, Any] | None = None) -> BotRuntime:
        # Keep the caller's dict (even when empty): the engine persists that exact object.
        return cls(
            config,
            create_strategy(config.strategy, config.params),
            state if state is not None else {},
        )

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def instrument(self) -> str:
        return self.config.instrument

    @property
    def granularity(self) -> str:
        return self.config.granularity

    @property
    def max_trades(self) -> int:
        return self.config.max_trades or self.strategy.max_trades

    def context(
        self,
        candles: Sequence[Candle],
        trades: list[Trade],
        account: AccountState,
        spec: InstrumentSpec,
    ) -> StrategyContext:
        return StrategyContext(
            bot_id=self.id,
            instrument=self.instrument,
            granularity_seconds=granularity_seconds(self.granularity),
            candles=candles,
            trades=trades,
            state=self.state.setdefault("strategy", {}),
            account=account,
            spec=spec,
        )


class Executor:
    def __init__(self, broker: Broker, risk: RiskManager) -> None:
        self.broker = broker
        self.risk = risk

    def execute(self, bot: BotRuntime, intents: Sequence[Intent]) -> ExecutionReport:
        """Closes run before opens so a reversal frees its slot first."""
        report = ExecutionReport()
        for intent in intents:
            if isinstance(intent, CloseIntent):
                try:
                    report.closed.append(self.broker.close_trade(intent.trade_id, intent.reason))
                except BrokerError as exc:
                    report.rejected.append(f"close {intent.trade_id}: {exc}")
        for intent in intents:
            if isinstance(intent, OpenIntent):
                try:
                    report.opened.append(self._open(bot, intent))
                except (BrokerError, ConversionError, ValueError) as exc:
                    side = "buy" if intent.direction > 0 else "sell"
                    report.rejected.append(f"{side} {bot.instrument} ({intent.reason}): {exc}")
        for message in report.rejected:
            log.debug("[%s] skipped %s", bot.id, message)
        return report

    def _open(self, bot: BotRuntime, intent: OpenIntent) -> Trade:
        broker = self.broker
        quote = broker.quote(bot.instrument)
        if not quote.tradeable:
            raise OrderRejected("market is closed")
        spec = broker.spec(bot.instrument)
        trades = broker.open_trades()
        reason = self.risk.check_open(
            open_total=len(trades),
            open_for_bot=sum(1 for t in trades if t.bot_id == bot.id),
            bot_max=bot.max_trades,
            quote=quote,
            spec=spec,
        )
        if reason:
            raise OrderRejected(reason)

        direction = 1 if intent.direction > 0 else -1
        price = quote.ask if direction > 0 else quote.bid
        stop_loss = intent.stop_loss
        if stop_loss is None and intent.sl_distance:
            stop_loss = price - direction * intent.sl_distance
        take_profit = intent.take_profit
        if take_profit is None and intent.tp_distance:
            take_profit = price + direction * intent.tp_distance
        if stop_loss is not None and (stop_loss - price) * direction >= 0:
            raise OrderRejected(f"price {price} is already past the stop loss {stop_loss}")
        if take_profit is not None and (take_profit - price) * direction <= 0:
            raise OrderRejected(f"price {price} is already past the take profit {take_profit}")

        account = broker.account()
        conversion = broker.conversion_rate(bot.instrument)
        units = self.risk.units_for(
            bot.config.sizing.scaled(intent.size_multiplier),
            price=price,
            stop_distance=abs(price - stop_loss) if stop_loss is not None else None,
            equity=account.equity,
            conversion_rate=conversion,
            spec=spec,
        )
        if units < spec.min_units:
            raise OrderRejected(
                f"size rounds to {units} units, below the {spec.min_units} minimum; "
                f"raise the bot's sizing value"
            )
        reason = self.risk.check_margin(
            units, price=price, conversion_rate=conversion, spec=spec, account=account
        )
        if reason:
            raise OrderRejected(reason)
        trade = broker.open_trade(
            bot.id,
            bot.instrument,
            direction * units,
            stop_loss,
            take_profit,
            comment=intent.comment,
            reason=intent.reason,
        )
        log.debug(
            "[%s] opened %s %s %s @ %s (SL %s, TP %s): %s",
            bot.id,
            "BUY" if direction > 0 else "SELL",
            abs(trade.units),
            bot.instrument,
            trade.entry_price,
            trade.stop_loss,
            trade.take_profit,
            intent.reason,
        )
        return trade
