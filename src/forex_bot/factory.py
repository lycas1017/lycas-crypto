"""Build data feeds and brokers from the configuration."""

from __future__ import annotations

from forex_bot.brokers.oanda import OandaClient
from forex_bot.config import AppConfig, ConfigError
from forex_bot.feeds import CsvFeed, DataFeed, SyntheticFeed, YahooFeed
from forex_bot.feeds.oanda import OandaFeed


def oanda_client(config: AppConfig) -> OandaClient:
    if not config.oanda.configured:
        raise ConfigError(
            "OANDA is not set up: put OANDA_API_TOKEN and OANDA_ACCOUNT_ID in .env "
            "(a free practice account works; see README)"
        )
    return OandaClient(config.oanda.token, config.oanda.account_id, config.oanda.environment)


def make_feed(config: AppConfig, source: str | None = None) -> DataFeed:
    source = (source or config.resolved_source()).lower()
    if source == "auto":
        source = config.resolved_source()
    if source == "oanda":
        return OandaFeed(oanda_client(config))
    if source == "yahoo":
        return YahooFeed(config.data.yahoo_symbols)
    if source == "csv":
        return CsvFeed(config.data.csv_dir)
    if source == "synthetic":
        return SyntheticFeed()
    raise ConfigError(f"unknown data source {source!r}")
