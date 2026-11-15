"""Backtests against a real database: what is read, what is refused, and the command an operator types."""

from __future__ import annotations

import asyncio
import io
import json

import numpy as np
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from fxlab.cli import main
from fxlab.config import get_settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.models import Bar, DataSource, PriceBasis
from fxlab.research.bars import Bars, load_provider_bars
from fxlab.research.board import BoardRequest, build_board, window_signals
from fxlab.research.report import BacktestError, BacktestRequest, build_report
from fxlab.services.backtest import describe_lab, history_inventory, run_backtest, run_board, run_window
from fxlab.storage import repo
from fxlab.timeutil import iso
from tests.conftest import DATA_TABLES, utc_ms
from tests.integration.test_cli import ThreadedFake, cli_env, provider  # noqa: F401  (fixtures)
from tests.unit.test_research_report import market

pytestmark = pytest.mark.db

REQUEST = {
    "symbol": "EURUSD",
    "strategy": "bollinger_reversion",
    "start": "2026-10-05",
    "end": "2026-10-24",
    "execution": {"min_potential_pips": 2, "target_pips": 3, "add_step_pips": 3, "stop_pips": 6},
}


async def store(engine: AsyncEngine, registry: InstrumentRegistry, bars: Bars, timeframe: str = "1m") -> int:
    """Store ``bars`` as the provider's history of EUR/USD, next to live bars that must never be read."""
    async with engine.begin() as conn:
        ids = await repo.sync_instruments(conn, registry)
        rows = [
            repo.bar_row(
                ids["EURUSD"],
                Bar(
                    "EURUSD",
                    timeframe,
                    int(t),
                    float(o),
                    float(h),
                    float(lo),
                    float(c),
                    DataSource.MASSIVE_AGG,
                    PriceBasis.BID,
                ),
            )
            for t, o, h, lo, c in zip(bars.ts, bars.open, bars.high, bars.low, bars.close, strict=True)
        ]
        await repo.upsert_bars(conn, rows)
        # the system's own live bars of the same minutes, at absurd prices: a backtest that read them would show it
        live = [
            repo.bar_row(
                ids["EURUSD"], Bar("EURUSD", timeframe, int(t), 9.0, 9.5, 8.5, 9.0, DataSource.LIVE, PriceBasis.MID)
            )
            for t in bars.ts[:500]
        ]
        await repo.upsert_bars(conn, live)
    return ids["EURUSD"]


async def test_bars_come_back_from_the_database_as_they_went_in(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    bars = market(days=3)
    instrument_id = await store(engine, registry, bars)
    loaded = await load_provider_bars(engine, instrument_id, "1m", 60, int(bars.ts[0]), int(bars.ts[-1]) + 1)
    assert loaded.seconds == 60 and loaded.ts.dtype == np.int64
    for name in ("ts", "open", "high", "low", "close"):
        assert np.array_equal(getattr(loaded, name), getattr(bars, name)), name
    # the range is half-open, and other timeframes or instruments are not mixed in
    part = await load_provider_bars(engine, instrument_id, "1m", 60, int(bars.ts[10]), int(bars.ts[20]))
    assert part.ts.tolist() == bars.ts[10:20].tolist()
    assert len(await load_provider_bars(engine, instrument_id, "5m", 300, 0, 2**42)) == 0
    assert len(await load_provider_bars(engine, instrument_id + 1, "1m", 60, 0, 2**42)) == 0


async def test_a_backtest_reads_the_provider_history_and_nothing_else(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    bars = market()
    await store(engine, registry, bars)
    request = BacktestRequest.from_mapping(REQUEST)
    report = await run_backtest(engine, registry, request)
    assert report == build_report(bars, registry.get("EURUSD"), request)  # the same as on the arrays themselves
    assert report["summary"]["trades"] > 20 and report["data"]["base_bars"] == len(bars)
    assert all(1.0 < t["entry_price"] < 1.3 for t in report["trades"])  # none of the absurd live prices
    # the dates of the request bound what is read
    week = await run_backtest(engine, registry, BacktestRequest.from_mapping({**REQUEST, "end": "2026-10-10"}))
    assert week["data"]["trading_days"] == 5 and week["data"]["last_day"] == "2026-10-09"
    # a backtest only reads
    async with engine.connect() as conn:
        counts = [(await conn.execute(text(f"select count(*) from {t}"))).scalar() for t in DATA_TABLES]
    assert counts[DATA_TABLES.index("bars")] == len(bars) + 500 and counts[DATA_TABLES.index("history_jobs")] == 0


async def test_what_is_stored_and_what_a_form_can_offer(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    assert await history_inventory(engine, "EURUSD") == []  # nothing yet, not even the instrument row
    bars = market(days=3)
    await store(engine, registry, bars)
    five = Bars(bars.ts[::5], bars.open[::5], bars.high[::5], bars.low[::5], bars.close[::5], 300)
    await store(engine, registry, five, "5m")
    assert await history_inventory(engine, "EURUSD") == [
        {"timeframe": "1m", "seconds": 60, "bars": 2700, "first": iso(int(bars.ts[0])), "last": iso(int(bars.ts[-1]))},
        {"timeframe": "5m", "seconds": 300, "bars": 540, "first": iso(int(five.ts[0])), "last": iso(int(five.ts[-1]))},
    ]
    assert await history_inventory(engine, "USDJPY") == []
    lab = await describe_lab(engine, registry, "EURUSD")
    assert [s["key"] for s in lab["strategies"]][:2] == ["rsi_reversion", "bollinger_reversion"]
    assert lab["execution"]["spread_pips"] == 1.0 and lab["execution"]["clip_units"] == 1_000_000
    assert lab["execution"]["flat_time"] == "16:45" and lab["execution"]["max_clips"] == 5
    assert (lab["symbol"], lab["currency"], lab["unit_currency"]) == ("EURUSD", "USD", "EUR")
    assert [h["timeframe"] for h in lab["history"]] == ["1m", "5m"]
    assert lab["timeframes"] == ["1m", "5m", "15m", "30m", "1h"]
    assert (lab["pip_size"], lab["price_precision"], lab["session_tz"]) == (0.0001, 5, "America/New_York")
    json.dumps(lab)
    with pytest.raises(BacktestError, match="unknown instrument"):
        await describe_lab(engine, registry, "XAUUSD")


async def test_requests_that_cannot_be_run_say_why(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    await store(engine, registry, market(days=3))
    for changes, message in (
        ({"symbol": "XAUUSD"}, "unknown instrument 'XAUUSD'"),
        ({"start": "2026-10-24", "end": "2026-10-05"}, "end must be after start"),
        ({"start": "2020-01-01"}, "at most 1098 days"),
        ({"start": "2026-06-01", "end": "2026-06-08"}, "no 1m history of EURUSD is stored between .* fxlab history"),
        ({"symbol": "USDJPY"}, "no 1m history of USDJPY is stored"),
        ({"base_timeframe": "5s"}, "no 5s history of EURUSD"),
        ({"base_timeframe": "1d"}, "cannot be simulated on 1d bars"),
        ({"base_timeframe": "2m"}, "unsupported timeframe"),
        ({"strategy": "martingale"}, "unknown strategy"),
    ):
        with pytest.raises(BacktestError, match=message):
            await run_backtest(engine, registry, BacktestRequest.from_mapping({**REQUEST, **changes}))


async def test_a_period_with_too_many_bars_is_refused_before_it_is_loaded(
    engine: AsyncEngine, registry: InstrumentRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    await store(engine, registry, market(days=3))
    monkeypatch.setattr("fxlab.services.backtest.MAX_BARS", 2699)
    with pytest.raises(BacktestError, match="holds 2,700 1m bars; one test loads at most 2,699"):
        await run_backtest(engine, registry, BacktestRequest.from_mapping(REQUEST))
    monkeypatch.setattr("fxlab.services.backtest.MAX_BARS", 2700)
    assert (await run_backtest(engine, registry, BacktestRequest.from_mapping(REQUEST)))["data"]["base_bars"] == 2700


# --- every method at once ------------------------------------------------------------------

BOARD = {
    "symbol": "EURUSD",
    "start": "2026-10-05",
    "end": "2026-10-24",
    "timeframes": ["5m", "15m"],
    "strategies": ["bollinger_reversion", "donchian_breakout", "random_entry"],
    "execution": REQUEST["execution"],
}
WINDOW = {**BOARD, "start": "2026-10-15", "end": "2026-10-17", "chart_timeframe": "15m"}


async def test_a_board_from_the_database_is_the_board(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    bars = market()
    await store(engine, registry, bars)
    seen: list[str] = []
    request = BoardRequest.from_mapping(BOARD)
    board = await run_board(
        engine, registry, request, lambda done, total, label: seen.append(f"{done}/{total} {label}")
    )
    assert board == build_board(bars, registry.get("EURUSD"), request)
    assert seen[0] == "1/6 bollinger_reversion@5m" and seen[-1] == "6/6 random_entry@15m"
    assert all(1.0 < price < 1.3 for price in (board["data"]["first_bar"] and [bars.close[0]]))  # provider bars only
    for changes, message in (
        ({"symbol": "XAUUSD"}, "unknown instrument"),
        ({"start": "2026-10-24", "end": "2026-10-05"}, "end must be after start"),
        ({"start": "2020-01-01"}, "one run covers at most 1098 days"),
        ({"start": "2026-06-01", "end": "2026-06-08"}, "no 1m history of EURUSD is stored between"),
        ({"strategies": ["martingale"]}, "unknown strategy"),
    ):
        with pytest.raises(BacktestError, match=message):
            await run_board(engine, registry, BoardRequest.from_mapping({**BOARD, **changes}))


async def test_the_signals_of_a_window_from_the_database(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    bars = market()
    await store(engine, registry, bars)
    request = BoardRequest.from_mapping(WINDOW)
    window = await run_window(engine, registry, request)
    # what is loaded: three weeks before the window (here: from the start of the data) and one day after it
    loaded = bars.ts < utc_ms(2026, 10, 18)
    part = Bars(bars.ts[loaded], bars.open[loaded], bars.high[loaded], bars.low[loaded], bars.close[loaded], 60)
    assert window == window_signals(part, registry.get("EURUSD"), request, 0)
    assert window["signals"] and len(window["bars"]["t"]) == 2 * 60  # two days of 15 hours in quarter hours
    # a trade opened on the last evening of the window is followed to its end on that day
    assert not any(s["status"] == "end_of_data" for s in window["signals"])
    with pytest.raises(BacktestError, match="a window of signals covers at most 62 days"):
        await run_window(engine, registry, BoardRequest.from_mapping({**WINDOW, "start": "2026-07-01"}))
    with pytest.raises(BacktestError, match="no 1m history of EURUSD is stored between"):
        await run_window(
            engine, registry, BoardRequest.from_mapping({**WINDOW, "start": "2026-03-02", "end": "2026-03-04"})
        )


# --- the command line ----------------------------------------------------------------------


def fill_database(url: str, registry: InstrumentRegistry, bars: Bars) -> None:
    async def go() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
            await store(engine, registry, bars)
        finally:
            await engine.dispose()

    asyncio.run(go())


def test_the_backtest_command(
    cli_env: pytest.MonkeyPatch,  # noqa: F811
    migrated_database: str,
    registry: InstrumentRegistry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_env.setenv("DATABASE_URL", migrated_database)
    get_settings.cache_clear()
    bars = market()
    fill_database(migrated_database, registry, bars)
    settings = ["-x", "min_potential_pips=2", "-x", "target_pips=3", "-x", "add_step_pips=3", "-x", "stop_pips=6"]
    expected = build_report(bars, registry.get("EURUSD"), BacktestRequest.from_mapping(REQUEST))

    # no dates: everything that is stored
    assert main(["backtest", "--strategy", "bollinger_reversion", *settings, "--trades", "2", "--days", "3"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Bollinger reversion on EURUSD, 5m bars (trades simulated on 1m bid bars)")
    assert "Data: 2026-10-05 to 2026-10-23, 15 trading days, 13,500 bars" in out
    net = next(line for line in out.splitlines() if line.startswith("Net result"))
    assert net.split()[2] == f"{expected['summary']['net_pnl']:,.0f}"
    assert "In-sample" in out and "Out-of-sample" in out and "No adding" in out
    assert "In-sample: 2026-10-05 to 2026-10-19 (11 days), to choose parameters on." in out
    assert "Out-of-sample: 2026-10-20 to 2026-10-23 (4 days), to check them on." in out
    assert "Last 2 trades:" in out and "Last 3 days:" in out and "not a forecast" in out

    # the same as JSON, with dates, a parameter and another timeframe
    args = ["backtest", "--strategy", "rsi_reversion", "--start", "2026-10-05", "--end", "2026-10-17", "--json"]
    assert main([*args, "-p", "rsi_period=7", "--timeframe", "1m", "--split", "none", "-x", "max_clips=1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["request"]["params"]["rsi_period"] == 7 and report["request"]["timeframe"] == "1m"
    assert report["data"]["trading_days"] == 10 and report["split_day"] is None and "without_adding" not in report
    assert report["request"]["execution"]["max_clips"] == 1 and report["summary"]["max_units"] in (0.0, 1_000_000.0)

    # a request as JSON on standard input: what the application itself sends
    cli_env.setattr("sys.stdin", io.StringIO(json.dumps(REQUEST)))
    assert main(["backtest", "--request", "-"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(expected))

    # what cannot be run says why, and says so in the same form as the answer was asked for
    assert main(["backtest", "--start", "2026-10-05", "--end", "2026-10-24"]) == 2
    assert "--strategy is required" in capsys.readouterr().err
    assert main(["backtest", "--strategy", "bollinger_reversion", "-p", "period"]) == 2
    assert "parameters are given as name=value" in capsys.readouterr().err
    assert main(["backtest", "--strategy", "bollinger_reversion", "-x", "leverage=10"]) == 2
    assert "unknown execution setting 'leverage'" in capsys.readouterr().err
    assert main(["backtest", "--strategy", "bollinger_reversion", "--base", "5m"]) == 2
    assert "no 5m history of EURUSD is stored. Download it first: fxlab history" in capsys.readouterr().err
    assert main(["backtest", "--strategy", "bollinger_reversion", "--symbol", "usdjpy", "--json"]) == 2
    assert "no 1m history of USDJPY" in json.loads(capsys.readouterr().out)["error"]
    cli_env.setattr("sys.stdin", io.StringIO("{not json"))
    assert main(["backtest", "--request", "-"]) == 2
    assert "not valid JSON" in json.loads(capsys.readouterr().out)["error"]
    cli_env.setattr("sys.stdin", io.StringIO("[1, 2]"))
    assert main(["backtest", "--request", "-"]) == 2
    assert "must be a JSON object" in json.loads(capsys.readouterr().out)["error"]
    assert main(["backtest", "--request", "/nonexistent/request.json"]) == 2
    assert "cannot read the request" in json.loads(capsys.readouterr().out)["error"]


def test_the_board_and_signals_commands(
    cli_env: pytest.MonkeyPatch,  # noqa: F811
    migrated_database: str,
    registry: InstrumentRegistry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_env.setenv("DATABASE_URL", migrated_database)
    get_settings.cache_clear()
    bars = market()
    fill_database(migrated_database, registry, bars)
    eurusd = registry.get("EURUSD")

    cli_env.setattr("sys.stdin", io.StringIO(json.dumps(BOARD)))
    assert main(["board", "--request", "-", "--progress"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == json.loads(
        json.dumps(build_board(bars, eurusd, BoardRequest.from_mapping(BOARD)))
    )
    lines = captured.err.splitlines()
    assert lines[0] == "PROGRESS 1 6 bollinger_reversion@5m" and lines[-1] == "PROGRESS 6 6 random_entry@15m"

    cli_env.setattr("sys.stdin", io.StringIO(json.dumps(BOARD)))
    assert main(["board", "--request", "-"]) == 0  # without --progress nothing is written beside the answer
    assert capsys.readouterr().err == ""

    cli_env.setattr("sys.stdin", io.StringIO(json.dumps(WINDOW)))
    assert main(["signals", "--request", "-"]) == 0
    window = json.loads(capsys.readouterr().out)
    assert window["request"]["chart_timeframe"] == "15m" and len(window["signals"]) > 10
    assert {s["method"] for s in window["signals"]} <= {m["id"] for m in window["methods"]}

    for argv, stdin, message in (
        (["board"], "", "--request FILE is required"),
        (["board", "--request", "-"], "{not json", "not valid JSON"),
        (["board", "--request", "-"], json.dumps({**BOARD, "strategies": ["nope"]}), "unknown strategy 'nope'"),
        (["signals", "--request", "-"], json.dumps({**WINDOW, "symbol": "USDJPY"}), "no 1m history of USDJPY"),
        (["signals", "--request", "-"], json.dumps({**WINDOW, "chart_timeframe": "1d"}), "follow the trading day"),
    ):
        cli_env.setattr("sys.stdin", io.StringIO(stdin))
        assert main(argv) == 2
        assert message in json.loads(capsys.readouterr().out)["error"]


def test_the_strategies_command(cli_env: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    assert main(["strategies"]) == 0
    out = capsys.readouterr().out
    assert "rsi_reversion  (RSI reversion; usually on 5m bars)" in out and "-p rsi_period=14" in out
    assert "session_breakout" in out and "one of london, new_york, tokyo" in out
    assert "random_entry  (Random entry (benchmark); usually on 5m bars)" in out
    assert main(["strategies", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [s["key"] for s in listed][:4] == [
        "rsi_reversion",
        "bollinger_reversion",
        "ema_momentum",
        "session_breakout",
    ]
    assert len(listed) == 15 and listed[-1]["family"] == "benchmark" and listed[0]["short"] == "RSI"


def test_the_history_command_splits_a_long_range_into_jobs(
    provider: ThreadedFake,  # noqa: F811
    cli_env: pytest.MonkeyPatch,  # noqa: F811
    migrated_database: str,
    registry: InstrumentRegistry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_env.setenv("DATABASE_URL", migrated_database)
    get_settings.cache_clear()
    fill_database(migrated_database, registry, market(days=1))
    # quotes on three days that are more than a week apart; second-level history is limited to 7 days a job
    days = [utc_ms(2026, 11, 2, 9), utc_ms(2026, 11, 10, 9), utc_ms(2026, 11, 16, 9)]
    provider.publish([{"b": 1.16, "a": 1.1601, "t": day + i * 1000} for day in days for i in range(20)])
    assert (
        main(["history", "--symbol", "EURUSD", "--timeframe", "5s", "--start", "2026-11-02", "--end", "2026-11-17"])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert (result["status"], result["rows_written"], result["timeframe"]) == ("done", 12, "5s")
    assert [(j["start_ms"], j["end_ms"], j["rows_written"]) for j in result["jobs"]] == [
        (utc_ms(2026, 11, 2), utc_ms(2026, 11, 9), 4),
        (utc_ms(2026, 11, 9), utc_ms(2026, 11, 16), 4),
        (utc_ms(2026, 11, 16), utc_ms(2026, 11, 17), 4),
    ]
    assert (
        main(["history", "--symbol", "EURUSD", "--timeframe", "1m", "--start", "2026-11-17", "--end", "2026-11-02"])
        == 2
    )
    assert "end must be after start" in capsys.readouterr().err
    assert (
        main(["history", "--symbol", "XAUUSD", "--timeframe", "1m", "--start", "2026-11-02", "--end", "2026-11-17"])
        == 2
    )
    assert "unknown instrument" in capsys.readouterr().err
