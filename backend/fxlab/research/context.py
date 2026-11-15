"""What the market was doing around a trade: the two things a failed trade is asked about.

**Momentum.**  Every momentum method (the trend followers and the breakouts, on every bar
length of the run) points up, down or nowhere: the direction of its last signal, for as
long as that signal is fresh.  The *reading* is the number pointing up minus the number
pointing down.  The *state* is "up" or "down" once a fifth of the methods agree on a side,
and it stays until as many agree on the other side.  A change of state is a momentum change.

**The trend channel.**  A straight line through the last day of five-minute closes with a
band of two standard deviations around it, fitted to the bars *before* each moment and
extended to it, so that the newest price can be outside the channel it is compared with.

Every value in a ``Context`` row was known at the moment the row is stamped with: nothing
here looks ahead.  What happened *after* a trade was opened (momentum turned, the price left
the channel) is worked out too, and is kept apart: it describes how a trade failed and can
never be a reason for not having taken it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
import numpy.typing as npt

from fxlab.research import indicators as ind
from fxlab.research.bars import Bars, Floats, Ints, resample
from fxlab.research.engine import Signals, Trade

#: the grid the context is kept on: five-minute bars
GRID_SECONDS = 300
#: hours of trading the trend channel is fitted to, and its half-height in standard deviations
CHANNEL_HOURS = 24.0
CHANNEL_WIDTH = 2.0
#: a signal speaks for its method for this many of the method's own bars
FRESH_BARS = 96
#: the share of the momentum methods that has to agree on a side for the state to be that side
AGREEMENT = 0.2

#: what can be said about a trade.  "entry": known when the trade was opened, so it can be
#: used to skip trades.  "after": seen only while the trade was open.
CONDITIONS: tuple[tuple[str, str, str], ...] = (
    ("against_momentum", "Against the momentum when it was opened", "entry"),
    ("against_channel", "Against the slope of the trend channel", "entry"),
    ("outside_channel", "Opened outside the trend channel, on the losing side", "entry"),
    ("momentum_turned", "Momentum turned against it while it was open", "after"),
    ("left_channel", "The price left the trend channel against it while it was open", "after"),
)
ENTRY_CONDITIONS = tuple(key for key, _, when in CONDITIONS if when == "entry")


@dataclass(frozen=True)
class Context:
    known_ms: Ints  # when each row was known: the close of a bar of the grid, ascending
    close: Floats  # the (bid) price at that moment
    votes: npt.NDArray[np.int16]  # momentum methods pointing up minus those pointing down
    voters: int  # how many momentum methods there are
    state: npt.NDArray[np.int8]  # +1 up, -1 down, 0 not decided yet
    middle: Floats  # the trend channel, drawn ahead from the bars before; nan while it warms up
    upper: Floats
    lower: Floats
    slope: Floats  # what the middle line rises from one row to the next
    channel_bars: int  # how many rows the line is fitted to
    seconds: int  # length of a bar of the grid

    def __len__(self) -> int:
        return len(self.known_ms)

    def at(self, moments: npt.ArrayLike) -> Ints:
        """The row that was known at each moment (the last one stamped at or before it); -1: none."""
        return np.asarray(np.searchsorted(self.known_ms, moments, side="right") - 1, dtype=np.int64)

    @property
    def threshold(self) -> int:
        """How many more methods must point one way than the other for the state to be that way."""
        return max(1, int(np.ceil(AGREEMENT * self.voters)))

    def turns(self) -> Ints:
        """The rows at which momentum went from up to down or from down to up."""
        decided = np.flatnonzero(self.state != 0)
        if len(decided) == 0:
            return decided
        states = self.state[decided]
        return np.asarray(decided[np.flatnonzero(states[1:] != states[:-1]) + 1], dtype=np.int64)

    def changes(self) -> list[tuple[int, int]]:
        """(moment, new state) wherever momentum went from up to down or from down to up."""
        return [(int(self.known_ms[row]), int(self.state[row])) for row in self.turns()]


def pointing(signals: Signals, fresh_bars: int = FRESH_BARS) -> npt.NDArray[np.int8]:
    """Where a method points after each of its bars: the direction of its last signal while it is fresh."""
    direction = signals.direction
    n = len(direction)
    position = np.arange(n)
    last = np.maximum.accumulate(np.where(direction != 0, position, -1))
    fresh = (last >= 0) & (position - last < fresh_bars)
    out = np.zeros(n, dtype=np.int8)
    out[fresh] = direction[last[fresh]]
    return out


def build_context(
    base: Bars,
    momentum: Sequence[tuple[Bars, Signals]],
    *,
    grid_seconds: int = GRID_SECONDS,
    channel_hours: float = CHANNEL_HOURS,
    channel_width: float = CHANNEL_WIDTH,
) -> Context:
    """The context of ``base`` from the signals of the momentum methods (each with the bars it read)."""
    seconds = max(grid_seconds, base.seconds)
    grid = resample(base, seconds)
    known = grid.ts + grid.span_ms
    votes = np.zeros(len(grid), dtype=np.int16)
    for bars, signals in momentum:
        if len(bars) == 0:
            continue
        points = pointing(signals)
        # what the method said after the last of its bars that had closed by then
        index = np.searchsorted(bars.ts + bars.span_ms, known, side="right") - 1
        votes += np.where(index >= 0, points[np.maximum(index, 0)], 0).astype(np.int16)
    voters = len(momentum)
    threshold = max(1, int(np.ceil(AGREEMENT * voters)))
    decided = np.where(votes >= threshold, 1, np.where(votes <= -threshold, -1, 0)).astype(np.int8)
    # the state stays what it last was decided to be
    position = np.arange(len(grid))
    last = np.maximum.accumulate(np.where(decided != 0, position, -1))
    state = np.where(last >= 0, decided[np.maximum(last, 0)], 0).astype(np.int8)
    n = max(20, round(channel_hours * 3600 / seconds))
    middle, upper, lower, slope = ind.regression_channel(grid.close, n, channel_width, ahead=1)
    return Context(known, grid.close, votes, voters, state, middle, upper, lower, slope, n, seconds)


def entry_conditions(
    context: Context, moments: npt.ArrayLike, direction: npt.ArrayLike
) -> dict[str, npt.NDArray[np.bool_]]:
    """For trades that would be opened at ``moments`` in ``direction``: the conditions known at that time.

    Where the context does not reach back to a moment, or the channel is still warming up,
    a condition is False: nothing is known against the trade.
    """
    d = np.asarray(direction, dtype=np.int64)
    row = context.at(moments)
    known = row >= 0
    k = np.maximum(row, 0)
    half = context.upper[k] - context.middle[k]
    drawn = known & ~np.isnan(half)
    with np.errstate(invalid="ignore"):
        distance = d * (context.close[k] - context.middle[k])  # how far the price is on the trade's good side
        rise = d * context.slope[k] * (context.channel_bars - 1)  # what the line does over its length, for the trade
        return {
            "against_momentum": known & (context.state[k] == -d) & (d != 0),
            # a channel that has moved against the trade by more than half its own height
            "against_channel": drawn & (rise < -half),
            # the price is already through the edge the trade would lose at
            "outside_channel": drawn & (distance < -half),
        }


def describe_trades(context: Context, trades: Sequence[Trade]) -> list[dict[str, Any]]:
    """For each trade: the momentum reading it was opened on and which of ``CONDITIONS`` hold for it."""
    if not trades:
        return []
    signal = np.asarray([t.signal_ms for t in trades], dtype=np.int64)
    direction = np.asarray([t.direction for t in trades], dtype=np.int64)
    entry = entry_conditions(context, signal, direction)
    first = context.at(signal)
    last = context.at(np.asarray([t.exit_ms for t in trades], dtype=np.int64))
    out: list[dict[str, Any]] = []
    for i, trade in enumerate(trades):
        k0, k1, d = int(first[i]), int(last[i]), trade.direction
        tags = {key: bool(values[i]) for key, values in entry.items()}
        turned = left = False
        if k0 >= 0 and k1 > k0:
            rows = slice(k0 + 1, k1 + 1)
            turned = not tags["against_momentum"] and bool((context.state[rows] == -d).any())
            half = context.upper[k0] - context.middle[k0]
            if not np.isnan(half):
                # the channel as it was drawn when the trade was opened, extended over the trade
                steps = np.arange(1, k1 - k0 + 1)
                line = context.middle[k0] + context.slope[k0] * steps
                left = bool((d * (context.close[rows] - line) < -half).any())
        tags["momentum_turned"] = turned
        tags["left_channel"] = left
        out.append(
            {
                "votes": int(context.votes[k0]) if k0 >= 0 else 0,
                "momentum": int(context.state[k0]) * d if k0 >= 0 else 0,  # +1 with the trade, -1 against it
                **tags,
            }
        )
    return out


def explain(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Whether the conditions tell trades that failed from trades that worked.

    ``rows`` are trade rows carrying ``pnl`` and the tags of ``describe_trades``.  For every
    condition: the trades it holds for and the trades it does not, with what became of each
    group.  A condition explains failures only if the two groups differ; one that is as common
    among the trades that worked says nothing.  ``difference`` is how much more often the trades
    it holds for worked; ``noise`` is how large that gets by chance between two groups of these
    sizes.  A difference inside the noise is no finding.
    """

    def group(members: list[dict[str, Any]]) -> dict[str, Any]:
        wins = sum(1 for r in members if r["pnl"] > 0)
        losses = sum(1 for r in members if r["pnl"] < 0)
        return {
            "trades": len(members),
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / len(members), 4) if members else None,
            "net": round(sum(r["pnl"] for r in members), 2),
        }

    failed = [r for r in rows if r["pnl"] < 0]
    conditions = []
    for key, label, when in CONDITIONS:
        held, free = group([r for r in rows if r[key]]), group([r for r in rows if not r[key]])
        difference = noise = None
        if held["trades"] and free["trades"]:
            # how much more or less often the trades it holds for worked, and how large such a difference
            # gets by chance alone between two groups of these sizes (two standard errors)
            share = (held["wins"] + free["wins"]) / (held["trades"] + free["trades"])
            difference = round(held["win_rate"] - free["win_rate"], 4)
            noise = round(2.0 * (share * (1.0 - share) * (1.0 / held["trades"] + 1.0 / free["trades"])) ** 0.5, 4)
        conditions.append(
            {
                "key": key,
                "label": label,
                "when": when,
                "with": held,
                "without": free,
                "failed_with": sum(1 for r in failed if r[key]),
                "difference": difference,
                "noise": noise,
            }
        )
    return {
        "trades": len(rows),
        "failed": len(failed),
        # a failed trade none of the conditions holds for: the price simply went the other way
        "failed_unexplained": sum(1 for r in failed if not any(r[key] for key, _, _ in CONDITIONS)),
        "conditions": conditions,
    }
