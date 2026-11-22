from collections.abc import Mapping
from typing import Any

from forex_bot.strategies.base import Strategy, StrategyConfigError, StrategyContext
from forex_bot.strategies.dca import DcaStrategy
from forex_bot.strategies.ema_rsi import EmaRsiStrategy
from forex_bot.strategies.grid import GridStrategy

STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls for cls in (EmaRsiStrategy, GridStrategy, DcaStrategy)
}


def create_strategy(name: str, params: Mapping[str, Any] | None = None) -> Strategy:
    try:
        cls = STRATEGIES[name]
    except KeyError as exc:
        raise StrategyConfigError(
            f"unknown strategy {name!r}; choose from {', '.join(STRATEGIES)}"
        ) from exc
    return cls(params)


__all__ = [
    "STRATEGIES",
    "DcaStrategy",
    "EmaRsiStrategy",
    "GridStrategy",
    "Strategy",
    "StrategyConfigError",
    "StrategyContext",
    "create_strategy",
]
