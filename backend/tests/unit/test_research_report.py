"""Totals, trading days and the report: numbers worked out by hand, and sums that must add up."""

from __future__ import annotations

import json

import numpy as np
import pytest

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.research import metrics
from fxlab.research.bars import Bars
from fxlab.research.engine import Execution, Fill, Trade
from fxlab.research.report import (
    BacktestError,
    BacktestRequest,
    build_report,
    default_execution,
    format_report,
    resolve_execution,
)
from fxlab.timeutil import MINUTE_MS, iso
from tests.conftest import utc_ms

EX = Execution()
PIP_VALUE = 100.0  # USD per pip on a clip of 1 mn EUR


def trade(pnl: float, exit_ms: int, clips: int = 1, direction: int = 1, reason: str = "target") -> Trade:
    """A trade with a given result (prices made to fit: one unit of price difference on ``clips`` clips)."""
    entry = 1.1000
    fills = tuple(Fill(exit_ms - 10 * MINUTE_MS, entry, 1_000_000.0) for _ in range(clips))
    exit_price = entry + direction * pnl / (clips * 1_000_000.0)
    return Trade(direction, exit_ms - 11 * MINUTE_MS, fills, exit_ms, exit_price, reason, 1.09, 1.1005, pnl, 9.0, 1.0)


def rows_and_days(trades: list[Trade], all_days: list[str] | None = None) -> tuple[list[dict], list[dict]]:
    exits = np.asarray([t.exit_ms for t in trades], dtype=np.int64)
    days = metrics.trading_days(exits, "America/New_York", "17:00")
    rows = metrics.trade_rows(trades, EX, days)
    return rows, metrics.daily_rows(rows, all_days if all_days is not None else sorted(set(days)), PIP_VALUE)


# --- trading days --------------------------------------------------------------------------


def test_a_trading_day_ends_at_five_in_the_afternoon_in_new_york() -> None:
    stamps = np.array(
        [
            utc_ms(2026, 11, 12, 14, 0),  # Thursday morning in New York
            utc_ms(2026, 11, 12, 21, 59),  # 16:59 there: still Thursday
            utc_ms(2026, 11, 12, 22, 0),  # 17:00: Friday's trading day has begun
            utc_ms(2026, 11, 13, 3, 0),  # Friday, before dawn in Europe
            utc_ms(2026, 11, 15, 22, 30),  # Sunday evening after the open: Monday's trading day
            utc_ms(2026, 7, 2, 20, 59),  # summer: 16:59 in New York
            utc_ms(2026, 7, 2, 21, 0),  # summer: 17:00
        ]
    )
    assert metrics.trading_days(stamps, "America/New_York", "17:00") == [
        "2026-11-12",
        "2026-11-12",
        "2026-11-13",
        "2026-11-13",
        "2026-11-16",
        "2026-07-02",
        "2026-07-03",
    ]
    assert metrics.trading_days(np.empty(0, dtype=np.int64), "UTC", "00:00") == []
    assert metrics.trading_days(stamps[:2], "UTC", "00:00") == ["2026-11-12", "2026-11-12"]


# --- totals --------------------------------------------------------------------------------


def test_totals_by_hand() -> None:
    day1, day2, day3 = utc_ms(2026, 11, 10, 14), utc_ms(2026, 11, 11, 14), utc_ms(2026, 11, 12, 14)
    trades = [
        trade(500, day1),
        trade(500, day1 + MINUTE_MS),
        trade(-1000, day1 + 2 * MINUTE_MS, reason="stop"),
        trade(1000, day2, clips=2),
        trade(-10_000, day3, clips=5, reason="stop", direction=-1),
        trade(500, day3 + MINUTE_MS, direction=-1),
        trade(0, day3 + 2 * MINUTE_MS, reason="time"),
    ]
    rows, days = rows_and_days(trades, ["2026-11-09", "2026-11-10", "2026-11-11", "2026-11-12"])
    s = metrics.summarize(rows, days, PIP_VALUE)
    assert (s["trades"], s["wins"], s["losses"]) == (7, 4, 2)  # the trade that made nothing is neither
    assert s["win_rate"] == pytest.approx(4 / 7, abs=1e-4)
    assert (s["net_pnl"], s["net_pips"]) == (-8500.0, -85.0)
    assert (s["gross_profit"], s["gross_loss"]) == (2500.0, 11_000.0)
    assert s["profit_factor"] == pytest.approx(2500 / 11_000, abs=1e-3)
    assert s["expectancy"] == pytest.approx(-8500 / 7, abs=0.01)
    assert (s["average_win"], s["average_loss"]) == (625.0, -5500.0)
    assert (s["largest_win"], s["largest_loss"]) == (1000.0, -10_000.0)
    # running total: 500, 1000, 0, 1000, -9000, -8500, -8500 -> from the high of 1000 down to -9000
    assert s["max_drawdown"] == 10_000.0
    assert s["longest_losing_run"] == 1
    assert (s["max_units"], s["trades_with_adds"]) == (5_000_000.0, 2)
    assert s["average_clips"] == pytest.approx(12 / 7, abs=0.01)  # 1 + 1 + 1 + 2 + 5 + 1 + 1 clips
    assert s["by_reason"] == {
        "target": {"trades": 4, "wins": 4, "pnl": 2500.0, "win_rate": 1.0},
        "stop": {"trades": 2, "wins": 0, "pnl": -11_000.0, "win_rate": 0.0},
        "time": {"trades": 1, "wins": 0, "pnl": 0.0, "win_rate": 0.0},
    }
    assert s["by_direction"]["short"] == {"trades": 2, "wins": 1, "pnl": -9500.0, "win_rate": 0.5}
    # every example trade is entered ten minutes before its exit just after 14:00: in the hour from 13:00
    assert list(s["by_entry_hour_utc"]) == ["13"] and s["by_entry_hour_utc"]["13"]["trades"] == 7
    assert (s["days"], s["days_traded"], s["days_positive"], s["days_negative"]) == (4, 3, 1, 1)
    assert (s["best_day"], s["worst_day"]) == (1000.0, -9500.0)
    assert s["average_day"] == pytest.approx(-8500 / 4)
    assert s["daily_sharpe"] is None  # four days say nothing about day-to-day variation
    assert [(d["day"], d["trades"], d["pnl"], d["cumulative"]) for d in days] == [
        ("2026-11-09", 0, 0.0, 0.0),
        ("2026-11-10", 3, 0.0, 0.0),
        ("2026-11-11", 1, 1000.0, 1000.0),
        ("2026-11-12", 3, -9500.0, -8500.0),
    ]
    assert days[3]["max_units"] == 5_000_000.0 and days[3]["wins"] == 1 and days[3]["pips"] == -95.0
    assert metrics.equity_points(rows)[-1] == [day3 + 2 * MINUTE_MS, -8500.0]


def test_totals_of_nothing_and_of_only_winners() -> None:
    empty = metrics.summarize([], [], PIP_VALUE)
    assert empty["trades"] == 0 and empty["net_pnl"] == 0.0 and empty["max_drawdown"] == 0.0
    assert empty["win_rate"] is None and empty["profit_factor"] is None and empty["expectancy"] is None
    assert empty["largest_loss"] is None and empty["best_day"] is None and empty["average_day"] is None
    rows, days = rows_and_days([trade(500, utc_ms(2026, 11, 10, 14)), trade(500, utc_ms(2026, 11, 10, 15))])
    winners = metrics.summarize(rows, days, PIP_VALUE)
    assert winners["profit_factor"] is None  # nothing was lost: there is no ratio to speak of
    assert winners["win_rate"] == 1.0 and winners["max_drawdown"] == 0.0 and winners["average_loss"] is None


def test_the_losing_run_and_the_sharpe_ratio() -> None:
    start = utc_ms(2026, 6, 1, 14)
    pnls = [100, -50, -50, -50, 200, -10, -10, 300] * 3  # 24 days, one trade a day
    trades = [trade(p, start + i * 86_400_000) for i, p in enumerate(pnls)]
    rows, days = rows_and_days(trades)
    s = metrics.summarize(rows, days, PIP_VALUE)
    assert s["longest_losing_run"] == 3 and s["days"] == 24
    expected = np.mean(pnls) / np.std(pnls, ddof=1) * 252**0.5
    assert s["daily_sharpe"] == pytest.approx(expected, abs=0.01)


def test_the_times_of_the_first_and_last_bar_of_each_trading_day() -> None:
    stamps = np.array(
        [
            utc_ms(2026, 11, 11, 22, 0),  # Wednesday 17:00 New York: Thursday's trading day begins
            utc_ms(2026, 11, 12, 3, 0),
            utc_ms(2026, 11, 12, 21, 59),  # its last minute
            utc_ms(2026, 11, 12, 22, 0),  # Friday's trading day
            utc_ms(2026, 11, 13, 9, 30),
        ]
    )
    assert metrics.day_spans(stamps, "America/New_York", "17:00") == {
        "2026-11-12": (utc_ms(2026, 11, 11, 22, 0), utc_ms(2026, 11, 12, 21, 59)),
        "2026-11-13": (utc_ms(2026, 11, 12, 22, 0), utc_ms(2026, 11, 13, 9, 30)),
    }
    assert metrics.day_spans(np.empty(0, dtype=np.int64), "UTC", "00:00") == {}


def test_trade_rows_carry_what_a_chart_needs() -> None:
    t = Trade(
        direction=-1,
        signal_ms=utc_ms(2026, 11, 12, 14, 5),
        fills=(Fill(utc_ms(2026, 11, 12, 14, 6), 1.1000, 1e6), Fill(utc_ms(2026, 11, 12, 14, 20), 1.1005, 1e6)),
        exit_ms=utc_ms(2026, 11, 12, 14, 41),
        exit_price=1.09975,
        reason="target",
        stop_price=1.1030,
        target_price=1.09975,
        pnl=1000.0,
        potential_pips=8.2,
        adverse_pips=7.0,
    )
    (row,) = metrics.trade_rows([t], EX, ["2026-11-12"])
    assert row["direction"] == "short" and row["clips"] == 2 and row["units"] == 2e6
    assert row["average_price"] == 1.10025 and row["pips"] == 5.0 and row["minutes"] == 35.0
    assert row["fills"] == [[utc_ms(2026, 11, 12, 14, 6), 1.1, 1e6], [utc_ms(2026, 11, 12, 14, 20), 1.1005, 1e6]]
    assert row["entry_hour_utc"] == 14 and row["n"] == 1 and row["day"] == "2026-11-12"
    json.dumps(row)  # plain values only


# --- the report ----------------------------------------------------------------------------


def market(days: int = 15, seed: int = 1) -> Bars:
    """One-minute bars for ``days`` weekdays, 06:00 to 21:00 UTC, on the five-decimal grid."""
    rng = np.random.default_rng(seed)
    stamps = []
    day = utc_ms(2026, 10, 5, 0, 0)  # a Monday
    while len(stamps) < days * 900:
        if (day // 86_400_000 + 3) % 7 < 5:  # Monday to Friday (1970-01-01 was a Thursday)
            stamps += [day + (6 * 60 + m) * MINUTE_MS for m in range(900)]
        day += 86_400_000
    n = len(stamps)
    close = np.round(1.16 + np.cumsum(rng.normal(0, 0.00012, n)), 5)
    o = np.concatenate((close[:1], close[:-1]))
    high = np.round(np.maximum(o, close) + np.abs(rng.normal(0, 0.00005, n)), 5)
    low = np.round(np.minimum(o, close) - np.abs(rng.normal(0, 0.00005, n)), 5)
    return Bars(np.asarray(stamps, dtype=np.int64), o, high, low, close, 60)


@pytest.fixture(scope="module")
def eurusd(registry: InstrumentRegistry):  # type: ignore[no-untyped-def]
    return registry.get("EURUSD")


def request(**changes: object) -> BacktestRequest:
    values: dict = {
        "symbol": "EURUSD",
        "strategy": "bollinger_reversion",
        "start": "2026-10-05",
        "end": "2026-10-24",
        "execution": {"min_potential_pips": 2, "target_pips": 3, "add_step_pips": 3, "stop_pips": 6},
    }
    values.update(changes)
    return BacktestRequest.from_mapping(values)


def test_the_assumptions_come_from_the_instrument(eurusd) -> None:  # type: ignore[no-untyped-def]
    ex = default_execution(eurusd)
    # what was agreed for EUR/USD: a spread of 1.0 pip (the venue quotes about 0.5), no commission,
    # clips of 1 mn, at most 5, 5 pips apart, target 5 pips, at least 5 pips of potential
    assert (ex.pip_size, ex.spread_pips, ex.commission_per_million) == (0.0001, 1.0, 0.0)
    assert (ex.clip_units, ex.max_clips, ex.add_step_pips, ex.target_pips, ex.min_potential_pips) == (
        1e6,
        5,
        5.0,
        5.0,
        5.0,
    )
    assert (ex.session_tz, ex.day_end, ex.flat_time) == ("America/New_York", "17:00", "16:45")
    changed = resolve_execution(eurusd, {"spread_pips": "0.5", "max_clips": 1})
    assert (changed.spread_pips, changed.max_clips, changed.target_pips) == (0.5, 1, 5.0)
    for fixed in ("pip_size", "session_tz", "day_end"):
        with pytest.raises(BacktestError, match="fixed by the instrument"):
            resolve_execution(eurusd, {fixed: "1"})
    with pytest.raises(BacktestError, match="unknown execution setting 'leverage'"):
        resolve_execution(eurusd, {"leverage": 10})


def test_a_report_adds_up(eurusd) -> None:  # type: ignore[no-untyped-def]
    report = build_report(market(), eurusd, request())
    summary, trades, daily = report["summary"], report["trades"], report["daily"]
    assert summary["trades"] == len(trades) > 20
    assert report["signals"]["traded"] == len(trades)
    assert sum(v for k, v in report["signals"].items() if k != "signals") == report["signals"]["signals"]
    # the days: every weekday of the data, each trade in exactly one of them
    assert [d["day"] for d in daily] == sorted({d["day"] for d in daily}) and len(daily) == 15
    assert sum(d["trades"] for d in daily) == len(trades)
    assert sum(d["pnl"] for d in daily) == pytest.approx(summary["net_pnl"], abs=0.01)
    assert daily[-1]["cumulative"] == pytest.approx(summary["net_pnl"], abs=0.01)
    assert sum(t["pnl"] for t in trades) == pytest.approx(summary["net_pnl"], abs=0.01)
    assert report["equity"][-1][1] == pytest.approx(summary["net_pnl"], abs=0.01)
    assert sum(g["trades"] for g in summary["by_reason"].values()) == len(trades)
    assert sum(g["trades"] for g in summary["by_direction"].values()) == len(trades)
    # in-sample and out-of-sample are the two parts of the whole, split at a day
    first, second = report["in_sample"], report["out_of_sample"]
    assert report["split_day"] == daily[11]["day"] == second["first_day"]  # 3 in 10 of 15 days: the last 4
    assert first["last_day"] == daily[10]["day"] and first["first_day"] == daily[0]["day"]
    assert first["summary"]["days"] == 11 and second["summary"]["days"] == 4
    assert first["summary"]["trades"] + second["summary"]["trades"] == len(trades)
    assert first["summary"]["net_pnl"] + second["summary"]["net_pnl"] == pytest.approx(summary["net_pnl"], abs=0.01)
    assert first["summary"]["trades"] == sum(1 for t in trades if t["day"] < report["split_day"])
    # what the request became
    assert report["request"]["timeframe"] == "5m" and report["request"]["base_timeframe"] == "1m"
    assert report["request"]["params"] == {"period": 20, "width": 2.0}
    assert report["request"]["execution"]["target_pips"] == 3.0 and report["request"]["execution"]["spread_pips"] == 1.0
    assert (report["currency"], report["unit_currency"], report["pip_value"]) == ("USD", "EUR", 100.0)
    assert report["data"]["base_bars"] == 15 * 900 and report["data"]["signal_bars"] == 15 * 180
    assert report["data"]["price_basis"] == "bid" and report["data"]["trading_days"] == 15
    assert report["data"]["holes_over_30_minutes"] == 12  # the nights between weekdays; weekends are not holes
    # each day says when its first and its last bar began: 06:00 and 20:59 UTC in this market
    assert all(d["first_bar_ms"] % 86_400_000 == 6 * 3_600_000 for d in daily)
    assert all(d["last_bar_ms"] - d["first_bar_ms"] == 899 * MINUTE_MS for d in daily)
    json.dumps(report)


def test_trades_in_a_report_obey_the_rules_they_were_run_with(eurusd) -> None:  # type: ignore[no-untyped-def]
    report = build_report(market(), eurusd, request())
    ex = report["request"]["execution"]
    for t in report["trades"]:
        assert 1 <= t["clips"] <= ex["max_clips"]
        assert 6 * 60 <= (t["entry_ms"] % 86_400_000) // 60_000 < 20 * 60  # the entry hours, UTC
        assert t["entry_ms"] >= t["signal_ms"] + 5_000  # never before the bar closed and the delay passed
        assert t["minutes"] <= ex["max_hold_minutes"] + 1
        if t["reason"] == "target":
            assert t["pips"] == pytest.approx(ex["target_pips"], abs=0.01)
            assert t["pnl"] == pytest.approx(ex["target_pips"] * 100 * t["clips"], abs=0.1)
        if t["reason"] == "stop":
            assert t["pnl"] < 0


def test_the_same_signals_without_adding_are_always_shown_next_to_it(eurusd) -> None:  # type: ignore[no-untyped-def]
    bars = market()
    report = build_report(bars, eurusd, request())
    alone = build_report(bars, eurusd, request(execution={**request().execution, "max_clips": 1}))
    assert "without_adding" not in alone and alone["summary"]["trades_with_adds"] == 0
    assert alone["summary"]["max_units"] == 1_000_000
    assert report["without_adding"]["summary"] == alone["summary"]
    # the same days, without the times of the bars (the chart takes those from the main table)
    keys = ("day", "trades", "wins", "pnl", "pips", "cumulative", "max_units")
    assert report["without_adding"]["daily"] == [{k: d[k] for k in keys} for d in alone["daily"]]
    assert report["summary"]["trades_with_adds"] > 0 and report["summary"]["max_units"] > 1_000_000


def test_the_split_can_be_set_or_switched_off(eurusd) -> None:  # type: ignore[no-untyped-def]
    bars = market()
    none = build_report(bars, eurusd, request(split="none"))
    assert none["split_day"] is None and "in_sample" not in none and "out_of_sample" not in none
    dated = build_report(bars, eurusd, request(split="2026-10-19"))
    assert dated["split_day"] == "2026-10-19" and dated["out_of_sample"]["first_day"] == "2026-10-19"
    assert dated["in_sample"]["last_day"] == "2026-10-16" and dated["out_of_sample"]["summary"]["days"] == 5
    outside = build_report(bars, eurusd, request(split="2027-01-01"))
    assert outside["out_of_sample"]["summary"]["trades"] == 0 and outside["out_of_sample"]["first_day"] is None
    assert any("outside the data" in w for w in outside["warnings"])
    short = build_report(market(days=6), eurusd, request())
    assert short["split_day"] is None and any("too few to keep some back" in w for w in short["warnings"])
    with pytest.raises(BacktestError, match="split must be"):
        build_report(bars, eurusd, request(split="next week"))


def test_a_report_warns_about_what_weakens_it(eurusd) -> None:  # type: ignore[no-untyped-def]
    bars = market()
    few = build_report(bars, eurusd, request(execution={"min_potential_pips": 40}))
    assert few["summary"]["trades"] == 0 and any("No signal was traded" in w for w in few["warnings"])
    assert few["signals"]["skipped: potential below the minimum"] == few["signals"]["signals"] > 0
    wide = build_report(bars, eurusd, request(start="2026-09-01", end="2026-12-01"))
    assert any("less than the period asked for" in w for w in wide["warnings"])
    assert not any("less than the period" in w for w in build_report(bars, eurusd, request())["warnings"])
    # cut the data in the middle of a position: the last trade is valued, and said to be
    report = build_report(bars, eurusd, request())
    middle = next(t for t in report["trades"] if t["minutes"] >= 3)
    cut = int(np.searchsorted(bars.ts, middle["entry_ms"])) + 2
    part = Bars(bars.ts[:cut], bars.open[:cut], bars.high[:cut], bars.low[:cut], bars.close[:cut], 60)
    open_end = build_report(part, eurusd, request(split="none"))
    assert open_end["trades"][-1]["reason"] == "end_of_data"
    assert any("still open when the data ended" in w for w in open_end["warnings"])


def numberless(rows: list[dict]) -> list[dict]:
    return [{k: v for k, v in row.items() if k != "n"} for row in rows]


@pytest.mark.parametrize(
    ("strategy", "timeframe"),
    [("bollinger_reversion", "5m"), ("donchian_breakout", "15m"), ("macd_cross", "15m"), ("pivot_reversion", "5m")],
)
def test_bars_before_the_period_let_the_indicators_settle(eurusd, strategy: str, timeframe: str) -> None:  # type: ignore[no-untyped-def]
    """A period reported with earlier bars as run-in is the later part of the longer report."""
    bars = market()  # three weeks from Monday 5 October 2026
    start = utc_ms(2026, 10, 13)
    whole = build_report(bars, eurusd, request(strategy=strategy, timeframe=timeframe))
    warm = build_report(bars, eurusd, request(strategy=strategy, timeframe=timeframe, start="2026-10-13"))
    tail = [t for t in whole["trades"] if t["signal_ms"] >= start]
    assert numberless(warm["trades"]) == numberless(tail) and 0 < len(tail) < len(whole["trades"])
    assert [t["n"] for t in warm["trades"]] == list(range(1, len(tail) + 1))
    # the period is what was asked for: its days, its bars, its signals
    assert (warm["data"]["first_day"], warm["data"]["first_bar"]) == ("2026-10-13", iso(utc_ms(2026, 10, 13, 6)))
    assert warm["data"]["trading_days"] == 9 and warm["data"]["base_bars"] == 9 * 15 * 60
    assert [d["day"] for d in warm["daily"]] == [d["day"] for d in whole["daily"]][-9:]
    assert warm["daily"][0]["first_bar_ms"] == utc_ms(2026, 10, 13, 6)
    assert sum(d["trades"] for d in warm["daily"]) == len(tail) == warm["signals"]["traded"]
    assert warm["signals"]["signals"] == sum(v for k, v in warm["signals"].items() if k != "signals")
    assert warm["signals"]["signals"] < whole["signals"]["signals"]
    assert warm["without_adding"]["summary"]["trades"] <= whole["without_adding"]["summary"]["trades"]
    # the report says where its run-in began, and warns when there is none
    assert warm["data"]["run_in_from"] == iso(int(bars.ts[0])) and whole["data"]["run_in_from"] is None
    assert not any("stored before this period" in w for w in warm["warnings"])
    assert any(w.startswith("No history is stored before this period") for w in whole["warnings"])

    # the same days without the earlier bars: the indicators begin unsettled, and the first day shows it
    first = int(np.searchsorted(bars.ts, start))
    late = Bars(bars.ts[first:], bars.open[first:], bars.high[first:], bars.low[first:], bars.close[first:], 60)
    cold = build_report(late, eurusd, request(strategy=strategy, timeframe=timeframe, start="2026-10-13"))
    assert cold["data"]["run_in_from"] is None and cold["data"]["base_bars"] == warm["data"]["base_bars"]
    assert cold["signals"]["signals"] < warm["signals"]["signals"]
    assert cold["daily"][0]["trades"] < warm["daily"][0]["trades"]


def test_a_short_run_in_is_said_to_be_short(eurusd) -> None:  # type: ignore[no-untyped-def]
    bars = market()
    report = build_report(bars, eurusd, request(start="2026-10-07"))  # the data begins two days earlier
    assert report["data"]["run_in_from"] == iso(int(bars.ts[0]))
    assert any(w.startswith("Only 2.0 days of history are stored before this period") for w in report["warnings"])
    enough = build_report(bars, eurusd, request(start="2026-10-12"))  # a week of it
    assert not any("stored before this period" in w for w in enough["warnings"])
    assert "13,500 bars" not in format_report(enough) and "indicators run in from 2026-10-05" in format_report(enough)
    assert "run in from" not in format_report(build_report(bars, eurusd, request()))
    with pytest.raises(BacktestError, match="there are no bars in this range"):
        build_report(bars, eurusd, request(start="2026-11-02", end="2026-11-06"))  # a run-in and nothing after it


def test_quotes_from_the_weekend_are_not_traded(eurusd) -> None:  # type: ignore[no-untyped-def]
    week = market(days=10)  # Monday 5 to Friday 16 October 2026, 06:00 to 21:00 UTC
    # a provider that also publishes on Friday night after the close (17:00 New York = 21:00 UTC) and on Saturday
    stray = np.array(
        [utc_ms(2026, 10, 9, 21, m) for m in range(0, 30)] + [utc_ms(2026, 10, 10, 9, m) for m in range(0, 60)],
        dtype=np.int64,
    )
    ts = np.concatenate((week.ts, stray))
    order = np.argsort(ts)
    wild = np.full(len(stray), 1.30)  # far from the market: any trade on these bars would show

    def merged(values: np.ndarray) -> np.ndarray:
        return np.concatenate((values, wild))[order]

    bars = Bars(ts[order], merged(week.open), merged(week.high), merged(week.low), merged(week.close), 60)
    with_strays = build_report(bars, eurusd, request(end="2026-10-17"))
    clean = build_report(week, eurusd, request(end="2026-10-17"))
    assert any("90 bars lie outside the instrument's trading week" in w for w in with_strays["warnings"])
    assert with_strays["data"]["base_bars"] == len(week) and with_strays["data"]["trading_days"] == 10
    assert with_strays["trades"] == clean["trades"] and with_strays["summary"] == clean["summary"]
    assert not any("outside the instrument's trading week" in w for w in clean["warnings"])
    only_weekend = Bars(stray, wild, wild, wild, wild, 60)
    with pytest.raises(BacktestError, match="no bars inside the trading week"):
        build_report(only_weekend, eurusd, request())


def test_requests_are_checked(eurusd) -> None:  # type: ignore[no-untyped-def]
    bars = market(days=6)
    for changes, message in (
        ({"strategy": "martingale"}, "unknown strategy"),
        ({"timeframe": "4h"}, "follow the trading day"),
        ({"timeframe": "45s"}, "unsupported timeframe"),
        ({"timeframe": "30s"}, "cannot be built from 1m bars"),
        ({"base_timeframe": "5m"}, "not of the base timeframe"),
        ({"params": {"period": 2}}, "at least 5"),
        ({"params": {"speed": 2}}, "no parameter 'speed'"),
        ({"execution": {"max_clips": 0}}, "max_clips must be at least 1"),
        ({"execution": {"flat_time": "soon"}}, "not a time of day"),
    ):
        with pytest.raises(BacktestError, match=message):
            build_report(bars, eurusd, request(**changes))
    with pytest.raises(BacktestError, match="no bars"):
        build_report(Bars.from_rows([], 60), eurusd, request())
    for bad, message in (
        ({"symbol": "EURUSD", "strategy": "x", "start": "2026-01-01"}, "end is required"),
        ({"symbol": "EURUSD", "strategy": "x", "start": "soon", "end": "later"}, "must be dates"),
        ({"symbol": "EURUSD", "strategy": "x", "start": 1, "end": 2, "leverage": 3}, "unknown field 'leverage'"),
        ({"symbol": "EURUSD", "strategy": "x", "start": 1, "end": 2, "params": [1]}, "params must be an object"),
    ):
        with pytest.raises(BacktestError, match=message):
            BacktestRequest.from_mapping(bad)
    parsed = BacktestRequest.from_mapping(
        {"symbol": "eurusd", "strategy": "x", "start": "2026-01-01", "end": "2026-02-01"}
    )
    assert (parsed.symbol, parsed.base_timeframe, parsed.split, parsed.timeframe) == ("EURUSD", "1m", "auto", None)
    assert parsed.start_ms == utc_ms(2026, 1, 1) and parsed.end_ms == utc_ms(2026, 2, 1)


def test_other_timeframes_and_strategies_run(eurusd) -> None:  # type: ignore[no-untyped-def]
    bars = market()
    # the test market opens at 06:00 UTC: two hours of range before London's 08:00
    params = {"session_breakout": {"range_hours": 2}}
    for strategy in ("rsi_reversion", "ema_momentum", "session_breakout"):
        for timeframe in ("1m", "15m"):
            report = build_report(
                bars,
                eurusd,
                request(
                    strategy=strategy,
                    timeframe=timeframe,
                    params=params.get(strategy, {}),
                    execution={"min_potential_pips": 0},
                ),
            )
            assert report["request"]["timeframe"] == timeframe
            assert report["signals"]["signals"] > 0 and report["summary"]["trades"] > 0, (strategy, timeframe)
    nothing = build_report(bars, eurusd, request(strategy="session_breakout", timeframe="15m"))
    assert nothing["signals"] == {"signals": 0, "traded": 0} and nothing["summary"]["trades"] == 0


def test_the_text_report_shows_the_parts_side_by_side(eurusd) -> None:  # type: ignore[no-untyped-def]
    report = build_report(market(), eurusd, request())
    text = format_report(report, trades=3, days=4)
    lines = text.splitlines()
    assert lines[0] == "Bollinger reversion on EURUSD, 5m bars (trades simulated on 1m bid bars)"
    head = next(line for line in lines if line.startswith("(money in USD)"))
    assert head.split()[3:] == ["Whole", "period", "In-sample", "Out-of-sample", "No", "adding"]
    net = next(line for line in lines if line.startswith("Net result"))
    assert net.split()[2:] == [f"{report[k]['net_pnl']:,.0f}" for k in ("summary",)] + [
        f"{report[k]['summary']['net_pnl']:,.0f}" for k in ("in_sample", "out_of_sample", "without_adding")
    ]
    assert "Last 3 trades:" in text and "Last 4 days:" in text and "Signals:" in text
    assert "not a forecast" in text and "known to be profitable" in text
    assert format_report(build_report(market(days=6), eurusd, request())).count("!") >= 1
