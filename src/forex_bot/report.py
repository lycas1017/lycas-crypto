"""Backtest output: a console summary, CSV/JSON files and a self-contained HTML report."""

from __future__ import annotations

import csv
import html
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from forex_bot.backtest import BacktestResult
from forex_bot.models import format_time

MAX_CHART_POINTS = 1200
MAX_TRADE_ROWS = 500


def _money(value: float | None, currency: str = "") -> str:
    if value is None:
        return "n/a"
    text = f"{value:,.2f}"
    return f"{text} {currency}".strip()


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def _num(value: float | None, digits: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def summary_text(result: BacktestResult, metrics: dict[str, Any] | None = None) -> str:
    m = metrics or result.metrics()
    cur = result.currency
    start = result.start.strftime("%Y-%m-%d") if result.start else "?"
    end = result.end.strftime("%Y-%m-%d") if result.end else "?"
    lines = [
        f"Backtest {start} to {end} ({m['days']:.0f} days)",
        f"  Starting balance   {_money(m['initial_balance'], cur)}",
        f"  Final equity       {_money(m['final_equity'], cur)}",
        f"  Net profit         {_money(m['net_profit'], cur)} ({_pct(m['return_pct'])})",
        f"  Annualized return  {_pct(m['annualized_return_pct'])}",
        f"  Max drawdown       {_pct(m['max_drawdown_pct'])} ({_money(m['max_drawdown'], cur)})",
        f"  Sharpe (daily)     {_num(m['sharpe'])}",
        f"  Trades             {m['trades']} ({m['wins']} won, {m['losses']} lost, "
        f"win rate {_pct(m['win_rate_pct'])})",
        f"  Profit factor      {_num(m['profit_factor'])}",
        f"  Avg win / loss     {_money(m['avg_win'])} / {_money(m['avg_loss'])}",
        "",
        f"  {'Bot':<22}{'Trades':>8}{'Win rate':>10}{'Net P/L':>16}{'PF':>7}",
    ]
    for bot in result.bots:
        stats = m["per_bot"].get(bot.id)
        if not stats:
            lines.append(f"  {bot.id:<22}{0:>8}{'-':>10}{'0.00':>16}{'-':>7}")
            continue
        lines.append(
            f"  {bot.id:<22}{stats['trades']:>8}{_pct(stats['win_rate_pct']):>10}"
            f"{_money(stats['net_pl']):>16}{_num(stats['profit_factor']):>7}"
        )
    if m["exit_reasons"]:
        reasons = ", ".join(f"{k} {v}" for k, v in sorted(m["exit_reasons"].items()))
        lines += ["", f"  Exits: {reasons}"]
    if result.rejections:
        lines += ["", "  Orders the risk rules or broker skipped:"]
        for reason, count in result.rejections.most_common(6):
            lines.append(f"    {count:>5} x {reason}")
    for when, event in result.risk_events:
        lines.append(f"  ! {when:%Y-%m-%d %H:%M} {event}")
    return "\n".join(lines)


def write_report(result: BacktestResult, directory: str | Path, html_report: bool = True) -> Path:
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)
    metrics = result.metrics()

    with open(out / "trades.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["id", "bot", "instrument", "units", "entry", "exit", "opened", "closed",
             "pl", "reason", "comment"]
        )
        for t in result.trades:
            writer.writerow(
                [t.id, t.bot_id, t.instrument, t.units, t.entry_price, t.exit_price,
                 format_time(t.open_time), format_time(t.close_time),
                 round(t.realized_pl, 2), t.reason, t.comment]
            )
    with open(out / "equity.csv", "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time", "equity"])
        for when, equity in result.equity_curve:
            writer.writerow([format_time(when), round(equity, 2)])
    summary = {
        "currency": result.currency,
        "start": format_time(result.start) if result.start else None,
        "end": format_time(result.end) if result.end else None,
        "bots": [
            {"id": b.id, "instrument": b.instrument, "granularity": b.granularity,
             "strategy": b.strategy, "params": b.params,
             "sizing": {"mode": b.sizing.mode, "value": b.sizing.value}}
            for b in result.bots
        ],
        "metrics": metrics,
        "rejections": dict(result.rejections),
        "risk_events": [[format_time(w), e] for w, e in result.risk_events],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=str), "utf-8")
    if html_report:
        (out / "report.html").write_text(render_html(result, metrics), "utf-8")
    return out


def _downsample(curve: Sequence[tuple[datetime, float]]) -> list[tuple[datetime, float]]:
    if len(curve) <= MAX_CHART_POINTS:
        return list(curve)
    step = len(curve) / MAX_CHART_POINTS
    picked = [curve[int(i * step)] for i in range(MAX_CHART_POINTS)]
    picked.append(curve[-1])
    return picked


def _chart(result: BacktestResult) -> str:
    points = _downsample(result.equity_curve)
    if len(points) < 2:
        return "<p class='muted'>Not enough data for a chart.</p>"
    width, height, pad = 960, 300, 8
    values = [v for _, v in points]
    low = min(min(values), result.initial_balance)
    high = max(max(values), result.initial_balance)
    span = (high - low) or 1.0
    first = points[0][0].timestamp()
    total = (points[-1][0].timestamp() - first) or 1.0

    def xy(when: datetime, value: float) -> tuple[float, float]:
        x = pad + (when.timestamp() - first) / total * (width - 2 * pad)
        y = pad + (high - value) / span * (height - 2 * pad)
        return x, y

    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(w, v) for w, v in points))
    _, base_y = xy(points[0][0], result.initial_balance)
    first_x, _ = xy(points[0][0], values[0])
    last_x, _ = xy(points[-1][0], values[-1])
    area = f"{first_x:.1f},{height - pad} {line} {last_x:.1f},{height - pad}"
    return (
        f"<svg viewBox='0 0 {width} {height}' role='img' "
        f"aria-label='Equity from {_money(values[0])} to {_money(values[-1])}'>"
        f"<polygon class='area' points='{area}'/>"
        f"<line class='base' x1='{pad}' x2='{width - pad}' y1='{base_y:.1f}' y2='{base_y:.1f}'/>"
        f"<polyline class='line' points='{line}'/></svg>"
        f"<div class='axis'><span>{points[0][0]:%Y-%m-%d}</span>"
        f"<span>high {_money(high)} / low {_money(low)}</span>"
        f"<span>{points[-1][0]:%Y-%m-%d}</span></div>"
    )


def render_html(result: BacktestResult, metrics: dict[str, Any]) -> str:
    m = metrics
    cur = result.currency
    esc = html.escape
    tone = "pos" if m["net_profit"] >= 0 else "neg"
    cards = [
        ("Net profit", f"{_money(m['net_profit'], cur)}", tone),
        ("Return", _pct(m["return_pct"]), tone),
        ("Max drawdown", _pct(m["max_drawdown_pct"]), "neg" if m["max_drawdown_pct"] else ""),
        ("Trades", str(m["trades"]), ""),
        ("Win rate", _pct(m["win_rate_pct"]), ""),
        ("Profit factor", _num(m["profit_factor"]), ""),
        ("Sharpe (daily)", _num(m["sharpe"]), ""),
        ("Final equity", _money(m["final_equity"], cur), ""),
    ]
    card_html = "".join(
        f"<div class='card'><div class='label'>{esc(k)}</div>"
        f"<div class='value {cls}'>{esc(v)}</div></div>"
        for k, v, cls in cards
    )
    bot_rows = ""
    for bot in result.bots:
        s = m["per_bot"].get(bot.id) or {"trades": 0, "win_rate_pct": 0.0, "net_pl": 0.0,
                                         "profit_factor": None}
        cls = "pos" if s["net_pl"] >= 0 else "neg"
        bot_rows += (
            f"<tr><td>{esc(bot.id)}</td><td>{esc(bot.instrument)}</td>"
            f"<td>{esc(bot.granularity)}</td><td>{esc(bot.strategy)}</td>"
            f"<td class='num'>{s['trades']}</td><td class='num'>{_pct(s['win_rate_pct'])}</td>"
            f"<td class='num {cls}'>{_money(s['net_pl'])}</td>"
            f"<td class='num'>{_num(s['profit_factor'])}</td></tr>"
        )
    trade_rows = ""
    for t in result.trades[-MAX_TRADE_ROWS:][::-1]:
        cls = "pos" if t.realized_pl >= 0 else "neg"
        side = "Buy" if t.units > 0 else "Sell"
        trade_rows += (
            f"<tr><td>{t.close_time:%Y-%m-%d %H:%M}</td><td>{esc(t.bot_id)}</td>"
            f"<td>{side} {abs(t.units):g} {esc(t.instrument)}</td>"
            f"<td class='num'>{t.entry_price:.5g}</td><td class='num'>{t.exit_price:.5g}</td>"
            f"<td class='num {cls}'>{_money(t.realized_pl)}</td><td>{esc(t.reason)}</td></tr>"
        )
    period = (
        f"{result.start:%Y-%m-%d} to {result.end:%Y-%m-%d}" if result.start and result.end else ""
    )
    shown = min(len(result.trades), MAX_TRADE_ROWS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Backtest report</title>
<style>
:root {{ --bg:#f6f7f9; --panel:#fff; --fg:#14171c; --muted:#5d6673; --line:#e2e5ea;
  --pos:#137a4b; --neg:#b42318; --accent:#b8860b; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --panel:#171a20; --fg:#e8eaee;
  --muted:#9aa3af; --line:#2a2f38; --pos:#4cc38a; --neg:#ff7b72; --accent:#e3b341; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width:1040px; margin:0 auto; padding:24px 16px 48px; }}
h1 {{ font-size:1.5rem; margin:0 0 4px; }} h2 {{ font-size:1.05rem; margin:32px 0 12px; }}
.muted {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:12px;
  margin-top:20px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px 14px; }}
.label {{ color:var(--muted); font-size:.8rem; }} .value {{ font-size:1.2rem; font-weight:600;
  font-variant-numeric:tabular-nums; }}
.pos {{ color:var(--pos); }} .neg {{ color:var(--neg); }}
.panel {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:12px;
  overflow-x:auto; }}
svg {{ width:100%; height:auto; display:block; }}
.line {{ fill:none; stroke:var(--accent); stroke-width:2; vector-effect:non-scaling-stroke; }}
.area {{ fill:var(--accent); opacity:.12; }}
.base {{ stroke:var(--muted); stroke-dasharray:4 4; stroke-width:1; vector-effect:non-scaling-stroke; }}
.axis {{ display:flex; justify-content:space-between; gap:8px; color:var(--muted); font-size:.8rem;
  margin-top:6px; flex-wrap:wrap; }}
table {{ width:100%; border-collapse:collapse; font-size:.9rem; }}
th, td {{ text-align:left; padding:7px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }}
th {{ color:var(--muted); font-weight:600; }} .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
.note {{ margin-top:28px; font-size:.85rem; color:var(--muted); }}
</style></head>
<body><main>
<h1>Backtest report</h1>
<div class="muted">{esc(period)} · {len(result.bots)} bot(s) · starting balance
{_money(result.initial_balance, cur)}</div>
<div class="cards">{card_html}</div>
<h2>Equity</h2><div class="panel">{_chart(result)}</div>
<h2>Bots</h2><div class="panel"><table>
<thead><tr><th>Bot</th><th>Instrument</th><th>Timeframe</th><th>Strategy</th><th class="num">Trades</th>
<th class="num">Win rate</th><th class="num">Net P/L</th><th class="num">PF</th></tr></thead>
<tbody>{bot_rows}</tbody></table></div>
<h2>Trades <span class="muted">(latest {shown} of {len(result.trades)})</span></h2>
<div class="panel"><table>
<thead><tr><th>Closed</th><th>Bot</th><th>Trade</th><th class="num">Entry</th><th class="num">Exit</th>
<th class="num">P/L</th><th>Exit reason</th></tr></thead>
<tbody>{trade_rows}</tbody></table></div>
<p class="note">Simulated with modelled spreads and no overnight financing. Past results,
especially on synthetic data, do not predict future returns.</p>
</main></body></html>
"""
