"""Strategies: pure functions from closed bars to signals.

A strategy looks at a series of bars (bid prices, at its own timeframe) and says, for every
bar, whether it would buy or sell *when that bar has closed*, and how far it expects the
price to go ("potential", in pips, before any cost).  It knows nothing about position
size, stops or targets: those belong to the engine.

The one rule: the answer for bar ``i`` may use bars ``0..i`` only.  The tests cut every
series short at many places and require the answers before the cut to stay the same; that is
what makes a backtest free of look-ahead, and what lets the live engine call the very same
function on the bars it has so far.

Signals are *events* (a line being crossed), not states: a strategy does not keep shouting
while a condition lasts, so a position that was stopped out is not re-opened by the same
condition on the next bar.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from fxlab.research import indicators as ind
from fxlab.research.bars import Bars, Floats, utc_offsets_ms
from fxlab.research.engine import Signals, clock_ms
from fxlab.timeutil import DAY_MS, HOUR_MS

Values = Mapping[str, Any]


@dataclass(frozen=True)
class Param:
    name: str
    label: str
    default: int | float | str
    kind: str  # int | float | choice
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""

    def coerce(self, raw: Any) -> int | float | str:
        try:
            if self.kind == "choice":
                value: int | float | str = str(raw)
                if value not in self.choices:
                    raise ValueError
                return value
            number = float(raw)
            if not np.isfinite(number):
                raise ValueError
            if self.kind == "int":
                if number != int(number):
                    raise ValueError
                value = int(number)
            else:
                value = number
        except (TypeError, ValueError):
            allowed = f" (one of: {', '.join(self.choices)})" if self.choices else ""
            raise ValueError(f"{self.name}: {raw!r} is not a valid value{allowed}") from None
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.name} must be at least {self.minimum:g}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"{self.name} must be at most {self.maximum:g}")
        return value

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "default": self.default,
            "kind": self.kind,
            "minimum": self.minimum,
            "maximum": self.maximum,
            "choices": list(self.choices),
            "help": self.help,
        }


@dataclass(frozen=True)
class Strategy:
    key: str
    title: str
    summary: str  # what makes it buy or sell, and what it calls the potential
    timeframe: str  # the bar length it is usually run on
    params: tuple[Param, ...]
    compute: Callable[[Bars, Values, float], Signals]
    #: raises ValueError when values that are each allowed do not go together
    check: Callable[[Values], None] | None = None

    def resolve(self, given: Values | None = None) -> dict[str, int | float | str]:
        """Defaults overlaid with ``given``; unknown names and invalid values are errors."""
        given = dict(given or {})
        known = {p.name for p in self.params}
        unknown = sorted(set(given) - known)
        if unknown:
            raise ValueError(f"{self.key} has no parameter {unknown[0]!r}; it has: {', '.join(sorted(known))}")
        values = {p.name: p.coerce(given.get(p.name, p.default)) for p in self.params}
        if self.check is not None:
            self.check(values)
        return values

    def signals(self, bars: Bars, pip_size: float, given: Values | None = None) -> Signals:
        return self.compute(bars, self.resolve(given), pip_size)

    def describe(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": self.title,
            "summary": self.summary,
            "timeframe": self.timeframe,
            "params": [p.describe() for p in self.params],
        }


def _previous(values: Floats) -> Floats:
    """Each value moved one bar later (the first has no predecessor)."""
    out = np.empty_like(values)
    out[:1] = np.nan
    out[1:] = values[:-1]
    return out


def _signals(long: npt.NDArray[np.bool_], short: npt.NDArray[np.bool_], up: Floats, down: Floats) -> Signals:
    """Combine buy and sell events; a bar that says both says nothing."""
    both = long & short
    long, short = long & ~both, short & ~both
    direction = np.zeros(len(long), dtype=np.int8)
    direction[long] = 1
    direction[short] = -1
    potential = np.full(len(long), np.nan)
    potential[long] = up[long]
    potential[short] = down[short]
    return Signals(direction, potential)


# --- mean reversion ------------------------------------------------------------------------


def _rsi_reversion(bars: Bars, p: Values, pip: float) -> Signals:
    close = bars.close
    rsi = ind.rsi(close, int(p["rsi_period"]))
    before = _previous(rsi)
    low, high = float(p["oversold"]), float(p["overbought"])
    if p["trigger"] == "enter":  # the moment the RSI falls into the zone
        long = (rsi < low) & (before >= low)
        short = (rsi > high) & (before <= high)
    else:  # "leave": the moment it comes back out, a first sign that the move has turned
        long = (rsi > low) & (before <= low)
        short = (rsi < high) & (before >= high)
    mean = ind.sma(close, int(p["mean_period"]))
    return _signals(long, short, (mean - close) / pip, (close - mean) / pip)


def _bollinger_reversion(bars: Bars, p: Values, pip: float) -> Signals:
    close = bars.close
    middle, upper, lower = ind.bollinger(close, int(p["period"]), float(p["width"]))
    before = _previous(close)
    long = (close < lower) & (before >= _previous(lower))
    short = (close > upper) & (before <= _previous(upper))
    return _signals(long, short, (middle - close) / pip, (close - middle) / pip)


# --- trend following -----------------------------------------------------------------------


def _ema_momentum(bars: Bars, p: Values, pip: float) -> Signals:
    close = bars.close
    gap = ind.ema(close, int(p["fast"])) - ind.ema(close, int(p["slow"]))
    before = _previous(gap)
    long = (gap > 0) & (before <= 0)
    short = (gap < 0) & (before >= 0)
    if float(p["adx_min"]) > 0:  # only when the market is trending at all
        trending = ind.adx(bars.high, bars.low, close, int(p["adx_period"]))[0] >= float(p["adx_min"])
        long, short = long & trending, short & trending
    reach = float(p["atr_multiple"]) * ind.atr(bars.high, bars.low, close, int(p["atr_period"])) / pip
    return _signals(long, short, reach, reach)


def _check_ema(p: Values) -> None:
    if int(p["fast"]) >= int(p["slow"]):
        raise ValueError("fast must be shorter than slow")


#: where and when the sessions open (wall clock of the place, so daylight saving is followed)
SESSIONS: dict[str, tuple[str, str]] = {
    "london": ("Europe/London", "08:00"),
    "new_york": ("America/New_York", "08:00"),
    "tokyo": ("Asia/Tokyo", "09:00"),
}


def _session_breakout(bars: Bars, p: Values, pip: float) -> Signals:
    """First close outside the range of the hours before a session opens."""
    n = len(bars)
    direction = np.zeros(n, dtype=np.int8)
    potential = np.full(n, np.nan)
    if n == 0:
        return Signals(direction, potential)
    tz, opens = SESSIONS[str(p["session"])]
    range_ms = int(float(p["range_hours"]) * HOUR_MS)  # at most 12 hours each (see the parameters),
    window_ms = int(float(p["window_hours"]) * HOUR_MS)  # so the two always fit into one day
    # a clock on which every session starts at 0: [0, range) is the quiet stretch before the
    # open, [range, range + window) the time in which a breakout counts
    clock = bars.ts + utc_offsets_ms(bars.ts, tz) - clock_ms(opens) + range_ms
    day, phase = clock // DAY_MS, clock % DAY_MS
    starts = np.flatnonzero(np.concatenate(([True], day[1:] != day[:-1])))
    ends = np.concatenate((starts[1:], [n]))
    needed = float(p["min_range_bars"])
    for a, b in zip(starts, ends, strict=True):
        ph = phase[a:b]
        quiet = int(np.searchsorted(ph, range_ms, side="left"))  # bars of the range come first
        last = int(np.searchsorted(ph, range_ms + window_ms, side="left"))
        if quiet < needed or last <= quiet:
            continue
        top = float(bars.high[a : a + quiet].max())
        bottom = float(bars.low[a : a + quiet].min())
        close = bars.close[a + quiet : a + last]
        height = (top - bottom) / pip
        for side, outside in ((1, close > top), (-1, close < bottom)):
            where = np.flatnonzero(outside)
            if len(where):  # the first close beyond the range, once a day in each direction
                at = a + quiet + int(where[0])
                direction[at] = side
                potential[at] = height
    return Signals(direction, potential)


STRATEGIES: dict[str, Strategy] = {
    s.key: s
    for s in (
        Strategy(
            key="rsi_reversion",
            title="RSI reversion",
            summary=(
                "Buys when the RSI drops into the oversold zone and sells when it rises into the overbought "
                "zone, expecting the price to come back to its moving average.  Potential = distance to that average."
            ),
            timeframe="5m",
            params=(
                Param("rsi_period", "RSI period (bars)", 14, "int", 2, 200),
                Param("oversold", "Oversold below", 30.0, "float", 1, 49),
                Param("overbought", "Overbought above", 70.0, "float", 51, 99),
                Param(
                    "trigger",
                    "Signal when the RSI",
                    "enter",
                    "choice",
                    choices=("enter", "leave"),
                    help="enter: as it falls into the zone.  leave: as it comes back out (later, the move has turned).",
                ),
                Param(
                    "mean_period",
                    "Moving average (bars)",
                    20,
                    "int",
                    2,
                    500,
                    help="the level the price is expected back at",
                ),
            ),
            compute=_rsi_reversion,
        ),
        Strategy(
            key="bollinger_reversion",
            title="Bollinger reversion",
            summary=(
                "Buys when a bar closes below the lower Bollinger band and sells when one closes above the upper "
                "band, expecting a return to the middle of the bands.  Potential = distance to the middle band."
            ),
            timeframe="5m",
            params=(
                Param("period", "Band period (bars)", 20, "int", 5, 500),
                Param("width", "Band width (standard deviations)", 2.0, "float", 0.5, 5),
            ),
            compute=_bollinger_reversion,
        ),
        Strategy(
            key="ema_momentum",
            title="EMA momentum",
            summary=(
                "Buys when the fast EMA crosses above the slow one and sells when it crosses below, optionally only "
                "while the ADX shows a trend.  Potential = a multiple of the average true range."
            ),
            timeframe="5m",
            params=(
                Param("fast", "Fast EMA (bars)", 12, "int", 2, 200),
                Param("slow", "Slow EMA (bars)", 48, "int", 3, 1000),
                Param("adx_min", "Minimum ADX (0 = off)", 0.0, "float", 0, 100),
                Param("adx_period", "ADX period (bars)", 14, "int", 2, 200),
                Param("atr_period", "ATR period (bars)", 14, "int", 2, 200),
                Param("atr_multiple", "Potential = ATR x", 3.0, "float", 0.1, 50),
            ),
            compute=_ema_momentum,
            check=_check_ema,
        ),
        Strategy(
            key="session_breakout",
            title="Session breakout",
            summary=(
                "Measures the range of the hours before a session opens and trades the first close outside it "
                "during the hours after the open, once a day in each direction.  Potential = height of that range."
            ),
            timeframe="5m",
            params=(
                Param("session", "Session", "london", "choice", choices=tuple(SESSIONS)),
                Param("range_hours", "Range: hours before the open", 5.0, "float", 0.5, 12),
                Param("window_hours", "Trade for hours after the open", 3.0, "float", 0.25, 12),
                Param(
                    "min_range_bars",
                    "Bars needed in the range",
                    12,
                    "int",
                    1,
                    10_000,
                    help="a range built from fewer bars (a holiday, a hole in the data) is not traded",
                ),
            ),
            compute=_session_breakout,
        ),
    )
}


def get_strategy(key: str) -> Strategy:
    try:
        return STRATEGIES[key]
    except KeyError:
        raise ValueError(f"unknown strategy {key!r}; there are: {', '.join(STRATEGIES)}") from None
