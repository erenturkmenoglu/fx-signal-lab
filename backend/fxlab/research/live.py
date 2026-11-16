"""Live signals on the one-second grid: the rule, its settings and the trend channels.

The rule reads two indicators on the **bid** at the close of each bar: Wilder's RSI and
Bollinger bands, the same functions the backtests use (``fxlab.research.indicators``).  A bar
is one second unless the settings say 5, 15, 30 or 60.

* A **buy** is signalled at the bar on which "RSI at or below its lower level" and "bid at or
  below the lower band" become true together (``rule = "both"``), or on which either of them
  does (``rule = "either"``).  A **sell** is the mirror image, with the upper level and band.
* A signal is raised once, at the bar on which its condition turns from false to true, and
  not again while the condition stays true.
* Bands need movement.  When the price did not change at all over the bars the bands are
  built from, both bands are the price itself, and which side of them the price is on would
  be a matter of rounding.  Such a bar meets neither band.
* Only prices that may feed signals count.  A bar whose closing second is stale, missing or
  outside market hours ends the run of bars: the indicators start again after it, and nothing
  is signalled until they are defined on bars of the new run alone.

Two ways to the same answer:

* :func:`replay` works a whole stretch of seconds at once (the day so far, when a page opens);
* :class:`Stepper` takes one second at a time (what runs while prices arrive).

They agree value for value, and the tests hold them to it: a signal raised live is the signal
a later replay of the same stored seconds finds.  Every indicator value depends on the bars of
its run up to that bar and on nothing later.

Nothing here sends an order, sizes a position or works out a profit.  A signal says that the
condition of the rule became true at that second, and at which bid and ask.
"""

from __future__ import annotations

import itertools
import math
from collections import deque
from dataclasses import asdict, dataclass, fields
from typing import Any

import numpy as np
import numpy.typing as npt

from fxlab.research import indicators as ind
from fxlab.timeutil import SECOND_MS

Floats = npt.NDArray[np.float64]
Ints = npt.NDArray[np.int64]
Bools = npt.NDArray[np.bool_]

#: the bar lengths the rule can read, in seconds
BAR_SECONDS = (1, 5, 15, 30, 60)
RULES = ("both", "either")
#: a trend channel is not drawn through fewer bars than this
MIN_CHANNEL_BARS = 10
#: the trend channels are fitted to one-minute bars
CHANNEL_BAR_MINUTES = 1

#: what each setting may be: (lowest, highest)
LIMITS: dict[str, tuple[float, float]] = {
    "rsi_period": (2, 1000),
    "rsi_buy_below": (1, 99),
    "rsi_sell_above": (1, 99),
    "bollinger_period": (2, 2000),
    "bollinger_width": (0.1, 10),
    "channel_short_hours": (0.25, 48),
    "channel_medium_hours": (1, 240),
    "channel_long_days": (1, 30),
    "channel_width": (0.5, 5),
}
_WHOLE = {"bar_seconds", "rsi_period", "bollinger_period"}


class LiveError(ValueError):
    """The settings cannot be used as given."""


def band_reach(period: int) -> float:
    """The furthest one of ``period`` values can lie from their average, in their standard deviations.

    It is the square root of ``period - 1``, reached when all the other values are equal.  Bands
    as wide as that or wider cannot be met by any price.
    """
    return math.sqrt(period - 1)


@dataclass(frozen=True)
class LiveSettings:
    """What the owner can change.  The defaults are the textbook ones, on one-second bars."""

    bar_seconds: int = 1
    rsi_period: int = 14
    rsi_buy_below: float = 30.0
    rsi_sell_above: float = 70.0
    bollinger_period: int = 20
    bollinger_width: float = 2.0
    rule: str = "both"  # both: RSI and Bollinger have to agree.  either: one of them is enough
    channel_short_hours: float = 1.0
    channel_medium_hours: float = 24.0
    channel_long_days: float = 10.0
    channel_width: float = 2.0  # standard deviations on either side of the line

    @classmethod
    def from_mapping(cls, data: Any) -> LiveSettings:
        """Settings from what a form or a stored value holds; whatever is left out keeps its default."""
        if not isinstance(data, dict):
            raise LiveError("the settings must be an object of names and values")
        names = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - names)
        if unknown:
            raise LiveError(f"unknown setting(s): {', '.join(unknown)}; known: {', '.join(sorted(names))}")
        values: dict[str, Any] = {}
        for name, given in data.items():
            if name == "rule":
                if given not in RULES:
                    raise LiveError(f"rule must be one of: {', '.join(RULES)}")
                values[name] = given
                continue
            if isinstance(given, bool) or not isinstance(given, (int, float)) or not math.isfinite(given):
                raise LiveError(f"{name} must be a number")
            if name in _WHOLE and given != int(given):
                raise LiveError(f"{name} must be a whole number")
            value = int(given) if name in _WHOLE else float(given)
            if name == "bar_seconds":
                if value not in BAR_SECONDS:
                    raise LiveError(f"bar_seconds must be one of: {', '.join(str(s) for s in BAR_SECONDS)}")
            else:
                low, high = LIMITS[name]
                if not low <= value <= high:
                    raise LiveError(f"{name} must be between {low:g} and {high:g}")
            values[name] = value
        settings = cls(**values)
        if not settings.rsi_buy_below < settings.rsi_sell_above:
            raise LiveError("rsi_buy_below must be lower than rsi_sell_above")
        reach = band_reach(settings.bollinger_period)
        if settings.bollinger_width >= reach:
            raise LiveError(
                f"bollinger_width must be below {reach:.2f} with a bollinger_period of {settings.bollinger_period}: "
                f"among {settings.bollinger_period} prices none can be further from their average than that, "
                "so the bands would never be met"
            )
        short, medium, long = settings.channel_bars()
        if not short < medium < long:
            raise LiveError("the channels must grow: short shorter than medium, medium shorter than long")
        return settings

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def channel_bars(self) -> tuple[int, int, int]:
        """The three channel lengths in one-minute bars: short, medium, long."""
        per_hour = 60 // CHANNEL_BAR_MINUTES
        return (
            round(self.channel_short_hours * per_hour),
            round(self.channel_medium_hours * per_hour),
            round(self.channel_long_days * 24 * per_hour),
        )

    @property
    def first_signal_at(self) -> int:
        """Position in a run of bars of the first bar that can carry a signal (the first is 0).

        Both indicators have to be defined on that bar and on the one before it: the RSI from
        position ``rsi_period``, the bands from position ``bollinger_period - 1``.
        """
        return max(self.rsi_period, self.bollinger_period - 1) + 1


def describe() -> dict[str, Any]:
    """What a settings form needs: the defaults and what each field may be."""
    return {
        "defaults": LiveSettings().as_dict(),
        # besides these: the buy level below the sell level, the channels growing in length, and the
        # width of the bands below the square root of (their period - 1), see band_reach()
        "limits": {name: list(bounds) for name, bounds in LIMITS.items()},
        "bar_seconds": list(BAR_SECONDS),
        "rules": list(RULES),
    }


@dataclass(frozen=True)
class Signal:
    """The rule's condition became true at the end of the second that starts at ``ts_ms``."""

    ts_ms: int
    side: str  # "buy" or "sell"
    bid: float
    ask: float
    rsi: float
    middle: float
    upper: float
    lower: float
    by: str  # which condition held at that bar: "both", "rsi" or "band"

    def as_dict(self) -> dict[str, Any]:
        return {
            "t": self.ts_ms,
            "side": self.side,
            "bid": self.bid,
            "ask": self.ask,
            "rsi": self.rsi,
            "middle": self.middle,
            "upper": self.upper,
            "lower": self.lower,
            "by": self.by,
        }


def payload(signal: Signal, precision: int) -> dict[str, Any]:
    """A signal as the dashboard gets it: rounded for the wire.

    Which condition held (``by``) is the rule's own word, from the exact values: the rounded
    ones can sit on the other side of a level by less than their last digit.
    """
    return {
        "t": signal.ts_ms,
        "side": signal.side,
        "bid": round(signal.bid, precision),
        "ask": round(signal.ask, precision),
        "rsi": round(signal.rsi, 2),
        # a band is an average of prices: two digits finer than a price
        "middle": round(signal.middle, precision + 2),
        "upper": round(signal.upper, precision + 2),
        "lower": round(signal.lower, precision + 2),
        "by": signal.by,
    }


def _by(rsi_met: bool, band_met: bool) -> str:
    """Which of the two conditions held at a bar that carries a signal."""
    return "both" if rsi_met and band_met else "rsi" if rsi_met else "band"


# --------------------------------------------------------------------------- a stretch at once
def _closing(ts: Ints, usable: Bools, bar_seconds: int) -> Ints:
    """Positions of the seconds that close a bar and carry a price that may feed signals."""
    return np.flatnonzero(usable & ((ts // SECOND_MS + 1) % bar_seconds == 0))


def _runs(t: Ints, step_ms: int) -> list[tuple[int, int]]:
    """Half-open stretches of bars that follow each other without a break."""
    if len(t) == 0:
        return []
    edges = [0, *(np.flatnonzero(np.diff(t) != step_ms) + 1).tolist(), len(t)]
    return list(itertools.pairwise(edges))


def _moved(close: Floats, n: int) -> Bools:
    """Whether the ``n`` values up to each position hold more than one price (False before there are ``n``)."""
    out = np.zeros(len(close), dtype=bool)
    if len(close) >= n:
        changes = np.concatenate(([0], np.cumsum(np.diff(close) != 0)))  # changes of price up to each position
        out[n - 1 :] = changes[n - 1 :] > changes[: len(close) - n + 1]
    return out


@dataclass(frozen=True)
class _Judged:
    """One run of bars as the rule reads it, position by position."""

    known: Bools  # both indicators are defined
    buy: Bools  # the buy condition holds
    sell: Bools
    weak: Bools  # RSI at or below its lower level
    strong: Bools
    low: Bools  # bid at or below the lower band
    high: Bools
    rsi: Floats
    middle: Floats
    upper: Floats
    lower: Floats


def _judge(close: Floats, settings: LiveSettings) -> _Judged:
    rsi = ind.rsi(close, settings.rsi_period)
    middle, upper, lower = ind.bollinger(close, settings.bollinger_period, settings.bollinger_width)
    known = ~(np.isnan(rsi) | np.isnan(middle))
    moved = _moved(close, settings.bollinger_period)  # bands of no width are met by nothing
    with np.errstate(invalid="ignore"):  # comparisons with "not defined yet" are simply false
        weak, strong = rsi <= settings.rsi_buy_below, rsi >= settings.rsi_sell_above
        low, high = (close <= lower) & moved, (close >= upper) & moved
    if settings.rule == "both":
        buy, sell = weak & low, strong & high
    else:
        buy, sell = weak | low, strong | high
    return _Judged(known, buy & known, sell & known, weak, strong, low, high, rsi, middle, upper, lower)


def replay(ts: Ints, bid: Floats, ask: Floats, usable: Bools, settings: LiveSettings) -> tuple[list[Signal], Stepper]:
    """The signals of a stretch of stored seconds, and a stepper that has seen all of them.

    ``ts`` holds the start of each second in ascending order (epoch milliseconds), ``usable``
    whether its price may feed signals.  Seconds may be missing.
    """
    step = settings.bar_seconds * SECOND_MS
    at = _closing(ts, usable, settings.bar_seconds)
    t, close, offer = ts[at], bid[at], ask[at]
    signals: list[Signal] = []
    stepper = Stepper(settings)
    runs = _runs(t, step)
    for a, b in runs:
        x = close[a:b]
        if b - a <= settings.first_signal_at and (a, b) != runs[-1]:
            continue  # too short to carry a signal, and not the run a stepper would continue
        j = _judge(x, settings)
        before = np.concatenate(([False], j.known[:-1]))
        rising_buy = j.buy & before & ~np.concatenate(([False], j.buy[:-1]))
        rising_sell = j.sell & before & ~np.concatenate(([False], j.sell[:-1]))
        for i in np.flatnonzero(rising_buy | rising_sell).tolist():
            values = (
                float(x[i]),
                float(offer[a + i]),
                float(j.rsi[i]),
                float(j.middle[i]),
                float(j.upper[i]),
                float(j.lower[i]),
            )
            if rising_buy[i]:
                signals.append(Signal(int(t[a + i]), "buy", *values, _by(bool(j.weak[i]), bool(j.low[i]))))
            if rising_sell[i]:
                signals.append(Signal(int(t[a + i]), "sell", *values, _by(bool(j.strong[i]), bool(j.high[i]))))
        if (a, b) == runs[-1]:
            stepper = Stepper._resumed(settings, int(t[b - 1]), x, bool(j.known[-1]), bool(j.buy[-1]), bool(j.sell[-1]))
    if len(ts) and len(t):
        # The run in progress is the last one only if the newest second that should have closed a
        # bar did close one: a stale or missing closing second has ended the run already.
        newest = int(ts[-1]) // SECOND_MS
        due = ((newest + 1) // settings.bar_seconds) * settings.bar_seconds - 1
        if int(t[-1]) != due * SECOND_MS:
            stepper = Stepper(settings)
    return signals, stepper


def scan(ts: Ints, bid: Floats, ask: Floats, usable: Bools, settings: LiveSettings) -> list[Signal]:
    """The signals of a stretch of stored seconds (see :func:`replay`)."""
    return replay(ts, bid, ask, usable, settings)[0]


# -------------------------------------------------------------------------- one second at a time
class Stepper:
    """The rule one second at a time, in constant work per second.

    Feed it every stored second in time order with :meth:`push`.  It keeps what the indicators
    need to go on: Wilder's two averages for the RSI, the last closes for the bands, and
    whether each condition held at the bar before.
    """

    def __init__(self, settings: LiveSettings) -> None:
        self.settings = settings
        self._step = settings.bar_seconds * SECOND_MS
        self._last: int | None = None  # closing second of the newest bar of the run in progress
        self._count = 0  # bars of that run
        self._previous = 0.0  # its newest close
        self._gains: list[float] = []  # the first changes of the run, until the averages exist
        self._losses: list[float] = []
        self._gain = 0.0  # Wilder's average gain and loss
        self._loss = 0.0
        self._window: deque[float] = deque(maxlen=settings.bollinger_period)
        self._buy: bool | None = None  # the conditions at the newest bar; None: not defined there
        self._sell: bool | None = None

    @classmethod
    def _resumed(cls, settings: LiveSettings, last: int, close: Floats, known: bool, buy: bool, sell: bool) -> Stepper:
        """The state :meth:`push` would be in after the bars ``close`` of a run that ended at ``last``."""
        self = cls(settings)
        n = settings.rsi_period
        self._last, self._count, self._previous = last, len(close), float(close[-1])
        change = np.diff(close)
        gains, losses = np.where(change > 0, change, 0.0), np.where(change < 0, -change, 0.0)
        if len(change) < n:
            self._gains, self._losses = gains.tolist(), losses.tolist()
        else:
            gain, loss = ind.rsi_averages(close, n)
            self._gain, self._loss = float(gain[-1]), float(loss[-1])
        self._window.extend(close[-settings.bollinger_period :].tolist())
        self._buy, self._sell = (buy, sell) if known else (None, None)
        return self

    def push(self, ts_ms: int, bid: float, ask: float, usable: bool) -> list[Signal]:
        """One stored second; the signals that are known at its end (none, one, rarely two)."""
        s = self.settings
        if (ts_ms // SECOND_MS + 1) % s.bar_seconds:
            return []  # inside a bar: nothing is judged before its close
        if not usable:
            self._last = None  # the run ends here
            return []
        if self._last is None or ts_ms - self._last != self._step:
            self._count = 0
            self._gains, self._losses = [], []
            self._window.clear()
            self._buy = self._sell = None
        self._last = ts_ms

        n = s.rsi_period
        rsi = math.nan
        if self._count:
            change = bid - self._previous
            gain = change if change > 0 else 0.0
            loss = -change if change < 0 else 0.0
            if self._count <= n:
                self._gains.append(gain)
                self._losses.append(loss)
                if self._count == n:
                    self._gain = float(np.asarray(self._gains, dtype=np.float64).mean())
                    self._loss = float(np.asarray(self._losses, dtype=np.float64).mean())
            else:
                self._gain = (self._gain * (n - 1) + gain) / n
                self._loss = (self._loss * (n - 1) + loss) / n
            if self._count >= n:
                if self._loss == 0:
                    rsi = 100.0 if self._gain > 0 else 50.0
                else:
                    rsi = 100.0 - 100.0 / (1.0 + self._gain / self._loss)
        self._previous = bid
        self._count += 1

        self._window.append(bid)
        middle = upper = lower = math.nan
        moved = False
        if len(self._window) == s.bollinger_period:
            window = np.fromiter(self._window, dtype=np.float64, count=s.bollinger_period)
            m, u, lo = ind.bollinger(window, s.bollinger_period, s.bollinger_width)
            middle, upper, lower = float(m[-1]), float(u[-1]), float(lo[-1])
            moved = bool(window.min() != window.max())  # bands of no width are met by nothing

        if math.isnan(rsi) or math.isnan(middle):
            self._buy = self._sell = None
            return []
        weak, strong = rsi <= s.rsi_buy_below, rsi >= s.rsi_sell_above
        low, high = moved and bid <= lower, moved and bid >= upper
        buy, sell = (weak and low, strong and high) if s.rule == "both" else (weak or low, strong or high)
        out: list[Signal] = []
        if buy and self._buy is False:
            out.append(Signal(ts_ms, "buy", bid, ask, rsi, middle, upper, lower, _by(weak, low)))
        if sell and self._sell is False:
            out.append(Signal(ts_ms, "sell", bid, ask, rsi, middle, upper, lower, _by(strong, high)))
        self._buy, self._sell = buy, sell
        return out


# ---------------------------------------------------------------------------- trend channels
def channel(ts: Ints, close: Floats, bars: int, width: float, day_start_ms: int | None = None) -> dict[str, Any] | None:
    """The straight trend channel through the newest ``bars`` values, or None for too few.

    A line is fitted to the values by least squares (in bars, so a weekend is not a distance),
    and the channel lies ``width`` standard deviations of the values around the line on either
    side.  ``from``/``to`` are the times of its first and last bar, ``start``/``end`` the line
    there, ``half`` the distance from the line to either edge.  With ``day_start_ms``, ``day``
    says where the line stands at the first of its bars that belongs to that day.
    """
    n = min(bars, len(close))
    if n < MIN_CHANNEL_BARS:
        return None
    middle, upper, _lower, slope = ind.regression_channel(close[-n:], n, width)
    end, rise, half = float(middle[-1]), float(slope[-1]), float(upper[-1] - middle[-1])
    start = end - rise * (n - 1)
    out: dict[str, Any] = {
        "bars": n,
        "asked": bars,
        "from": int(ts[-n]),
        "to": int(ts[-1]),
        "start": start,
        "end": end,
        "half": half,
        "rise_per_bar": rise,
    }
    if day_start_ms is not None:
        first = int(np.searchsorted(ts[-n:], day_start_ms, side="left"))
        out["day"] = None if first >= n else {"from": int(ts[len(ts) - n + first]), "start": start + rise * first}
    return out
