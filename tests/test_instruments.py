import pytest

from forex_bot.instruments import ConversionError, default_spec, quote_to_account_rate


def test_known_specs():
    gold = default_spec("XAU_USD")
    assert gold.pip == pytest.approx(0.01)
    assert gold.round_price(2345.67891) == 2345.679
    jpy = default_spec("USD_JPY")
    assert jpy.pip == pytest.approx(0.01)
    eur = default_spec("EUR_USD")
    assert eur.pip == pytest.approx(0.0001)
    assert eur.to_pips(0.0012) == pytest.approx(12)


def test_unknown_but_valid_names_get_sensible_defaults():
    assert default_spec("CHF_JPY").pip == pytest.approx(0.01)
    assert default_spec("EUR_NOK").pip == pytest.approx(0.0001)
    with pytest.raises(ValueError):
        default_spec("EURUSD")


def test_units_round_toward_zero():
    spec = default_spec("XAU_USD")
    assert spec.round_units(3.99) == 3
    assert spec.round_units(-3.99) == -3
    assert isinstance(spec.round_units(2.0), int)


def test_conversion_to_account_currency():
    assert quote_to_account_rate("EUR_USD", 1.1, "USD") == 1.0
    assert quote_to_account_rate("USD_JPY", 150.0, "USD") == pytest.approx(1 / 150)
    assert quote_to_account_rate("EUR_GBP", 0.85, "USD", {"GBP_USD": 1.25}) == 1.25
    assert quote_to_account_rate("EUR_CHF", 0.95, "USD", {"USD_CHF": 0.9}) == pytest.approx(1 / 0.9)
    with pytest.raises(ConversionError):
        quote_to_account_rate("EUR_GBP", 0.85, "USD")
