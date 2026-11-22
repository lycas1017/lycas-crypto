"""Instrument metadata (pip size, precision, margin) and currency conversion helpers.

Names follow OANDA's ``BASE_QUOTE`` convention: ``EUR_USD``, ``USD_JPY``, ``XAU_USD``.
For gold, one unit is one troy ounce and prices are quoted in USD per ounce.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

GRANULARITY_SECONDS: dict[str, int] = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D": 86400,
}

# Accepts "EUR_USD", "XAU_USD", "USD_JPY". Rejects "EURUSD", "eur_usd", "EUR-USD".
_INSTRUMENT_PATTERN = re.compile(r"^[A-Z0-9]{3,6}_[A-Z]{3}$")


class ConversionError(ValueError):
    """No exchange rate is known to turn a quote currency into the account currency."""


@dataclass(frozen=True)
class InstrumentSpec:
    name: str
    pip_location: int
    display_precision: int
    units_precision: int = 0
    min_units: float = 1.0
    margin_rate: float = 0.05

    @property
    def base(self) -> str:
        return self.name.split("_")[0]

    @property
    def quote(self) -> str:
        return self.name.split("_")[1]

    @property
    def pip(self) -> float:
        return 10.0**self.pip_location

    def to_pips(self, price_difference: float) -> float:
        return price_difference / self.pip

    def round_price(self, price: float) -> float:
        return round(price, self.display_precision)

    def round_units(self, units: float) -> float:
        """Round toward zero so sizing never exceeds what risk allows."""
        factor = 10**self.units_precision
        rounded = math.floor(abs(units) * factor + 1e-9) / factor
        rounded = math.copysign(rounded, units)
        return int(rounded) if self.units_precision == 0 else rounded


def _forex(name: str, margin_rate: float = 0.0333) -> InstrumentSpec:
    jpy = name.endswith("_JPY")
    return InstrumentSpec(
        name=name,
        pip_location=-2 if jpy else -4,
        display_precision=3 if jpy else 5,
        margin_rate=margin_rate,
    )


KNOWN_INSTRUMENTS: dict[str, InstrumentSpec] = {
    spec.name: spec
    for spec in (
        InstrumentSpec("XAU_USD", pip_location=-2, display_precision=3, margin_rate=0.05),
        InstrumentSpec("XAG_USD", pip_location=-4, display_precision=5, margin_rate=0.10),
        InstrumentSpec("XAU_EUR", pip_location=-2, display_precision=3, margin_rate=0.05),
        *(
            _forex(name)
            for name in (
                "EUR_USD",
                "GBP_USD",
                "AUD_USD",
                "NZD_USD",
                "USD_CHF",
                "USD_CAD",
                "USD_JPY",
                "EUR_GBP",
                "EUR_JPY",
                "GBP_JPY",
                "EUR_CHF",
                "AUD_JPY",
            )
        ),
    )
}

# Typical retail spreads in pips, used by the paper broker and backtests when no live
# bid/ask is available. Real spreads widen around news and the daily rollover.
DEFAULT_SPREAD_PIPS: dict[str, float] = {
    "XAU_USD": 35.0,
    "XAG_USD": 250.0,
    "EUR_USD": 1.0,
    "GBP_USD": 1.4,
    "USD_JPY": 1.2,
    "AUD_USD": 1.3,
    "NZD_USD": 1.8,
    "USD_CHF": 1.6,
    "USD_CAD": 1.8,
    "EUR_GBP": 1.5,
    "EUR_JPY": 1.9,
    "GBP_JPY": 2.8,
}


def is_valid_instrument(name: str) -> bool:
    return bool(_INSTRUMENT_PATTERN.match(name))


def default_spec(name: str) -> InstrumentSpec:
    if not is_valid_instrument(name):
        raise ValueError(f"instrument must look like EUR_USD or XAU_USD, got {name!r}")
    if name in KNOWN_INSTRUMENTS:
        return KNOWN_INSTRUMENTS[name]
    if name.startswith("XAU_"):
        return InstrumentSpec(name, pip_location=-2, display_precision=3, margin_rate=0.05)
    return _forex(name)


def quote_to_account_rate(
    instrument: str,
    price: float,
    account_currency: str,
    rates: Mapping[str, float] | None = None,
) -> float:
    """How much one unit of the instrument's quote currency is worth in account currency.

    ``price`` is the instrument's own mid price. ``rates`` maps other instrument names
    (``GBP_USD``) to mid prices and is only needed for cross pairs such as ``EUR_GBP``
    on a USD account.
    """
    base, quote = instrument.split("_")
    if quote == account_currency:
        return 1.0
    if base == account_currency:
        if price <= 0:
            raise ConversionError(f"cannot convert with non-positive price {price}")
        return 1.0 / price
    rates = rates or {}
    direct = rates.get(f"{quote}_{account_currency}")
    if direct:
        return direct
    inverse = rates.get(f"{account_currency}_{quote}")
    if inverse:
        return 1.0 / inverse
    raise ConversionError(
        f"need a {quote}_{account_currency} or {account_currency}_{quote} rate to value "
        f"{instrument} in {account_currency}; add it under paper.conversion_rates"
    )
