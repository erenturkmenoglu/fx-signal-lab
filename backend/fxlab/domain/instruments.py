"""Instrument registry.

Everything that differs between currency pairs lives here and is loaded from
``config/instruments.yaml``: symbols, pip size, precision, session calendar,
data-quality thresholds and trading-cost assumptions.  No other module may hardcode
pair-specific values.  Adding a pair is a configuration change, not a code change.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from fxlab.domain.calendar import (
    DAY_INDEX,
    AlwaysOpenCalendar,
    WeeklyCalendar,
    WeeklySchedule,
    parse_hhmm,
)

DayName = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class WeekTime(_Model):
    day: DayName
    time: str

    @field_validator("time")
    @classmethod
    def _check_time(cls, v: str) -> str:
        parse_hhmm(v)
        return v


class SessionConfig(_Model):
    """Trading-session characteristics of an instrument."""

    calendar: Literal["fx_24x5", "always_open"] = "fx_24x5"
    tz: str = "America/New_York"
    week_open: WeekTime = WeekTime(day="sun", time="17:00")
    week_close: WeekTime = WeekTime(day="fri", time="17:00")
    daily_rollover: str = "17:00"

    @field_validator("daily_rollover")
    @classmethod
    def _check_rollover(cls, v: str) -> str:
        parse_hhmm(v)
        return v

    def schedule(self) -> WeeklySchedule:
        return WeeklySchedule(
            tz=self.tz,
            open_day=DAY_INDEX[self.week_open.day],
            open_minute=parse_hhmm(self.week_open.time),
            close_day=DAY_INDEX[self.week_close.day],
            close_minute=parse_hhmm(self.week_close.time),
            rollover_minute=parse_hhmm(self.daily_rollover),
        )


class DataQualityConfig(_Model):
    """Per-instrument data freshness and validation thresholds."""

    stale_after_ms: int = Field(5_000, gt=0, description="quote older than this is STALE")
    max_spread_pips: float = Field(15.0, gt=0, description="quotes wider than this are rejected")
    max_jump_pct: float = Field(0.5, gt=0, description="mid jump vs last accepted quote that is suspect")
    jump_confirm_quotes: int = Field(3, ge=1, description="consecutive quotes confirming a new level")
    jump_ref_max_age_ms: int = Field(60_000, gt=0, description="reference older than this is not used")
    soft_future_skew_ms: int = Field(1_500, ge=0, description="source ts ahead of local clock: warn")
    hard_future_skew_ms: int = Field(60_000, gt=0, description="source ts ahead of local clock: reject")


class CostConfig(_Model):
    """Default trading-cost assumptions used by research/backtests (later phases)."""

    assumed_spread_pips: float = Field(1.0, ge=0)
    slippage_pips: float = Field(0.1, ge=0)
    commission_per_million: float = Field(0.0, ge=0, description="per 1M base-currency notional, per side")


class ProviderSymbols(_Model):
    """Optional overrides when a provider does not follow the default symbol pattern."""

    massive_ws_pair: str | None = None  # default "EUR-USD"
    massive_rest_aggs: str | None = None  # default "C:EURUSD"
    massive_rest_quotes: str | None = None  # default "C:EUR-USD"


class Instrument(_Model):
    symbol: str = Field(pattern=r"^[A-Z]{6}$")
    base: str = Field(pattern=r"^[A-Z]{3}$")
    quote: str = Field(pattern=r"^[A-Z]{3}$")
    description: str = ""
    pip_size: float = Field(gt=0)
    price_precision: int = Field(ge=0, le=10)
    active: bool = False
    session: SessionConfig = SessionConfig()
    data_quality: DataQualityConfig = DataQualityConfig()
    costs: CostConfig = CostConfig()
    strategy_eligibility: list[str] = Field(default_factory=lambda: ["all"])
    history_start: str | None = Field(None, description="earliest date with usable history, if known")
    provider_symbols: ProviderSymbols = ProviderSymbols()

    @model_validator(mode="after")
    def _check_symbol(self) -> Instrument:
        if self.symbol != self.base + self.quote:
            raise ValueError(f"symbol {self.symbol} must equal base+quote ({self.base}{self.quote})")
        return self

    # -- pair-specific arithmetic (never hardcode these elsewhere) ---------------
    def to_pips(self, price_difference: float) -> float:
        return price_difference / self.pip_size

    def from_pips(self, pips: float) -> float:
        return pips * self.pip_size

    def round_price(self, price: float) -> float:
        return round(price, self.price_precision)

    def make_calendar(self, *, always_open: bool = False) -> WeeklyCalendar:
        schedule = self.session.schedule()
        if always_open or self.session.calendar == "always_open":
            return AlwaysOpenCalendar(schedule)
        return WeeklyCalendar(schedule)


class UnknownInstrumentError(KeyError):
    pass


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class InstrumentRegistry:
    """In-memory registry of configured instruments, keyed by canonical symbol (e.g. ``EURUSD``)."""

    def __init__(self, instruments: list[Instrument]) -> None:
        self._by_symbol: dict[str, Instrument] = {}
        for inst in instruments:
            if inst.symbol in self._by_symbol:
                raise ValueError(f"duplicate instrument {inst.symbol}")
            self._by_symbol[inst.symbol] = inst

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstrumentRegistry:
        defaults = data.get("defaults") or {}
        items = data.get("instruments") or []
        if not isinstance(items, list):
            raise ValueError("'instruments' must be a list")
        return cls([Instrument.model_validate(_deep_merge(defaults, raw)) for raw in items])

    @classmethod
    def from_yaml(cls, path: str | Path) -> InstrumentRegistry:
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls.from_dict(data)

    def get(self, symbol: str) -> Instrument:
        try:
            return self._by_symbol[symbol]
        except KeyError:
            raise UnknownInstrumentError(symbol) from None

    def __contains__(self, symbol: object) -> bool:
        return symbol in self._by_symbol

    def all(self) -> list[Instrument]:
        return list(self._by_symbol.values())

    def active(self) -> list[Instrument]:
        return [i for i in self._by_symbol.values() if i.active]
