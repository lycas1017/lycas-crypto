"""Performance statistics for a list of closed trades and an equity curve."""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from forex_bot.models import ClosedTrade


def max_drawdown(curve: Sequence[tuple[datetime, float]]) -> tuple[float, float]:
    """Largest peak-to-trough fall as (percent, amount)."""
    peak = -math.inf
    worst_pct = 0.0
    worst_amount = 0.0
    for _, equity in curve:
        peak = max(peak, equity)
        if peak > 0:
            worst_pct = max(worst_pct, (peak - equity) / peak * 100)
            worst_amount = max(worst_amount, peak - equity)
    return worst_pct, worst_amount


def daily_returns(curve: Sequence[tuple[datetime, float]]) -> list[float]:
    closes: dict[str, float] = {}
    for when, equity in curve:
        closes[when.date().isoformat()] = equity
    values = list(closes.values())
    return [b / a - 1 for a, b in zip(values, values[1:], strict=False) if a > 0]


def trade_stats(trades: Sequence[ClosedTrade]) -> dict[str, Any]:
    pls = [t.realized_pl for t in trades]
    wins = [pl for pl in pls if pl > 0]
    losses = [pl for pl in pls if pl <= 0]
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trades": len(pls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": len(wins) / len(pls) * 100 if pls else 0.0,
        "net_pl": sum(pls),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "avg_win": statistics.fmean(wins) if wins else 0.0,
        "avg_loss": statistics.fmean(losses) if losses else 0.0,
        "expectancy": statistics.fmean(pls) if pls else 0.0,
        "best_trade": max(pls) if pls else 0.0,
        "worst_trade": min(pls) if pls else 0.0,
    }


def compute_metrics(
    initial_balance: float,
    trades: Sequence[ClosedTrade],
    curve: Sequence[tuple[datetime, float]],
) -> dict[str, Any]:
    final = curve[-1][1] if curve else initial_balance
    dd_pct, dd_amount = max_drawdown(curve)
    returns = daily_returns(curve)
    sharpe = None
    if len(returns) >= 2:
        spread = statistics.pstdev(returns)
        if spread > 0:
            sharpe = statistics.fmean(returns) / spread * math.sqrt(252)
    days = (curve[-1][0] - curve[0][0]).total_seconds() / 86400 if len(curve) > 1 else 0.0
    annualized = None
    if days >= 30 and final > 0:
        annualized = ((final / initial_balance) ** (365 / days) - 1) * 100

    per_bot: dict[str, list[ClosedTrade]] = defaultdict(list)
    for trade in trades:
        per_bot[trade.bot_id].append(trade)

    return {
        "initial_balance": initial_balance,
        "final_equity": final,
        "net_profit": final - initial_balance,
        "return_pct": (final / initial_balance - 1) * 100,
        "annualized_return_pct": annualized,
        "max_drawdown_pct": dd_pct,
        "max_drawdown": dd_amount,
        "sharpe": sharpe,
        "days": days,
        **trade_stats(trades),
        "exit_reasons": dict(Counter(t.reason for t in trades)),
        "per_bot": {bot: trade_stats(items) for bot, items in sorted(per_bot.items())},
    }
