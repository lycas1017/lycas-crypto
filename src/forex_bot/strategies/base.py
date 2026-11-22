"""What every strategy receives and returns.

A strategy never talks to a broker. On each closed candle it looks at recent candles,
its own open trades and its saved state, and returns intents: open a trade or close
one. The executor turns intents into sized, risk-checked orders, so the same strategy
code runs unchanged in backtests, paper trading and live trading.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from forex_bot.instruments import InstrumentSpec
from forex_bot.models import AccountState, Candle, Intent, Trade


class StrategyConfigError(ValueError):
    pass


@dataclass
class StrategyContext:
    bot_id: str
    instrument: str
    granularity_seconds: int
    candles: Sequence[Candle]  # closed candles, oldest first; candles[-1] just closed
    trades: list[Trade]  # this bot's open trades only
    state: dict[str, Any]  # saved between runs; the strategy may change it freely
    account: AccountState
    spec: InstrumentSpec

    @property
    def last(self) -> Candle:
        return self.candles[-1]


class Strategy(ABC):
    name: ClassVar[str]
    defaults: ClassVar[dict[str, Any]] = {}

    def __init__(self, params: Mapping[str, Any] | None = None) -> None:
        params = dict(params or {})
        unknown = sorted(set(params) - set(self.defaults))
        if unknown:
            raise StrategyConfigError(
                f"unknown {self.name} parameter(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(self.defaults))}"
            )
        self.params: dict[str, Any] = {**self.defaults, **params}
        self.validate()

    def validate(self) -> None:  # noqa: B027 - optional hook, not every strategy needs it
        """Raise ``StrategyConfigError`` for impossible parameter combinations."""

    @property
    @abstractmethod
    def warmup(self) -> int:
        """Closed candles needed before the first signal."""

    @property
    def lookback(self) -> int:
        """How many recent candles to hand the strategy each time (indicators converge)."""
        return max(200, self.warmup * 4)

    @property
    def max_trades(self) -> int:
        """Most trades this strategy keeps open at once (the executor enforces it)."""
        return 1

    @abstractmethod
    def on_candle(self, ctx: StrategyContext) -> list[Intent]: ...

    def describe(self) -> str:
        params = ", ".join(f"{key}={value}" for key, value in self.params.items())
        return f"{self.name}({params})"

    def _require(self, condition: bool, message: str) -> None:
        if not condition:
            raise StrategyConfigError(f"{self.name}: {message}")
