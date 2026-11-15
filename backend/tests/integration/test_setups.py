"""Setups (the owner's own rules) against a real database, on the command line and over HTTP."""

from __future__ import annotations

import asyncio
import io
import json

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.cli import main
from fxlab.config import get_settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.research.bars import Bars
from fxlab.research.report import BacktestError
from fxlab.research.setups import SetupRequest, build_setup
from fxlab.services import backtest as service
from fxlab.services.backtest import delete_setup, list_setups, run_setup, save_setup
from fxlab.timeutil import iso
from tests.conftest import utc_ms
from tests.integration.test_api import AppServer
from tests.integration.test_auth import PASSWORD, protected
from tests.integration.test_backtest import REQUEST, fill_database, store
from tests.integration.test_cli import ThreadedFake, cli_env, provider  # noqa: F401  (fixtures)
from tests.integration.test_lab_api import lab  # noqa: F401  (fixture)
from tests.unit.test_research_report import market

pytestmark = pytest.mark.db

#: what is kept of a setup ...
DEFINITION = {
    "name": "RSI or Bollinger",
    "methods": ["rsi_reversion@5m", "bollinger_reversion@5m"],
    "mode": "any",
    "execution": {"min_potential_pips": 2, "target_pips": 5, "stop_pips": 5},
}
#: ... and a run of it: the instrument and the period come with the run
SETUP = {"symbol": "EURUSD", "start": "2026-10-05", "end": "2026-10-24", **DEFINITION}


async def test_a_setup_from_the_database_is_the_setup(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    bars = market()
    await store(engine, registry, bars)
    request = SetupRequest.from_mapping(SETUP)
    report = await run_setup(engine, registry, request)
    assert report == build_setup(bars, registry.get("EURUSD"), request)
    assert report["summary"]["trades"] > 30 and report["request"]["strategy_title"] == "RSI or Bollinger"
    assert report["request"]["setup"]["methods"] == DEFINITION["methods"]
    assert (report["request"]["execution"]["target_pips"], report["request"]["execution"]["stop_pips"]) == (5.0, 5.0)
    # every trade says what was going on around it, and the table of reasons counts every trade
    tags = ("against_momentum", "against_channel", "outside_channel", "momentum_turned", "left_channel")
    assert all(isinstance(t[tag], bool) for t in report["trades"] for tag in tags)
    assert report["why"]["trades"] == report["summary"]["trades"]
    assert [c["key"] for c in report["why"]["conditions"]] == list(tags)
    for changes, message in (
        ({"symbol": "XAUUSD"}, "unknown instrument"),
        ({"start": "2026-10-24", "end": "2026-10-05"}, "end must be after start"),
        ({"start": "2020-01-01"}, "one run covers at most 1098 days"),
        ({"start": "2026-06-01", "end": "2026-06-08"}, "no 1m history of EURUSD is stored between"),
        ({"methods": ["martingale@5m"]}, "unknown method 'martingale@5m'"),
        ({"methods": []}, "a setup needs at least one method"),
        ({"mode": "most"}, "mode must be one of: any, all"),
        ({"skip": ["left_channel"]}, "unknown condition 'left_channel'"),
    ):
        with pytest.raises(BacktestError, match=message):
            await run_setup(engine, registry, SetupRequest.from_mapping({**SETUP, **changes}))


async def test_a_setup_runs_in_on_the_weeks_before_its_period(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    bars = market(days=30)  # six weeks from Monday 5 October 2026
    await store(engine, registry, bars)
    request = SetupRequest.from_mapping({**SETUP, "start": "2026-11-02", "end": "2026-11-07"})
    report = await run_setup(engine, registry, request)
    keep = (bars.ts >= utc_ms(2026, 10, 12)) & (bars.ts < utc_ms(2026, 11, 7))  # three weeks before, to the end
    loaded = Bars(bars.ts[keep], bars.open[keep], bars.high[keep], bars.low[keep], bars.close[keep], 60)
    assert report == build_setup(loaded, registry.get("EURUSD"), request)
    assert report["data"]["run_in_from"] == iso(utc_ms(2026, 10, 12, 6))
    assert (report["data"]["first_day"], report["data"]["trading_days"]) == ("2026-11-02", 5)
    assert report["summary"]["trades"] > 10
    # momentum and the channel were known from the first trade on: the run-in is what they were made from
    assert any(t["momentum"] != 0 for t in report["trades"][:5])


async def test_setups_are_kept_under_their_names(
    engine: AsyncEngine, registry: InstrumentRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await list_setups(engine) == []
    first = await save_setup(engine, registry, "EURUSD", DEFINITION, 1000)
    assert first == {
        "id": 1,
        "name": "RSI or Bollinger",
        "methods": ["rsi_reversion@5m", "bollinger_reversion@5m"],
        "mode": "any",
        "within_minutes": 30.0,
        "skip": [],
        "execution": {"min_potential_pips": 2, "target_pips": 5, "stop_pips": 5},
        "updated_ms": 1000,
    }
    both = {**DEFINITION, "name": "  Both agree ", "mode": "all", "within_minutes": 15}
    second = await save_setup(engine, registry, "EURUSD", both, 2000)
    assert (second["id"], second["name"], second["mode"], second["within_minutes"]) == (2, "Both agree", "all", 15.0)
    # the same name again, however it is written: that setup is replaced and keeps its place
    changed = {**DEFINITION, "name": "rsi OR bollinger", "skip": ["against_momentum"], "execution": {}}
    replaced = await save_setup(engine, registry, "EURUSD", changed, 3000)
    assert (replaced["id"], replaced["name"], replaced["skip"]) == (1, "rsi OR bollinger", ["against_momentum"])
    assert replaced["execution"] == {} and replaced["updated_ms"] == 3000
    assert await list_setups(engine) == [replaced, second]

    assert await delete_setup(engine, 1) is True and await delete_setup(engine, 1) is False
    assert await list_setups(engine) == [second]
    third = await save_setup(engine, registry, "EURUSD", DEFINITION, 4000)
    assert third["id"] == 3  # a number is not given twice

    # what could not be run cannot be saved
    for changes, message in (
        ({"methods": []}, "a setup needs at least one method"),
        ({"methods": ["rsi_reversion"]}, "unknown method 'rsi_reversion'"),
        ({"methods": ["rsi_reversion@5m", "rsi_reversion@5m"]}, "names each method once"),
        ({"methods": ["rsi_reversion@4h"]}, "4h bars cannot be built from 1m bars"),
        ({"start": "2026-10-05"}, "unknown field 'start'; a setup has: name, methods"),
        ({"mode": "most"}, "mode must be one of"),
        ({"within_minutes": 0}, "within_minutes must be positive"),
        ({"within_minutes": "soon"}, "within_minutes must be a number"),
        ({"skip": ["momentum_turned"]}, "unknown condition 'momentum_turned'"),
        ({"execution": {"stop_pips": -1}}, "stop_pips"),
        ({"execution": {"pip_size": 0.01}}, "fixed by the instrument"),
        ({"execution": [5, 5]}, "execution must be an object"),
    ):
        with pytest.raises(BacktestError, match=message):
            await save_setup(engine, registry, "EURUSD", {**DEFINITION, **changes}, 5000)
    with pytest.raises(BacktestError, match="unknown instrument 'XAUUSD'"):
        await save_setup(engine, registry, "XAUUSD", DEFINITION, 5000)
    assert [s["id"] for s in await list_setups(engine)] == [2, 3]

    monkeypatch.setattr(service, "MAX_SETUPS", 2)
    with pytest.raises(BacktestError, match="2 setups are saved already; delete one first"):
        await save_setup(engine, registry, "EURUSD", {**DEFINITION, "name": "One more"}, 6000)
    again = await save_setup(engine, registry, "EURUSD", {**DEFINITION, "mode": "all"}, 7000)  # replacing is not adding
    assert (again["id"], again["mode"]) == (3, "all")


async def test_two_saves_at_once_are_both_kept(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    names = [f"Setup {k}" for k in range(8)]
    saved = await asyncio.gather(
        *(save_setup(engine, registry, "EURUSD", {**DEFINITION, "name": name}, 1000) for name in names)
    )
    assert sorted(s["id"] for s in saved) == list(range(1, 9))
    assert sorted(s["name"] for s in await list_setups(engine)) == names


def test_the_setup_command(
    cli_env: pytest.MonkeyPatch,  # noqa: F811
    migrated_database: str,
    registry: InstrumentRegistry,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_env.setenv("DATABASE_URL", migrated_database)
    get_settings.cache_clear()
    bars = market()
    fill_database(migrated_database, registry, bars)

    cli_env.setattr("sys.stdin", io.StringIO(json.dumps(SETUP)))
    assert main(["setup", "--request", "-"]) == 0
    captured = capsys.readouterr()
    expected = build_setup(bars, registry.get("EURUSD"), SetupRequest.from_mapping(SETUP))
    assert json.loads(captured.out) == json.loads(json.dumps(expected)) and captured.err == ""

    for argv, stdin, message in (
        (["setup"], "", "--request FILE is required"),
        (["setup", "--request", "-"], "{not json", "not valid JSON"),
        (["setup", "--request", "-"], json.dumps({**SETUP, "methods": ["nope@5m"]}), "unknown method 'nope@5m'"),
        (["setup", "--request", "-"], json.dumps({**SETUP, "symbol": "USDJPY"}), "no 1m history of USDJPY"),
        (["setup", "--request", "-"], json.dumps({**SETUP, "strategy": "rsi_reversion"}), "unknown field 'strategy'"),
    ):
        cli_env.setattr("sys.stdin", io.StringIO(stdin))
        assert main(argv) == 2
        assert message in json.loads(capsys.readouterr().out)["error"]


async def test_a_setup_over_http_is_the_setup(lab: tuple[AppServer, Bars], registry: InstrumentRegistry) -> None:  # noqa: F811
    app, bars = lab
    expected = build_setup(bars, registry.get("EURUSD"), SetupRequest.from_mapping(SETUP))
    async with httpx.AsyncClient(base_url=app.url, timeout=60) as http:
        response = await http.post("/api/setups/run", json=SETUP)
        assert response.status_code == 200 and response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == json.loads(json.dumps(expected))
        assert response.json()["summary"]["trades"] > 30 and response.json()["why"]["failed"] > 5
        for changes, message in (
            ({"methods": ["martingale@5m"]}, "unknown method 'martingale@5m'"),
            ({"methods": []}, "a setup needs at least one method"),
            ({"skip": ["momentum_turned"]}, "unknown condition 'momentum_turned'"),
            ({"execution": {"max_clips": 0}}, "max_clips must be at least 1"),
            ({"symbol": "XAUUSD"}, "unknown instrument 'XAUUSD'"),
            ({"params": {}}, "unknown field 'params'"),
            ({"start": "2026-10-24", "end": "2026-10-05"}, "end must be after start"),
            # nothing wrong with the request, but nothing stored for it: found out by the run itself
            ({"start": "2026-06-01", "end": "2026-06-08"}, "no 1m history of EURUSD is stored between"),
        ):
            refused = await http.post("/api/setups/run", json={**SETUP, **changes})
            assert refused.status_code == 400 and message in refused.json()["detail"], (changes, refused.text)
        assert (await http.post("/api/setups/run", json=[1, 2])).status_code == 422
        # one run at a time, whatever kind of run it is
        first, second = await asyncio.gather(
            http.post("/api/setups/run", json=SETUP), http.post("/api/backtests", json=REQUEST)
        )
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        refused = first if first.status_code == 409 else second
        assert "try again when it has finished" in refused.json()["detail"]
        # the live feed of the same process was not disturbed
        assert (await http.get("/api/status")).json()["connection"]["state"] == "streaming"


async def test_setups_are_saved_changed_and_deleted_over_http(lab: tuple[AppServer, Bars]) -> None:  # noqa: F811
    app, _ = lab
    async with httpx.AsyncClient(base_url=app.url, timeout=60) as http:
        assert (await http.get("/api/setups")).json() == []
        saved = await http.put("/api/setups", json=DEFINITION)
        assert saved.status_code == 200 and saved.json()["id"] == 1 and saved.json()["within_minutes"] == 30.0
        assert saved.json()["updated_ms"] > utc_ms(2020, 1, 1)
        other = await http.put("/api/setups?symbol=usdjpy", json={**DEFINITION, "name": "Yen", "execution": {}})
        assert other.status_code == 200 and other.json()["id"] == 2
        again = await http.put("/api/setups", json={**DEFINITION, "mode": "all", "within_minutes": 10})
        assert (again.json()["id"], again.json()["mode"], again.json()["within_minutes"]) == (1, "all", 10.0)
        listed = (await http.get("/api/setups")).json()
        assert listed == [again.json(), other.json()]
        assert (await http.get("/api/setups")).headers["cache-control"] == "no-store"

        # a saved setup is what a run is asked with
        run = await http.post(
            "/api/setups/run",
            json={
                "symbol": "EURUSD",
                "start": "2026-10-05",
                "end": "2026-10-24",
                **{k: listed[0][k] for k in service.SETUP_FIELDS},
            },
        )
        assert run.status_code == 200 and run.json()["request"]["setup"]["mode"] == "all"

        for body, message in (
            ({**DEFINITION, "methods": ["rsi_reversion"]}, "unknown method 'rsi_reversion'"),
            ({**DEFINITION, "symbol": "EURUSD"}, "unknown field 'symbol'"),
            ({"name": "Nothing"}, "a setup needs at least one method"),
        ):
            refused = await http.put("/api/setups", json=body)
            assert refused.status_code == 400 and message in refused.json()["detail"], body
        unknown = await http.put("/api/setups?symbol=XAUUSD", json=DEFINITION)
        assert unknown.status_code == 400 and "unknown instrument 'XAUUSD'" in unknown.json()["detail"]
        assert (await http.put("/api/setups", json=[1])).status_code == 422

        assert (await http.delete("/api/setups/1")).status_code == 204
        gone = await http.delete("/api/setups/1")
        assert gone.status_code == 404 and gone.json() == {"detail": "there is no such setup"}
        assert (await http.delete("/api/setups/first")).status_code == 422
        assert [s["id"] for s in (await http.get("/api/setups")).json()] == [2]


async def test_setups_need_the_login(engine: AsyncEngine, migrated_database: str, registry: InstrumentRegistry) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url, timeout=60) as http:
        await store(engine, registry, market(days=6))
        assert (await http.get("/api/setups")).status_code == 401
        assert (await http.put("/api/setups", json=DEFINITION)).status_code == 401
        assert (await http.delete("/api/setups/1")).status_code == 401
        assert (await http.post("/api/setups/run", json=SETUP)).status_code == 401
        assert await list_setups(engine) == []  # nothing was written
        assert (await http.post("/api/auth/login", json={"password": PASSWORD})).status_code == 200
        assert (await http.put("/api/setups", json=DEFINITION)).json()["id"] == 1
        report = await http.post("/api/setups/run", json={**SETUP, "end": "2026-10-13"})
        assert report.status_code == 200 and report.json()["data"]["trading_days"] == 6
