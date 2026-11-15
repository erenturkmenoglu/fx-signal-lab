"""The board (every method at once) and the signals of a window, checked against single backtests."""

from __future__ import annotations

import json

import numpy as np
import pytest

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.research import board as board_module
from fxlab.research.bars import Bars, resample
from fxlab.research.board import BoardRequest, build_board, prepare_board, window_signals
from fxlab.research.report import BacktestError, BacktestRequest, build_report
from fxlab.research.strategies import STRATEGIES
from fxlab.timeutil import MINUTE_MS, iso
from tests.conftest import utc_ms
from tests.unit.test_research_report import market

#: looser rules than the defaults, so that the short test market produces enough trades
RULES = {"min_potential_pips": 2, "target_pips": 3, "add_step_pips": 3, "stop_pips": 6, "max_clips": 5}


@pytest.fixture(scope="module")
def eurusd(registry: InstrumentRegistry):  # type: ignore[no-untyped-def]
    return registry.get("EURUSD")


@pytest.fixture(scope="module")
def bars() -> Bars:
    return market()  # 15 weekdays from Monday 5 October 2026, 06:00 to 21:00 UTC, one-minute bars


def ask(**changes: object) -> BoardRequest:
    values: dict = {"symbol": "EURUSD", "start": "2026-10-05", "end": "2026-10-24", "execution": RULES}
    values.update(changes)
    return BoardRequest.from_mapping(values)


@pytest.fixture(scope="module")
def result(bars: Bars, eurusd) -> dict:  # type: ignore[no-untyped-def]
    return build_board(bars, eurusd, ask(timeframes=["5m", "15m"]))


def test_a_board_holds_every_strategy_on_every_bar_length(result: dict) -> None:
    ids = [c["id"] for c in result["combos"]]
    # ... except that a strategy which follows the clock is there once, on its usual bars:
    # the same signals on other bars would be the same line again
    clock = [key for key, strategy in STRATEGIES.items() if strategy.clock_only]
    assert clock == ["session_drift"] and STRATEGIES["session_drift"].timeframe == "5m"
    assert ids == [f"{key}@{tf}" for tf in ("5m", "15m") for key in STRATEGIES if tf == "5m" or key not in clock]
    assert len(ids) == 2 * len(STRATEGIES) - 1
    assert result["request"]["strategies"] == list(STRATEGIES) and result["request"]["timeframes"] == ["5m", "15m"]
    assert "random_entry@5m" in ids  # the method that knows nothing is always there to compare against
    assert result["variants"] == ["all", "one"] and (result["currency"], result["pip_value"]) == ("USD", 100.0)
    assert len(result["days"]) == 15 and result["days"][0] == "2026-10-05" and result["split_day"] == "2026-10-20"
    assert result["day_spans"][0] == [utc_ms(2026, 10, 5, 6, 0), utc_ms(2026, 10, 5, 20, 59)]
    first = result["combos"][0]
    assert (first["title"], first["short"], first["family"], first["timeframe"]) == (
        "RSI reversion",
        "RSI",
        "reversion",
        "5m",
    )
    assert any(f"{len(ids)} methods were tried on the same prices" in w for w in result["warnings"])
    json.dumps(result)


def test_each_line_of_the_board_is_the_backtest_of_that_method(result: dict, bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    """The board is a shorter way to the same numbers, not a second opinion."""
    checked = 0
    for combo in result["combos"]:
        if combo["strategy"] not in ("bollinger_reversion", "macd_cross", "donchian_breakout", "random_entry"):
            continue
        report = build_report(
            bars,
            eurusd,
            BacktestRequest.from_mapping(
                {
                    "symbol": "EURUSD",
                    "strategy": combo["strategy"],
                    "timeframe": combo["timeframe"],
                    "start": "2026-10-05",
                    "end": "2026-10-24",
                    "execution": RULES,
                }
            ),
        )
        assert combo["signals"] == report["signals"]["signals"]
        for name, summary, daily in (
            ("all", report["summary"], report["daily"]),
            ("one", report["without_adding"]["summary"], report["without_adding"]["daily"]),
        ):
            got = combo[name]
            assert (got["trades"], got["wins"], got["losses"]) == (
                summary["trades"],
                summary["wins"],
                summary["losses"],
            )
            assert got["net"] == pytest.approx(summary["net_pnl"], abs=0.01)
            assert got["profit_factor"] == summary["profit_factor"] and got["largest_loss"] == summary["largest_loss"]
            assert got["max_drawdown"] == pytest.approx(summary["max_drawdown"], abs=0.01)
            assert got["max_units"] == summary["max_units"]
            assert (got["days_traded"], got["days_won"], got["days_lost"]) == (
                summary["days_traded"],
                summary["days_positive"],
                summary["days_negative"],
            )
            assert (got["best_day"], got["worst_day"]) == (summary["best_day"], summary["worst_day"])
            assert got["daily"]["trades"] == [d["trades"] for d in daily]
            assert got["daily"]["wins"] == [d["wins"] for d in daily]
            assert got["daily"]["pnl"] == pytest.approx([d["pnl"] for d in daily], abs=0.01)
            assert all(
                w + lost <= n
                for w, lost, n in zip(got["daily"]["wins"], got["daily"]["losses"], got["daily"]["trades"], strict=True)
            )
        for part in ("in_sample", "out_of_sample"):
            assert combo["all"][part]["trades"] == report[part]["summary"]["trades"]
            assert combo["all"][part]["wins"] == report[part]["summary"]["wins"]
            assert combo["all"][part]["net"] == pytest.approx(report[part]["summary"]["net_pnl"], abs=0.01)
        checked += 1
        assert combo["all"]["trades"] > 5, combo["id"]
    assert checked == 8


def test_the_two_parts_of_the_period_add_up_and_days_add_up(result: dict) -> None:
    for combo in result["combos"]:
        for name in result["variants"]:
            got = combo[name]
            assert got["in_sample"]["trades"] + got["out_of_sample"]["trades"] == got["trades"]
            if got["wins"] and got["losses"]:  # what is won and lost on average adds up to the net result
                total = got["wins"] * got["average_win"] + got["losses"] * got["average_loss"]
                assert total == pytest.approx(got["net"], abs=0.01 * got["trades"])
                assert got["average_win"] > 0 > got["average_loss"] >= got["largest_loss"]
            assert got["in_sample"]["net"] + got["out_of_sample"]["net"] == pytest.approx(got["net"], abs=0.02)
            assert sum(got["daily"]["trades"]) == got["trades"] and sum(got["daily"]["wins"]) == got["wins"]
            assert sum(got["daily"]["losses"]) == got["losses"]
            assert sum(got["daily"]["pnl"]) == pytest.approx(got["net"], abs=0.05)
            assert len(got["daily"]["pnl"]) == len(result["days"])
        assert combo["one"]["max_units"] in (0.0, 1_000_000.0)


def test_progress_is_reported_method_by_method(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    seen: list[tuple[int, int, str]] = []
    out = build_board(
        bars, eurusd, ask(timeframes=["15m"], strategies=["rsi_reversion", "random_entry"]), lambda *a: seen.append(a)
    )
    assert seen == [(1, 2, "rsi_reversion@15m"), (2, 2, "random_entry@15m")]
    assert [c["id"] for c in out["combos"]] == ["rsi_reversion@15m", "random_entry@15m"]


def test_a_strategy_that_follows_the_clock_is_on_the_board_once(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    seen: list[tuple[int, int, str]] = []
    out = build_board(
        bars,
        eurusd,
        ask(timeframes=["15m", "1h"], strategies=["session_drift", "random_entry"]),
        lambda *a: seen.append(a),
    )
    # its usual five-minute bars were not asked for: it runs on the first length that was
    assert [c["id"] for c in out["combos"]] == ["session_drift@15m", "random_entry@15m", "random_entry@1h"]
    assert [s[:2] for s in seen] == [(1, 3), (2, 3), (3, 3)]
    # and that loses nothing: on other bars it signals on the same occasions (at the same moments
    # where the bars around the session opens are complete; this market begins at one of the opens)
    again = build_board(bars, eurusd, ask(timeframes=["5m"], strategies=["session_drift"]))
    assert again["combos"][0]["signals"] == out["combos"][0]["signals"] > 0
    assert again["combos"][0]["all"]["trades"] == out["combos"][0]["all"]["trades"] > 0


def test_a_board_runs_in_on_the_bars_before_its_period(result: dict, bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    """The days of a later period are the same days of the longer board: nothing depends on where a period begins."""
    later = build_board(bars, eurusd, ask(start="2026-10-13", timeframes=["5m", "15m"]))
    assert later["days"] == result["days"][6:] and later["day_spans"] == result["day_spans"][6:]
    assert later["data"]["run_in_from"] == iso(int(bars.ts[0])) and result["data"]["run_in_from"] is None
    assert later["data"]["base_bars"] == 9 * 15 * 60 and later["data"]["first_day"] == "2026-10-13"
    assert any(w.startswith("No history is stored before this period") for w in result["warnings"])
    assert not any("stored before this period" in w for w in later["warnings"])
    traded = 0
    for long, short in zip(result["combos"], later["combos"], strict=True):
        assert long["id"] == short["id"] and 0 <= short["signals"] <= long["signals"]
        for name in ("all", "one"):
            for series in ("trades", "wins", "losses", "pnl"):
                assert short[name]["daily"][series] == long[name]["daily"][series][6:], (long["id"], name, series)
            assert short[name]["trades"] == sum(long[name]["daily"]["trades"][6:])
            traded += short[name]["trades"]
    assert traded > 100
    # without the earlier bars the first day is thinner for the methods that look far back
    first = int(np.searchsorted(bars.ts, utc_ms(2026, 10, 13)))
    late = Bars(bars.ts[first:], bars.open[first:], bars.high[first:], bars.low[first:], bars.close[first:], 60)
    cold = build_board(late, eurusd, ask(start="2026-10-13", timeframes=["5m", "15m"]))
    unsettled = {c["id"]: c["signals"] for c in cold["combos"]}
    settled = {c["id"]: c["signals"] for c in later["combos"]}
    assert unsettled["macd_cross@15m"] < settled["macd_cross@15m"]  # 26 bars of a quarter of an hour: most of a day
    assert unsettled["donchian_breakout@15m"] < settled["donchian_breakout@15m"]
    assert unsettled["random_entry@5m"] == settled["random_entry@5m"]  # looks at nothing: nothing to settle
    assert sum(unsettled.values()) < sum(settled.values())


def test_a_board_without_adding_has_one_set_of_numbers(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    out = build_board(
        bars, eurusd, ask(timeframes=["15m"], strategies=["rsi_reversion"], execution={**RULES, "max_clips": 1})
    )
    assert out["variants"] == ["one"] and "all" not in out["combos"][0]
    assert not any("methods were tried" in w for w in out["warnings"])  # one method: nothing to choose from
    unsplit = build_board(bars, eurusd, ask(timeframes=["15m"], strategies=["rsi_reversion"], split="none"))
    assert unsplit["split_day"] is None and "in_sample" not in unsplit["combos"][0]["one"]


def test_board_requests_are_checked(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    plan = prepare_board(eurusd, ask())
    assert [tf.code for tf in plan.timeframes] == ["1m", "5m", "15m", "30m", "1h"]  # every length from the base up
    assert len(plan.strategies) == len(STRATEGIES) and plan.execution.target_pips == 3.0
    assert [tf.code for tf in prepare_board(eurusd, ask(base_timeframe="5m")).timeframes] == ["5m", "15m", "30m", "1h"]
    assert [tf.code for tf in prepare_board(eurusd, ask(timeframes=["15m", "5m", "15m"])).timeframes] == ["15m", "5m"]
    for changes, message in (
        ({"strategies": ["martingale"]}, "unknown strategy 'martingale'"),
        ({"timeframes": ["4h"]}, "follow the trading day"),
        ({"timeframes": ["7m"]}, "unsupported timeframe"),
        ({"timeframes": ["30s"]}, "cannot be built from 1m bars"),
        ({"base_timeframe": "1d"}, "cannot be simulated on 1d bars"),
        ({"execution": {"max_clips": 0}}, "max_clips must be at least 1"),
        ({"execution": {"pip_size": 1}}, "fixed by the instrument"),
        ({"split": "later"}, "split must be"),
        ({"chart_timeframe": "1d"}, "follow the trading day"),
    ):
        with pytest.raises(BacktestError, match=message):
            build_board(bars, eurusd, ask(**changes))
    for bad, message in (
        ({"symbol": "EURUSD", "start": "2026-10-05"}, "end is required"),
        ({"symbol": "EURUSD", "start": 1, "end": 2, "strategies": "rsi_reversion"}, "strategies must be a list"),
        ({"symbol": "EURUSD", "start": 1, "end": 2, "timeframes": [5]}, "timeframes must be a list"),
        ({"symbol": "EURUSD", "start": 1, "end": 2, "params": {}}, "unknown field 'params'"),
        ({"symbol": "EURUSD", "start": "x", "end": "y"}, "must be dates"),
    ):
        with pytest.raises(BacktestError, match=message):
            BoardRequest.from_mapping(bad)
    with pytest.raises(BacktestError, match="no bars"):
        build_board(Bars.from_rows([], 60), eurusd, ask())
    with pytest.raises(BacktestError, match="not of the base timeframe"):
        build_board(resample(bars, 300), eurusd, ask())


# --- the signals of a window ---------------------------------------------------------------

WINDOW = ("2026-10-15T00:00:00Z", "2026-10-17T00:00:00Z")  # Thursday and Friday of the second week


@pytest.fixture(scope="module")
def window(bars: Bars, eurusd) -> dict:  # type: ignore[no-untyped-def]
    return window_signals(bars, eurusd, ask(start=WINDOW[0], end=WINDOW[1], timeframes=["5m", "15m"]))


def test_the_trades_of_a_window_are_the_trades_the_board_counts(window: dict, result: dict) -> None:
    """The chart of a week and the table of days are two views of the same runs."""
    days = {name: k for k, name in enumerate(result["days"])}
    for combo in result["combos"]:
        mine = [s for s in window["signals"] if s["method"] == combo["id"] and "pnl" in s]
        for day in ("2026-10-15", "2026-10-16"):  # the two trading days the window covers
            closed = [s for s in mine if iso(s["exit_ms"])[:10] == day]  # this market trades from 06:00 to 21:00 UTC
            daily = combo["all"]["daily"]
            assert len(closed) == daily["trades"][days[day]], (combo["id"], day)
            assert sum(1 for s in closed if s["pnl"] > 0) == daily["wins"][days[day]]
            assert round(sum(s["pnl"] for s in closed), 2) == daily["pnl"][days[day]]
    assert sum(1 for s in window["signals"] if "pnl" in s) > 100


def test_a_window_holds_every_signal_of_every_method_once(window: dict, bars: Bars) -> None:
    start, end = utc_ms(2026, 10, 15), utc_ms(2026, 10, 17)
    signals = window["signals"]
    assert signals and all(start <= s["t"] < end for s in signals)
    assert [(s["t"], s["method"]) for s in signals] == sorted((s["t"], s["method"]) for s in signals)
    assert len({(s["t"], s["method"]) for s in signals}) == len(signals)
    assert len(window["methods"]) == 2 * len(STRATEGIES) - 1  # the strategy that follows the clock: once
    assert [m["id"] for m in window["methods"] if m["strategy"] == "session_drift"] == ["session_drift@5m"]
    assert window["methods"][0] == {
        "id": "rsi_reversion@5m",
        "strategy": "rsi_reversion",
        "short": "RSI",
        "title": "RSI reversion",
        "family": "reversion",
        "style": "reversion",
        "timeframe": "5m",
    }
    # counted independently: the strategy's own signals whose bar closed inside the window
    for key, timeframe, seconds in (
        ("bollinger_reversion", "5m", 300),
        ("macd_cross", "15m", 900),
        ("random_entry", "5m", 300),
    ):
        signal_bars = resample(bars, seconds)
        own = STRATEGIES[key].signals(signal_bars, 0.0001)
        known = signal_bars.ts[own.direction != 0] + seconds * 1000
        expected = sorted(int(t) for t in known if start <= t < end)
        got = [s for s in signals if s["method"] == f"{key}@{timeframe}"]
        assert [s["t"] for s in got] == expected and len(expected) > 3
        directions = {int(t): int(d) for t, d in zip(signal_bars.ts + seconds * 1000, own.direction, strict=True)}
        assert all(s["d"] == directions[s["t"]] for s in got)
    statuses = {s["status"] for s in signals}
    assert statuses <= {
        "target",
        "stop",
        "time",
        "end_of_day",
        "end_of_data",
        "potential",
        "busy",
        "hours",
        "day_end",
        "no_price",
        "off",
    }
    assert {"target", "potential", "busy"} <= statuses
    json.dumps(window)


def test_traded_signals_in_a_window_are_the_trades_of_the_backtest(window: dict, bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    for key, timeframe in (("bollinger_reversion", "5m"), ("stochastic_reversion", "15m")):
        report = build_report(
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
        start, end = utc_ms(2026, 10, 15), utc_ms(2026, 10, 17)
        trades = [t for t in report["trades"] if start <= t["signal_ms"] < end]
        traded = [s for s in window["signals"] if s["method"] == f"{key}@{timeframe}" and "pnl" in s]
        assert len(traded) == len(trades) > 2
        for s, t in zip(traded, trades, strict=True):
            assert (s["t"], s["status"], s["clips"], s["entry_ms"], s["exit_ms"]) == (
                t["signal_ms"],
                t["reason"],
                t["clips"],
                t["entry_ms"],
                t["exit_ms"],
            )
            assert s["pnl"] == t["pnl"] and s["d"] == (1 if t["direction"] == "long" else -1)
            assert (s["entry_price"], s["exit_price"]) == (t["entry_price"], t["exit_price"])
            assert s["potential"] == pytest.approx(t["potential_pips"], abs=0.06)
        skipped = [s for s in window["signals"] if s["method"] == f"{key}@{timeframe}" and "pnl" not in s]
        assert skipped and all(s["status"] in ("potential", "busy", "hours", "day_end", "no_price") for s in skipped)


def test_a_window_comes_with_the_bars_to_draw_it_on(window: dict, bars: Bars) -> None:
    chart = window["bars"]
    assert (chart["timeframe"], chart["seconds"]) == ("5m", 300)
    inside = (bars.ts >= utc_ms(2026, 10, 15)) & (bars.ts < utc_ms(2026, 10, 17))  # the window, not the days after it
    five = resample(
        Bars(bars.ts[inside], bars.open[inside], bars.high[inside], bars.low[inside], bars.close[inside], 60), 300
    )
    assert chart["t"] == five.ts.tolist() and chart["c"] == five.close.tolist() and chart["h"] == five.high.tolist()
    assert chart["t"][0] == utc_ms(2026, 10, 15, 6, 0) and len(chart["t"]) == 2 * 180  # two days of 15 hours
    assert all(t % (5 * MINUTE_MS) == 0 for t in chart["t"])
    assert window["run_in_from"] == iso(int(bars.ts[0])) and window["request"]["chart_timeframe"] == "5m"


def test_a_window_says_what_momentum_and_the_trend_channel_were_bar_by_bar(window: dict, bars: Bars) -> None:
    """Under the prices: what the momentum methods said, the mean-reversion signals, and the trend channel."""
    context, chart, signals = window["context"], window["bars"], window["signals"]
    n, span = len(chart["t"]), chart["seconds"] * 1000
    styles = {m["id"]: m["style"] for m in window["methods"]}
    voters = [m for m in window["methods"] if m["style"] == "momentum"]
    assert context["voters"] == len(voters) == 16 and context["threshold"] == 4  # a fifth of them, rounded up
    assert {m["strategy"] for m in voters} == {key for key, s in STRATEGIES.items() if s.style == "momentum"}
    assert all(len(context[name]) == n for name in ("votes", "state"))
    assert all(len(context["channel"][name]) == n for name in ("middle", "upper", "lower"))
    assert all(len(context["reversion"][name]) == n for name in ("buys", "sells"))

    # every signal is on the bar of the chart by whose close it had come
    assert all(chart["t"][s["bar"]] < s["t"] <= chart["t"][s["bar"]] + span for s in signals)

    # the reading, counted independently: each momentum method points the way of its last signal for
    # 96 of its own bars; a bar of the chart shows what had been said by its close
    expected = np.zeros(n, dtype=int)
    for method in voters:
        seconds = {"5m": 300, "15m": 900}[method["timeframe"]]
        signal_bars = resample(bars, seconds)
        own = STRATEGIES[method["strategy"]].signals(signal_bars, 0.0001)
        closed = signal_bars.ts + seconds * 1000
        for i, t in enumerate(chart["t"]):
            done = np.flatnonzero((closed <= t + span) & (own.direction != 0))
            if len(done) and np.count_nonzero(closed <= t + span) - 1 - done[-1] < 96:
                expected[i] += int(own.direction[done[-1]])
    assert context["votes"] == expected.tolist()
    assert min(context["votes"]) <= -context["threshold"] and max(context["votes"]) >= context["threshold"]

    # the state: up once four more point up than down, and so it stays until four more point down
    state = context["state"]
    assert set(state) == {1, -1}
    for i in range(1, n):
        if context["votes"][i] >= 4:
            assert state[i] == 1
        elif context["votes"][i] <= -4:
            assert state[i] == -1
        else:
            assert state[i] == state[i - 1]
    turns = [i for i in range(1, n) if state[i] != state[i - 1]]
    assert [c["bar"] for c in context["changes"]] == turns == [10, 41, 195]
    assert all(c["to"] == state[c["bar"]] and c["t"] == chart["t"][c["bar"]] + span for c in context["changes"])
    # each change carries the reading that made it and the price of that moment
    assert all(c["votes"] == context["votes"][c["bar"]] and abs(c["votes"]) >= 4 for c in context["changes"])
    assert all(c["votes"] * c["to"] > 0 and c["price"] == chart["c"][c["bar"]] for c in context["changes"])

    # mean-reversion signals: the buys and the sells of each bar
    for name, d in (("buys", 1), ("sells", -1)):
        counted = [0] * n
        for sig in signals:
            if styles[sig["method"]] == "reversion" and sig["d"] == d:
                counted[sig["bar"]] += 1
        assert context["reversion"][name] == counted and sum(counted) > 20

    # the trend channel: a line through the day of five-minute closes before each bar, drawn on to it
    five = resample(bars, 300)
    channel = context["channel"]
    assert (channel["hours"], channel["width"]) == (24.0, 2.0)
    for i in (0, 57, n - 1):
        k = int(np.searchsorted(five.ts, chart["t"][i]))
        assert five.ts[k] == chart["t"][i]
        before = five.close[k - 288 : k]
        slope, level = np.polyfit(np.arange(288), before, 1)
        spread = float(np.std(before - (level + slope * np.arange(288))))
        assert channel["middle"][i] == pytest.approx(level + slope * 288, abs=6e-7)
        assert channel["upper"][i] - channel["middle"][i] == pytest.approx(2 * spread, abs=2e-6)
        assert channel["middle"][i] - channel["lower"][i] == pytest.approx(2 * spread, abs=2e-6)
    json.dumps(window)


def test_the_context_of_a_window_follows_the_bars_of_its_chart(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    """Whatever bars are drawn, a bar shows what was known when it closed."""
    five = window_signals(bars, eurusd, ask(start=WINDOW[0], end=WINDOW[1], timeframes=["5m", "15m"]))
    for code, seconds in (("1m", 60), ("30m", 1800)):
        other = window_signals(
            bars, eurusd, ask(start=WINDOW[0], end=WINDOW[1], timeframes=["5m", "15m"], chart_timeframe=code)
        )
        closes = np.asarray(five["bars"]["t"]) + 300_000
        span = seconds * 1000
        compared = 0
        for i, t in enumerate(other["bars"]["t"]):
            j = int(np.searchsorted(closes, t + span, side="right")) - 1  # the last five-minute bar closed by then
            if j < 0:
                continue  # the first minutes of the window: what was known then is from the day before it
            compared += 1
            for name in ("votes", "state"):
                assert other["context"][name][i] == five["context"][name][j], (code, name, i)
            assert other["context"]["channel"]["middle"][i] == five["context"]["channel"]["middle"][j]
        assert compared >= len(other["bars"]["t"]) - 4
        # the same signals and the same changes of momentum, on other bars
        assert [(s["t"], s["method"]) for s in other["signals"]] == [(s["t"], s["method"]) for s in five["signals"]]
        # ... with the reading and the price of the moment of the change, whatever the bars of the chart
        assert [(c["t"], c["to"], c["votes"], c["price"]) for c in other["context"]["changes"]] == [
            (c["t"], c["to"], c["votes"], c["price"]) for c in five["context"]["changes"]
        ]
        assert all(
            other["bars"]["t"][c["bar"]] < c["t"] <= other["bars"]["t"][c["bar"]] + span
            for c in other["context"]["changes"]
        )
        assert all(
            other["bars"]["t"][s["bar"]] < s["t"] <= other["bars"]["t"][s["bar"]] + span for s in other["signals"]
        )
        assert sum(other["context"]["reversion"]["buys"]) == sum(five["context"]["reversion"]["buys"])
        assert sum(other["context"]["reversion"]["sells"]) == sum(five["context"]["reversion"]["sells"])


def test_a_window_has_the_straight_channel_a_chart_would_show(window: dict, bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    """One line through the closes of the window with a band around it: the channel as it stands at the window's end."""
    chart, straight = window["bars"], window["context"]["channel"]["straight"]
    n = len(chart["t"])
    slope, level = np.polyfit(np.arange(n), chart["c"], 1)
    spread = float(np.std(np.asarray(chart["c"]) - (level + slope * np.arange(n))))
    assert (straight["width"], straight["bars"]) == (2.0, n)
    assert straight["start"] == pytest.approx(level, abs=6e-7)
    assert straight["end"] == pytest.approx(level + slope * (n - 1), abs=6e-7)
    assert straight["half"] == pytest.approx(2 * spread, abs=6e-7) and straight["half"] > 0.0005
    # nearly all closes of the window lie inside it: it was fitted to them
    line = straight["start"] + (straight["end"] - straight["start"]) * np.arange(n) / (n - 1)
    inside = np.abs(np.asarray(chart["c"]) - line) <= straight["half"]
    assert 0.9 < inside.mean() <= 1.0
    # its right-hand end is the channel known at the last bar: fitted to the bars up to and including it
    known = window_signals(bars, eurusd, ask(start=WINDOW[0], end="2026-10-16T12:00:00Z", timeframes=["5m", "15m"]))
    shorter = known["context"]["channel"]["straight"]
    assert shorter["bars"] < n and shorter["end"] != straight["end"]
    # a window too short for a line has none
    brief = window_signals(
        bars,
        eurusd,
        ask(start="2026-10-16T06:00:00Z", end="2026-10-16T07:00:00Z", timeframes=["15m"], strategies=["rsi_reversion"]),
    )
    assert len(brief["bars"]["t"]) == 12 and brief["context"]["channel"]["straight"] is None


def test_a_window_can_come_without_the_list_of_its_signals(window: dict, bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    """A month of signals is megabytes; the chart of what the methods said together does not need them."""
    short = window_signals(
        bars, eurusd, ask(start=WINDOW[0], end=WINDOW[1], timeframes=["5m", "15m"], list_signals=False)
    )
    assert short["signals"] is None and short["signal_count"] == window["signal_count"] == len(window["signals"])
    assert {k: v for k, v in short.items() if k != "signals"} == {k: v for k, v in window.items() if k != "signals"}
    assert len(json.dumps(short)) < len(json.dumps(window)) / 2
    with pytest.raises(BacktestError, match="list_signals must be true or false"):
        ask(list_signals="no")


def test_a_window_without_momentum_methods_has_no_momentum(bars: Bars, eurusd) -> None:  # type: ignore[no-untyped-def]
    out = window_signals(
        bars,
        eurusd,
        ask(start=WINDOW[0], end=WINDOW[1], timeframes=["15m"], strategies=["rsi_reversion", "random_entry"]),
    )
    context = out["context"]
    assert (context["voters"], context["threshold"], context["changes"]) == (0, 1, [])
    assert set(context["votes"]) == {0} and set(context["state"]) == {0}
    assert sum(context["reversion"]["buys"]) + sum(context["reversion"]["sells"]) == sum(
        1 for s in out["signals"] if s["method"] == "rsi_reversion@15m"
    )
    assert all(value is not None for value in context["channel"]["middle"])  # the channel needs no methods


def test_windows_that_cannot_be_drawn_are_refused(bars: Bars, eurusd, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(board_module, "MAX_CHART_BARS", 100)
    with pytest.raises(BacktestError, match="this window has 360 5m bars; a chart is given at most 100"):
        window_signals(bars, eurusd, ask(start=WINDOW[0], end=WINDOW[1], timeframes=["15m"]))
    hourly = window_signals(
        bars,
        eurusd,
        ask(start=WINDOW[0], end=WINDOW[1], timeframes=["15m"], chart_timeframe="1h", strategies=["rsi_reversion"]),
    )
    assert len(hourly["bars"]["t"]) == 30 and hourly["bars"]["timeframe"] == "1h"
    with pytest.raises(BacktestError, match="no bars in this window"):
        window_signals(bars, eurusd, ask(start="2026-12-01", end="2026-12-05"))


def test_a_signal_whose_potential_is_unknown_says_so() -> None:
    registry = InstrumentRegistry.from_yaml(__import__("tests.conftest", fromlist=["x"]).INSTRUMENTS_YAML)
    bars = market(days=4)
    out = window_signals(
        bars,
        registry.get("EURUSD"),
        ask(start="2026-10-06", end="2026-10-09", timeframes=["5m"], strategies=["pivot_reversion", "random_entry"]),
    )
    assert all(s["potential"] is None or isinstance(s["potential"], float) for s in out["signals"])
    assert any(s["method"] == "random_entry@5m" and s["potential"] == 998.0 for s in out["signals"])
