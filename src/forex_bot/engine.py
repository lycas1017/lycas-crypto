"""The paper/live trading loop.

Every ``poll_seconds`` the engine:

1. refreshes prices (paper mode: the simulated broker checks stops against them),
2. updates the daily loss limit and drawdown kill switch,
3. reports trades the broker closed on its own (stop loss or take profit),
4. for each bot whose candle has just closed, runs the strategy and executes its intents,
5. saves state, so a restart continues where it left off.
"""

from __future__ import annotations

import contextlib
import logging
import signal
import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from forex_bot.brokers.base import Broker, BrokerError
from forex_bot.brokers.paper import PaperBroker
from forex_bot.config import AppConfig, BotConfig
from forex_bot.execution import BotRuntime, Executor
from forex_bot.feeds.base import DataError, DataFeed, granularity_seconds
from forex_bot.instruments import ConversionError
from forex_bot.models import ClosedTrade, ExecutionReport, Trade, format_time, parse_time, utcnow
from forex_bot.notifier import Notifier
from forex_bot.risk import RiskManager
from forex_bot.state import StateStore

log = logging.getLogger(__name__)
ALERT_AFTER_FAILURES = 3


class TradingEngine:
    def __init__(
        self,
        config: AppConfig,
        bots: Sequence[BotConfig],
        broker: Broker,
        feed: DataFeed,
        store: StateStore,
        notifier: Notifier | None = None,
        mode: str = "paper",
    ) -> None:
        if not feed.live:
            raise ValueError(f"the {feed.name} data source has no live prices; use oanda or yahoo")
        self.config = config
        self.broker = broker
        self.feed = feed
        self.store = store
        self.notifier = notifier or Notifier()
        self.mode = mode
        self.state: dict[str, Any] = store.load()
        saved_mode = self.state.get("mode")
        if saved_mode and saved_mode != mode:
            raise ValueError(f"{store.path} belongs to {saved_mode} mode, not {mode}")
        self.state["mode"] = mode
        if isinstance(broker, PaperBroker) and self.state.get("paper"):
            broker.restore(self.state["paper"])
        self.risk = RiskManager(config.risk, self.state.setdefault("risk", {}))
        saved_bots = self.state.setdefault("bots", {})
        self.bots = [BotRuntime.build(bot, saved_bots.setdefault(bot.id, {})) for bot in bots]
        self.executor = Executor(broker, self.risk)
        self.bot_ids = {bot.id for bot in self.bots}
        self.known_trades = {t.id for t in self._our_trades()}
        self._stop = threading.Event()
        self._failures = 0

    @property
    def instruments(self) -> list[str]:
        return sorted({bot.instrument for bot in self.bots})

    # Lifecycle ------------------------------------------------------------------------

    def startup_checks(self) -> list[str]:
        """Warnings worth showing before the first tick."""
        warnings: list[str] = []
        per_instrument: dict[str, list[str]] = {}
        for bot in self.bots:
            per_instrument.setdefault(bot.instrument, []).append(bot.id)
        hedging = getattr(self.broker, "hedging_enabled", True)
        if not hedging:
            for instrument, ids in per_instrument.items():
                if len(ids) > 1:
                    warnings.append(
                        f"{', '.join(ids)} all trade {instrument}, but this OANDA account does "
                        "not allow hedging: opposite trades will close each other. Give each "
                        "bot its own instrument."
                    )
            for bot in self.bots:
                if bot.config.strategy == "grid" and bot.strategy.params.get("mode") == "neutral":
                    warnings.append(f"{bot.id}: a neutral grid needs a hedging-enabled account")
        return warnings

    def stop(self, *_: Any) -> None:
        self._stop.set()

    def run(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError):  # only possible in the main thread
                signal.signal(sig, self.stop)
        for warning in self.startup_checks():
            log.warning(warning)
            self.notifier.send(f"Warning: {warning}")
        account = self.broker.account()
        names = ", ".join(b.id for b in self.bots)
        self.notifier.send(
            f"forex-bot started in {self.mode.upper()} mode with {len(self.bots)} bot(s): "
            f"{names}. Equity {account.equity:,.2f} {account.currency}."
        )
        while not self._stop.is_set():
            try:
                self.tick()
                self._failures = 0
            except Exception as exc:  # keep running through network hiccups
                self._failures += 1
                log.exception("tick failed (%d in a row)", self._failures)
                if self._failures == ALERT_AFTER_FAILURES:
                    self.notifier.send(f"forex-bot keeps failing: {exc}")
            self._stop.wait(self.config.engine.poll_seconds)
        self.save()
        self.notifier.send("forex-bot stopped. Open trades keep their stop loss and take profit.")

    # One cycle ------------------------------------------------------------------------

    def tick(self, now: datetime | None = None) -> None:
        now = now or utcnow()
        closed: list[ClosedTrade] = []
        if isinstance(self.broker, PaperBroker):
            closed.extend(self._refresh_paper_prices())

        account = self.broker.account()
        self._daily_summary(now, account.equity)
        for event in self.risk.update(now, account.equity):
            log.warning(event)
            self.notifier.send(event)
            if self.risk.kill_switch and self.config.risk.close_all_on_max_drawdown:
                closed.extend(self._close_everything("max drawdown kill switch"))

        reported = {c.id for c in closed}
        current = {t.id for t in self._our_trades()}
        for trade_id in sorted(self.known_trades - current - reported):
            gone = self.broker.get_closed_trade(trade_id)
            if gone:
                closed.append(gone)
        for item in closed:
            self._announce_close(item)

        for bot in self.bots:
            try:
                self._run_bot(bot, now)
            except (DataError, BrokerError, ConversionError) as exc:
                log.warning("[%s] skipped this cycle: %s", bot.id, exc)
        self.known_trades = {t.id for t in self._our_trades()}
        self.save()

    def _run_bot(self, bot: BotRuntime, now: datetime) -> None:
        step = timedelta(seconds=granularity_seconds(bot.granularity))
        seen = bot.state.get("last_candle")
        if seen and now < parse_time(seen) + 2 * step:
            return  # the next candle has not closed yet
        candles = self.feed.recent(bot.instrument, bot.granularity, bot.strategy.lookback)
        if len(candles) < bot.strategy.warmup:
            log.warning(
                "[%s] only %d candles available, need %d", bot.id, len(candles), bot.strategy.warmup
            )
            return
        newest = candles[-1].time
        if seen and parse_time(seen) >= newest:
            return
        bot.state["last_candle"] = format_time(newest)
        if isinstance(self.broker, PaperBroker):
            try:
                self.broker.quote(bot.instrument)
            except BrokerError:
                self.broker.set_mid(bot.instrument, candles[-1].close, now)
        context = bot.context(
            candles,
            self.broker.open_trades(bot.id),
            self.broker.account(),
            self.broker.spec(bot.instrument),
        )
        intents = bot.strategy.on_candle(context)
        if intents:
            self._announce(bot, self.executor.execute(bot, intents))

    # Helpers --------------------------------------------------------------------------

    def _refresh_paper_prices(self) -> list[ClosedTrade]:
        assert isinstance(self.broker, PaperBroker)
        try:
            quotes = self.feed.quotes(self.instruments)
        except DataError as exc:
            log.warning("could not refresh prices: %s", exc)
            return []
        closed: list[ClosedTrade] = []
        for quote in quotes.values():
            if self.feed.has_bid_ask:
                closed.extend(self.broker.update_quote(quote))
            else:
                closed.extend(
                    self.broker.set_mid(quote.instrument, quote.mid, quote.time, quote.tradeable)
                )
        return closed

    def _our_trades(self) -> list[Trade]:
        return [t for t in self.broker.open_trades() if t.bot_id in self.bot_ids]

    def _close_everything(self, reason: str) -> list[ClosedTrade]:
        closed = []
        for trade in self._our_trades():
            try:
                closed.append(self.broker.close_trade(trade.id, reason))
            except BrokerError as exc:
                log.error("could not close %s: %s", trade.id, exc)
                self.notifier.send(f"Could not close trade {trade.id}: {exc}. Check the account!")
        return closed

    def _announce(self, bot: BotRuntime, report: ExecutionReport) -> None:
        for item in report.closed:
            self._announce_close(item)
        for trade in report.opened:
            side = "BUY" if trade.units > 0 else "SELL"
            message = (
                f"[{bot.id}] {side} {abs(trade.units):g} {trade.instrument} @ {trade.entry_price}"
                f" | SL {trade.stop_loss} | TP {trade.take_profit}"
            )
            log.info(message)
            self.notifier.send(message)
        for message in report.rejected:
            log.info("[%s] skipped %s", bot.id, message)

    def _announce_close(self, item: ClosedTrade) -> None:
        side = "long" if item.units > 0 else "short"
        message = (
            f"[{item.bot_id}] closed {side} {abs(item.units):g} {item.instrument} "
            f"{item.entry_price} -> {item.exit_price}: {item.realized_pl:+,.2f} ({item.reason})"
        )
        log.info(message)
        self.notifier.send(message)

    def _daily_summary(self, now: datetime, equity: float) -> None:
        day = now.date().isoformat()
        last = self.state.get("summary_day")
        if last == day:
            return
        self.state["summary_day"] = day
        if last is None:
            return
        start = float(self.risk.state.get("day_start_equity", equity))
        change = equity - start
        self.notifier.send(
            f"Daily summary {last}: equity {equity:,.2f} ({change:+,.2f}), "
            f"{len(self._our_trades())} open trade(s)."
        )

    def save(self) -> None:
        if isinstance(self.broker, PaperBroker):
            self.state["paper"] = self.broker.to_dict()
        self.store.save(self.state)
