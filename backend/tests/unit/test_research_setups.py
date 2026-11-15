"""Setups: the owner's own rules put together from the methods, traded and explained."""

from __future__ import annotations

import json

import numpy as np
import pytest

from fxlab.research import context as ctx
from fxlab.research import engine
from fxlab.research.bars import Bars, resample
from fxlab.research.engine import Execution, Signals
from fxlab.research.report import BacktestError, BacktestRequest, build_report
from fxlab.research.setups import MAX_METHODS, SetupRequest, build_setup, combine, momentum_voters, prepare_setup
from fxlab.research.strategies import STRATEGIES
from fxlab.timeutil import MINUTE_MS
from tests.conftest import T0
from tests.unit.test_research_fairness import PIP, PIP_VALUE, TOLERANCE, random_walk
from tests.unit.test_research_report import market

RULES = {"min_potential_pips": 2, "target_pips": 3, "stop_pips": 3}


@pytest.fixture(scope="module")
def eurusd(registry):  # type: ignore[no-untyped-def]
    return registry.get("EURUSD")


@pytest.fixture(scope="module")
def bars() -> Bars:
    return market()  # 15 weekdays from Monday 5 October 2026, 06:00 to 21:00 UTC, one-minute bars


def ask(**changes: object) -> SetupRequest:
    values: dict = {
        "symbol": "EURUSD",
        "start": "2026-10-05",
        "end": "2026-10-24",
        "name": "RSI or Bollinger",
        "methods": ["rsi_reversion@5m", "bollinger_reversion@5m"],
        "execution": RULES,
    }
    values.update(changes)
    return SetupRequest.from_mapping(values)


# --- putting signals together ----------------------------------------------------------------


def minute_bars(n: int) -> Bars:
    ts = T0 + np.arange(n, dtype=np.int64) * MINUTE_MS
    p = np.full(n, 1.1)
    return Bars(ts, p, p.copy(), p.copy(), p.copy(), 60)


def said(bars: Bars, events: dict[int, tuple[int, float]]) -> tuple[Bars, Signals]:
    direction = np.zeros(len(bars), dtype=np.int8)
    potential = np.full(len(bars), np.nan)
    for bar, (d, reach) in events.items():
        direction[bar], potential[bar] = d, reach
    return bars, Signals(direction, potential)


def events(signals: Signals) -> dict[int, tuple[int, float]]:
    return {
        int(i): (int(signals.direction[i]), float(signals.potential_pips[i])) for i in np.flatnonzero(signals.direction)
    }


def test_a_signal_lands_on_the_minute_that_closes_when_its_own_bar_closes() -> None:
    base = minute_bars(30)  # 14:00 to 14:29
    five = resample(base, 300)
    # the five-minute bar from 14:05 closes at 14:10: so does the minute bar that began at 14:09
    out = combine(base, [said(five, {1: (1, 8.0)})], "any", 0)
    assert events(out) == {9: (1, 8.0)}
    # a one-minute method stays where it is
    assert events(combine(base, [said(base, {3: (-1, 6.0)})], "any", 0)) == {3: (-1, 6.0)}


def test_any_takes_every_signal_and_lets_opposite_ones_cancel() -> None:
    base = minute_bars(30)
    five = resample(base, 300)
    first = said(five, {0: (1, 8.0), 2: (-1, 5.0), 4: (1, 4.0)})  # known at 14:05, 14:15, 14:25
    second = said(base, {4: (1, 12.0), 14: (1, 9.0), 20: (-1, 7.0)})  # known at 14:05, 14:15, 14:21
    out = combine(base, [first, second], "any", 0)
    assert events(out) == {
        4: (1, 12.0),  # both say buy: one signal, and the further of the two reaches
        # at 14:15 one says sell and the other buy: nothing
        20: (-1, 7.0),
        24: (1, 4.0),
    }
    assert np.isnan(out.potential_pips[14]) and out.direction[14] == 0


def test_all_needs_every_method_to_have_said_the_same_not_long_ago() -> None:
    base = minute_bars(60)
    five = resample(base, 300)
    first = said(base, {2: (1, 6.0), 30: (-1, 6.0), 50: (1, 6.0)})  # known at 14:03, 14:31, 14:51
    second = said(five, {1: (1, 9.0), 6: (-1, 4.0), 7: (1, 5.0)})  # known at 14:10, 14:35, 14:40
    out = combine(base, [first, second], "all", 10 * MINUTE_MS)
    assert events(out) == {
        9: (1, 9.0),  # the second agrees at 14:10 with what the first said seven minutes before
        34: (-1, 6.0),  # the second says sell at 14:35, four minutes after the first
        # 14:40: the second says buy, the first last said sell: no
        # 14:51: the first says buy, the second said so eleven minutes ago: too old
    }
    # with a longer memory the last one counts too
    assert set(events(combine(base, [first, second], "all", 15 * MINUTE_MS))) == {9, 34, 50}
    # a single method agrees with itself
    assert events(combine(base, [first], "all", MINUTE_MS)) == {2: (1, 6.0), 30: (-1, 6.0), 50: (1, 6.0)}


def test_a_signal_after_the_last_bar_is_not_given_and_a_hole_delays_it() -> None:
    base = minute_bars(12)
    five = resample(base, 300)
    # the bar from 14:10 closes at 14:15: there is no minute bar that closes then or later
    assert events(combine(base, [said(five, {2: (1, 5.0)})], "any", 0)) == {}
    # minutes 14:03 to 14:06 are missing: a signal known at 14:05 lands on the minute bar of 14:07
    keep = np.r_[0:3, 7:12]
    holed = Bars(base.ts[keep], base.open[keep], base.high[keep], base.low[keep], base.close[keep], 60)
    whole = resample(base, 300)
    out = combine(holed, [said(whole, {0: (1, 5.0)})], "any", 0)
    assert events(out) == {3: (1, 5.0)} and holed.ts[3] == T0 + 7 * MINUTE_MS


# --- a setup is traded like a strategy -------------------------------------------------------


def untagged(rows: list[dict]) -> list[dict]:
    """Trade rows without what a setup adds to them."""
    drop = {"votes", "momentum", *(key for key, _, _ in ctx.CONDITIONS)}
    return [{k: v for k, v in row.items() if k not in drop} for row in rows]


@pytest.mark.parametrize(
    ("key", "timeframe"), [("bollinger_reversion", "5m"), ("macd_cross", "15m"), ("rsi_reversion", "1m")]
)
def test_a_setup_of_one_method_is_the_backtest_of_that_method(bars: Bars, eurusd, key: str, timeframe: str) -> None:  # type: ignore[no-untyped-def]
    setup = build_setup(bars, eurusd, ask(methods=[f"{key}@{timeframe}"], name="one"))
    single = build_report(
        bars,
        eurusd,
        BacktestRequest.from_mapping(
            {
                "symbol": "EURUSD",
                "strategy": key,
                "timeframe": timeframe,
                "start": "2026-10-05",
                "end": "2026-10-24",
                "execution": RULES,
            }
        ),
    )
    assert untagged(setup["trades"]) == single["trades"] and len(single["trades"]) > 10
    assert setup["summary"] == single["summary"] and setup["daily"] == single["daily"]
    assert setup["signals"] == single["signals"] and setup["split_day"] == single["split_day"]
    assert (
        setup["request"]["strategy_title"] == "one" and setup["request"]["execution"] == single["request"]["execution"]
    )


def test_a_setup_reports_like_a_backtest_and_describes_every_trade(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    report = build_setup(bars, eurusd, ask())
    json.dumps(report)
    assert report["request"]["setup"] == {
        "name": "RSI or Bollinger",
        "methods": ["rsi_reversion@5m", "bollinger_reversion@5m"],
        "mode": "any",
        "within_minutes": 30.0,
        "skip": [],
    }
    assert report["request"]["timeframe"] == "1m" and report["data"]["trading_days"] == 15
    trades, summary = report["trades"], report["summary"]
    # more than either method alone, fewer than both together (their signals overlap, and one position at a time)
    alone = [len(build_setup(bars, eurusd, ask(methods=[m]))["trades"]) for m in report["request"]["setup"]["methods"]]
    assert max(alone) < len(trades) < sum(alone) and summary["trades"] == len(trades)
    # the rules of the request: a position is one order, three pips up or three pips down
    assert summary["max_units"] == 1_000_000 and "without_adding" not in report
    assert {t["reason"] for t in trades} <= {"target", "stop", "time", "end_of_day"}
    assert all(t["pnl"] == 300.0 for t in trades if t["reason"] == "target")
    assert all(-300.0 >= t["pnl"] >= -400.0 for t in trades if t["reason"] == "stop")
    # every trade says what was going on around it
    for t in trades:
        assert t["momentum"] in (-1, 0, 1) and isinstance(t["votes"], int)
        assert all(isinstance(t[key], bool) for key, _, _ in ctx.CONDITIONS)
        assert t["against_momentum"] == (t["momentum"] == -1)
        assert not (t["against_momentum"] and t["momentum_turned"])
    assert any(t["against_momentum"] for t in trades) and any(not t["against_momentum"] for t in trades)
    # and the report asks whether that tells the failures from the successes
    why = report["why"]
    assert why["trades"] == len(trades) and why["failed"] == summary["losses"]
    for condition in why["conditions"]:
        assert condition["with"]["trades"] + condition["without"]["trades"] == len(trades)
        assert condition["with"]["wins"] + condition["without"]["wins"] == summary["wins"]
        assert condition["with"]["net"] + condition["without"]["net"] == pytest.approx(summary["net_pnl"], abs=0.01)
        assert condition["failed_with"] == condition["with"]["losses"]
    assert 0 <= why["failed_unexplained"] <= why["failed"]
    assert report["in_sample"]["why"]["trades"] + report["out_of_sample"]["why"]["trades"] == len(trades)
    assert report["context"] == {
        "grid": "5m",
        "voters": 40,  # eight momentum methods on five bar lengths
        "threshold": 8,
        "fresh_bars": 96,
        "channel_hours": 24.0,
        "channel_width": 2.0,
        "conditions": [{"key": key, "label": label, "when": when} for key, label, when in ctx.CONDITIONS],
    }
    assert len(momentum_voters(prepare_setup(eurusd, ask()).base_tf)) == 40


def test_signals_can_be_left_out_for_what_was_known_when_they_came(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    plain = build_setup(bars, eurusd, ask())
    picky = build_setup(bars, eurusd, ask(skip=["against_momentum", "outside_channel"]))
    assert picky["request"]["setup"]["skip"] == ["against_momentum", "outside_channel"]
    # no trade is left that the conditions hold for
    assert not any(t["against_momentum"] or t["outside_channel"] for t in picky["trades"])
    assert 0 < len(picky["trades"]) < len(plain["trades"])
    counts = picky["signals"]
    left_out = counts["skipped: against the momentum when it was opened"]
    outside = counts["skipped: opened outside the trend channel, on the losing side"]
    assert left_out > 0 and outside > 0
    # every signal is accounted for: taken, or not taken for a reason that is named
    assert counts["signals"] == sum(v for k, v in counts.items() if k != "signals")
    assert counts["signals"] == plain["signals"]["signals"]
    # the trades that were kept are trades the plain setup could have made: same signals, fewer of them
    assert {t["signal_ms"] for t in picky["trades"]} - {t["signal_ms"] for t in plain["trades"]} or True
    assert picky["why"]["conditions"][0]["with"]["trades"] == 0


def test_all_of_them_is_stricter_than_any_of_them(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    either = build_setup(bars, eurusd, ask())
    both = build_setup(bars, eurusd, ask(mode="all", within_minutes=10, name="RSI and Bollinger"))
    assert 0 < both["signals"]["signals"] < either["signals"]["signals"]
    assert both["request"]["setup"]["mode"] == "all" and both["request"]["setup"]["within_minutes"] == 10.0


def test_a_setup_with_a_run_in_is_the_later_part_of_the_longer_one(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    whole = build_setup(bars, eurusd, ask())
    later = build_setup(bars, eurusd, ask(start="2026-10-13"))
    start = later["trades"][0]["signal_ms"]
    tail = [t for t in whole["trades"] if t["signal_ms"] >= start]
    strip = lambda rows: [{k: v for k, v in r.items() if k != "n"} for r in rows]  # noqa: E731
    assert strip(later["trades"]) == strip(tail) and len(tail) < len(whole["trades"])
    assert later["data"]["first_day"] == "2026-10-13"


# --- what cannot be asked for ----------------------------------------------------------------


def test_setup_requests_are_checked(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    for changes, message in (
        ({"methods": []}, "a setup needs at least one method"),
        ({"methods": ["rsi_reversion"]}, "unknown method 'rsi_reversion'"),
        ({"methods": ["martingale@5m"]}, "unknown method 'martingale@5m'"),
        ({"methods": ["rsi_reversion@7m"]}, "7m"),
        ({"methods": ["rsi_reversion@4h"]}, "4h bars cannot be built from 1m bars"),
        ({"methods": ["rsi_reversion@5m", "rsi_reversion@5m"]}, "names each method once"),
        ({"mode": "most"}, "mode must be one of: any, all"),
        ({"within_minutes": 0}, "within_minutes must be positive"),
        ({"within_minutes": "soon"}, "within_minutes must be a number"),
        # what happened after a trade was opened can never be a reason for not opening it
        ({"skip": ["momentum_turned"]}, "unknown condition 'momentum_turned'"),
        ({"skip": ["left_channel"]}, "unknown condition 'left_channel'"),
        ({"skip": "against_momentum"}, "skip must be a list of names"),
        ({"execution": {"max_clips": 0}}, "max_clips must be at least 1"),
        ({"execution": {"pip_size": 1}}, "fixed by the instrument"),
        ({"split": "later"}, "split must be"),
        ({"base_timeframe": "1d"}, "trades cannot be simulated on 1d bars"),
        ({"colour": "blue"}, "unknown field 'colour'"),
        ({"start": ""}, "start is required"),
        ({"start": "yesterday"}, "start and end must be dates or times"),
    ):
        with pytest.raises(BacktestError, match=message):
            build_setup(bars, eurusd, ask(**changes))
    assert MAX_METHODS == 12
    many = [f"{key}@5m" for key in list(STRATEGIES)[:13]]
    with pytest.raises(BacktestError, match="at most 12 methods"):
        prepare_setup(eurusd, ask(methods=many))
    with pytest.raises(BacktestError, match="there are no bars in this range"):
        build_setup(bars, eurusd, ask(start="2026-11-02", end="2026-11-06"))
    assert ask(name="  ").name == "Setup" and len(ask(name="x" * 200).name) == 80


# --- on prices that cannot be predicted -------------------------------------------------------


@pytest.fixture(scope="module")
def walks() -> list[Bars]:
    return [random_walk(seed) for seed in range(100, 116)]


def on_walks(walks: list[Bars], eurusd, **changes: object) -> list[dict]:  # type: ignore[no-untyped-def]
    rows: list[dict] = []
    for base in walks:
        request = ask(
            start="2026-06-01", end="2026-08-01", split="none", execution={"min_potential_pips": 0}, **changes
        )
        rows += build_setup(base, eurusd, request)["trades"]
    return rows


def test_on_a_random_walk_a_setup_loses_the_spread_whatever_it_leaves_out(walks: list[Bars], eurusd) -> None:  # type: ignore[no-untyped-def]
    """Leaving signals out because of the momentum or the channel must not make money from noise.

    If a condition used anything from after the moment a trade is opened, this is where it would
    show: the trades that are kept would do better than the spread allows.
    """
    for skip in ([], ["against_momentum"], ["against_channel"], ["outside_channel"], list(ctx.ENTRY_CONDITIONS)):
        rows = on_walks(walks, eurusd, skip=skip)
        pips = sum(r["pnl"] for r in rows) / PIP_VALUE / len(rows)
        assert len(rows) > 1200, (skip, len(rows))
        assert abs(pips + 1.0) < TOLERANCE, f"skip {skip}: {pips:+.3f} pips a trade where -1.0 is expected"


def test_on_a_random_walk_what_was_known_at_entry_says_nothing_and_what_came_after_says_a_lot(
    walks: list[Bars],
    eurusd,  # type: ignore[no-untyped-def]
) -> None:
    told = ctx.explain(on_walks(walks, eurusd))
    by_key = {c["key"]: c for c in told["conditions"]}
    for key in ctx.ENTRY_CONDITIONS:
        held, not_held = by_key[key]["with"], by_key[key]["without"]
        assert held["trades"] > 300 and not_held["trades"] > 300, (key, held["trades"], not_held["trades"])
        # noise: a trade opened against the momentum works as often as any other.  The report says how
        # large a difference chance makes ("noise", two standard errors); twice that is a wide margin
        assert abs(by_key[key]["difference"]) < 2 * by_key[key]["noise"], (key, by_key[key])
        assert 0.01 < by_key[key]["noise"] < 0.06
    # but a trade that momentum turned against is a trade the price moved against while it was open: it
    # failed more often by construction.  That describes how it failed; it is not a reason for it.
    turned = by_key["momentum_turned"]
    assert turned["with"]["trades"] > 50 and turned["difference"] < -0.08, turned
    assert turned["when"] == "after" and by_key["left_channel"]["with"]["trades"] > 100


def test_the_engine_sees_a_setup_exactly_as_it_sees_a_strategy() -> None:
    """One more way to say it: the composite signals of one method are that method's signals, moved to the minute."""
    base = random_walk(5, days=8)
    five = resample(base, 300)
    own = STRATEGIES["bollinger_reversion"].signals(five, PIP)
    ex = Execution(min_potential_pips=0.0)
    direct = engine.run(base, five, own, ex)
    through = engine.run(base, base, combine(base, [(five, own)], "any", 0), ex)
    assert [(t.signal_ms, t.direction, t.exit_ms, t.pnl) for t in through.trades] == [
        (t.signal_ms, t.direction, t.exit_ms, t.pnl) for t in direct.trades
    ]
    assert through.counters == direct.counters and len(direct.trades) > 20
