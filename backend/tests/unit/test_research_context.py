"""The context of a trade: where momentum points, where the trend channel is, and what follows from them."""

from __future__ import annotations

import numpy as np
import pytest

from fxlab.research import context as ctx
from fxlab.research import indicators as ind
from fxlab.research.bars import Bars, resample
from fxlab.research.context import Context, build_context, describe_trades, entry_conditions, explain, pointing
from fxlab.research.engine import Fill, Signals, Trade
from fxlab.timeutil import MINUTE_MS
from tests.conftest import T0
from tests.unit.test_research_fairness import random_walk

PIP = 0.0001


def signals(n: int, events: dict[int, int]) -> Signals:
    direction = np.zeros(n, dtype=np.int8)
    for bar, d in events.items():
        direction[bar] = d
    return Signals(direction, np.where(direction != 0, 10.0, np.nan))


def flat_bars(n: int, seconds: int = 300, price: float = 1.1) -> Bars:
    ts = T0 + np.arange(n, dtype=np.int64) * seconds * 1000
    p = np.full(n, price)
    return Bars(ts, p, p.copy(), p.copy(), p.copy(), seconds)


# --- where a method points -----------------------------------------------------------------


def test_a_method_points_the_way_of_its_last_signal_while_that_is_fresh() -> None:
    said = signals(12, {1: 1, 4: -1})
    assert pointing(said, fresh_bars=3).tolist() == [0, 1, 1, 1, -1, -1, -1, 0, 0, 0, 0, 0]
    # a signal of the same side keeps it pointing; nothing said: pointing nowhere
    assert pointing(signals(6, {0: 1, 2: 1}), fresh_bars=3).tolist() == [1, 1, 1, 1, 1, 0]
    assert pointing(signals(4, {}), fresh_bars=3).tolist() == [0, 0, 0, 0]
    assert pointing(signals(0, {})).tolist() == []


def test_the_reading_counts_the_methods_and_the_state_needs_a_fifth_of_them() -> None:
    base = flat_bars(12)
    # ten methods on the bars of the grid itself: two more up than down are needed for a state
    plan = [{1: 1}, {1: 1}, {3: -1}, {3: -1}, {3: -1}, {5: -1}, {7: 1}, {7: 1}, {7: 1}, {7: 1}]
    context = build_context(base, [(base, signals(12, events)) for events in plan])
    assert (context.voters, context.threshold) == (10, 2)
    # row k is what was known when bar k had closed
    assert context.votes.tolist() == [0, 2, 2, -1, -1, -2, -2, 2, 2, 2, 2, 2]
    # up at +2, still up at -1 (not enough on the other side), down at -2, up again at +2
    assert context.state.tolist() == [0, 1, 1, 1, 1, -1, -1, 1, 1, 1, 1, 1]
    span = 5 * MINUTE_MS
    assert context.known_ms.tolist() == [T0 + (k + 1) * span for k in range(12)]
    # the first decision is not a change; the two turns are
    assert context.changes() == [(T0 + 6 * span, -1), (T0 + 8 * span, 1)]
    assert build_context(base, []).state.tolist() == [0] * 12 and build_context(base, []).changes() == []


def test_a_method_on_longer_bars_is_heard_when_its_bar_has_closed() -> None:
    base = flat_bars(12, seconds=60)  # one-minute bars from 14:00: the grid has rows known at 14:05 and 14:10
    quarter = resample(base, 900)  # one bar of a quarter of an hour, closing at 14:15: after the data ends
    five = resample(base, 300)
    said = [(five, signals(len(five), {0: 1})), (quarter, signals(len(quarter), {0: -1}))]
    context = build_context(base, said)
    assert context.known_ms.tolist() == [T0 + 5 * MINUTE_MS, T0 + 10 * MINUTE_MS, T0 + 15 * MINUTE_MS]
    # the five-minute method spoke at 14:05; the other one is not heard before 14:15
    assert context.votes.tolist() == [1, 1, 0]


def test_the_context_never_changes_when_later_bars_are_added() -> None:
    base = random_walk(3, days=6)
    cut = len(base) * 2 // 3

    def made(bars: Bars) -> Context:
        from fxlab.research.strategies import STRATEGIES

        methods = []
        for seconds in (60, 300, 900):
            longer = resample(bars, seconds)
            for key in ("ema_momentum", "macd_cross", "donchian_breakout", "channel_break"):
                methods.append((longer, STRATEGIES[key].signals(longer, PIP)))
        return build_context(bars, methods)

    short = Bars(base.ts[:cut], base.open[:cut], base.high[:cut], base.low[:cut], base.close[:cut], 60)
    whole, part = made(base), made(short)
    # the last row of the shorter one may be a bar that had not finished: everything before it is final
    rows = len(part) - 1
    assert rows > 500 and (whole.known_ms[:rows] == part.known_ms[:rows]).all()
    for name in ("close", "votes", "state", "middle", "upper", "lower", "slope"):
        assert np.array_equal(getattr(whole, name)[:rows], getattr(part, name)[:rows], equal_nan=True), name
    assert (whole.state != 0).any() and len(whole.changes()) > 3


def test_the_channel_is_the_one_drawn_from_the_bars_before() -> None:
    base = random_walk(1, days=4)
    context = build_context(base, [], channel_hours=2.0)
    grid = resample(base, 300)
    assert context.channel_bars == 24 and context.seconds == 300
    middle, upper, lower, slope = ind.regression_channel(grid.close, 24, ctx.CHANNEL_WIDTH, ahead=1)
    for name, values in (("middle", middle), ("upper", upper), ("lower", lower), ("slope", slope)):
        assert np.array_equal(getattr(context, name), values, equal_nan=True), name
    assert np.isnan(context.middle[:24]).all() and not np.isnan(context.middle[24:]).any()
    # which row was known at a moment: the last one stamped at or before it
    moments = [context.known_ms[0] - 1, context.known_ms[0], context.known_ms[5] + 1, context.known_ms[-1] + 10**9]
    assert context.at(moments).tolist() == [-1, 0, 5, len(context) - 1]


# --- what is known when a trade is opened --------------------------------------------------


def made_context(
    close: list[float], state: list[int], middle: float = 1.1, half: float = 0.001, slope: float = 0.0
) -> Context:
    n = len(close)
    line = middle + slope * np.arange(n)
    return Context(
        known_ms=T0 + np.arange(1, n + 1, dtype=np.int64) * 5 * MINUTE_MS,
        close=np.asarray(close, dtype=np.float64),
        votes=np.asarray(state, dtype=np.int16) * 3,
        voters=10,
        state=np.asarray(state, dtype=np.int8),
        middle=line,
        upper=line + half,
        lower=line - half,
        slope=np.full(n, slope),
        channel_bars=21,
        seconds=300,
    )


def test_what_is_known_against_a_trade_when_it_would_be_opened() -> None:
    # ten pips either side of 1.1000; momentum down in rows 2 and 3
    context = made_context([1.1, 1.1, 1.0985, 1.1012, 1.1], [0, 1, -1, -1, 1])
    at = context.known_ms
    buy = entry_conditions(context, at, [1, 1, 1, 1, 1])
    sell = entry_conditions(context, at, [-1, -1, -1, -1, -1])
    assert buy["against_momentum"].tolist() == [False, False, True, True, False]
    assert sell["against_momentum"].tolist() == [False, True, False, False, True]
    # row 2 closed 15 pips under the line: under the channel, where a buy loses; row 3 closed above it
    assert buy["outside_channel"].tolist() == [False, False, True, False, False]
    assert sell["outside_channel"].tolist() == [False, False, False, True, False]
    assert not buy["against_channel"].any() and not sell["against_channel"].any()  # the channel is level
    # before the first row nothing is known, and no direction is no trade
    early = entry_conditions(context, [at[0] - 1], [1])
    assert not any(values.any() for values in early.values())
    assert not any(values.any() for values in entry_conditions(context, at, [0, 0, 0, 0, 0]).values())

    # a channel that falls a pip a row: twenty pips over its twenty steps, more than its half-height of ten
    falling = made_context([1.1] * 5, [0] * 5, slope=-0.0001)
    assert entry_conditions(falling, falling.known_ms, [1] * 5)["against_channel"].all()
    assert not entry_conditions(falling, falling.known_ms, [-1] * 5)["against_channel"].any()
    gentle = made_context([1.1] * 5, [0] * 5, slope=-0.00004)  # eight pips over its length: not enough
    assert not entry_conditions(gentle, gentle.known_ms, [1] * 5)["against_channel"].any()

    # while the channel is still warming up nothing is said about it
    unknown = made_context([1.0] * 3, [0] * 3)
    unknown.middle[:] = unknown.upper[:] = unknown.lower[:] = np.nan
    told = entry_conditions(unknown, unknown.known_ms, [1, 1, 1])
    assert not told["outside_channel"].any() and not told["against_channel"].any()


def trade(direction: int, signal_row: int, exit_row: int, pnl: float) -> Trade:
    """A trade opened on what was known in one row of the context and closed in a later one."""
    signal_ms = T0 + (signal_row + 1) * 5 * MINUTE_MS
    exit_ms = T0 + (exit_row + 1) * 5 * MINUTE_MS + MINUTE_MS
    fills = (Fill(signal_ms + MINUTE_MS, 1.1, 1_000_000.0),)
    return Trade(direction, signal_ms, fills, exit_ms, 1.1 + direction * pnl / 1e6, "stop", 1.09, 1.1005, pnl, 9.0, 1.0)


def test_what_happened_to_a_trade_while_it_was_open() -> None:
    #                        row:   0     1       2       3       4      5
    context = made_context([1.1, 1.1, 1.0995, 1.0988, 1.1, 1.1], [1, 1, 1, -1, -1, 1])
    described = describe_trades(
        context,
        [
            trade(1, 0, 4, -500.0),  # bought with momentum up; it turned down in row 3 and the price fell out in row 3
            trade(1, 3, 5, 500.0),  # bought against momentum, under the channel
            trade(-1, 0, 2, 500.0),  # sold against momentum up; nothing changed until row 2
            trade(1, 4, 5, 500.0),  # bought against momentum; it turned up afterwards, which is not "against"
        ],
    )
    first, second, third, fourth = described
    assert first == {
        "votes": 3,
        "momentum": 1,
        "against_momentum": False,
        "against_channel": False,
        "outside_channel": False,
        "momentum_turned": True,
        "left_channel": True,  # 1.0988 is twelve pips under the line that was drawn when it was opened
    }
    assert (second["momentum"], second["against_momentum"], second["outside_channel"]) == (-1, True, True)
    assert not second["momentum_turned"]  # it was against from the start: nothing turned
    assert (third["momentum"], third["against_momentum"], third["momentum_turned"], third["left_channel"]) == (
        -1,
        True,
        False,
        False,
    )
    assert (fourth["against_momentum"], fourth["momentum_turned"], fourth["left_channel"]) == (True, False, False)
    assert describe_trades(context, []) == []
    # a trade from before the context began is described as one about which nothing is known
    blank = describe_trades(
        context, [Trade(1, T0, (Fill(T0, 1.1, 1e6),), T0 + 1, 1.1, "time", 1.09, 1.1005, 0.0, 9.0, 0.0)]
    )
    assert blank == [{"votes": 0, "momentum": 0, **{key: False for key, _, _ in ctx.CONDITIONS}}]


def test_the_channel_a_trade_can_leave_is_the_one_drawn_when_it_was_opened() -> None:
    # a channel rising two pips a row, five pips either side; the price stays at 1.1000
    rising = made_context([1.1] * 6, [0] * 6, half=0.0005, slope=0.0002)
    # opened in row 0, where the line is at 1.1000.  By row 3 the line as drawn then is at 1.1006: the
    # price is six pips under it, out of the channel on the losing side of a buy
    (told,) = describe_trades(rising, [trade(1, 0, 3, -500.0)])
    assert told["left_channel"] and not told["outside_channel"]
    (early,) = describe_trades(rising, [trade(1, 0, 2, -500.0)])  # closed a row earlier: four pips under, still inside
    assert not early["left_channel"]
    (seller,) = describe_trades(
        rising, [trade(-1, 0, 5, 500.0)]
    )  # for a sale, falling behind the line is the good side
    assert not seller["left_channel"]


# --- do the conditions tell failures from successes? ---------------------------------------


def test_a_condition_is_judged_by_the_trades_it_holds_for_and_those_it_does_not() -> None:
    def row(pnl: float, **tags: bool) -> dict:
        return {"pnl": pnl, **{key: False for key, _, _ in ctx.CONDITIONS}, **tags}

    rows = [
        row(-500.0, against_momentum=True),
        row(-500.0, against_momentum=True, left_channel=True),
        row(500.0, against_momentum=True),
        row(500.0),
        row(500.0),
        row(-500.0),  # failed with nothing to say about it
        row(0.0),
    ]
    told = explain(rows)
    assert (told["trades"], told["failed"], told["failed_unexplained"]) == (7, 3, 1)
    by_key = {c["key"]: c for c in told["conditions"]}
    assert [c["key"] for c in told["conditions"]] == [key for key, _, _ in ctx.CONDITIONS]
    against = by_key["against_momentum"]
    assert against["when"] == "entry" and against["failed_with"] == 2
    assert against["with"] == {"trades": 3, "wins": 1, "losses": 2, "win_rate": 0.3333, "net": -500.0}
    assert against["without"] == {"trades": 4, "wins": 2, "losses": 1, "win_rate": 0.5, "net": 500.0}
    # a third against a half: 17 points less.  Between groups of three and four trades chance alone
    # makes differences of 76 points: this one says nothing
    assert against["difference"] == pytest.approx(-0.1667, abs=1e-4)
    assert against["noise"] == pytest.approx(2 * (3 / 7 * 4 / 7 * (1 / 3 + 1 / 4)) ** 0.5, abs=1e-4)
    assert abs(against["difference"]) < against["noise"]
    left = by_key["left_channel"]
    assert left["when"] == "after" and left["with"]["trades"] == 1 and left["without"]["trades"] == 6
    assert by_key["outside_channel"]["with"] == {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "net": 0.0}
    assert by_key["outside_channel"]["difference"] is None and by_key["outside_channel"]["noise"] is None
    assert explain([]) == {
        "trades": 0,
        "failed": 0,
        "failed_unexplained": 0,
        "conditions": [
            {
                "key": key,
                "label": label,
                "when": when,
                "with": {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "net": 0.0},
                "without": {"trades": 0, "wins": 0, "losses": 0, "win_rate": None, "net": 0.0},
                "failed_with": 0,
                "difference": None,
                "noise": None,
            }
            for key, label, when in ctx.CONDITIONS
        ],
    }
    assert ctx.ENTRY_CONDITIONS == ("against_momentum", "against_channel", "outside_channel")
    with pytest.raises(KeyError):
        explain([{"pnl": 1.0}])
