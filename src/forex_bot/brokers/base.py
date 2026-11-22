"""The interface every broker (simulated or real) implements."""

from __future__ import annotations

from abc import ABC, abstractmethod

from forex_bot.instruments import InstrumentSpec
from forex_bot.models import AccountState, ClosedTrade, Quote, Trade


class BrokerError(RuntimeError):
    """The broker could not be reached or returned something unusable."""


class OrderRejected(BrokerError):
    """The broker (or the simulator) refused an order. The bot logs it and carries on."""


class Broker(ABC):
    currency: str

    @abstractmethod
    def account(self) -> AccountState: ...

    @abstractmethod
    def quote(self, instrument: str) -> Quote: ...

    @abstractmethod
    def spec(self, instrument: str) -> InstrumentSpec: ...

    @abstractmethod
    def conversion_rate(self, instrument: str) -> float:
        """Value of one unit of the instrument's quote currency in account currency."""

    @abstractmethod
    def open_trades(self, bot_id: str | None = None) -> list[Trade]: ...

    @abstractmethod
    def open_trade(
        self,
        bot_id: str,
        instrument: str,
        units: float,
        stop_loss: float | None,
        take_profit: float | None,
        comment: str = "",
        reason: str = "",
    ) -> Trade: ...

    @abstractmethod
    def close_trade(self, trade_id: str, reason: str = "") -> ClosedTrade: ...

    @abstractmethod
    def get_closed_trade(self, trade_id: str) -> ClosedTrade | None:
        """Details of a trade that closed on the broker side (stop loss, take profit)."""
