"""The board (every method at once) and the signals of a window, checked against single backtests."""

from __future__ import annotations

import json

import pytest

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.research import board as board_module
from fxlab.research.bars import Bars, resample
from fxlab.research.board import BoardRequest, build_board, prepare_board, window_signals
from fxlab.research.report import BacktestError, BacktestRequest, build_report
from fxlab.research.strategies import STRATEGIES
from fxlab.timeutil import DAY_MS, MINUTE_MS, iso
from tests.conftest import utc_ms
from tests.unit.test_research_report import market

#: looser rules than the defaults, so that the short test market produces enough trades
RULES = {"min_potential_pips": 2, "target_pips": 3, "add_step_pips": 3, "stop_pips": 6}


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
    assert ids == [f"{key}@{tf}" for tf in ("5m", "15m") for key in STRATEGIES]
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
    assert any("30 methods were tried on the same prices" in w for w in result["warnings"])
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
    return window_signals(bars, eurusd, ask(start=WINDOW[0], end=WINDOW[1], timeframes=["5m", "15m"]), 10 * DAY_MS)


def test_a_window_holds_every_signal_of_every_method_once(window: dict, bars: Bars) -> None:
    start, end = utc_ms(2026, 10, 15), utc_ms(2026, 10, 17)
    signals = window["signals"]
    assert signals and all(start <= s["t"] < end for s in signals)
    assert [(s["t"], s["method"]) for s in signals] == sorted((s["t"], s["method"]) for s in signals)
    assert len({(s["t"], s["method"]) for s in signals}) == len(signals)
    assert len(window["methods"]) == 2 * len(STRATEGIES)
    assert window["methods"][0] == {
        "id": "rsi_reversion@5m",
        "strategy": "rsi_reversion",
        "short": "RSI",
        "title": "RSI reversion",
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


def test_windows_that_cannot_be_drawn_are_refused(bars: Bars, eurusd, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(board_module, "MAX_CHART_BARS", 100)
    with pytest.raises(BacktestError, match="this window has 360 5m bars; a chart is given at most 100"):
        window_signals(bars, eurusd, ask(start=WINDOW[0], end=WINDOW[1], timeframes=["15m"]), 0)
    hourly = window_signals(
        bars,
        eurusd,
        ask(start=WINDOW[0], end=WINDOW[1], timeframes=["15m"], chart_timeframe="1h", strategies=["rsi_reversion"]),
        0,
    )
    assert len(hourly["bars"]["t"]) == 30 and hourly["bars"]["timeframe"] == "1h"
    with pytest.raises(BacktestError, match="no bars in this window"):
        window_signals(bars, eurusd, ask(start="2026-12-01", end="2026-12-05"), 0)


def test_a_signal_whose_potential_is_unknown_says_so() -> None:
    registry = InstrumentRegistry.from_yaml(__import__("tests.conftest", fromlist=["x"]).INSTRUMENTS_YAML)
    bars = market(days=4)
    out = window_signals(
        bars,
        registry.get("EURUSD"),
        ask(start="2026-10-06", end="2026-10-09", timeframes=["5m"], strategies=["pivot_reversion", "random_entry"]),
        0,
    )
    assert all(s["potential"] is None or isinstance(s["potential"], float) for s in out["signals"])
    assert any(s["method"] == "random_entry@5m" and s["potential"] == 998.0 for s in out["signals"])
