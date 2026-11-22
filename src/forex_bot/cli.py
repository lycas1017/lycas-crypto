"""Command line: ``forex-bot <command>``. Run ``forex-bot --help`` for the list."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import shutil
import sys
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from importlib import resources
from pathlib import Path

import yaml

from forex_bot import __version__
from forex_bot.backtest import make_broker, run_backtest
from forex_bot.brokers.base import BrokerError
from forex_bot.config import AppConfig, ConfigError, load_config, parse_config
from forex_bot.factory import make_feed, oanda_client
from forex_bot.feeds import DataError, SyntheticFeed, granularity_seconds, write_csv
from forex_bot.models import Candle, parse_time, utcnow
from forex_bot.notifier import make_notifier
from forex_bot.report import summary_text, write_report
from forex_bot.state import StateStore, state_path

log = logging.getLogger("forex_bot")

# Default history per timeframe. Intraday limits follow what Yahoo keeps (about 60 days
# of M5 to M30); OANDA has more, so pass --start for longer M15 backtests there.
DEFAULT_BACKTEST_DAYS = {"M1": 5, "M5": 30, "M15": 55, "M30": 55, "H1": 365, "H4": 365, "D": 730}
SOURCES = ("auto", "oanda", "yahoo", "csv", "synthetic")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args) or 0)
    except (ConfigError, DataError, BrokerError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forex-bot",
        description="Gold and forex trading bot: backtest, paper trade, trade live on OANDA.",
    )
    parser.add_argument("--version", action="version", version=f"forex-bot {__version__}")
    sub = parser.add_subparsers(metavar="command")

    def add(name: str, func, help_text: str, config: bool = True) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=help_text, description=help_text)
        command.set_defaults(func=func)
        if config:
            command.add_argument("-c", "--config", default="config.yaml", help="config file")
        command.add_argument("-v", "--verbose", action="store_true", help="debug logging")
        return command

    cmd = add("init", cmd_init, "create config.yaml and .env from the templates", config=False)
    cmd.add_argument("directory", nargs="?", default=".", help="where to create them")
    cmd.add_argument("--force", action="store_true", help="overwrite existing files")

    cmd = add("demo", cmd_demo, "offline backtest of all strategies on synthetic data",
              config=False)
    cmd.add_argument("--days", type=int, default=180)
    cmd.add_argument("--out", default="reports", help="report directory")

    cmd = add("backtest", cmd_backtest, "replay history through the enabled bots")
    cmd.add_argument("--bot", action="append", help="only this bot id (repeatable)")
    cmd.add_argument("--start", help="first day, YYYY-MM-DD (default depends on timeframe)")
    cmd.add_argument("--end", help="last day, YYYY-MM-DD (default: now)")
    cmd.add_argument("--source", choices=SOURCES, help="data source (default: config)")
    cmd.add_argument("--balance", type=float, help="starting balance (default: config)")
    cmd.add_argument("--out", default="reports", help="report directory")
    cmd.add_argument("--no-html", action="store_true", help="skip report.html")

    cmd = add("fetch", cmd_fetch, "download candles to a CSV file for offline backtests")
    cmd.add_argument("instrument", help="e.g. XAU_USD")
    cmd.add_argument("granularity", help="M1 M5 M15 M30 H1 H4 D")
    cmd.add_argument("--start", help="YYYY-MM-DD")
    cmd.add_argument("--end", help="YYYY-MM-DD")
    cmd.add_argument("--source", choices=("auto", "oanda", "yahoo"), default="auto")
    cmd.add_argument("--out", help="output file (default: <csv_dir>/<INSTRUMENT>_<TF>.csv)")

    cmd = add("paper", cmd_paper, "paper trade with live prices and a simulated account")
    cmd.add_argument("--reset", action="store_true", help="start a fresh paper account")
    cmd.add_argument("--once", action="store_true", help="run one cycle and exit")

    cmd = add("live", cmd_live, "trade on OANDA (needs LIVE_TRADING=true)")
    cmd.add_argument("--yes", action="store_true", help="skip the real-money confirmation")
    cmd.add_argument("--once", action="store_true", help="run one cycle and exit")

    cmd = add("check", cmd_check, "validate the config and test data, OANDA and Telegram")
    cmd.add_argument("--telegram", action="store_true", help="send a Telegram test message")

    cmd = add("status", cmd_status, "show the saved paper or live account state")
    cmd.add_argument("--mode", choices=("paper", "live"), default="paper")

    cmd = add("reset-risk", cmd_reset_risk, "clear the drawdown kill switch and daily halt")
    cmd.add_argument("--mode", choices=("paper", "live"), default="paper")
    return parser


# Setup ---------------------------------------------------------------------------------


def setup_logging(verbose: bool, config: AppConfig | None = None, to_file: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    if config and not verbose:
        level = getattr(logging, config.engine.log_level, logging.INFO)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
    root.addHandler(console)
    if to_file and config and config.engine.log_file:
        config.engine.log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            config.engine.log_file, maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s %(message)s")
        )
        root.addHandler(handler)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def template_text(name: str) -> str:
    return resources.files("forex_bot").joinpath("templates", name).read_text(encoding="utf-8")


def _day(text: str | None) -> datetime | None:
    return parse_time(text) if text else None


# Commands ------------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.directory)
    target.mkdir(parents=True, exist_ok=True)
    for template, name in (("config.example.yaml", "config.yaml"), ("env.example", ".env")):
        path = target / name
        if path.exists() and not args.force:
            print(f"kept existing {path}")
            continue
        path.write_text(template_text(template), encoding="utf-8")
        print(f"created {path}")
    print("\nNext: forex-bot demo        (offline test run)")
    print("      forex-bot backtest    (real history from Yahoo Finance or OANDA)")
    print("      forex-bot paper       (paper trading with live prices)")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    setup_logging(args.verbose)
    raw = yaml.safe_load(template_text("config.example.yaml"))
    for bot in raw["bots"]:
        bot["enabled"] = True  # show every strategy in the demo
    config = parse_config(raw, env={})
    end = utcnow().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    feed = SyntheticFeed()
    data = {
        bot.id: feed.history(bot.instrument, bot.granularity, start, end) for bot in config.bots
    }
    print(f"Running {len(config.bots)} bots on {args.days} days of SYNTHETIC data...\n")
    result = run_backtest(config, data, config.bots)
    print(summary_text(result))
    out = write_report(result, Path(args.out) / f"demo-{utcnow():%Y%m%d-%H%M%S}")
    print(f"\nReport: {out / 'report.html'}")
    print("Synthetic prices only show that everything works. "
          "Use 'forex-bot backtest' for real data.")
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(args.verbose, config)
    bots = config.enabled_bots(args.bot)
    if not bots:
        raise ConfigError("no enabled bots; set enabled: true or pass --bot ID")
    feed = make_feed(config, args.source)
    end = _day(args.end) or utcnow()
    cache: dict[tuple[str, str], list[Candle]] = {}
    data: dict[str, list[Candle]] = {}
    for bot in bots:
        start = _day(args.start) or end - timedelta(days=DEFAULT_BACKTEST_DAYS[bot.granularity])
        key = (bot.instrument, bot.granularity)
        if key not in cache:
            print(f"Loading {bot.instrument} {bot.granularity} from {feed.name} "
                  f"({start:%Y-%m-%d} to {end:%Y-%m-%d})...")
            cache[key] = feed.history(bot.instrument, bot.granularity, start, end)
        data[bot.id] = [c for c in cache[key] if c.time >= start]
        if not data[bot.id]:
            raise DataError(f"no {bot.instrument} {bot.granularity} candles for that period")
    print()
    result = run_backtest(config, data, bots, args.balance)
    print(summary_text(result))
    out = write_report(result, Path(args.out) / f"backtest-{utcnow():%Y%m%d-%H%M%S}",
                       html_report=not args.no_html)
    print(f"\nFiles: {out}")
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(args.verbose, config)
    instrument = args.instrument.upper()
    granularity = args.granularity.upper()
    seconds = granularity_seconds(granularity)
    end = _day(args.end) or utcnow()
    default_days = DEFAULT_BACKTEST_DAYS.get(granularity, 365)
    start = _day(args.start) or end - timedelta(days=default_days)
    feed = make_feed(config, args.source)
    candles = feed.history(instrument, granularity, start, end)
    if not candles:
        raise DataError(f"{feed.name} returned no candles")
    out = Path(args.out) if args.out else config.data.csv_dir / f"{instrument}_{granularity}.csv"
    count = write_csv(out, candles)
    print(f"Saved {count} {granularity} candles ({seconds // 60} min) of {instrument} "
          f"from {feed.name} to {out}")
    return 0


def _run_engine(args: argparse.Namespace, config: AppConfig, mode: str, broker, feed) -> int:
    from forex_bot.engine import TradingEngine

    bots = config.enabled_bots()
    if not bots:
        raise ConfigError("no enabled bots in the config")
    store = StateStore(state_path(config.engine.state_file, mode))
    prefix = "[PAPER] " if mode == "paper" else ""
    engine = TradingEngine(
        config, bots, broker, feed, store, make_notifier(config.telegram, prefix), mode
    )
    log.info("%s mode | data: %s | state: %s", mode.upper(), feed.name, store.path)
    for bot in engine.bots:
        log.info("  %s: %s %s %s, sizing %s %g", bot.id, bot.instrument, bot.granularity,
                 bot.strategy.describe(), bot.config.sizing.mode, bot.config.sizing.value)
    if args.once:
        for warning in engine.startup_checks():
            log.warning(warning)
        engine.tick()
        account = broker.account()
        log.info("one cycle done: equity %.2f %s, %d open trade(s)",
                 account.equity, account.currency, account.open_trade_count)
        return 0
    engine.run()
    return 0


def cmd_paper(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(args.verbose, config, to_file=True)
    path = state_path(config.engine.state_file, "paper")
    if args.reset and path.exists():
        backup = path.with_name(f"{path.name}.{utcnow():%Y%m%d%H%M%S}.bak")
        shutil.move(path, backup)
        print(f"Old paper account saved as {backup}")
    feed = make_feed(config)
    if not feed.live:
        raise ConfigError(f"paper trading needs live prices: set data.source to auto, oanda or "
                          f"yahoo (it is {config.data.source})")
    return _run_engine(args, config, "paper", make_broker(config), feed)


def cmd_live(args: argparse.Namespace) -> int:
    from forex_bot.brokers.oanda import OandaBroker
    from forex_bot.feeds.oanda import OandaFeed

    config = load_config(args.config)
    setup_logging(args.verbose, config, to_file=True)
    if not config.live_trading_enabled:
        raise ConfigError(
            "live trading is switched off. Backtest and paper trade first, then set "
            "LIVE_TRADING=true in .env to allow real orders."
        )
    client = oanda_client(config)
    broker = OandaBroker(client)
    if broker.currency != config.account_currency:
        raise ConfigError(
            f"OANDA account is in {broker.currency} but account_currency is "
            f"{config.account_currency}; make them match"
        )
    broker.load_specs(sorted({bot.instrument for bot in config.enabled_bots()}))
    account = broker.account()
    real_money = config.oanda.environment == "live"
    print(f"\nOANDA {'LIVE (REAL MONEY)' if real_money else 'practice (demo money)'} account "
          f"{config.oanda.account_id}: balance {account.balance:,.2f} {account.currency}, "
          f"{account.open_trade_count} open trade(s)")
    if real_money and not args.yes:
        if not sys.stdin.isatty():
            raise ConfigError("real-money trading needs --yes when not run from a terminal")
        answer = input("This will place REAL orders with REAL money. Type YES to continue: ")
        if answer.strip() != "YES":
            print("cancelled")
            return 1
    if real_money and not args.once:
        print("Starting in 10 seconds. Press Ctrl+C to cancel.")
        time.sleep(10)
    return _run_engine(args, config, "live", broker, OandaFeed(client))


def cmd_check(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(args.verbose, config)
    print(f"Config OK: {config.path}")
    print(f"Account currency {config.account_currency}, data source {config.resolved_source()}")
    for bot in config.bots:
        state = "on " if bot.enabled else "off"
        print(f"  [{state}] {bot.id}: {bot.instrument} {bot.granularity} {bot.strategy} "
              f"(sizing {bot.sizing.mode} {bot.sizing.value:g})")
    ok = True
    feed = make_feed(config)
    for instrument, granularity in sorted({(b.instrument, b.granularity) for b in config.bots}):
        try:
            candles = feed.recent(instrument, granularity, 5)
            last = candles[-1] if candles else None
            print(f"  data {instrument} {granularity}: "
                  + (f"last close {last.close} at {last.time:%Y-%m-%d %H:%M} UTC" if last
                     else "NO DATA"))
            ok = ok and bool(last)
        except (DataError, BrokerError) as exc:
            print(f"  data {instrument} {granularity}: FAILED {exc}")
            ok = False
    if config.oanda.configured:
        try:
            summary = oanda_client(config).account_summary()
            print(f"  OANDA {config.oanda.environment}: account {summary['id']}, "
                  f"balance {float(summary['balance']):,.2f} {summary['currency']}, "
                  f"hedging {'on' if summary.get('hedgingEnabled') else 'off'}")
        except BrokerError as exc:
            print(f"  OANDA: FAILED {exc}")
            ok = False
    else:
        print("  OANDA: not set up (fine for backtests and paper trading with Yahoo data)")
    print(f"  Telegram: {'set up' if config.telegram.enabled else 'not set up (optional)'}")
    print(f"  Live trading switch: {'ON' if config.live_trading_enabled else 'off'}")
    if args.telegram:
        make_notifier(config.telegram).send("forex-bot test message: notifications work.")
        print("  Telegram test message sent")
    print("All checks passed." if ok else "Some checks failed.")
    return 0 if ok else 1


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    path = state_path(config.engine.state_file, args.mode)
    if not path.exists():
        print(f"No {args.mode} state yet ({path}). Start the bot with: forex-bot {args.mode}")
        return 0
    state = StateStore(path).load()
    print(f"{args.mode.capitalize()} state, last saved {state.get('updated', '?')}")
    risk = state.get("risk", {})
    kill = f"ON: {risk.get('kill_reason')}" if risk.get("kill_switch") else "off"
    print(f"  peak equity {float(risk.get('peak_equity', 0)):,.2f}, kill switch {kill}, "
          f"daily halt {'ON' if risk.get('daily_halt') else 'off'}")
    paper = state.get("paper")
    if args.mode == "paper" and paper:
        broker = make_broker(config)
        broker.restore(paper)
        trades = paper.get("trades", [])
        closed = paper.get("closed", [])
        realized = sum(float(c["realized_pl"]) for c in closed)
        print(f"  balance {broker.balance:,.2f} {broker.currency} | realized P/L {realized:+,.2f} "
              f"over {len(closed)} closed trade(s)")
        for trade in trades:
            side = "BUY " if trade["units"] > 0 else "SELL"
            print(f"  open {trade['id']:>6} {trade['bot_id']:<16} {side} {abs(trade['units']):g} "
                  f"{trade['instrument']} @ {trade['entry_price']} SL {trade['stop_loss']} "
                  f"TP {trade['take_profit']}")
    for bot_id, bot_state in state.get("bots", {}).items():
        print(f"  bot {bot_id}: last candle {bot_state.get('last_candle', 'none yet')}")
    return 0


def cmd_reset_risk(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    store = StateStore(state_path(config.engine.state_file, args.mode))
    state = store.load()
    risk = state.setdefault("risk", {})
    risk.update({"kill_switch": False, "kill_reason": "", "daily_halt": False})
    risk.pop("peak_equity", None)  # re-measure drawdown from the current equity
    store.save(state)
    print(f"Risk switches cleared for {args.mode} mode. Drawdown is measured from now on.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
