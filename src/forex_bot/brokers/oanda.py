"""OANDA v20 REST API: a small client plus a ``Broker`` adapter for live trading.

Stop loss and take profit are attached to each order on OANDA's servers
(``stopLossOnFill``/``takeProfitOnFill``), so they protect the trade even if the bot
stops running. Every trade carries the bot id in its client extensions ``tag``, which is
how each bot recognises its own trades and leaves manual trades alone.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

import requests

from forex_bot.brokers.base import Broker, BrokerError, OrderRejected
from forex_bot.instruments import InstrumentSpec, default_spec
from forex_bot.models import (
    AccountState,
    Candle,
    ClosedTrade,
    Quote,
    Trade,
    format_time,
    parse_time,
)

log = logging.getLogger(__name__)

API_URLS = {
    "practice": "https://api-fxpractice.oanda.com",
    "live": "https://api-fxtrade.oanda.com",
}
MAX_CANDLES_PER_REQUEST = 5000
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OandaError(BrokerError):
    def __init__(self, message: str, status: int | None = None, payload: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.payload = payload


class OandaClient:
    def __init__(
        self,
        token: str,
        account_id: str,
        environment: str = "practice",
        session: requests.Session | None = None,
        timeout: float = 20.0,
        retries: int = 3,
    ) -> None:
        if environment not in API_URLS:
            raise ValueError(f"OANDA environment must be 'practice' or 'live', got {environment!r}")
        if not token or not account_id:
            raise ValueError("OANDA_API_TOKEN and OANDA_ACCOUNT_ID are required")
        self.environment = environment
        self.account_id = account_id
        self.base_url = API_URLS[environment]
        self.timeout = timeout
        self.retries = retries
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept-Datetime-Format": "RFC3339",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                response = self.session.request(
                    method, url, params=params, json=body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                last_error = exc
                log.warning("OANDA %s %s failed (%s), attempt %d", method, path, exc, attempt + 1)
                time.sleep(min(2**attempt, 10))
                continue
            if response.status_code in RETRYABLE_STATUS and attempt < self.retries - 1:
                log.warning("OANDA %s %s returned %s, retrying", method, path, response.status_code)
                time.sleep(min(2**attempt, 10))
                continue
            try:
                payload = response.json() if response.content else {}
            except ValueError:
                payload = {"errorMessage": response.text[:200]}
            if response.status_code >= 400:
                message = payload.get("errorMessage") or payload.get("message") or response.reason
                raise OandaError(
                    f"OANDA {method} {path}: HTTP {response.status_code} {message}",
                    response.status_code,
                    payload,
                )
            return payload
        raise OandaError(
            f"OANDA {method} {path} failed after {self.retries} attempts: {last_error}"
        )

    # Account and market data ----------------------------------------------------------

    def account_summary(self) -> dict[str, Any]:
        return self.request("GET", f"/v3/accounts/{self.account_id}/summary")["account"]

    def instruments(self, names: list[str]) -> list[dict[str, Any]]:
        data = self.request(
            "GET",
            f"/v3/accounts/{self.account_id}/instruments",
            params={"instruments": ",".join(names)},
        )
        return data.get("instruments", [])

    def pricing(self, instruments: list[str], home_conversions: bool = False) -> dict[str, Any]:
        params = {"instruments": ",".join(instruments)}
        if home_conversions:
            params["includeHomeConversions"] = "true"
        return self.request("GET", f"/v3/accounts/{self.account_id}/pricing", params=params)

    def candles(
        self,
        instrument: str,
        granularity: str,
        count: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Mid-price candles. Only complete candles are returned."""
        params: dict[str, Any] = {"granularity": granularity, "price": "M"}
        if count is not None:
            params["count"] = min(count, MAX_CANDLES_PER_REQUEST)
        if start is not None:
            params["from"] = format_time(start)
        if end is not None:
            params["to"] = format_time(end)
        data = self.request("GET", f"/v3/instruments/{instrument}/candles", params=params)
        return [parse_candle(item) for item in data.get("candles", []) if item.get("complete")]

    def candle_history(
        self, instrument: str, granularity: str, start: datetime, end: datetime
    ) -> list[Candle]:
        """Every complete candle between ``start`` and ``end``, paging 5000 at a time."""
        out: list[Candle] = []
        cursor = start
        while cursor < end:
            raw = self.candles(instrument, granularity, count=MAX_CANDLES_PER_REQUEST, start=cursor)
            batch = [c for c in raw if c.time < end and (not out or c.time > out[-1].time)]
            out.extend(batch)
            if not batch or len(raw) < MAX_CANDLES_PER_REQUEST // 2:
                break  # reached ``end`` or the most recent complete candle
            cursor = batch[-1].time + timedelta(seconds=1)
        return out

    # Trading --------------------------------------------------------------------------

    def open_trades(self) -> list[dict[str, Any]]:
        data = self.request("GET", f"/v3/accounts/{self.account_id}/openTrades")
        return data.get("trades", [])

    def trade(self, trade_id: str) -> dict[str, Any]:
        return self.request("GET", f"/v3/accounts/{self.account_id}/trades/{trade_id}")["trade"]

    def market_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return self.request("POST", f"/v3/accounts/{self.account_id}/orders", body={"order": order})

    def close_trade(self, trade_id: str) -> dict[str, Any]:
        return self.request(
            "PUT",
            f"/v3/accounts/{self.account_id}/trades/{trade_id}/close",
            body={"units": "ALL"},
        )


def parse_candle(item: dict[str, Any]) -> Candle:
    mid = item["mid"]
    return Candle(
        time=parse_time(item["time"]),
        open=float(mid["o"]),
        high=float(mid["h"]),
        low=float(mid["l"]),
        close=float(mid["c"]),
        volume=float(item.get("volume", 0)),
    )


def parse_price(item: dict[str, Any]) -> Quote:
    """Top-of-book bid/ask, falling back to closeout prices when the book is empty."""
    bids, asks = item.get("bids") or [], item.get("asks") or []
    bid = float(bids[0]["price"] if bids else item["closeoutBid"])
    ask = float(asks[0]["price"] if asks else item["closeoutAsk"])
    return Quote(
        instrument=item["instrument"],
        bid=bid,
        ask=ask,
        time=parse_time(item["time"]),
        tradeable=bool(item.get("tradeable", True)),
    )


def parse_spec(item: dict[str, Any]) -> InstrumentSpec:
    return InstrumentSpec(
        name=item["name"],
        pip_location=int(item["pipLocation"]),
        display_precision=int(item["displayPrecision"]),
        units_precision=int(item.get("tradeUnitsPrecision", 0)),
        min_units=float(item.get("minimumTradeSize", 1)),
        margin_rate=float(item.get("marginRate", 0.05)),
    )


def parse_trade(item: dict[str, Any]) -> Trade:
    extensions = item.get("clientExtensions") or {}
    stop = item.get("stopLossOrder") or {}
    target = item.get("takeProfitOrder") or {}
    return Trade(
        id=str(item["id"]),
        instrument=item["instrument"],
        units=float(item.get("currentUnits", item.get("initialUnits"))),
        entry_price=float(item["price"]),
        open_time=parse_time(item["openTime"]),
        bot_id=extensions.get("tag", ""),
        stop_loss=float(stop["price"]) if stop.get("price") else None,
        take_profit=float(target["price"]) if target.get("price") else None,
        comment=extensions.get("comment", ""),
        unrealized_pl=float(item.get("unrealizedPL", 0.0)),
    )


class OandaBroker(Broker):
    def __init__(self, client: OandaClient) -> None:
        self.client = client
        self._specs: dict[str, InstrumentSpec] = {}
        summary = client.account_summary()
        self.currency = summary["currency"]
        self.hedging_enabled = bool(summary.get("hedgingEnabled", False))

    def account(self) -> AccountState:
        summary = self.client.account_summary()
        return AccountState(
            currency=summary["currency"],
            balance=float(summary["balance"]),
            equity=float(summary["NAV"]),
            margin_used=float(summary.get("marginUsed", 0.0)),
            margin_available=float(summary.get("marginAvailable", 0.0)),
            open_trade_count=int(summary.get("openTradeCount", 0)),
        )

    def load_specs(self, instruments: list[str]) -> None:
        for item in self.client.instruments(instruments):
            spec = parse_spec(item)
            self._specs[spec.name] = spec

    def spec(self, instrument: str) -> InstrumentSpec:
        if instrument not in self._specs:
            self.load_specs([instrument])
        return self._specs.get(instrument) or default_spec(instrument)

    def quote(self, instrument: str) -> Quote:
        prices = self.client.pricing([instrument]).get("prices", [])
        if not prices:
            raise BrokerError(f"OANDA returned no price for {instrument}")
        return parse_price(prices[0])

    def quotes(self, instruments: list[str]) -> dict[str, Quote]:
        prices = self.client.pricing(instruments).get("prices", [])
        return {item["instrument"]: parse_price(item) for item in prices}

    def conversion_rate(self, instrument: str) -> float:
        quote_currency = instrument.split("_")[1]
        if quote_currency == self.currency:
            return 1.0
        data = self.client.pricing([instrument], home_conversions=True)
        for item in data.get("homeConversions", []):
            if item.get("currency") == quote_currency:
                return float(item["positionValue"])
        raise BrokerError(f"OANDA gave no {quote_currency} to {self.currency} conversion")

    def open_trades(self, bot_id: str | None = None) -> list[Trade]:
        trades = [parse_trade(item) for item in self.client.open_trades()]
        return [t for t in trades if bot_id is None or t.bot_id == bot_id]

    def open_trade(
        self,
        bot_id: str,
        instrument: str,
        units: float,
        stop_loss: float | None,
        take_profit: float | None,
        comment: str = "",
        reason: str = "",
    ) -> Trade:
        spec = self.spec(instrument)
        units = spec.round_units(units)
        if abs(units) < spec.min_units:
            raise OrderRejected(f"{units} units is below the {spec.min_units} minimum")
        order: dict[str, Any] = {
            "type": "MARKET",
            "instrument": instrument,
            "units": _format_units(units, spec),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "tradeClientExtensions": {"tag": bot_id[:120], "comment": comment[:120]},
        }
        if stop_loss is not None:
            order["stopLossOnFill"] = {
                "price": f"{spec.round_price(stop_loss):.{spec.display_precision}f}",
                "timeInForce": "GTC",
            }
        if take_profit is not None:
            order["takeProfitOnFill"] = {
                "price": f"{spec.round_price(take_profit):.{spec.display_precision}f}",
                "timeInForce": "GTC",
            }
        try:
            result = self.client.market_order(order)
        except OandaError as exc:
            reason_text = (exc.payload or {}).get("orderRejectTransaction", {}).get("rejectReason")
            raise OrderRejected(reason_text or str(exc)) from exc
        if "orderCancelTransaction" in result:
            raise OrderRejected(
                f"OANDA cancelled the order: {result['orderCancelTransaction'].get('reason')}"
            )
        fill = result.get("orderFillTransaction")
        if not fill or not fill.get("tradeOpened"):
            raise OrderRejected(f"OANDA did not open a trade: {result}")
        opened = fill["tradeOpened"]
        return Trade(
            id=str(opened["tradeID"]),
            instrument=instrument,
            units=float(opened["units"]),
            entry_price=float(opened.get("price", fill.get("price"))),
            open_time=parse_time(fill["time"]),
            bot_id=bot_id,
            stop_loss=stop_loss,
            take_profit=take_profit,
            comment=comment,
        )

    def close_trade(self, trade_id: str, reason: str = "") -> ClosedTrade:
        try:
            result = self.client.close_trade(trade_id)
        except OandaError as exc:
            raise OrderRejected(f"could not close trade {trade_id}: {exc}") from exc
        fill = result.get("orderFillTransaction") or {}
        closed = self.get_closed_trade(trade_id)
        if closed is None:
            raise OrderRejected(f"OANDA did not close trade {trade_id}: {result}")
        if fill.get("price"):
            closed.exit_price = float(fill["price"])
        closed.reason = reason or closed.reason
        return closed

    def get_closed_trade(self, trade_id: str) -> ClosedTrade | None:
        try:
            item = self.client.trade(trade_id)
        except OandaError:
            return None
        if item.get("state") != "CLOSED":
            return None
        extensions = item.get("clientExtensions") or {}
        reason = "closed"
        if (item.get("stopLossOrder") or {}).get("state") == "FILLED":
            reason = "stop_loss"
        elif (item.get("takeProfitOrder") or {}).get("state") == "FILLED":
            reason = "take_profit"
        return ClosedTrade(
            id=str(item["id"]),
            instrument=item["instrument"],
            bot_id=extensions.get("tag", ""),
            units=float(item["initialUnits"]),
            entry_price=float(item["price"]),
            exit_price=float(item.get("averageClosePrice", item["price"])),
            open_time=parse_time(item["openTime"]),
            close_time=parse_time(item.get("closeTime", item["openTime"])),
            realized_pl=float(item.get("realizedPL", 0.0)) + float(item.get("financing", 0.0)),
            reason=reason,
            comment=extensions.get("comment", ""),
        )


def _format_units(units: float, spec: InstrumentSpec) -> str:
    if spec.units_precision == 0:
        return str(int(units))
    return f"{units:.{spec.units_precision}f}"
