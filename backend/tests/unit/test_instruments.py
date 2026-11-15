"""Instrument registry: the shipped configuration and the validation rules."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from fxlab.domain.instruments import InstrumentRegistry, UnknownInstrumentError


def test_shipped_registry_matches_specification(registry: InstrumentRegistry) -> None:
    symbols = {i.symbol for i in registry.all()}
    assert symbols == {"EURUSD", "GBPUSD", "USDJPY", "USDZAR", "USDPLN", "USDTRY", "EURGBP", "EURJPY"}
    # only EUR/USD is active initially
    assert [i.symbol for i in registry.active()] == ["EURUSD"]


def test_pip_conventions_are_per_instrument(registry: InstrumentRegistry) -> None:
    eurusd, usdjpy = registry.get("EURUSD"), registry.get("USDJPY")
    assert eurusd.pip_size == 0.0001 and eurusd.price_precision == 5
    assert usdjpy.pip_size == 0.01 and usdjpy.price_precision == 3
    assert eurusd.to_pips(0.0007) == pytest.approx(7.0)
    assert usdjpy.to_pips(0.07) == pytest.approx(7.0)
    assert usdjpy.from_pips(15) == pytest.approx(0.15)
    assert eurusd.round_price(1.1600049) == 1.16000


def test_defaults_are_merged_and_overridable(registry: InstrumentRegistry) -> None:
    eurusd, usdtry = registry.get("EURUSD"), registry.get("USDTRY")
    assert eurusd.data_quality.stale_after_ms == 5000  # from defaults
    assert usdtry.data_quality.stale_after_ms == 60000  # overridden
    assert usdtry.data_quality.jump_confirm_quotes == 3  # untouched default survives a partial override
    assert eurusd.costs.assumed_spread_pips == 0.8
    assert eurusd.session.week_open.day == "sun" and eurusd.session.daily_rollover == "17:00"


def test_unknown_instrument() -> None:
    reg = InstrumentRegistry([])
    with pytest.raises(UnknownInstrumentError):
        reg.get("EURUSD")
    assert "EURUSD" not in reg


def test_new_pair_is_a_pure_configuration_change() -> None:
    """Acceptance criterion 15: adding a pair needs no code."""
    reg = InstrumentRegistry.from_dict(
        {
            "defaults": {"data_quality": {"stale_after_ms": 7000}},
            "instruments": [
                {
                    "symbol": "AUDNZD",
                    "base": "AUD",
                    "quote": "NZD",
                    "pip_size": 0.0001,
                    "price_precision": 5,
                    "active": True,
                },
            ],
        }
    )
    inst = reg.get("AUDNZD")
    assert inst.active and inst.data_quality.stale_after_ms == 7000
    assert inst.make_calendar().is_open(0) is not None


@pytest.mark.parametrize(
    "bad",
    [
        {
            "symbol": "EURUSD",
            "base": "EUR",
            "quote": "GBP",
            "pip_size": 0.0001,
            "price_precision": 5,
        },  # mismatch
        {"symbol": "eurusd", "base": "EUR", "quote": "USD", "pip_size": 0.0001, "price_precision": 5},  # case
        {"symbol": "EURUSD", "base": "EUR", "quote": "USD", "pip_size": 0, "price_precision": 5},  # pip size
        {
            "symbol": "EURUSD",
            "base": "EUR",
            "quote": "USD",
            "pip_size": 0.0001,
            "price_precision": 5,
            "typo": 1,
        },
        {
            "symbol": "EURUSD",
            "base": "EUR",
            "quote": "USD",
            "pip_size": 0.0001,
            "price_precision": 5,
            "session": {"daily_rollover": "25:00"},
        },
    ],
)
def test_invalid_configuration_is_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        InstrumentRegistry.from_dict({"instruments": [bad]})


def test_duplicate_symbols_are_rejected() -> None:
    entry = {"symbol": "EURUSD", "base": "EUR", "quote": "USD", "pip_size": 0.0001, "price_precision": 5}
    with pytest.raises(ValueError, match="duplicate"):
        InstrumentRegistry.from_dict({"instruments": [entry, entry]})
