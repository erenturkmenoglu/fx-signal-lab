"""`fxlab verify-provider` against the local fake provider (no database needed)."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest

from fxlab.clock import SystemClock
from fxlab.config import Settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.services.provider_check import format_report, recent_open_window, verify_provider
from tests.conftest import utc_ms
from tests.support.fake_massive import FakeMassiveServer


@pytest.fixture()
async def server() -> AsyncIterator[FakeMassiveServer]:
    srv = await FakeMassiveServer().start()
    yield srv
    await srv.stop()


def settings_for(server: FakeMassiveServer, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        data_provider="massive",
        massive_api_key=server.fake.api_key,
        massive_ws_url=server.ws_url,
        massive_rest_url=server.rest_url,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


async def seed_history(server: FakeMassiveServer, registry: InstrumentRegistry) -> None:
    """Put quotes on the fake's tape inside the window the probe will ask for."""
    _, end = recent_open_window(registry.get("EURUSD"), SystemClock().now_ms())
    quotes = [
        {"b": 1.16 + (i % 9) * 0.00001, "a": 1.16008 + (i % 9) * 0.00001, "t": end - 30 * 60_000 + i * 2_000}
        for i in range(900)
    ]
    await server.fake.publish(quotes)


async def test_full_capability_report(server: FakeMassiveServer, registry: InstrumentRegistry) -> None:
    fake = server.fake
    await seed_history(server, registry)

    async def live_market() -> None:
        while True:
            await fake.publish(b=1.16, a=1.16008)
            await asyncio.sleep(0.05)
            await fake.publish(b=1.16001, a=1.16009)
            await asyncio.sleep(0.05)

    market = asyncio.create_task(live_market())
    try:
        report = await verify_provider(settings_for(server), registry, ws_seconds=3)
    finally:
        market.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await market

    statuses = {c["name"]: c["status"] for c in report["checks"]}
    assert statuses == {
        "rest_auth_market_status": "ok",
        "instrument_availability": "ok",
        "history_minute_aggregates": "ok",
        "history_second_aggregates": "ok",
        "history_quotes": "ok",
        "last_quote": "ok",
        "websocket": "ok",
    }
    by_symbol = {i["symbol"]: i["status"] for i in report["instruments"]}
    assert by_symbol["EURUSD"] == "supported" and by_symbol["USDTRY"] == "unsupported"  # per the fake's ticker list
    assert len(by_symbol) == 8  # every configured pair is checked, not only the active one

    ws = next(c for c in report["checks"] if c["name"] == "websocket")["detail"]
    assert ws["quotes_received"] > 5 and ws["channel"] == "C.EUR-USD" and ws["sample"]["symbol"] == "EURUSD"
    assert ws["latency_ms"]["n"] > 0
    quotes = next(c for c in report["checks"] if c["name"] == "history_quotes")["detail"]
    assert quotes["timestamp_resolution"] == "1 second"  # the seeded tape uses whole-second stamps
    assert report["summary"] == {
        "live_feed_verified": True,
        "history_verified": True,
        "gap_recovery_possible": True,
        "all": statuses,
    }

    text = format_report(report)
    assert "NOT Massive's production endpoints" in text  # a run against a test endpoint says so
    assert fake.api_key not in text and fake.api_key not in str(report)


async def test_plan_without_quotes_and_closed_or_silent_feed(
    server: FakeMassiveServer, registry: InstrumentRegistry
) -> None:
    fake = server.fake
    fake.quotes_entitled = False
    report = await verify_provider(settings_for(server), registry, ws_seconds=1.5)
    statuses = {c["name"]: c["status"] for c in report["checks"]}
    assert statuses["history_quotes"] == "not_entitled"
    assert statuses["history_minute_aggregates"] == "no_data"  # nothing on the tape
    assert statuses["websocket"] == "no_data"  # authenticated, subscribed, but nothing arrived
    assert report["summary"]["gap_recovery_possible"] is False and report["summary"]["live_feed_verified"] is False
    ws = next(c for c in report["checks"] if c["name"] == "websocket")["detail"]
    assert "auth_success" in ws["events"] and "hint" in ws


async def test_rejected_key_stops_early(server: FakeMassiveServer, registry: InstrumentRegistry) -> None:
    report = await verify_provider(settings_for(server, massive_api_key="wrong-key-123"), registry, ws_seconds=1)
    assert report["checks"][0]["status"] == "failed" and "rejected" in report["error"]
    assert len(report["checks"]) == 1
    assert "wrong-key-123" not in str(report)


async def test_websocket_problems_are_explained(server: FakeMassiveServer, registry: InstrumentRegistry) -> None:
    server.fake.max_connections_mode = True
    report = await verify_provider(settings_for(server), registry, ws_seconds=2)
    ws = next(c for c in report["checks"] if c["name"] == "websocket")
    assert ws["status"] == "failed" and "single forex connection" in ws["detail"]["hint"]

    skipped = await verify_provider(settings_for(server), registry, include_ws=False)
    assert next(c for c in skipped["checks"] if c["name"] == "websocket")["status"] == "skipped"


async def test_missing_key(registry: InstrumentRegistry) -> None:
    report = await verify_provider(Settings(_env_file=None, massive_api_key=""), registry)  # type: ignore[call-arg]
    assert report["error"] == "MASSIVE_API_KEY is not set" and report["checks"] == []


def test_recent_open_window_skips_the_weekend(registry: InstrumentRegistry) -> None:
    eurusd = registry.get("EURUSD")
    saturday = utc_ms(2026, 11, 14, 12)
    start, end = recent_open_window(eurusd, saturday)
    assert end == utc_ms(2026, 11, 13, 22) and end - start == 30 * 60_000  # the last half hour before Friday's close
    thursday = utc_ms(2026, 11, 12, 14)
    assert recent_open_window(eurusd, thursday)[1] == thursday - 3_600_000
