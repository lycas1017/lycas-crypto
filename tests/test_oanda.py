import pytest

from forex_bot.brokers import OrderRejected
from forex_bot.brokers.oanda import OandaBroker, OandaClient, OandaError


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.reason = "status"
        self.content = b"{}"
        self.text = str(payload)

    def json(self):
        return self._payload


class FakeSession:
    """Answers requests from a {(METHOD, path suffix): (status, payload)} table."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.sent = []

    def request(self, method, url, params=None, json=None, timeout=None):
        self.sent.append((method, url, params, json))
        for (verb, suffix), (status, payload) in self.routes.items():
            if verb == method and url.split("?")[0].endswith(suffix):
                return FakeResponse(status, payload)
        return FakeResponse(404, {"errorMessage": f"no route for {method} {url}"})


SUMMARY = {"account": {"id": "101-1", "currency": "USD", "balance": "1000.0", "NAV": "1010.5",
                       "marginUsed": "50", "marginAvailable": "960.5", "openTradeCount": 1,
                       "hedgingEnabled": False}}
GOLD = {"instruments": [{"name": "XAU_USD", "pipLocation": -2, "displayPrecision": 3,
                         "tradeUnitsPrecision": 0, "minimumTradeSize": "1", "marginRate": "0.05"}]}


def broker(extra=None):
    routes = {("GET", "/summary"): (200, SUMMARY), ("GET", "/instruments"): (200, GOLD)}
    routes.update(extra or {})
    session = FakeSession(routes)
    client = OandaClient("token", "101-1", "practice", session=session, retries=1)
    return OandaBroker(client), session


def test_client_validates_settings():
    with pytest.raises(ValueError):
        OandaClient("", "101-1")
    with pytest.raises(ValueError):
        OandaClient("t", "101-1", environment="demo")


def test_account_and_candles():
    candles = {"candles": [
        {"complete": True, "time": "2025-03-03T10:00:00.000000000Z", "volume": 5,
         "mid": {"o": "2900.1", "h": "2905", "l": "2899", "c": "2903.5"}},
        {"complete": False, "time": "2025-03-03T11:00:00.000000000Z", "volume": 1,
         "mid": {"o": "2903.5", "h": "2904", "l": "2903", "c": "2903.9"}},
    ]}
    paper, session = broker({("GET", "/XAU_USD/candles"): (200, candles)})
    account = paper.account()
    assert account.equity == pytest.approx(1010.5) and account.open_trade_count == 1
    assert not paper.hedging_enabled
    result = paper.client.candles("XAU_USD", "H1", count=2)
    assert len(result) == 1 and result[0].close == pytest.approx(2903.5)
    assert result[0].time.hour == 10
    assert session.headers["Authorization"] == "Bearer token"


def test_market_order_sends_stops_tag_and_parses_fill():
    fill = {"orderFillTransaction": {"time": "2025-03-03T10:00:01Z", "price": "2903.6",
                                     "tradeOpened": {"tradeID": "42", "units": "3",
                                                     "price": "2903.6"}}}
    paper, session = broker({("POST", "/orders"): (201, fill)})
    trade = paper.open_trade("gold-trend", "XAU_USD", 3.7, 2880.12345, 2950.0, "c", "why")
    assert trade.id == "42" and trade.units == 3 and trade.bot_id == "gold-trend"
    body = session.sent[-1][3]["order"]
    assert body["units"] == "3"
    assert body["stopLossOnFill"]["price"] == "2880.123"
    assert body["takeProfitOnFill"]["price"] == "2950.000"
    assert body["tradeClientExtensions"]["tag"] == "gold-trend"


def test_cancelled_and_rejected_orders_raise_order_rejected():
    cancelled = {"orderCancelTransaction": {"reason": "MARKET_HALTED"}}
    paper, _ = broker({("POST", "/orders"): (201, cancelled)})
    with pytest.raises(OrderRejected, match="MARKET_HALTED"):
        paper.open_trade("b", "XAU_USD", 1, None, None)
    rejected = {"orderRejectTransaction": {"rejectReason": "INSUFFICIENT_MARGIN"},
                "errorMessage": "no"}
    paper, _ = broker({("POST", "/orders"): (400, rejected)})
    with pytest.raises(OrderRejected, match="INSUFFICIENT_MARGIN"):
        paper.open_trade("b", "XAU_USD", 1, None, None)


def test_open_trades_are_filtered_by_bot_tag():
    trades = {"trades": [
        {"id": "1", "instrument": "XAU_USD", "currentUnits": "2", "price": "2900",
         "openTime": "2025-03-03T10:00:00Z", "unrealizedPL": "4.2",
         "clientExtensions": {"tag": "gold-trend", "comment": "x"},
         "stopLossOrder": {"price": "2880"}},
        {"id": "2", "instrument": "EUR_USD", "currentUnits": "-1000", "price": "1.08",
         "openTime": "2025-03-03T10:00:00Z"},  # manual trade without a tag
    ]}
    paper, _ = broker({("GET", "/openTrades"): (200, trades)})
    mine = paper.open_trades("gold-trend")
    assert [t.id for t in mine] == ["1"]
    assert mine[0].stop_loss == 2880 and mine[0].take_profit is None
    assert len(paper.open_trades()) == 2


def test_closed_trade_details_and_errors():
    detail = {"trade": {"id": "9", "instrument": "XAU_USD", "state": "CLOSED",
                        "initialUnits": "2", "price": "2900", "averageClosePrice": "2880",
                        "openTime": "2025-03-03T10:00:00Z", "closeTime": "2025-03-03T12:00:00Z",
                        "realizedPL": "-40", "financing": "-0.5",
                        "stopLossOrder": {"state": "FILLED"},
                        "clientExtensions": {"tag": "gold-trend"}}}
    paper, _ = broker({("GET", "/trades/9"): (200, detail)})
    closed = paper.get_closed_trade("9")
    assert closed.reason == "stop_loss" and closed.realized_pl == pytest.approx(-40.5)
    assert paper.get_closed_trade("10") is None
    with pytest.raises(OandaError):
        paper.client.request("GET", "/v3/unknown")
