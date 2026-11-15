"""The backtest engine: turns signals into trades the way they could really have been made.

Rules (every one of them errs against the strategy when the data cannot decide):

**Prices.**  Bars are bid prices.  A buy is filled at the ask (bid + spread), a sell at the
bid.  All distances (target, stop, the step between clips) are measured between prices that
were actually traded, so they are *net* of the spread: a 5 pip target means the position is
closed 5 pips better than its average entry, spread already paid.

**Timing.**  A signal belongs to a bar that has closed.  The order is filled at the open of
the first bar that starts after the close plus the reaction delay - never inside the bar
that produced the signal.

**One position at a time**, built from clips.  The first clip is the entry.  While the
position is open and the price moves against it by ``add_step_pips`` from the last clip, a
further clip is added, up to ``max_clips``.  Adding lowers the average entry of a buy.

**Exits**, for the whole position at once:

* target: ``target_pips`` beyond the average entry (the level moves when a clip is added);
* stop: ``stop_pips`` beyond the price of the last clip that *could* be added (a fixed level);
* time: after ``max_hold_minutes``;
* end of day: at ``flat_time`` in the session's time zone, before the trading day ends.  Between
  that time and the end of the day (``day_end``) no position is opened either.

**Inside one bar** the order of events is unknown, so the worse order is assumed:

* stop and target both touched: the stop was first;
* the bar reaches the stop: every clip that could still be added was added on the way;
* target touched and clips touched: the target was first (the smaller position took the profit);
* clips touched, target not: the clips are filled, and the nearer target they create is only
  looked at from the next bar on;
* a bar that opens beyond the stop is closed at its open, not at the stop;
* a target is filled at its level, never better, even when the bar opens beyond it.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, fields
from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import numpy.typing as npt

from fxlab.research.bars import Bars, Floats
from fxlab.timeutil import DAY_MS, HOUR_MS, MINUTE_MS, SECOND_MS, dt_to_ms, ms_to_dt

_DECIMALS = 7  # prices are rounded to this many decimals so that equal levels compare equal


def clock_ms(value: str) -> int:
    """'HH:MM' -> milliseconds after midnight."""
    try:
        hours, minutes = value.split(":")
        result = int(hours) * HOUR_MS + int(minutes) * MINUTE_MS
    except (ValueError, AttributeError):
        raise ValueError(f"not a time of day (HH:MM): {value!r}") from None
    if not (0 <= result < DAY_MS and 0 <= int(minutes) < 60):
        raise ValueError(f"not a time of day (HH:MM): {value!r}")
    return result


@dataclass(frozen=True)
class Execution:
    """How signals are traded.

    The defaults are the owner's rules of 15 November 2026: one clip per position (no adding
    to a position that has gone against it), and a stop as near as the target.  The rules
    of an instrument come from its configuration (``rules`` in config/instruments.yaml).
    """

    pip_size: float = 0.0001
    spread_pips: float = 1.0  # the venue's typical spread is 0.5; 1.0 is the cautious figure
    slippage_pips: float = 0.0  # on stops and on timed exits (market orders)
    commission_per_million: float = 0.0  # quote currency per 1,000,000 of base currency, per side
    clip_units: float = 1_000_000.0
    max_clips: int = 1  # 1 = never add to a position
    add_step_pips: float = 5.0  # with more than one clip: the distance between them
    target_pips: float = 5.0
    stop_pips: float = 5.0  # beyond the last clip that could be added; with one clip: beyond the entry
    min_potential_pips: float = 5.0  # a signal that sees less than this after the spread is not taken
    reaction_delay_s: float = 5.0
    max_wait_s: float = 120.0  # no bar this long after the planned fill: the signal is dropped
    max_hold_minutes: float = 240.0
    cooldown_minutes: float = 5.0
    entry_start_utc: str = "06:00"
    entry_end_utc: str = "20:00"
    flat_time: str = "16:45"  # in ``session_tz``
    day_end: str = "17:00"  # in ``session_tz``: where one trading day ends and the next begins
    session_tz: str = "America/New_York"
    long: bool = True
    short: bool = True

    def __post_init__(self) -> None:
        if self.max_clips < 1:
            raise ValueError("max_clips must be at least 1")
        for name in ("pip_size", "clip_units", "add_step_pips", "target_pips", "stop_pips", "max_hold_minutes"):
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be positive")
        for name in (
            "spread_pips",
            "slippage_pips",
            "commission_per_million",
            "reaction_delay_s",
            "max_wait_s",
            "cooldown_minutes",
            "min_potential_pips",
        ):
            if not getattr(self, name) >= 0:
                raise ValueError(f"{name} must not be negative")
        for name in ("entry_start_utc", "entry_end_utc", "flat_time", "day_end"):
            clock_ms(getattr(self, name))
        try:
            ZoneInfo(self.session_tz)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown time zone {self.session_tz!r}") from None

    @classmethod
    def from_mapping(cls, values: dict[str, Any], **base: Any) -> Execution:
        """Build from loosely typed input (a form, a command line).  Unknown names are an error."""
        kinds = {f.name: f.type for f in fields(cls)}
        out: dict[str, Any] = dict(base)
        for name, raw in values.items():
            if name not in kinds:
                raise ValueError(f"unknown execution setting {name!r}; known: {', '.join(sorted(kinds))}")
            kind = kinds[name]
            try:
                if kind == "bool":
                    if isinstance(raw, str):
                        if raw.strip().lower() not in ("true", "false", "yes", "no", "1", "0", "on", "off"):
                            raise ValueError
                        out[name] = raw.strip().lower() in ("true", "yes", "1", "on")
                    else:
                        out[name] = bool(raw)
                elif kind == "int":
                    number = float(raw)
                    if number != int(number):
                        raise ValueError
                    out[name] = int(number)
                elif kind == "float":
                    out[name] = float(raw)
                    if not np.isfinite(out[name]):
                        raise ValueError
                else:
                    out[name] = str(raw)
            except (TypeError, ValueError):
                raise ValueError(f"{name}: {raw!r} is not a valid value") from None
        return cls(**out)


@dataclass(frozen=True)
class Signals:
    """What a strategy says for each of its bars, known when that bar has closed."""

    #: +1 buy, -1 sell, 0 nothing
    direction: npt.NDArray[np.int8]
    #: distance in pips from the bar's close to where the strategy expects the price to go
    #: (bid to bid, before the spread); nan = the strategy cannot say
    potential_pips: Floats


@dataclass(frozen=True)
class Fill:
    ts_ms: int
    price: float
    units: float


@dataclass(frozen=True)
class Trade:
    direction: int  # +1 long, -1 short
    signal_ms: int  # close of the bar that gave the signal
    fills: tuple[Fill, ...]  # the entry, then the clips added
    exit_ms: int
    exit_price: float
    reason: str  # target | stop | time | end_of_day | end_of_data
    stop_price: float  # the stop of the whole position (a price on the exit side)
    target_price: float  # the target at the moment of the exit
    pnl: float  # quote currency, after spread, slippage and commission
    potential_pips: float  # what the signal saw, after the spread
    adverse_pips: float  # the furthest the exit side went against the first entry while open

    @property
    def entry_ms(self) -> int:
        return self.fills[0].ts_ms

    @property
    def units(self) -> float:
        return sum(f.units for f in self.fills)

    @property
    def clips(self) -> int:
        return len(self.fills)

    @property
    def average_price(self) -> float:
        return sum(f.price * f.units for f in self.fills) / self.units


#: why a signal was not traded (the keys of ``Outcome.counters`` next to "signals" and "traded")
SKIP_OFF = "skipped: direction switched off"
SKIP_POTENTIAL = "skipped: potential below the minimum"
SKIP_BUSY = "skipped: a position was open or cooling down"
SKIP_HOURS = "skipped: outside the entry hours"
SKIP_NO_PRICE = "skipped: no price after the signal"
SKIP_DAY_END = "skipped: too close to the end of the day"


@dataclass
class Outcome:
    trades: list[Trade] = field(default_factory=list)
    #: how many signals there were and why some were not traded
    counters: Counter[str] = field(default_factory=Counter)
    #: what became of each signal, in order: (index of its bar, "traded" or the reason it was skipped);
    #: the n-th "traded" entry belongs to the n-th trade
    decisions: list[tuple[int, str]] = field(default_factory=list)

    def _skip(self, bar: int, reason: str) -> None:
        self.counters[reason] += 1
        self.decisions.append((bar, reason))


def since(outcome: Outcome, signal_bars: Bars, start_ms: int) -> Outcome:
    """The part of an outcome that belongs to the time from ``start_ms`` on.

    A run may begin before the period it is asked about, so that indicators have settled and
    the engine knows whether a position was open when the period began.  What was signalled
    before the period is not part of its result: a signal counts from the moment it was known.
    """
    kept = Outcome()
    kept.counters.update({"signals": 0, "traded": 0})
    span = signal_bars.span_ms
    for bar, decision in outcome.decisions:
        if int(signal_bars.ts[bar]) + span >= start_ms:
            kept.counters["signals"] += 1
            kept.counters[decision] += 1
            kept.decisions.append((bar, decision))
    kept.trades = [trade for trade in outcome.trades if trade.signal_ms >= start_ms]
    return kept


def next_clock_ms(ts_ms: int, time_of_day: str, tz: ZoneInfo) -> int:
    """The first moment at or after ``ts_ms`` at which the wall clock of ``tz`` shows ``time_of_day``.

    Daylight saving is followed: 16:45 in New York is 20:45 UTC in summer and 21:45 in winter.
    """
    clock = clock_ms(time_of_day)
    local = ms_to_dt(ts_ms).astimezone(tz)
    candidate = local.replace(
        hour=clock // HOUR_MS, minute=clock % HOUR_MS // MINUTE_MS, second=0, microsecond=0, fold=0
    )
    if dt_to_ms(candidate) < ts_ms:
        candidate += timedelta(days=1)  # wall-clock arithmetic: the same time of day tomorrow
    return dt_to_ms(candidate)


def _in_window(ts_ms: int, start: int, end: int) -> bool:
    tod = ts_ms % DAY_MS
    return start <= tod < end if start <= end else (tod >= start or tod < end)


def run(base: Bars, signal_bars: Bars, signals: Signals, ex: Execution) -> Outcome:
    """Trade ``signals`` (one entry per bar of ``signal_bars``) on the prices of ``base``."""
    if len(signals.direction) != len(signal_bars) or len(signals.potential_pips) != len(signal_bars):
        raise ValueError("signals and signal bars differ in length")
    out = Outcome()
    out.counters.update({"signals": 0, "traded": 0})  # always present, also when nothing happened
    ts = base.ts
    n = len(ts)
    start, end = clock_ms(ex.entry_start_utc), clock_ms(ex.entry_end_utc)
    tz = ZoneInfo(ex.session_tz)
    delay_ms = int(ex.reaction_delay_s * SECOND_MS)
    max_wait_ms = int(ex.max_wait_s * SECOND_MS)
    cooldown_ms = int(ex.cooldown_minutes * MINUTE_MS)
    free_at = -(2**62)
    free_index = 0  # the first bar in which a new position may be opened

    for i in np.flatnonzero(signals.direction != 0):
        direction = 1 if signals.direction[i] > 0 else -1
        signal_ms = int(signal_bars.ts[i]) + signal_bars.span_ms
        out.counters["signals"] += 1
        bar = int(i)
        if not (ex.long if direction > 0 else ex.short):
            out._skip(bar, SKIP_OFF)
            continue
        potential = float(signals.potential_pips[i]) - ex.spread_pips
        if not potential >= ex.min_potential_pips:  # also true for nan: the strategy cannot say
            out._skip(bar, SKIP_POTENTIAL)
            continue
        if signal_ms < free_at:
            out._skip(bar, SKIP_BUSY)
            continue
        if not _in_window(signal_ms, start, end):
            out._skip(bar, SKIP_HOURS)
            continue
        planned = signal_ms + delay_ms
        j = int(np.searchsorted(ts, planned, side="left"))
        if j >= n or int(ts[j]) - planned > max_wait_ms:
            out._skip(bar, SKIP_NO_PRICE)
            continue
        # the trading day of the signal ends at the next ``day_end``; positions are closed at the
        # ``flat_time`` before it, and a signal between the two belongs to no tradable time
        deadline = next_clock_ms(signal_ms, ex.flat_time, tz)
        if deadline > next_clock_ms(signal_ms + 1, ex.day_end, tz) or int(ts[j]) >= deadline:
            out._skip(bar, SKIP_DAY_END)
            continue
        if j < free_index:  # the previous position is still open in this bar
            out._skip(bar, SKIP_BUSY)
            continue
        trade, free_index = _trade(base, j, direction, signal_ms, deadline, potential, ex)
        out.trades.append(trade)
        out.counters["traded"] += 1
        out.decisions.append((bar, "traded"))
        free_at = trade.exit_ms + cooldown_ms
    return out


def _trade(
    base: Bars, j: int, d: int, signal_ms: int, flat_ms: int, potential: float, ex: Execution
) -> tuple[Trade, int]:
    """One position from its entry at the open of bar ``j`` to its exit, and the first bar free for the next one.

    Every level is turned into the *bid* that triggers it once, and bars (which are bids) are
    only ever compared with those triggers - the same comparison when searching for the bar
    and when deciding what happened in it.
    """
    ts, o, h, lo, c = base.ts, base.open, base.high, base.low, base.close
    n = len(ts)
    pip = ex.pip_size

    def price(value: float) -> float:
        return round(float(value), _DECIMALS)

    spread, slip, step = price(ex.spread_pips * pip), price(ex.slippage_pips * pip), price(ex.add_step_pips * pip)
    units = ex.clip_units
    # a long is bought at the ask and sold at the bid; a short is sold at the bid and bought at the ask
    buy_side = spread if d > 0 else 0.0  # bid -> the price a clip trades at
    exit_side = 0.0 if d > 0 else spread  # bid -> the price the exit trades at

    entry = price(o[j] + buy_side)
    fills = [Fill(int(ts[j]), entry, units)]
    adds_left = ex.max_clips - 1
    next_add = price(entry - d * step)
    stop = price(entry - d * (adds_left * step + ex.stop_pips * pip))  # fixed for the life of the position
    target = price(entry + d * ex.target_pips * pip)
    stop_bid = price(stop - exit_side)

    hold_ms = int(ts[j]) + int(ex.max_hold_minutes * MINUTE_MS)
    k_end = int(np.searchsorted(ts, min(hold_ms, flat_ms), side="left"))  # first bar at or after the deadline
    against, toward = (lo, h) if d > 0 else (h, lo)  # the side of a bar that hurts, the side that helps

    def reached(values: Any, level: float, adverse: bool) -> Any:
        """Did the bid get to ``level``?  Adverse levels lie below a long and above a short."""
        return values <= level if (d > 0) == adverse else values >= level

    reason, exit_price, exit_index = "", 0.0, j
    m = j
    while m < k_end:
        target_bid = price(target - exit_side)
        add_bid = price(next_add - buy_side)
        hit = reached(against[m:k_end], stop_bid, True) | reached(toward[m:k_end], target_bid, False)
        if adds_left:
            hit |= reached(against[m:k_end], add_bid, True)
        where = np.flatnonzero(hit)
        if len(where) == 0:
            break
        q = m + int(where[0])
        gapped = q > j and bool(reached(o[q], stop_bid, True))  # opened beyond the stop
        if gapped or reached(against[q], stop_bid, True):
            while adds_left:  # worst case: every clip was added before the stop was reached
                fills.append(Fill(int(ts[q]), next_add, units))
                adds_left -= 1
                next_add = price(next_add - d * step)
            exit_price = price((float(o[q]) + exit_side if gapped else stop) - d * slip)
            reason, exit_index = "stop", q
            break
        if reached(toward[q], target_bid, False):
            exit_price, reason, exit_index = target, "target", q
            break
        while adds_left and reached(against[q], price(next_add - buy_side), True):
            fills.append(Fill(int(ts[q]), next_add, units))
            adds_left -= 1
            next_add = price(next_add - d * step)
        total = sum(f.units for f in fills)
        target = price(sum(f.price * f.units for f in fills) / total + d * ex.target_pips * pip)
        m = q + 1

    free_index = exit_index + 1  # a stop or a target happens inside its bar
    if not reason:
        if k_end < n:  # a market order at the open of the first bar after the deadline
            exit_index = free_index = k_end  # closed at that bar's open: the bar itself is free again
            exit_price = price(float(o[k_end]) + exit_side - d * slip)
            reason = "end_of_day" if flat_ms <= hold_ms else "time"
        else:  # the data ends while the position is open
            exit_index, free_index = n - 1, n
            exit_price = price(float(c[n - 1]) + exit_side - d * slip)
            reason = "end_of_data"

    total = sum(f.units for f in fills)
    pnl = sum(d * f.units * (exit_price - f.price) for f in fills)
    pnl -= 2 * ex.commission_per_million * total / 1_000_000
    # the worst the exit side stood against the first entry: whole bars while the position was
    # open, and of the bar it left in only what is known to have happened before the exit
    worst = exit_price
    if reason == "target":
        worst = float(o[exit_index]) + exit_side if exit_index > j else entry - d * spread
    before = against[j:exit_index] if reason != "end_of_data" else against[j : exit_index + 1]
    if len(before):
        extreme = float(before.min() if d > 0 else before.max()) + exit_side
        worst = min(worst, extreme) if d > 0 else max(worst, extreme)
    trade = Trade(
        direction=d,
        signal_ms=signal_ms,
        fills=tuple(fills),
        exit_ms=int(ts[exit_index]),
        exit_price=exit_price,
        reason=reason,
        stop_price=stop,
        target_price=target,
        pnl=float(pnl),
        potential_pips=potential,
        adverse_pips=max(0.0, d * (entry - worst) / pip),
    )
    return trade, free_index
