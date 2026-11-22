"""Plain data types shared by brokers, strategies, the backtester and the live engine."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

LONG = 1
SHORT = -1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str | int | float | datetime) -> datetime:
    """Parse an ISO-8601 string, unix seconds or datetime into an aware UTC datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    text = str(value).strip()
    if text.replace(".", "", 1).isdigit():
        return datetime.fromtimestamp(float(text), tz=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # OANDA sends nanoseconds; datetime understands at most microseconds.
    if "." in text:
        head, _, tail = text.partition(".")
        digits = ""
        rest = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                digits += char
            else:
                rest = tail[index:]
                break
        text = f"{head}.{digits[:6].ljust(6, '0')}{rest}"
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def format_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Candle:
    """One OHLC bar. ``time`` is the bar's open time in UTC."""

    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Quote:
    instrument: str
    bid: float
    ask: float
    time: datetime
    tradeable: bool = True

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class Trade:
    """An open position leg. ``units`` is signed: positive is long, negative is short."""

    id: str
    instrument: str
    units: float
    entry_price: float
    open_time: datetime
    bot_id: str
    stop_loss: float | None = None
    take_profit: float | None = None
    comment: str = ""
    unrealized_pl: float = 0.0

    @property
    def direction(self) -> int:
        return LONG if self.units > 0 else SHORT

    @property
    def is_long(self) -> bool:
        return self.units > 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["open_time"] = format_time(self.open_time)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Trade:
        values = dict(data)
        values["open_time"] = parse_time(values["open_time"])
        return cls(**values)


@dataclass
class ClosedTrade:
    id: str
    instrument: str
    bot_id: str
    units: float
    entry_price: float
    exit_price: float
    open_time: datetime
    close_time: datetime
    realized_pl: float
    reason: str
    comment: str = ""

    @property
    def is_win(self) -> bool:
        return self.realized_pl > 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["open_time"] = format_time(self.open_time)
        data["close_time"] = format_time(self.close_time)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClosedTrade:
        values = dict(data)
        values["open_time"] = parse_time(values["open_time"])
        values["close_time"] = parse_time(values["close_time"])
        return cls(**values)


@dataclass(frozen=True)
class AccountState:
    currency: str
    balance: float
    equity: float
    margin_used: float
    margin_available: float
    open_trade_count: int = 0


@dataclass(frozen=True)
class Sizing:
    """How big a new trade is.

    - ``risk``: ``value`` percent of equity is lost if the stop loss is hit (needs a stop).
    - ``notional``: ``value`` in account currency worth of the instrument.
    - ``units``: exactly ``value`` units (1 unit of XAU_USD is one troy ounce).
    """

    mode: str
    value: float

    MODES = ("risk", "notional", "units")

    def __post_init__(self) -> None:
        if self.mode not in self.MODES:
            raise ValueError(f"sizing mode must be one of {self.MODES}, got {self.mode!r}")
        if self.value <= 0:
            raise ValueError(f"sizing value must be positive, got {self.value}")

    def scaled(self, factor: float) -> Sizing:
        return Sizing(self.mode, self.value * factor)


@dataclass(frozen=True)
class OpenIntent:
    """A strategy's request to open a trade.

    Stops can be absolute prices (``stop_loss``/``take_profit``) or distances from the
    fill price (``sl_distance``/``tp_distance``). Distances stay correct when the order
    fills at a different price than the signal candle closed at.
    """

    direction: int
    stop_loss: float | None = None
    take_profit: float | None = None
    sl_distance: float | None = None
    tp_distance: float | None = None
    size_multiplier: float = 1.0  # scales the bot's configured sizing (DCA safety orders)
    comment: str = ""
    reason: str = ""


@dataclass(frozen=True)
class CloseIntent:
    trade_id: str
    reason: str = ""


Intent = OpenIntent | CloseIntent


@dataclass
class ExecutionReport:
    """What happened when a list of intents was executed."""

    opened: list[Trade] = field(default_factory=list)
    closed: list[ClosedTrade] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
