"""The HTTP API and the live WebSocket stream, served by a real uvicorn server."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import connect

from fxlab.api.app import create_app
from fxlab.config import Settings
from fxlab.storage import repo
from fxlab.storage.guard import META_KEY
from tests.conftest import INSTRUMENTS_YAML, utc_ms
from tests.support.fake_massive import FakeMassiveServer

pytestmark = pytest.mark.db


class AppServer:
    def __init__(self, settings: Settings) -> None:
        self._server = uvicorn.Server(
            uvicorn.Config(create_app(settings), host="127.0.0.1", port=0, log_level="error", log_config=None)
        )
        self._task: asyncio.Task[None] | None = None
        self.port = 0

    async def __aenter__(self) -> AppServer:
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(1000):
            if self._server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("application did not start")
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        self._server.should_exit = True
        assert self._task is not None
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._task, timeout=20)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def mock_settings(database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        data_provider="mock",
        database_url=database_url,
        instruments_file=INSTRUMENTS_YAML,
        mock_seed=11,
        mock_quotes_per_second=20,
        writer_flush_interval_s=0.2,
        log_level="WARNING",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@pytest.fixture()
async def app(engine: AsyncEngine, migrated_database: str) -> AsyncIterator[AppServer]:
    async with AppServer(mock_settings(migrated_database)) as server:
        yield server


async def test_health_status_and_reference_endpoints(app: AppServer) -> None:
    async with httpx.AsyncClient(base_url=app.url) as http:
        assert (await http.get("/api/health/live")).json() == {"status": "ok"}
        ready = await http.get("/api/health/ready")
        assert ready.status_code == 200 and ready.json() == {"ready": True, "problems": []}

        status = (await http.get("/api/status")).json()
        assert status["provider"]["label"] == "MOCK" and status["provider"]["synthetic"] is True
        assert status["connection"]["state"] == "streaming"
        assert [i["symbol"] for i in status["instruments"]] == ["EURUSD"]

        instruments = (await http.get("/api/instruments")).json()
        assert len(instruments) == 8
        usdjpy = next(i for i in instruments if i["symbol"] == "USDJPY")
        assert usdjpy["pip_size"] == 0.01 and usdjpy["active"] is False and usdjpy["provider_status"] == "unverified"

        timeframes = (await http.get("/api/timeframes")).json()
        assert [t["code"] for t in timeframes] == [
            "1s",
            "5s",
            "15s",
            "30s",
            "1m",
            "5m",
            "15m",
            "30m",
            "1h",
            "4h",
            "1d",
        ]
        assert (await http.get("/docs")).status_code == 200  # OpenAPI documentation is served


async def test_market_data_endpoints(app: AppServer) -> None:
    await asyncio.sleep(6.5)
    async with httpx.AsyncClient(base_url=app.url) as http:
        body = (await http.get("/api/observations", params={"symbol": "eurusd", "limit": 50})).json()
        obs = body["observations"]
        assert body["symbol"] == "EURUSD" and body["synthetic"] is True and len(obs) >= 5
        stamps = [o["ts"] for o in obs]
        assert stamps == sorted(stamps) and all(b - a == 1000 for a, b in itertools.pairwise(stamps))
        assert {o["status"] for o in obs} <= {"fresh", "carried", "stale", "missing"}
        assert all(o["source"] == "mock" for o in obs)
        fresh = next(o for o in obs if o["status"] == "fresh")
        assert (
            fresh["bid"] < fresh["ask"]
            and fresh["mid_low"] <= fresh["mid"] <= fresh["mid_high"]
            and fresh["spread_pips"] > 0
        )

        # explicit range goes to the database
        ranged = (
            await http.get("/api/observations", params={"symbol": "EURUSD", "start": stamps[0], "end": stamps[2]})
        ).json()
        assert [o["ts"] for o in ranged["observations"]] == stamps[:2]

        # The bar in progress is appended and flagged.  For up to a second after a bar closes there
        # is no bar in progress yet (the grid advances once per second), so poll briefly.
        for _ in range(40):
            body = (await http.get("/api/bars", params={"symbol": "EURUSD", "timeframe": "5s"})).json()
            if body["bars"] and body["bars"][-1].get("partial"):
                break
            await asyncio.sleep(0.1)
        assert (body["source"], body["price_basis"], body["synthetic"]) == ("mock", "mid", True)
        bars = body["bars"]
        assert len(bars) >= 2 and bars[-1].get("partial") is True and bars[-1]["complete"] is False
        assert all("partial" not in b for b in bars[:-1])  # exactly one bar in progress, and it is the last
        assert [b["ts"] for b in bars] == sorted({b["ts"] for b in bars})
        assert all(b["low"] <= b["open"] <= b["high"] and b["low"] <= b["close"] <= b["high"] for b in bars)
        longer = (await http.get("/api/bars", params={"symbol": "EURUSD", "timeframe": "1d"})).json()["bars"]
        assert len(longer) == 1 and longer[0]["partial"] is True  # only the bar in progress exists yet

        assert (await http.get("/api/bars", params={"symbol": "EURUSD", "timeframe": "2m"})).status_code == 400
        assert (await http.get("/api/bars", params={"symbol": "EURUSD", "timeframe": "1s"})).status_code == 400
        assert (await http.get("/api/bars", params={"symbol": "XXXYYY"})).status_code == 404
        assert (
            await http.get("/api/observations", params={"symbol": "EURUSD", "start": "yesterday-ish"})
        ).status_code == 400
        assert (await http.get("/api/bars", params={"symbol": "EURUSD", "source": "provider"})).json()["bars"] == []

        events = (await http.get("/api/events")).json()
        assert {"connected", "subscribed", "first_data"} <= {e["kind"] for e in events}
        assert (await http.get("/api/gaps")).json() == []
        assert (await http.post("/api/gaps/scan")).json() == {"queued": 0}
        assert (await http.post("/api/gaps/12345/fill")).status_code == 404
        # no REST provider in mock mode: history download is refused, not faked
        refused = await http.post(
            "/api/history/jobs",
            json={"symbol": "EURUSD", "timeframe": "1m", "start": "2026-11-12", "end": "2026-11-13"},
        )
        assert refused.status_code == 409


async def test_live_websocket_stream(app: AppServer) -> None:
    async with connect(f"ws://127.0.0.1:{app.port}/ws/live", proxy=None) as ws:
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
        assert hello["type"] == "hello"
        assert hello["status"]["provider"]["label"] == "MOCK" and "EURUSD" in hello["recent"]

        # wait for a status message and for a second that actually had quotes (right after start-up
        # a second can be MISSING, and without any price there is no bar in progress either)
        seen: dict[str, dict[str, Any]] = {}
        deadline = asyncio.get_running_loop().time() + 10
        while not {"obs", "status"} <= set(seen) and asyncio.get_running_loop().time() < deadline:
            message = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            if message["type"] == "obs" and message["obs"]["status"] != "fresh":
                assert message["obs"]["status"] in ("carried", "stale", "missing")
                continue
            seen.setdefault(message["type"], message)
        assert {"obs", "status"} <= set(seen)
        obs = seen["obs"]
        assert obs["symbol"] == "EURUSD" and obs["obs"]["n_quotes"] > 0
        assert (
            set(obs["partials"]) <= {"5s", "15s", "30s", "1m", "5m", "15m", "30m", "1h", "4h", "1d"}
            and "1d" in obs["partials"]
        )
        assert seen["status"]["status"]["live_clients"] == 1
    # a second client gets its own stream
    async with connect(f"ws://127.0.0.1:{app.port}/ws/live", proxy=None) as ws:
        assert json.loads(await asyncio.wait_for(ws.recv(), timeout=5))["type"] == "hello"


async def test_startup_failure_is_reported_instead_of_crash_looping(
    engine: AsyncEngine, migrated_database: str
) -> None:
    async with engine.begin() as conn:  # the database already belongs to real data
        await repo.set_meta(
            conn, META_KEY, {"provider": "massive", "synthetic": False, "feed_host": "socket.massive.com"}
        )
    async with (
        AppServer(mock_settings(migrated_database)) as server,
        httpx.AsyncClient(base_url=server.url) as http,
    ):
        assert (await http.get("/api/health/live")).status_code == 200
        ready = await http.get("/api/health/ready")
        assert ready.status_code == 503 and "Refusing to mix" in ready.json()["problems"][0]
        assert "DataModeMismatchError" in (await http.get("/api/status")).json()["startup_error"]
        assert (await http.get("/api/observations", params={"symbol": "EURUSD"})).status_code == 503
        async with connect(f"ws://127.0.0.1:{server.port}/ws/live", proxy=None) as ws:
            assert json.loads(await ws.recv())["type"] == "error"


async def test_dashboard_static_files_are_served_next_to_the_api(
    engine: AsyncEngine, migrated_database: str, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("<html><body>dashboard</body></html>")
    async with (
        AppServer(mock_settings(migrated_database, static_dir=tmp_path)) as server,
        httpx.AsyncClient(base_url=server.url) as http,
    ):
        assert "dashboard" in (await http.get("/")).text
        assert (await http.get("/api/health/live")).json() == {"status": "ok"}


async def test_history_download_through_the_api(engine: AsyncEngine, migrated_database: str) -> None:
    fake_server = await FakeMassiveServer().start()
    fake = fake_server.fake
    fake.silent = True
    base = utc_ms(2026, 11, 10, 9, 0, 0)
    await fake.publish(
        [{"b": 1.16 + (i % 5) * 0.0001, "a": 1.1601 + (i % 5) * 0.0001, "t": base + i * 10_000} for i in range(60)]
    )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_provider="massive",
        massive_api_key=fake.api_key,
        massive_ws_url=fake_server.ws_url,
        massive_rest_url=fake_server.rest_url,
        database_url=migrated_database,
        instruments_file=INSTRUMENTS_YAML,
        writer_flush_interval_s=0.2,
        log_level="WARNING",
    )
    try:
        async with AppServer(settings) as server, httpx.AsyncClient(base_url=server.url) as http:
            assert (await http.get("/api/status")).json()["provider"]["label"] == "TEST ENDPOINT"
            created = await http.post(
                "/api/history/jobs",
                json={"symbol": "eurusd", "timeframe": "1m", "start": base, "end": base + 600_000},
            )
            assert created.status_code == 202
            job = created.json()
            for _ in range(100):
                job = (await http.get(f"/api/history/jobs/{job['id']}")).json()
                if job["status"] in ("done", "failed"):
                    break
                await asyncio.sleep(0.05)
            assert (job["status"], job["rows_written"]) == ("done", 10)
            assert (await http.get("/api/history/jobs")).json()[0]["id"] == job["id"]

            body = (
                await http.get(
                    "/api/bars",
                    params={
                        "symbol": "EURUSD",
                        "timeframe": "1m",
                        "source": "provider",
                        "start": base,
                        "end": base + 600_000,
                    },
                )
            ).json()
            assert (body["source"], body["price_basis"]) == ("massive_agg", "bid")
            assert len(body["bars"]) == 10 and all(b["n_quotes"] == 6 and b["complete"] is None for b in body["bars"])

            bad = await http.post(
                "/api/history/jobs", json={"symbol": "EURUSD", "timeframe": "1m", "start": base, "end": base}
            )
            assert bad.status_code == 400 and "end must be after start" in bad.json()["detail"]
            assert (await http.get("/api/history/jobs/999999")).status_code == 404
            assert (await http.get("/api/instruments")).json()[0]["provider_status"] == "supported"
    finally:
        await fake_server.stop()
