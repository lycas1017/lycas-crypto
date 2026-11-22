"""Load and validate ``config.yaml`` plus secrets from the environment or a ``.env`` file.

Secrets never belong in ``config.yaml``. Set them in ``.env`` (or real environment
variables): ``OANDA_API_TOKEN``, ``OANDA_ACCOUNT_ID``, ``OANDA_ENVIRONMENT``,
``TELEGRAM_BOT_TOKEN``, ``TELEGRAM_CHAT_ID`` and, only for real-money trading,
``LIVE_TRADING=true``.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from forex_bot.instruments import GRANULARITY_SECONDS, is_valid_instrument
from forex_bot.models import Sizing
from forex_bot.strategies import STRATEGIES, StrategyConfigError, create_strategy

DATA_SOURCES = ("auto", "oanda", "yahoo", "csv", "synthetic")
# Accepts "gold-trend", "eurusd_grid_2". Rejects "gold trend", "", "über-bot".
BOT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,40}$")


class ConfigError(ValueError):
    pass


@dataclass
class BotConfig:
    id: str
    instrument: str
    granularity: str
    strategy: str
    sizing: Sizing
    params: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    max_trades: int | None = None


@dataclass
class RiskConfig:
    max_open_trades: int = 25
    daily_loss_limit_pct: float = 4.0
    max_drawdown_pct: float = 20.0
    close_all_on_max_drawdown: bool = True
    margin_buffer_pct: float = 20.0
    max_spread_pips: dict[str, float] = field(default_factory=dict)


@dataclass
class PaperConfig:
    initial_balance: float = 10_000.0
    spread_pips: dict[str, float] = field(default_factory=dict)
    commission_per_trade: float = 0.0
    slippage_pips: float = 0.0
    conversion_rates: dict[str, float] = field(default_factory=dict)


@dataclass
class DataConfig:
    source: str = "auto"
    csv_dir: Path = Path("data")
    yahoo_symbols: dict[str, str] = field(default_factory=dict)


@dataclass
class OandaConfig:
    token: str = ""
    account_id: str = ""
    environment: str = "practice"

    @property
    def configured(self) -> bool:
        return bool(self.token and self.account_id)


@dataclass
class TelegramConfig:
    token: str = ""
    chat_id: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)


@dataclass
class EngineConfig:
    poll_seconds: float = 30.0
    state_file: Path = Path("state/forex_bot_state.json")
    log_file: Path | None = Path("logs/forex_bot.log")
    log_level: str = "INFO"


@dataclass
class AppConfig:
    bots: list[BotConfig]
    account_currency: str = "USD"
    risk: RiskConfig = field(default_factory=RiskConfig)
    paper: PaperConfig = field(default_factory=PaperConfig)
    data: DataConfig = field(default_factory=DataConfig)
    oanda: OandaConfig = field(default_factory=OandaConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    live_trading_enabled: bool = False
    path: Path | None = None

    def enabled_bots(self, only: list[str] | None = None) -> list[BotConfig]:
        if only:
            unknown = set(only) - {bot.id for bot in self.bots}
            if unknown:
                raise ConfigError(f"no bot with id {', '.join(sorted(unknown))}")
            return [bot for bot in self.bots if bot.id in only]
        return [bot for bot in self.bots if bot.enabled]

    def resolved_source(self) -> str:
        if self.data.source != "auto":
            return self.data.source
        return "oanda" if self.oanda.configured else "yahoo"


def load_dotenv(path: str | Path = ".env") -> None:
    """Read KEY=VALUE lines into ``os.environ`` without overriding variables already set."""
    path = Path(path)
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        os.environ.setdefault(key, value)


def load_config(path: str | Path, env: Mapping[str, str] | None = None) -> AppConfig:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"config file {path} not found. Create one with: forex-bot init")
    if env is None:
        load_dotenv(path.parent / ".env")
        load_dotenv(Path.cwd() / ".env")
        env = os.environ
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return parse_config(raw, env, base_dir=path.parent, path=path)


def parse_config(
    raw: Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    base_dir: Path | None = None,
    path: Path | None = None,
) -> AppConfig:
    try:
        return _parse_config(raw, env, base_dir, path)
    except ConfigError:
        raise
    except (AttributeError, TypeError, ValueError) as exc:
        raise ConfigError(f"invalid value in configuration: {exc}") from exc


def _parse_config(
    raw: Mapping[str, Any],
    env: Mapping[str, str] | None,
    base_dir: Path | None,
    path: Path | None,
) -> AppConfig:
    env = env if env is not None else {}
    base_dir = base_dir or Path.cwd()
    problems: list[str] = []

    def section(name: str) -> dict[str, Any]:
        value = raw.get(name) or {}
        if not isinstance(value, dict):
            problems.append(f"'{name}' must be a mapping")
            return {}
        return dict(value)

    def resolve(value: Any) -> Path:
        candidate = Path(str(value)).expanduser()
        return candidate if candidate.is_absolute() else base_dir / candidate

    currency = str(raw.get("account_currency", "USD")).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        problems.append(f"account_currency must be a 3-letter code like USD, got {currency!r}")

    risk_raw = section("risk")
    risk = RiskConfig(
        max_open_trades=int(risk_raw.get("max_open_trades", 25)),
        daily_loss_limit_pct=float(risk_raw.get("daily_loss_limit_pct", 4.0)),
        max_drawdown_pct=float(risk_raw.get("max_drawdown_pct", 20.0)),
        close_all_on_max_drawdown=bool(risk_raw.get("close_all_on_max_drawdown", True)),
        margin_buffer_pct=float(risk_raw.get("margin_buffer_pct", 20.0)),
        max_spread_pips={k: float(v) for k, v in (risk_raw.get("max_spread_pips") or {}).items()},
    )
    if risk.max_open_trades < 1:
        problems.append("risk.max_open_trades must be at least 1")
    for name in ("daily_loss_limit_pct", "max_drawdown_pct", "margin_buffer_pct"):
        if not 0 <= getattr(risk, name) < 100:
            problems.append(f"risk.{name} must be between 0 (off) and 100")

    paper_raw = section("paper")
    paper = PaperConfig(
        initial_balance=float(paper_raw.get("initial_balance", 10_000)),
        spread_pips={k: float(v) for k, v in (paper_raw.get("spread_pips") or {}).items()},
        commission_per_trade=float(paper_raw.get("commission_per_trade", 0.0)),
        slippage_pips=float(paper_raw.get("slippage_pips", 0.0)),
        conversion_rates={
            k: float(v) for k, v in (paper_raw.get("conversion_rates") or {}).items()
        },
    )
    if paper.initial_balance <= 0:
        problems.append("paper.initial_balance must be positive")

    data_raw = section("data")
    data = DataConfig(
        source=str(data_raw.get("source", "auto")).lower(),
        csv_dir=resolve(data_raw.get("csv_dir", "data")),
        yahoo_symbols={k: str(v) for k, v in (data_raw.get("yahoo_symbols") or {}).items()},
    )
    if data.source not in DATA_SOURCES:
        problems.append(f"data.source must be one of {', '.join(DATA_SOURCES)}")

    oanda_raw = section("oanda")
    oanda = OandaConfig(
        token=env.get("OANDA_API_TOKEN") or str(oanda_raw.get("token", "") or ""),
        account_id=env.get("OANDA_ACCOUNT_ID") or str(oanda_raw.get("account_id", "") or ""),
        environment=(env.get("OANDA_ENVIRONMENT") or str(oanda_raw.get("environment", "practice")))
        .strip()
        .lower(),
    )
    if oanda.environment not in ("practice", "live"):
        problems.append("oanda.environment (or OANDA_ENVIRONMENT) must be practice or live")

    telegram_raw = section("telegram")
    telegram = TelegramConfig(
        token=env.get("TELEGRAM_BOT_TOKEN") or str(telegram_raw.get("token", "") or ""),
        chat_id=env.get("TELEGRAM_CHAT_ID") or str(telegram_raw.get("chat_id", "") or ""),
    )

    engine_raw = section("engine")
    log_file = engine_raw.get("log_file", "logs/forex_bot.log")
    engine = EngineConfig(
        poll_seconds=float(engine_raw.get("poll_seconds", 30)),
        state_file=resolve(engine_raw.get("state_file", "state/forex_bot_state.json")),
        log_file=resolve(log_file) if log_file else None,
        log_level=str(engine_raw.get("log_level", "INFO")).upper(),
    )
    if engine.poll_seconds < 5:
        problems.append("engine.poll_seconds must be at least 5 (be kind to the APIs)")

    bots = _parse_bots(raw.get("bots"), problems)
    if problems:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))
    return AppConfig(
        bots=bots,
        account_currency=currency,
        risk=risk,
        paper=paper,
        data=data,
        oanda=oanda,
        telegram=telegram,
        engine=engine,
        live_trading_enabled=str(env.get("LIVE_TRADING", "")).strip().lower()
        in ("1", "true", "yes"),
        path=path,
    )


def _parse_bots(raw: Any, problems: list[str]) -> list[BotConfig]:
    if not isinstance(raw, list) or not raw:
        problems.append("'bots' must be a non-empty list")
        return []
    bots: list[BotConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        where = f"bots[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where} must be a mapping")
            continue
        bot_id = str(item.get("id", ""))
        where = f"bot '{bot_id}'" if bot_id else where
        if not BOT_ID_PATTERN.match(bot_id):
            problems.append(f"{where}: id must be 1-40 letters, digits, '-' or '_'")
        elif bot_id in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(bot_id)
        instrument = str(item.get("instrument", "")).upper()
        if not is_valid_instrument(instrument):
            problems.append(f"{where}: instrument must look like XAU_USD or EUR_USD")
        granularity = str(item.get("granularity", "H1")).upper()
        if granularity not in GRANULARITY_SECONDS:
            problems.append(
                f"{where}: granularity must be one of {', '.join(GRANULARITY_SECONDS)}"
            )
        strategy = str(item.get("strategy", ""))
        params = item.get("params") or {}
        if strategy not in STRATEGIES:
            problems.append(f"{where}: strategy must be one of {', '.join(STRATEGIES)}")
        elif not isinstance(params, dict):
            problems.append(f"{where}: params must be a mapping")
        else:
            try:
                create_strategy(strategy, params)
            except StrategyConfigError as exc:
                problems.append(f"{where}: {exc}")
        sizing_raw = item.get("sizing") or {}
        try:
            sizing = Sizing(str(sizing_raw.get("mode", "")), float(sizing_raw.get("value", 0)))
        except (AttributeError, TypeError, ValueError) as exc:
            problems.append(f"{where}: sizing needs mode (risk/notional/units) and value: {exc}")
            continue
        if strategy == "dca" and sizing.mode == "risk":
            problems.append(
                f"{where}: DCA trades have no per-trade stop, so use notional or units sizing"
            )
        if sizing.mode == "risk" and sizing.value > 5:
            problems.append(f"{where}: risking more than 5% per trade is not allowed")
        max_trades = item.get("max_trades")
        bots.append(
            BotConfig(
                id=bot_id,
                instrument=instrument,
                granularity=granularity,
                strategy=strategy,
                sizing=sizing,
                params=dict(params) if isinstance(params, dict) else {},
                enabled=bool(item.get("enabled", True)),
                max_trades=int(max_trades) if max_trades is not None else None,
            )
        )
    return bots
