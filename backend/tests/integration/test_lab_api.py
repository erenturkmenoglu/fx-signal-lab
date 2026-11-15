"""The strategy lab over HTTP, served by a real uvicorn server: a backtest is run in a child process."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.research.bars import Bars
from fxlab.research.report import BacktestRequest, build_report
from tests.integration.test_api import AppServer, mock_settings
from tests.integration.test_auth import PASSWORD, protected
from tests.integration.test_backtest import REQUEST, store
from tests.unit.test_research_report import market

pytestmark = pytest.mark.db


@pytest.fixture()
async def lab(
    engine: AsyncEngine, migrated_database: str, registry: InstrumentRegistry
) -> AsyncIterator[tuple[AppServer, Bars]]:
    bars = market()
    async with AppServer(mock_settings(migrated_database)) as server:
        await store(engine, registry, bars)  # after the start: the application creates the instrument rows
        yield server, bars


async def test_the_lab_describes_what_can_be_tested(lab: tuple[AppServer, Bars]) -> None:
    app, bars = lab
    async with httpx.AsyncClient(base_url=app.url) as http:
        described = (await http.get("/api/lab")).json()
        assert described["symbol"] == "EURUSD" and described["currency"] == "USD"
        assert [s["key"] for s in described["strategies"]] == [
            "rsi_reversion",
            "bollinger_reversion",
            "ema_momentum",
            "session_breakout",
        ]
        rsi = described["strategies"][0]
        assert rsi["timeframe"] == "5m" and rsi["params"][0] == {
            "name": "rsi_period",
            "label": "RSI period (bars)",
            "default": 14,
            "kind": "int",
            "minimum": 2,
            "maximum": 200,
            "choices": [],
            "help": "",
        }
        assert described["execution"]["spread_pips"] == 1.0 and described["execution"]["max_clips"] == 5
        assert [(h["timeframe"], h["bars"]) for h in described["history"]] == [("1m", len(bars))]
        assert (await http.get("/api/lab?symbol=eurusd")).json()["symbol"] == "EURUSD"
        unknown = await http.get("/api/lab?symbol=XAUUSD")
        assert unknown.status_code == 404 and "unknown instrument" in unknown.json()["detail"]
        # an instrument that is configured but has nothing stored
        assert (await http.get("/api/lab?symbol=USDJPY")).json()["history"] == []


async def test_a_backtest_over_http_is_the_backtest(lab: tuple[AppServer, Bars], registry: InstrumentRegistry) -> None:
    app, bars = lab
    expected = build_report(bars, registry.get("EURUSD"), BacktestRequest.from_mapping(REQUEST))
    async with httpx.AsyncClient(base_url=app.url, timeout=60) as http:
        response = await http.post("/api/backtests", json=REQUEST)
        assert response.status_code == 200 and response.headers["content-type"] == "application/json"
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == json.loads(json.dumps(expected))
        assert response.json()["summary"]["trades"] > 20
        # the live feed of the same process was not disturbed
        status = (await http.get("/api/status")).json()
        assert status["connection"]["state"] == "streaming"


async def test_requests_that_cannot_be_run_are_refused_with_the_reason(lab: tuple[AppServer, Bars]) -> None:
    app, _ = lab
    async with httpx.AsyncClient(base_url=app.url, timeout=60) as http:
        for changes, status, message in (
            ({"strategy": "martingale"}, 400, "unknown strategy 'martingale'"),
            ({"params": {"period": 2}}, 400, "period must be at least 5"),
            ({"execution": {"max_clips": 0}}, 400, "max_clips must be at least 1"),
            ({"execution": {"pip_size": 0.01}}, 400, "fixed by the instrument"),
            ({"symbol": "XAUUSD"}, 400, "unknown instrument 'XAUUSD'"),
            ({"leverage": 10}, 400, "unknown field 'leverage'"),
            ({"start": "2026-10-24", "end": "2026-10-05"}, 400, "end must be after start"),
            ({"split": "soon"}, 400, "split must be"),
            ({"timeframe": "4h"}, 400, "follow the trading day"),
            # nothing wrong with the request, but nothing stored for it: found out by the run itself
            ({"start": "2026-06-01", "end": "2026-06-08"}, 400, "no 1m history of EURUSD is stored between"),
            ({"symbol": "USDJPY"}, 400, "Download it first: fxlab history --symbol USDJPY"),
        ):
            response = await http.post("/api/backtests", json={**REQUEST, **changes})
            assert response.status_code == status, (changes, response.text)
            assert message in response.json()["detail"], (changes, response.json())
        assert (await http.post("/api/backtests", json=[1, 2])).status_code == 422
        assert (await http.post("/api/backtests", content=b"{not json")).status_code == 422


async def test_one_backtest_at_a_time(lab: tuple[AppServer, Bars]) -> None:
    app, _ = lab
    async with httpx.AsyncClient(base_url=app.url, timeout=60) as http:
        first, second = await asyncio.gather(
            http.post("/api/backtests", json=REQUEST), http.post("/api/backtests", json=REQUEST)
        )
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        refused = first if first.status_code == 409 else second
        assert "another backtest is running" in refused.json()["detail"]
        assert (await http.post("/api/backtests", json=REQUEST)).status_code == 200  # free again


async def test_a_run_that_takes_too_long_is_stopped(
    lab: tuple[AppServer, Bars], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = lab
    async with httpx.AsyncClient(base_url=app.url, timeout=60) as http:
        monkeypatch.setattr("fxlab.api.lab.MAX_RUN_SECONDS", 0.05)
        slow = await http.post("/api/backtests", json=REQUEST)
        assert slow.status_code == 504 and "was stopped; choose a shorter period" in slow.json()["detail"]
        monkeypatch.setattr("fxlab.api.lab.MAX_RUN_SECONDS", 120.0)
        assert (await http.post("/api/backtests", json=REQUEST)).status_code == 200  # nothing was left behind


async def test_a_run_that_breaks_says_so_without_details(
    lab: tuple[AppServer, Bars], monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _ = lab
    async with httpx.AsyncClient(base_url=app.url, timeout=60) as http:
        monkeypatch.setattr(sys, "executable", "/bin/false")  # the child exits at once with an error
        broken = await http.post("/api/backtests", json=REQUEST)
        assert broken.status_code == 500
        assert broken.json() == {"detail": "the backtest stopped with an error; see the application log"}


async def test_the_lab_needs_the_login(
    engine: AsyncEngine, migrated_database: str, registry: InstrumentRegistry
) -> None:
    async with AppServer(protected(migrated_database)) as app, httpx.AsyncClient(base_url=app.url, timeout=60) as http:
        await store(engine, registry, market(days=6))
        assert (await http.get("/api/lab")).status_code == 401
        assert (await http.post("/api/backtests", json=REQUEST)).status_code == 401
        assert (await http.post("/api/auth/login", json={"password": PASSWORD})).status_code == 200
        assert (await http.get("/api/lab")).json()["history"][0]["bars"] == 6 * 900
        report = await http.post("/api/backtests", json={**REQUEST, "end": "2026-10-13"})
        assert report.status_code == 200 and report.json()["data"]["trading_days"] == 6
