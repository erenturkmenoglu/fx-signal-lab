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

#: the two camps the families belong to.  A method of one camp fails where the other works: mean
#: reversion while a move goes on, momentum while the price keeps coming back.
STYLES = {"reversion": "reversion", "level": "reversion", "trend": "momentum", "breakout": "momentum"}


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
    short: str = ""  # a few letters for marks on a chart
    family: str = "reversion"  # reversion | trend | breakout | level | time | benchmark
    #: its signals follow the clock, not the shape of the bars: one bar length says all there is
    clock_only: bool = False

    @property
    def style(self) -> str:
        """What the strategy bets on: that the price comes back (reversion) or that it goes on (momentum)."""
        return STYLES.get(self.family, "other")

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
            "short": self.short or self.key,
            "family": self.family,
            "style": self.style,
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
    "europe": ("Europe/Berlin", "08:00"),
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
    # how many bars a full range would have, and the share of them that must be there
    needed = float(p["min_coverage"]) * range_ms / bars.span_ms
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


# --- more oscillators ----------------------------------------------------------------------


def _stochastic_reversion(bars: Bars, p: Values, pip: float) -> Signals:
    n = int(p["period"])
    k, d = ind.stochastic(bars.high, bars.low, bars.close, n, int(p["smooth"]), int(p["signal"]))
    k_before, d_before = _previous(k), _previous(d)
    low, high = float(p["oversold"]), float(p["overbought"])
    # %K turns up through %D while still in the oversold zone, or down through it in the overbought zone
    long = (k > d) & (k_before <= d_before) & (k_before < low)
    short = (k < d) & (k_before >= d_before) & (k_before > high)
    middle = (ind.rolling_max(bars.high, n) + ind.rolling_min(bars.low, n)) / 2.0
    return _signals(long, short, (middle - bars.close) / pip, (bars.close - middle) / pip)


def _cci_reversion(bars: Bars, p: Values, pip: float) -> Signals:
    n = int(p["period"])
    cci = ind.cci(bars.high, bars.low, bars.close, n)
    before = _previous(cci)
    level = float(p["level"])
    if p["trigger"] == "enter":
        long = (cci < -level) & (before >= -level)
        short = (cci > level) & (before <= level)
    else:  # "leave": back inside the band
        long = (cci > -level) & (before <= -level)
        short = (cci < level) & (before >= level)
    mean = ind.sma((bars.high + bars.low + bars.close) / 3.0, n)
    return _signals(long, short, (mean - bars.close) / pip, (bars.close - mean) / pip)


def _reach(bars: Bars, p: Values, pip: float, period_name: str = "atr_period") -> Floats:
    """The potential of a trend signal: a multiple of the average true range, in pips."""
    return float(p["atr_multiple"]) * ind.atr(bars.high, bars.low, bars.close, int(p[period_name])) / pip


def _macd_cross(bars: Bars, p: Values, pip: float) -> Signals:
    line, signal, _ = ind.macd(bars.close, int(p["fast"]), int(p["slow"]), int(p["signal"]))
    gap = line - signal
    before = _previous(gap)
    long = (gap > 0) & (before <= 0)
    short = (gap < 0) & (before >= 0)
    if p["zero"] == "against":  # a turn from the far side of zero: buy only below it
        long, short = long & (line < 0), short & (line > 0)
    elif p["zero"] == "with":  # a continuation: buy only above it
        long, short = long & (line > 0), short & (line < 0)
    reach = _reach(bars, p, pip)
    return _signals(long, short, reach, reach)


def _dmi_cross(bars: Bars, p: Values, pip: float) -> Signals:
    adx, plus, minus = ind.adx(bars.high, bars.low, bars.close, int(p["period"]))
    gap = plus - minus
    before = _previous(gap)
    trending = adx >= float(p["adx_min"])
    long = (gap > 0) & (before <= 0) & trending
    short = (gap < 0) & (before >= 0) & trending
    reach = _reach(bars, p, pip, "period")
    return _signals(long, short, reach, reach)


def _supertrend_flip(bars: Bars, p: Values, pip: float) -> Signals:
    _, direction = ind.supertrend(bars.high, bars.low, bars.close, int(p["period"]), float(p["multiple"]))
    before = _previous(direction)
    long = (direction > 0) & (before < 0)
    short = (direction < 0) & (before > 0)
    reach = _reach(bars, p, pip, "period")
    return _signals(long, short, reach, reach)


# --- breakouts -----------------------------------------------------------------------------


def _donchian_breakout(bars: Bars, p: Values, pip: float) -> Signals:
    n = int(p["period"])
    # the channel of the n bars before this one: a bar cannot break out of a range it is part of
    top = _previous(ind.rolling_max(bars.high, n))
    bottom = _previous(ind.rolling_min(bars.low, n))
    above, below = bars.close > top, bars.close < bottom
    was_above = np.concatenate(([False], above[:-1]))
    was_below = np.concatenate(([False], below[:-1]))
    height = (top - bottom) / pip
    return _signals(above & ~was_above, below & ~was_below, height, height)


def _run_length(flags: npt.NDArray[np.bool_]) -> npt.NDArray[np.int64]:
    """For each position, how many consecutive True values end there."""
    count = np.cumsum(flags)
    return np.asarray(count - np.maximum.accumulate(np.where(flags, 0, count)), dtype=np.int64)


def _squeeze_breakout(bars: Bars, p: Values, pip: float) -> Signals:
    n = int(p["period"])
    middle, bb_upper, bb_lower = ind.bollinger(bars.close, n, float(p["band_width"]))
    _, kc_upper, kc_lower = ind.keltner(bars.high, bars.low, bars.close, n, n, float(p["channel_width"]))
    squeezed = (bb_upper < kc_upper) & (bb_lower > kc_lower)  # false while either is still warming up
    lasted = np.concatenate(([0], _run_length(squeezed)[:-1]))  # length of the squeeze up to the previous bar
    released = ~squeezed & (lasted >= int(p["min_bars"])) & ~np.isnan(kc_upper)
    reach = float(p["atr_multiple"]) * ind.atr(bars.high, bars.low, bars.close, n) / pip
    return _signals(released & (bars.close > middle), released & (bars.close < middle), reach, reach)


# --- levels --------------------------------------------------------------------------------

#: where a trading day ends for the daily pivots (wall clock of the place)
DAY_ENDS: dict[str, tuple[str, str]] = {
    "new_york_close": ("America/New_York", "17:00"),
    "utc_midnight": ("UTC", "00:00"),
}


def _previous_day(bars: Bars, day_end: str) -> tuple[Floats, Floats, Floats]:
    """High, low and close of the trading day before the one each bar belongs to (nan on the first day)."""
    n = len(bars)
    out = (np.full(n, np.nan), np.full(n, np.nan), np.full(n, np.nan))
    if n == 0:
        return out
    tz, clock = DAY_ENDS[day_end]
    day = (bars.ts + utc_offsets_ms(bars.ts, tz) + (DAY_MS - clock_ms(clock)) % DAY_MS) // DAY_MS
    starts = np.flatnonzero(np.concatenate(([True], day[1:] != day[:-1])))
    ends = np.concatenate((starts[1:], [n]))
    highs = np.maximum.reduceat(bars.high, starts)
    lows = np.minimum.reduceat(bars.low, starts)
    closes = bars.close[ends - 1]
    for g in range(1, len(starts)):
        a, b = starts[g], ends[g]
        out[0][a:b], out[1][a:b], out[2][a:b] = highs[g - 1], lows[g - 1], closes[g - 1]
    return out


def _pivot_reversion(bars: Bars, p: Values, pip: float) -> Signals:
    high, low, close = _previous_day(bars, str(p["day_end"]))
    pivot = (high + low + close) / 3.0
    if int(p["level"]) == 1:
        support, resistance = 2 * pivot - high, 2 * pivot - low
    else:
        support, resistance = pivot - (high - low), pivot + (high - low)
    before = _previous(bars.close)
    # the bar dips to the support and closes back above it, coming from above; mirrored at the resistance
    long = (bars.low <= support) & (bars.close > support) & (before > support)
    short = (bars.high >= resistance) & (bars.close < resistance) & (before < resistance)
    return _signals(long, short, (pivot - bars.close) / pip, (bars.close - pivot) / pip)


def _round_number(bars: Bars, p: Values, pip: float) -> Signals:
    step = float(p["step_pips"])
    # prices in pips, rounded so that a price exactly on a level is on it
    close, high, low = (np.round(a / pip, 6) for a in (bars.close, bars.high, bars.low))
    before = _previous(close)
    expect = np.full(len(close), float(p["expect_pips"]))
    with np.errstate(invalid="ignore"):
        if p["mode"] == "break":  # a close on the other side of a level than the close before
            crossed = np.floor(close / step) != np.floor(before / step)
            long = crossed & (close > before)
            short = crossed & (close < before)
        else:  # "fade": the bar reaches the next level beyond the last close and closes back short of it
            zone = float(p["zone_pips"])
            above = np.floor(before / step) * step + step  # the next level above the last close
            below = np.ceil(before / step) * step - step  # the next level below it
            short = (high >= above - zone) & (close < above - zone) & (_previous(high) < above - zone)
            long = (low <= below + zone) & (close > below + zone) & (_previous(low) > below + zone)
    return _signals(long, short, expect, expect)


# --- the clock -----------------------------------------------------------------------------


def _session_drift(bars: Bars, p: Values, pip: float) -> Signals:
    """Sell when one session opens and buy when another does, every day."""
    n = len(bars)
    long = np.zeros(n, dtype=bool)
    short = np.zeros(n, dtype=bool)
    expect = np.full(n, float(p["expect_pips"]))
    if n:
        closes_at = bars.ts + bars.span_ms  # the moment each bar is complete
        for name, flags in ((str(p["sell_session"]), short), (str(p["buy_session"]), long)):
            tz, opens = SESSIONS[name]
            clock = closes_at + utc_offsets_ms(closes_at, tz) - clock_ms(opens)
            day, phase = clock // DAY_MS, clock % DAY_MS
            # the first bar complete at or after the open; the very first bar of the data is not
            # known to be one, and a first bar an hour late (a hole in the data) is not an open
            first = np.concatenate(([False], day[1:] != day[:-1]))
            np.logical_or(flags, first & (phase < HOUR_MS), out=flags)
    return _signals(long, short, expect, expect)


def _channel(bars: Bars, p: Values) -> tuple[Floats, Floats, Floats, Floats, int]:
    """The trend channel of the bars before each bar: (middle, upper, lower, rise over its length, bars)."""
    n = max(20, round(float(p["hours"]) * 3600 / bars.seconds))
    # the line of the n bars before this one, extended by one bar: a bar cannot break out of a
    # channel it is part of
    middle, upper, lower, slope = ind.regression_channel(bars.close, n, float(p["width"]), ahead=1)
    return middle, upper, lower, slope * (n - 1), n


def _channel_reversion(bars: Bars, p: Values, pip: float) -> Signals:
    """Back from the edge of the trend channel; by default never against the way the channel points."""
    middle, upper, lower, rise, _ = _channel(bars, p)
    long = (bars.low <= lower) & (bars.close > lower)  # dipped to the lower line and closed back inside
    short = (bars.high >= upper) & (bars.close < upper)
    if str(p["side"]) == "with":
        half = upper - middle
        # a channel that has fallen by more than half its own height is falling: no buying in it
        long, short = long & ~(rise < -half), short & ~(rise > half)
    return _signals(long, short, (middle - bars.close) / pip, (bars.close - middle) / pip)


def _channel_break(bars: Bars, p: Values, pip: float) -> Signals:
    """The first close outside the trend channel: the range the price was in no longer holds."""
    middle, upper, lower, _, _ = _channel(bars, p)
    above, below = bars.close > upper, bars.close < lower
    was_above = np.concatenate(([False], above[:-1]))
    was_below = np.concatenate(([False], below[:-1]))
    half = (upper - middle) / pip
    return _signals(above & ~was_above, below & ~was_below, half, half)


def _mix(values: npt.NDArray[np.uint64]) -> npt.NDArray[np.uint64]:
    """A fixed scrambling of whole numbers (the finishing step of MurmurHash3)."""
    x = values.copy()
    x ^= x >> np.uint64(33)
    x *= np.uint64(0xFF51AFD7ED558CCD)
    x ^= x >> np.uint64(33)
    x *= np.uint64(0xC4CEB9FE1A85EC53)
    x ^= x >> np.uint64(33)
    return x


def _random_entry(bars: Bars, p: Values, pip: float) -> Signals:
    """Buys or sells at random: what the trading rules alone produce, without any information."""
    slot = (bars.ts // bars.span_ms + int(p["seed"])).astype(np.uint64)  # the same bar always gets the same draw
    with np.errstate(over="ignore"):
        draw = _mix(slot)
    chosen = draw % np.uint64(int(p["every"])) == 0
    up = (draw >> np.uint64(40)) & np.uint64(1) == 1
    plenty = np.full(len(bars), 999.0)  # always passes the minimum potential
    return _signals(chosen & up, chosen & ~up, plenty, plenty)


STRATEGIES: dict[str, Strategy] = {
    s.key: s
    for s in (
        Strategy(
            key="rsi_reversion",
            short="RSI",
            family="reversion",
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
            short="BB",
            family="reversion",
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
            short="EMA",
            family="trend",
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
            short="SB",
            family="breakout",
            title="Session breakout",
            summary=(
                "Measures the range of the hours before a session opens and trades the first close outside it "
                "during the hours after the open, once a day in each direction.  Potential = height of that range."
            ),
            timeframe="5m",
            params=(
                Param("session", "Session", "london", "choice", choices=("london", "new_york", "tokyo")),
                Param("range_hours", "Range: hours before the open", 5.0, "float", 0.5, 12),
                Param("window_hours", "Trade for hours after the open", 3.0, "float", 0.25, 12),
                Param(
                    "min_coverage",
                    "Share of the range that must have bars",
                    0.5,
                    "float",
                    0.01,
                    1,
                    help="a range with fewer bars than this share (a holiday, a hole in the data) is not traded",
                ),
            ),
            compute=_session_breakout,
        ),
        Strategy(
            key="stochastic_reversion",
            short="STO",
            family="reversion",
            title="Stochastic reversion",
            summary=(
                "Buys when %K turns up through %D in the oversold zone and sells when it turns down through %D in "
                "the overbought zone.  Potential = distance to the middle of the range the oscillator is measured on."
            ),
            timeframe="5m",
            params=(
                Param("period", "Range (bars)", 14, "int", 3, 500),
                Param("smooth", "%K smoothing (bars)", 3, "int", 1, 50, help="1 = the fast stochastic"),
                Param("signal", "%D (bars)", 3, "int", 2, 50),
                Param("oversold", "Oversold below", 20.0, "float", 1, 49),
                Param("overbought", "Overbought above", 80.0, "float", 51, 99),
            ),
            compute=_stochastic_reversion,
        ),
        Strategy(
            key="cci_reversion",
            short="CCI",
            family="reversion",
            title="CCI reversion",
            summary=(
                "Buys when the Commodity Channel Index comes back from below -100 and sells when it comes back from "
                "above +100.  Potential = distance to the average the index is measured against."
            ),
            timeframe="5m",
            params=(
                Param("period", "Period (bars)", 20, "int", 5, 500),
                Param("level", "Extreme beyond +/-", 100.0, "float", 10, 500),
                Param(
                    "trigger",
                    "Signal when the CCI",
                    "leave",
                    "choice",
                    choices=("enter", "leave"),
                    help="enter: as it goes beyond the level.  leave: as it comes back inside.",
                ),
            ),
            compute=_cci_reversion,
        ),
        Strategy(
            key="macd_cross",
            short="MACD",
            family="trend",
            title="MACD cross",
            summary=(
                "Buys when the MACD line crosses above its signal line and sells when it crosses below.  "
                "Potential = a multiple of the average true range."
            ),
            timeframe="5m",
            params=(
                Param("fast", "Fast EMA (bars)", 12, "int", 2, 200),
                Param("slow", "Slow EMA (bars)", 26, "int", 3, 1000),
                Param("signal", "Signal line (bars)", 9, "int", 2, 200),
                Param(
                    "zero",
                    "Side of the zero line",
                    "any",
                    "choice",
                    choices=("any", "against", "with"),
                    help="against: buy only below zero (a turn).  with: buy only above zero (a continuation).",
                ),
                Param("atr_period", "ATR period (bars)", 14, "int", 2, 200),
                Param("atr_multiple", "Potential = ATR x", 3.0, "float", 0.1, 50),
            ),
            compute=_macd_cross,
            check=_check_ema,
        ),
        Strategy(
            key="dmi_cross",
            short="DMI",
            family="trend",
            title="DMI cross",
            summary=(
                "Buys when +DI crosses above -DI while the ADX shows a trend, and sells on the opposite cross.  "
                "Potential = a multiple of the average true range."
            ),
            timeframe="5m",
            params=(
                Param("period", "Period (bars)", 14, "int", 2, 200),
                Param("adx_min", "Minimum ADX", 20.0, "float", 0, 100),
                Param("atr_multiple", "Potential = ATR x", 3.0, "float", 0.1, 50),
            ),
            compute=_dmi_cross,
        ),
        Strategy(
            key="supertrend_flip",
            short="ST",
            family="trend",
            title="Supertrend flip",
            summary=(
                "Follows a line that trails the price by a multiple of the average true range: buys when a bar "
                "closes above it after a down-trend, sells when one closes below it after an up-trend.  "
                "Potential = a multiple of the average true range."
            ),
            timeframe="5m",
            params=(
                Param("period", "ATR period (bars)", 10, "int", 2, 200),
                Param("multiple", "Line at ATR x", 3.0, "float", 0.5, 20),
                Param("atr_multiple", "Potential = ATR x", 3.0, "float", 0.1, 50),
            ),
            compute=_supertrend_flip,
        ),
        Strategy(
            key="donchian_breakout",
            short="DON",
            family="breakout",
            title="Donchian breakout",
            summary=(
                "Buys on the first close above the highest high of the bars before it, sells on the first close "
                "below their lowest low.  Potential = height of that channel."
            ),
            timeframe="5m",
            params=(Param("period", "Channel (bars)", 48, "int", 2, 2000),),
            compute=_donchian_breakout,
        ),
        Strategy(
            key="squeeze_breakout",
            short="SQZ",
            family="breakout",
            title="Squeeze breakout",
            summary=(
                "Waits for a quiet stretch (Bollinger bands inside the Keltner channel) and trades its end: buys "
                "if the close is then above the average, sells if below.  Potential = a multiple of the average "
                "true range."
            ),
            timeframe="5m",
            params=(
                Param("period", "Period (bars)", 20, "int", 5, 500),
                Param("band_width", "Bollinger width (standard deviations)", 2.0, "float", 0.5, 5),
                Param("channel_width", "Keltner width (ATR x)", 1.5, "float", 0.5, 5),
                Param("min_bars", "Quiet for at least (bars)", 6, "int", 1, 500),
                Param("atr_multiple", "Potential = ATR x", 3.0, "float", 0.1, 50),
            ),
            compute=_squeeze_breakout,
        ),
        Strategy(
            key="pivot_reversion",
            short="PIV",
            family="level",
            title="Pivot reversion",
            summary=(
                "Daily floor pivots from the day before: buys when a bar dips to the support and closes back "
                "above it, sells when one pokes above the resistance and closes back below.  Potential = distance "
                "to the pivot."
            ),
            timeframe="5m",
            params=(
                Param("level", "Support / resistance", 1, "int", 1, 2, help="1 = S1 and R1, 2 = S2 and R2"),
                Param("day_end", "A day ends at", "new_york_close", "choice", choices=tuple(DAY_ENDS)),
            ),
            compute=_pivot_reversion,
        ),
        Strategy(
            key="round_number",
            short="RND",
            family="level",
            title="Round numbers",
            summary=(
                "Round price levels, where take-profit orders are known to cluster and stop-loss orders to sit just "
                "beyond.  Fade: trades back from a level that was reached but not held.  Break: follows a close "
                "through a level.  Potential = the move expected, a parameter."
            ),
            timeframe="5m",
            params=(
                Param("step_pips", "A level every (pips)", 50.0, "float", 5, 1000),
                Param("mode", "Trade", "fade", "choice", choices=("fade", "break")),
                Param("zone_pips", "Reached within (pips)", 2.0, "float", 0, 50, help="fade only"),
                Param("expect_pips", "Expected move (pips)", 10.0, "float", 0, 500),
            ),
            compute=_round_number,
        ),
        Strategy(
            key="channel_reversion",
            short="CHN",
            family="reversion",
            title="Trend channel, back from the edge",
            summary=(
                "A straight line through the last day of prices with a band around it (a regression channel).  "
                "Buys when a bar dips to the lower line and closes back inside, sells at the upper line; by "
                "default only with the direction of the channel, never into a falling one.  Potential = "
                "distance to the middle line."
            ),
            timeframe="5m",
            params=(
                Param(
                    "hours",
                    "Channel over (hours)",
                    24.0,
                    "float",
                    1,
                    720,
                    help="hours of trading the line is fitted to",
                ),
                Param("width", "Half-height (standard deviations)", 2.0, "float", 0.5, 5),
                Param(
                    "side",
                    "Trade",
                    "with",
                    "choice",
                    choices=("with", "both"),
                    help="with the channel only, or both edges",
                ),
            ),
            compute=_channel_reversion,
        ),
        Strategy(
            key="channel_break",
            short="CHB",
            family="breakout",
            title="Trend channel, break",
            summary=(
                "The same channel, the other bet: buys on the first close above the upper line and sells on "
                "the first close below the lower one.  A break says the range of the last day no longer "
                "holds.  Potential = half the height of the channel."
            ),
            timeframe="5m",
            params=(
                Param(
                    "hours",
                    "Channel over (hours)",
                    24.0,
                    "float",
                    1,
                    720,
                    help="hours of trading the line is fitted to",
                ),
                Param("width", "Half-height (standard deviations)", 2.0, "float", 0.5, 5),
            ),
            compute=_channel_break,
        ),
        Strategy(
            key="session_drift",
            short="TOD",
            family="time",
            clock_only=True,
            title="Time of day",
            summary=(
                "Trades the clock, not the price: sells when one session opens and buys when another does, on the "
                "published finding that a currency tends to weaken during its own working hours.  For EUR/USD: sell "
                "at the European open, buy at the New York open.  Potential = the move expected, a parameter."
            ),
            timeframe="5m",
            params=(
                Param("sell_session", "Sell at the open of", "europe", "choice", choices=tuple(SESSIONS)),
                Param("buy_session", "Buy at the open of", "new_york", "choice", choices=tuple(SESSIONS)),
                Param("expect_pips", "Expected move (pips)", 8.0, "float", 0, 500),
            ),
            compute=_session_drift,
        ),
        Strategy(
            key="random_entry",
            short="RAND",
            family="benchmark",
            title="Random entry (benchmark)",
            summary=(
                "Buys or sells at random moments.  It knows nothing, so its result is what the trading rules and "
                "the costs produce on their own: the line every real method has to beat."
            ),
            timeframe="5m",
            params=(
                Param("every", "One signal in (bars)", 12, "int", 1, 10_000),
                Param("seed", "Seed", 1, "int", 0, 1_000_000),
            ),
            compute=_random_entry,
        ),
    )
}


def get_strategy(key: str) -> Strategy:
    try:
        return STRATEGIES[key]
    except KeyError:
        raise ValueError(f"unknown strategy {key!r}; there are: {', '.join(STRATEGIES)}") from None
