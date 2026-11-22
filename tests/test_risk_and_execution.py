from datetime import timedelta

import pytest
from helpers import T0, make_config

from forex_bot.brokers import PaperBroker
from forex_bot.config import RiskConfig
from forex_bot.execution import BotRuntime, Executor
from forex_bot.instruments import default_spec
from forex_bot.models import LONG, SHORT, AccountState, CloseIntent, OpenIntent, Quote, Sizing
from forex_bot.risk import RiskManager

EUR = default_spec("EUR_USD")
GOLD = default_spec("XAU_USD")


def test_risk_sizing_modes():
    risk = RiskManager(RiskConfig())
    units = risk.units_for(Sizing("risk", 1.0), price=1.1, stop_distance=0.005,
                           equity=10_000, conversion_rate=1.0, spec=EUR)
    assert units == 20_000  # 100 USD at risk over a 50 pip stop
    ounces = risk.units_for(Sizing("notional", 5_000), price=2_500, stop_distance=None,
                            equity=10_000, conversion_rate=1.0, spec=GOLD)
    assert ounces == 2
    with pytest.raises(ValueError):
        risk.units_for(Sizing("risk", 1.0), price=1.1, stop_distance=None,
                       equity=10_000, conversion_rate=1.0, spec=EUR)


def test_sizing_validation():
    with pytest.raises(ValueError):
        Sizing("percent", 1)
    with pytest.raises(ValueError):
        Sizing("risk", 0)
    assert Sizing("units", 2).scaled(1.5) == Sizing("units", 3)


def test_daily_limit_resets_next_day_and_kill_switch_sticks():
    risk = RiskManager(RiskConfig(daily_loss_limit_pct=3, max_drawdown_pct=10))
    assert risk.update(T0, 10_000) == []
    assert risk.update(T0 + timedelta(hours=2), 9_750) == []
    events = risk.update(T0 + timedelta(hours=3), 9_690)
    assert risk.daily_halt and "DAILY LOSS LIMIT" in events[0]
    risk.update(T0 + timedelta(days=1), 9_690)
    assert not risk.daily_halt
    events = risk.update(T0 + timedelta(days=1, hours=1), 8_990)
    assert risk.kill_switch and any("MAX DRAWDOWN" in e for e in events)
    risk.update(T0 + timedelta(days=5), 9_500)
    assert risk.kill_switch  # only reset-risk clears it
    risk.reset(9_500)
    assert not risk.kill_switch


def test_check_open_limits_and_spread():
    risk = RiskManager(RiskConfig(max_open_trades=3, max_spread_pips={"default": 2.0}))
    tight = Quote("EUR_USD", 1.1000, 1.1001, T0)
    wide = Quote("EUR_USD", 1.1000, 1.1005, T0)
    kwargs = {"open_for_bot": 0, "bot_max": 2, "spec": EUR}
    assert risk.check_open(open_total=0, quote=tight, **kwargs) is None
    assert "spread" in risk.check_open(open_total=0, quote=wide, **kwargs)
    assert "open trades" in risk.check_open(open_total=3, quote=tight, **kwargs)
    assert "bot already" in risk.check_open(
        open_total=1, quote=tight, open_for_bot=2, bot_max=2, spec=EUR
    )


def test_margin_buffer():
    risk = RiskManager(RiskConfig(margin_buffer_pct=50))
    account = AccountState("USD", 10_000, 10_000, 0, 10_000)
    common = {"price": 1.1, "conversion_rate": 1, "spec": EUR, "account": account}
    assert risk.check_margin(50_000, **common) is None
    assert risk.check_margin(200_000, **common)


def setup_executor(sizing=None, **risk_kwargs):
    config = make_config([{
        "id": "eur", "instrument": "EUR_USD", "granularity": "H1", "strategy": "ema_rsi",
        "sizing": sizing or {"mode": "risk", "value": 1.0},
    }])
    broker = PaperBroker(spread_pips={"EUR_USD": 1.0})
    broker.set_mid("EUR_USD", 1.1000, T0)
    risk = RiskManager(RiskConfig(**risk_kwargs))
    return BotRuntime.build(config.bots[0]), broker, Executor(broker, risk), risk


def test_executor_turns_distances_into_stops_and_sizes_by_risk():
    bot, broker, executor, _ = setup_executor()
    report = executor.execute(bot, [OpenIntent(LONG, sl_distance=0.0050, tp_distance=0.0100)])
    assert report.rejected == []
    trade = report.opened[0]
    assert trade.stop_loss == pytest.approx(1.10005 - 0.0050)
    assert trade.take_profit == pytest.approx(1.10005 + 0.0100)
    assert trade.units == 20_000
    assert trade.bot_id == "eur"


def test_executor_closes_before_opening_a_reversal():
    bot, broker, executor, _ = setup_executor(max_open_trades=1)
    first = executor.execute(bot, [OpenIntent(LONG, sl_distance=0.005)]).opened[0]
    report = executor.execute(
        bot, [OpenIntent(SHORT, sl_distance=0.005), CloseIntent(first.id, "reverse")]
    )
    assert [c.id for c in report.closed] == [first.id]
    assert report.opened[0].units < 0


def test_executor_reports_rejections_instead_of_raising():
    bot, broker, executor, risk = setup_executor()
    risk.state["kill_switch"] = True
    report = executor.execute(bot, [OpenIntent(LONG, sl_distance=0.005)])
    assert report.opened == [] and "kill switch" in report.rejected[0]
    risk.state["kill_switch"] = False
    report = executor.execute(bot, [OpenIntent(LONG, stop_loss=1.2)])  # stop above a long
    assert "past the stop loss" in report.rejected[0]
    report = executor.execute(bot, [CloseIntent("nope")])
    assert "not open" in report.rejected[0]


def test_size_multiplier_scales_bot_sizing():
    bot, broker, executor, _ = setup_executor(sizing={"mode": "units", "value": 1000})
    report = executor.execute(bot, [OpenIntent(LONG, size_multiplier=2.5)])
    assert report.opened[0].units == 2500
