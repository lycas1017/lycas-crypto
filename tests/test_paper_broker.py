import pytest
from helpers import T0

from forex_bot.brokers import OrderRejected, PaperBroker
from forex_bot.models import Candle


def broker(**kwargs) -> PaperBroker:
    paper = PaperBroker(spread_pips={"EUR_USD": 2.0, "XAU_USD": 40.0}, **kwargs)
    paper.set_mid("EUR_USD", 1.1000, T0)
    paper.set_mid("XAU_USD", 2000.0, T0)
    return paper


def test_long_fills_at_ask_and_profits_in_usd():
    paper = broker()
    trade = paper.open_trade("bot", "EUR_USD", 10_000, 1.09, 1.12)
    assert trade.entry_price == pytest.approx(1.1001)  # mid + 1 pip
    paper.set_mid("EUR_USD", 1.1050, T0)
    closed = paper.close_trade(trade.id)
    assert closed.exit_price == pytest.approx(1.1049)  # bid
    assert closed.realized_pl == pytest.approx(48.0)
    assert paper.balance == pytest.approx(10_048.0)


def test_short_gold_loses_when_price_rises():
    paper = broker()
    trade = paper.open_trade("bot", "XAU_USD", -2, 2050.0, 1900.0)
    assert trade.entry_price == pytest.approx(1999.8)
    paper.set_mid("XAU_USD", 2010.0, T0)
    assert paper.account().equity == pytest.approx(10_000 + (1999.8 - 2010.2) * 2)


def test_usd_jpy_pl_is_converted():
    paper = PaperBroker(spread_pips={"USD_JPY": 0.0})
    paper.set_mid("USD_JPY", 150.0, T0)
    trade = paper.open_trade("bot", "USD_JPY", 1000, None, None)
    paper.set_mid("USD_JPY", 151.5, T0)
    closed = paper.close_trade(trade.id)
    assert closed.realized_pl == pytest.approx(1.5 * 1000 / 151.5)


def test_stop_loss_and_take_profit_from_quotes():
    paper = broker()
    trade = paper.open_trade("bot", "EUR_USD", 10_000, 1.0950, 1.1100)
    closed = paper.set_mid("EUR_USD", 1.1110, T0)
    assert [c.reason for c in closed] == ["take_profit"]
    assert paper.open_trades() == []
    assert closed[0].id == trade.id


def test_candle_touching_both_assumes_stop_first():
    paper = broker()
    paper.open_trade("bot", "EUR_USD", 10_000, 1.0950, 1.1050)
    candle = Candle(T0, 1.1000, 1.1100, 1.0900, 1.1000)
    closed = paper.process_candle("EUR_USD", candle, T0)
    assert closed[0].reason == "stop_loss"
    assert closed[0].exit_price == pytest.approx(1.0950)


def test_gap_through_stop_fills_at_the_open():
    paper = broker()
    paper.open_trade("bot", "EUR_USD", 10_000, 1.0950, None)
    candle = Candle(T0, 1.0900, 1.0920, 1.0880, 1.0910)
    closed = paper.process_candle("EUR_USD", candle, T0)
    assert closed[0].exit_price == pytest.approx(1.0899)  # open minus half spread, worse than SL


def test_candle_ignores_trades_opened_after_it_started():
    paper = broker()
    later = T0.replace(hour=2)
    paper.set_mid("EUR_USD", 1.1000, later)
    paper.open_trade("bot", "EUR_USD", 10_000, 1.0950, None)
    candle = Candle(T0, 1.1000, 1.1010, 1.0900, 1.1000)  # the low happened before the trade
    assert paper.process_candle("EUR_USD", candle, later) == []


def test_rejects_bad_stops_small_size_and_missing_margin():
    paper = broker()
    with pytest.raises(OrderRejected):
        paper.open_trade("bot", "EUR_USD", 1000, 1.2000, None)  # long stop above price
    with pytest.raises(OrderRejected):
        paper.open_trade("bot", "XAU_USD", 0.4, None, None)  # rounds to 0 ounces
    with pytest.raises(OrderRejected):
        paper.open_trade("bot", "XAU_USD", 1000, None, None)  # 2M USD of gold on 10k


def test_commission_is_part_of_realized_pl():
    paper = broker(commission_per_trade=5.0)
    trade = paper.open_trade("bot", "EUR_USD", 10_000, None, None)
    assert paper.balance == pytest.approx(9_995.0)
    closed = paper.close_trade(trade.id)
    assert closed.realized_pl == pytest.approx(-2.0 - 5.0)


def test_round_trip_persistence():
    paper = broker()
    paper.open_trade("bot", "EUR_USD", 10_000, 1.09, 1.12, comment="x")
    other = paper.open_trade("bot", "XAU_USD", 1, 1900.0, None)
    paper.close_trade(other.id)
    copy = PaperBroker()
    copy.restore(paper.to_dict())
    assert copy.balance == pytest.approx(paper.balance)
    assert [t.comment for t in copy.open_trades()] == ["x"]
    assert copy.get_closed_trade(other.id) is not None
    copy.set_mid("EUR_USD", 1.1, T0)
    assert copy.open_trade("bot", "EUR_USD", 1000, None, None).id == "P3"
