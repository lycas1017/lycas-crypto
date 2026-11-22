import os
from pathlib import Path

import pytest
import yaml

from forex_bot.cli import template_text
from forex_bot.config import ConfigError, load_config, load_dotenv, parse_config

BOT = {"id": "g", "instrument": "XAU_USD", "granularity": "H1", "strategy": "ema_rsi",
       "sizing": {"mode": "risk", "value": 1}}


def test_template_is_valid_and_covers_every_strategy():
    raw = yaml.safe_load(template_text("config.example.yaml"))
    config = parse_config(raw, env={})
    assert {bot.strategy for bot in config.bots} == {"ema_rsi", "grid", "dca"}
    assert "XAU_USD" in {bot.instrument for bot in config.bots}
    assert config.resolved_source() == "yahoo"  # no OANDA token in env
    assert not config.live_trading_enabled


def test_env_overrides_and_live_switch():
    env = {"OANDA_API_TOKEN": "tok", "OANDA_ACCOUNT_ID": "101-1", "OANDA_ENVIRONMENT": "LIVE",
           "LIVE_TRADING": "true", "TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
    config = parse_config({"bots": [BOT]}, env=env)
    assert config.oanda.configured and config.oanda.environment == "live"
    assert config.resolved_source() == "oanda"
    assert config.live_trading_enabled and config.telegram.enabled


def test_all_problems_are_reported_together():
    bad = [
        {**BOT, "id": "bad id"},
        {**BOT, "id": "x", "instrument": "GOLD"},
        {**BOT, "id": "y", "granularity": "H2"},
        {**BOT, "id": "z", "params": {"fast": 3, "colour": "red"}},
        {**BOT, "id": "d", "strategy": "dca"},  # risk sizing not allowed for DCA
        {**BOT, "id": "r", "sizing": {"mode": "risk", "value": 9}},
    ]
    with pytest.raises(ConfigError) as info:
        parse_config({"bots": bad, "data": {"source": "bloomberg"}}, env={})
    message = str(info.value)
    for fragment in ("id must be", "instrument must", "granularity must", "colour",
                     "DCA trades", "more than 5%", "data.source"):
        assert fragment in message


def test_wrong_types_become_config_errors():
    with pytest.raises(ConfigError):
        parse_config({"bots": [BOT], "risk": {"max_open_trades": "lots"}}, env={})
    with pytest.raises(ConfigError):
        parse_config({"bots": []}, env={})


def test_paths_resolve_next_to_the_config(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"bots": [BOT]}))
    config = load_config(path, env={})
    assert config.engine.state_file == tmp_path / "state" / "forex_bot_state.json"
    assert config.data.csv_dir == tmp_path / "data"
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml", env={})


def test_dotenv_parsing_does_not_override(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\nFXB_A=1\nexport FXB_B='two words'\nFXB_C=3 # trailing\nFXB_KEEP=new\n"
    )
    monkeypatch.setenv("FXB_KEEP", "old")
    for key in ("FXB_A", "FXB_B", "FXB_C"):
        monkeypatch.delenv(key, raising=False)
    load_dotenv(Path(env_file))
    assert os.environ["FXB_A"] == "1"
    assert os.environ["FXB_B"] == "two words"
    assert os.environ["FXB_C"] == "3"
    assert os.environ["FXB_KEEP"] == "old"
    for key in ("FXB_A", "FXB_B", "FXB_C"):
        monkeypatch.delenv(key)
