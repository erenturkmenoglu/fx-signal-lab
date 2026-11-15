"""End-to-end tests of the ingestion service on real PostgreSQL and real sockets.

The Massive code path is exercised against the local fake provider.  Time is real but
shifted to a fixed weekday (see OffsetClock) so the results do not depend on the day
the suite runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import math
import random
from collections import Counter
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
import yaml
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.config import Settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.marketdata.base import ConnectionState
from fxlab.pipeline.ingestion import IngestionService
from fxlab.services.reconcile import format_reconciliation, reconcile
from fxlab.storage import repo
from fxlab.storage.guard import DataModeMismatchError
from fxlab.timeutil import SECOND_MS, dt_to_ms, floor_ms, iso
from tests.conftest import INSTRUMENTS_YAML, T0, utc_ms
from tests.support.clocks import OffsetClock
from tests.support.fake_massive import FakeMassive, FakeMassiveServer

pytestmark = pytest.mark.db

SATURDAY = utc_ms(2026, 11, 14, 12, 0, 0)


async def wait_for(predicate: Callable[[], Any], timeout: float = 10.0, what: str = "condition") -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.05)


def massive_settings(server: FakeMassiveServer, database_url: str, **overrides: Any) -> Settings:
    values: dict[str, Any] = dict(
        data_provider="massive",
        massive_api_key=server.fake.api_key,
        massive_ws_url=server.ws_url,
        massive_rest_url=server.rest_url,
        database_url=database_url,
        ws_backoff_base_s=0.05,
        ws_backoff_max_s=0.2,
        writer_flush_interval_s=0.2,
        gap_min_seconds=2,
        gap_backfill_delay_s=0.3,
        gap_scan_interval_s=3600,
        massive_rest_max_retries=1,
        # These tests run on a fixed date (T0).  On TimescaleDB the compression and retention jobs
        # work on the real date and would start to compress or drop the test rows once T0 is old
        # enough; the policies have their own tests in test_storage.py.
        storage_policies_enabled=False,
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class Market:
    """Publishes a random-walk EUR/USD quote stream to the fake provider on the test clock."""

    def __init__(self, fake: FakeMassive, clock: OffsetClock, rate: float = 12.0) -> None:
        self.fake, self.clock, self.rate = fake, clock, rate
        self.mid = 1.1600
        self._rng = random.Random(5)
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._rng.expovariate(self.rate))
            self.mid *= math.exp(self._rng.gauss(0, 0.00003))
            await self.fake.publish(
                b=round(self.mid - 0.00004, 5), a=round(self.mid + 0.00004, 5), t=self.clock.now_ms()
            )

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


@pytest.fixture()
async def server() -> AsyncIterator[FakeMassiveServer]:
    srv = await FakeMassiveServer().start()
    yield srv
    await srv.stop()


async def fetch_all(engine: AsyncEngine, sql: str) -> list[Any]:
    async with engine.connect() as conn:
        return list((await conn.execute(text(sql))).all())


def streaming(service: IngestionService) -> bool:
    return service.adapter is not None and service.adapter.status().state is ConnectionState.STREAMING


# ------------------------------------------------------------------------------- mock ----


async def test_mock_mode_end_to_end_is_labelled_synthetic_everywhere(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str
) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        data_provider="mock",
        database_url=migrated_database,
        mock_seed=3,
        mock_quotes_per_second=25,
        writer_flush_interval_s=0.2,
        storage_policies_enabled=False,  # fixed test date, see massive_settings
    )
    service = IngestionService(settings, registry, engine, clock=OffsetClock(SATURDAY))
    await service.start()
    try:
        await asyncio.sleep(6.4)  # long enough for at least one complete 5 s bar
        status = service.status()
    finally:
        await service.stop()

    # labelling: nothing about this run can be mistaken for market data
    assert status["provider"] == {
        "name": "mock",
        "label": "MOCK",
        "configured": True,
        "synthetic": True,
        "official_endpoint": False,
        "feed_host": None,
        "rest_available": False,
    }
    assert status["connection"]["is_synthetic"] is True
    assert service.data_mode == {"provider": "mock", "synthetic": True, "feed_host": None}

    obs = await fetch_all(engine, "SELECT ts, status, source, n_quotes, bid FROM obs_1s ORDER BY ts")
    assert len(obs) >= 3
    assert {o.source for o in obs} == {"mock"}
    seconds = [dt_to_ms(o.ts) for o in obs]
    assert seconds == list(range(seconds[0], seconds[0] + len(seconds) * 1000, 1000))  # one row per second, no holes
    assert any(o.status == "fresh" and o.n_quotes > 0 for o in obs)

    flags = await fetch_all(engine, "SELECT DISTINCT flags & 4 AS synthetic FROM quote_ticks")
    assert [f.synthetic for f in flags] == [4]
    bars = await fetch_all(engine, "SELECT DISTINCT source, price_basis FROM bars")
    assert [(b.source, b.price_basis) for b in bars] == [("mock", "mid")]

    inst = status["instruments"][0]
    assert inst["symbol"] == "EURUSD" and inst["market_open"] is True  # the mock market never closes
    assert inst["provider_status"] == "mock (synthetic)"
    counters = inst["counters"]
    assert counters["received"] == counters["accepted"] + counters.get("duplicate", 0) + counters.get(
        "late", 0
    ) + counters.get("rejected", 0)
    assert status["storage"]["db_ok"] is True and status["storage"]["dropped"] == {"ticks": 0, "obs": 0}
    events = await fetch_all(engine, "SELECT kind, detail FROM connection_events ORDER BY id")
    assert events[0].detail["synthetic"] is True


async def test_mock_data_cannot_enter_a_database_of_real_data(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    real = IngestionService(massive_settings(server, migrated_database), registry, engine, clock=OffsetClock(T0))
    await real.start()
    await real.stop()
    mock = IngestionService(
        Settings(_env_file=None, data_provider="mock", database_url=migrated_database),
        registry,
        engine,  # type: ignore[call-arg]
    )
    with pytest.raises(DataModeMismatchError):
        await mock.start()


# ---------------------------------------------------------------------------- massive ----


async def test_not_configured_means_no_data_and_no_silent_mock_fallback(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str
) -> None:
    settings = Settings(
        _env_file=None,
        data_provider="massive",
        massive_api_key="",
        database_url=migrated_database,
        writer_flush_interval_s=0.2,
        storage_policies_enabled=False,
    )  # type: ignore[call-arg]
    service = IngestionService(settings, registry, engine, clock=OffsetClock(T0))
    await service.start()
    try:
        await asyncio.sleep(2.3)
        status = service.status()
        assert await service.scan_gaps() == 0  # an empty grid is not a gap when nothing is collected
    finally:
        await service.stop()
    assert service.adapter is None
    assert status["storage"]["policies_managed"] is False and status["storage"]["policy_errors"] == []
    assert await fetch_all(engine, "SELECT 1 FROM data_gaps") == []
    assert status["provider"]["label"] == "NOT CONFIGURED" and status["provider"]["synthetic"] is False
    assert status["connection"]["state"] == "not_configured"
    inst = status["instruments"][0]
    assert inst["bid"] is None and inst["signal_gate"] == {
        "allowed": False,
        "reason": "PROVIDER NOT CONFIGURED",
    }
    assert await fetch_all(engine, "SELECT 1 FROM obs_1s") == []
    assert await fetch_all(engine, "SELECT 1 FROM quote_ticks") == []


async def test_live_capture_outage_and_recovery_from_historical_quotes(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    """Acceptance criteria 2, 3, 4, 12: connect, persist, keep an honest 1 s grid, recover."""
    clock = OffsetClock(T0)
    fake = server.fake
    market = Market(fake, clock)
    service = IngestionService(massive_settings(server, migrated_database), registry, engine, clock=clock)
    await service.start()
    market.start()
    try:
        await wait_for(lambda: streaming(service), what="streaming")
        await asyncio.sleep(3.2)  # normal live capture
        assert service.status()["instruments"][0]["signal_gate"] == {"allowed": True, "reason": "OK"}

        # --- outage: the socket drops and stays silent while the market keeps quoting --------
        outage_begin = clock.now_ms()
        fake.silent = True
        await fake.drop_connections()
        await asyncio.sleep(4.5)
        fake.silent = False
        outage_end = clock.now_ms()

        async def gap_filled() -> bool:
            async with engine.connect() as conn:
                return bool(await repo.list_gaps(conn, status="filled"))

        await wait_for(gap_filled, timeout=15, what="gap backfill")
        await asyncio.sleep(1.5)  # let rebuilt bars and the next live seconds reach the database
        status = service.status()
    finally:
        await market.stop()
        await service.stop()

    # --- the outage was detected and recorded ---------------------------------------------
    async with engine.connect() as conn:
        (gap,) = await repo.list_gaps(conn)
    assert (gap["kind"], gap["status"], gap["symbol"]) == ("feed_outage", "filled", "EURUSD")
    assert gap["start_ms"] <= outage_begin and gap["end_ms"] >= outage_end - 1000
    assert gap["detail"]["provider_quotes"] > 20 and gap["detail"]["fresh_seconds"] >= 3

    events = [e.kind for e in await fetch_all(engine, "SELECT kind FROM connection_events ORDER BY id")]
    assert events.count("first_data") == 2 and "disconnected" in events and "reconnect_scheduled" in events
    assert status["connection"]["sessions_established"] == 2
    assert status["provider"]["label"] == "TEST ENDPOINT"  # never "LIVE" for a non-production host
    assert status["instruments"][0]["provider_status"] == "supported"

    # --- the grid: contiguous, and the outage is filled with rebuilt (not invented) data ----
    obs = await fetch_all(
        engine, "SELECT ts, status, source, n_quotes, bid, ask, mid_high, mid_low FROM obs_1s ORDER BY ts"
    )
    seconds = [dt_to_ms(o.ts) for o in obs]
    assert seconds == list(range(seconds[0], seconds[0] + len(seconds) * 1000, 1000))
    interior = [
        o
        for o in obs
        if floor_ms(outage_begin, SECOND_MS) + 1000 <= dt_to_ms(o.ts) < floor_ms(outage_end, SECOND_MS) - 1000
    ]
    assert len(interior) >= 2
    assert {o.source for o in interior} == {"backfill"}
    for o in interior:
        ts = dt_to_ms(o.ts)
        tape = [q for q in fake.tape if ts <= q["t"] < ts + 1000]
        assert o.n_quotes == len(tape) > 0 and o.status == "fresh"
        mids = [(q["b"] + q["a"]) / 2 for q in tape]
        assert o.mid_high == pytest.approx(max(mids)) and o.mid_low == pytest.approx(min(mids))
        assert (o.bid, o.ask) == (tape[-1]["b"], tape[-1]["a"])
    # seconds captured live are never overwritten by the backfill
    live_fresh = [o for o in obs if o.source == "live" and o.status == "fresh"]
    assert len(live_fresh) >= 4

    # --- ticks: outage quotes are stored as backfilled, without receipt time ----------------
    ticks = await fetch_all(engine, "SELECT source_ts, recv_ts, flags FROM quote_ticks ORDER BY source_ts")
    backfilled = [t for t in ticks if t.flags & 2]
    assert backfilled and all(t.recv_ts is None for t in backfilled)
    assert all(t.recv_ts is not None for t in ticks if not t.flags & 2)
    assert len(ticks) == len(fake.tape) or len(ticks) >= len(fake.tape) - 30  # all but the last moments
    # a quote is stored once: seconds captured live get no backfilled ticks on top
    live_seconds = {floor_ms(dt_to_ms(t.source_ts), SECOND_MS) for t in ticks if not t.flags & 2}
    assert not [t for t in backfilled if floor_ms(dt_to_ms(t.source_ts), SECOND_MS) in live_seconds]

    # --- bars covering the outage were rebuilt with the recovered data ----------------------
    bars = await fetch_all(
        engine,
        "SELECT ts, high, low, n_backfilled, n_valid, n_expected, complete, source, price_basis FROM bars WHERE timeframe = '5s' ORDER BY ts",
    )
    assert {(b.source, b.price_basis) for b in bars} == {("live", "mid")}
    rebuilt = [b for b in bars if (b.n_backfilled or 0) > 0]
    assert rebuilt
    full = [b for b in rebuilt if b.n_backfilled == 5]
    for b in full:
        ts = dt_to_ms(b.ts)
        mids = [(q["b"] + q["a"]) / 2 for q in fake.tape if ts <= q["t"] < ts + 5000]
        assert b.high == pytest.approx(max(mids)) and b.low == pytest.approx(min(mids)) and b.complete is True

    # --- per-minute statistics and the Authorization header on every REST call --------------
    assert all(h == f"Bearer {fake.api_key}" for h in fake.rest_auth_headers)
    assert "/v3/quotes/C:EUR-USD" in fake.rest_calls and "/v3/reference/tickers" in fake.rest_calls


async def test_restart_detects_the_downtime_hole_and_backfills_it(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    clock = OffsetClock(T0)
    fake = server.fake
    market = Market(fake, clock)
    market.start()
    settings = massive_settings(server, migrated_database)
    try:
        first = IngestionService(settings, registry, engine, clock=clock)
        await first.start()
        await wait_for(lambda: streaming(first), what="streaming")
        await asyncio.sleep(3.2)
        await first.stop()  # the process goes away ...
        stopped_at = clock.now_ms()
        await asyncio.sleep(4.2)  # ... while the market keeps trading
        restarted_at = clock.now_ms()

        second = IngestionService(settings, registry, engine, clock=clock)
        await second.start()
        try:

            async def hole_filled() -> bool:
                async with engine.connect() as conn:
                    return bool(await repo.list_gaps(conn, status="filled"))

            await wait_for(hole_filled, timeout=15, what="hole backfill")
            await asyncio.sleep(4.0)  # let the 5 s bars after the restart complete and reach the database
        finally:
            await second.stop()
    finally:
        await market.stop()

    async with engine.connect() as conn:
        (gap,) = await repo.list_gaps(conn)
    assert (gap["kind"], gap["status"]) == ("hole", "filled")
    assert stopped_at - 2000 <= gap["start_ms"] <= stopped_at + 1000
    assert restarted_at - 1000 <= gap["end_ms"] <= restarted_at + 2000

    obs = await fetch_all(engine, "SELECT ts, source, status FROM obs_1s ORDER BY ts")
    seconds = [dt_to_ms(o.ts) for o in obs]
    assert seconds == list(range(seconds[0], seconds[0] + len(seconds) * 1000, 1000))  # contiguous across the restart
    in_hole = [o for o in obs if gap["start_ms"] <= dt_to_ms(o.ts) < gap["end_ms"]]
    assert len(in_hole) == gap["seconds"] and {o.status for o in in_hole} == {"fresh"}
    # The gap runs up to the first new quote.  Everything in it was recovered, except its closing
    # second: that one holds the first quote captured after the restart, and live capture wins.
    assert {o.source for o in in_hole[:-1]} == {"backfill"} and in_hole[-1].source == "live"
    recovered = len(in_hole) - 1

    # the 5 s bars spanning the restart were rebuilt from the recovered seconds and are complete
    bars = await fetch_all(
        engine,
        "SELECT ts, n_valid, n_expected, n_backfilled, complete FROM bars WHERE timeframe = '5s' ORDER BY ts",
    )
    spanning = [b for b in bars if dt_to_ms(b.ts) < gap["end_ms"] and dt_to_ms(b.ts) + 5_000 > gap["start_ms"]]
    assert spanning and all((b.n_backfilled or 0) > 0 for b in spanning)
    first_fresh = min(dt_to_ms(o.ts) for o in obs if o.status == "fresh")
    covered = [b for b in spanning if first_fresh <= dt_to_ms(b.ts) and dt_to_ms(b.ts) + 5_000 <= seconds[-1] + 1000]
    assert covered and all(b.complete and b.n_valid == 5 for b in covered)
    assert sum(b.n_backfilled for b in spanning) == recovered  # every recovered second is accounted for

    # The second that was being captured when the process stopped has its live ticks on disk but
    # never got a live row.  It is rebuilt from the provider's quotes, and the ticks recovered
    # for it continue where live capture ended: a quote is stored once.
    ticks = await fetch_all(engine, "SELECT source_ts, flags FROM quote_ticks ORDER BY source_ts")
    stamps = [dt_to_ms(t.source_ts) for t in ticks]
    assert (
        len(stamps) - len(set(stamps)) <= 1
    )  # (two quotes may share a millisecond; a re-stored second would add many)
    cut = floor_ms(gap["start_ms"], SECOND_MS)
    first_second = [t for t in ticks if cut <= dt_to_ms(t.source_ts) < cut + 1000]
    live_part = [dt_to_ms(t.source_ts) for t in first_second if not t.flags & 2]
    recovered_part = [dt_to_ms(t.source_ts) for t in first_second if t.flags & 2]
    if live_part and recovered_part:
        assert max(live_part) < min(recovered_part)
    # Everything the provider published is stored exactly once.  The one exception is documented:
    # the closing second of the gap keeps its live capture, which began with the first new quote.
    closing = gap["end_ms"] - 1000
    resumed = min(dt_to_ms(t.source_ts) for t in ticks if dt_to_ms(t.source_ts) >= closing and not t.flags & 2)
    assert closing <= resumed < gap["end_ms"]
    on_tape = [q["t"] for q in fake.tape if stamps[0] <= q["t"] <= stamps[-1] and not closing <= q["t"] < resumed]
    assert sorted(stamps) == sorted(on_tape)


async def test_outage_in_progress_at_shutdown_is_recovered_after_the_restart(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    """The feed dies, then the process is stopped (or crashes) before the feed comes back."""
    clock = OffsetClock(T0)
    fake = server.fake
    market = Market(fake, clock)
    market.start()
    settings = massive_settings(server, migrated_database)
    try:
        first = IngestionService(settings, registry, engine, clock=clock)
        await first.start()
        await wait_for(lambda: streaming(first), what="streaming")
        await asyncio.sleep(3.2)
        outage_begin = clock.now_ms()
        fake.silent = True  # the market keeps quoting, the socket delivers nothing
        await fake.drop_connections()
        await asyncio.sleep(3.6)  # seconds without updates are written: carried, later stale
        await first.stop()  # no reconnect happened, so no outage was ever reported
        stopped_at = clock.now_ms()
        async with engine.connect() as conn:
            assert await repo.list_gaps(conn) == []
        await asyncio.sleep(2.4)
        fake.silent = False
        restarted_at = clock.now_ms()

        second = IngestionService(settings, registry, engine, clock=clock)
        await second.start()
        try:

            async def recovered() -> bool:
                async with engine.connect() as conn:
                    return bool(await repo.list_gaps(conn, status="filled"))

            await wait_for(recovered, timeout=15, what="recovery of the interrupted outage")
            await asyncio.sleep(1.0)
        finally:
            await second.stop()
    finally:
        await market.stop()

    async with engine.connect() as conn:
        (gap,) = await repo.list_gaps(conn)
    assert (gap["kind"], gap["status"]) == ("hole", "filled")
    # the gap starts where the updates stopped, not where the process stopped
    assert outage_begin - 2000 <= gap["start_ms"] <= outage_begin + 1000 < stopped_at - 2000
    assert restarted_at - 1000 <= gap["end_ms"] <= restarted_at + 2000
    obs = await fetch_all(engine, "SELECT ts, source, status FROM obs_1s ORDER BY ts")
    seconds = [dt_to_ms(o.ts) for o in obs]
    assert seconds == list(range(seconds[0], seconds[0] + len(seconds) * 1000, 1000))
    in_gap = [o for o in obs if gap["start_ms"] <= dt_to_ms(o.ts) < gap["end_ms"]]
    assert len(in_gap) == gap["seconds"] >= 5
    assert {(o.source, o.status) for o in in_gap[:-1]} == {("backfill", "fresh")}  # no stale second is left behind
    assert (in_gap[-1].source, in_gap[-1].status) == ("live", "fresh")  # the second of the first new quote


async def test_outage_detected_just_before_shutdown_is_filled_after_the_restart(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    """A gap is recorded when it is detected, not when it is filled: a restart cannot forget it."""
    clock = OffsetClock(T0)
    fake = server.fake
    market = Market(fake, clock)
    market.start()
    try:
        patient = massive_settings(server, migrated_database, gap_backfill_delay_s=60)
        first = IngestionService(patient, registry, engine, clock=clock)
        await first.start()
        await wait_for(lambda: streaming(first), what="streaming")
        await asyncio.sleep(2.2)
        fake.silent = True
        await fake.drop_connections()
        await asyncio.sleep(4.2)
        fake.silent = False

        async def recorded() -> bool:
            async with engine.connect() as conn:
                return bool(await repo.list_gaps(conn, status="open"))

        await wait_for(recorded, timeout=10, what="the outage to be recorded")
        await first.stop()  # ... long before the 60 s backfill delay has passed
        await asyncio.sleep(2.4)

        second = IngestionService(massive_settings(server, migrated_database), registry, engine, clock=clock)
        await second.start()
        try:

            async def all_filled() -> bool:
                async with engine.connect() as conn:
                    gaps = await repo.list_gaps(conn)
                return len(gaps) == 2 and all(g["status"] == "filled" for g in gaps)

            await wait_for(all_filled, timeout=25, what="both gaps to be filled after the restart")
        finally:
            await second.stop()
    finally:
        await market.stop()

    async with engine.connect() as conn:
        gaps = await repo.list_gaps(conn)
    assert sorted(g["kind"] for g in gaps) == ["feed_outage", "hole"]
    obs = await fetch_all(engine, "SELECT ts, source, status FROM obs_1s ORDER BY ts")
    for gap in gaps:
        inside = [o for o in obs if gap["start_ms"] + 1000 <= dt_to_ms(o.ts) < gap["end_ms"] - 1000]
        assert inside and {o.source for o in inside} == {"backfill"}
    assert not [o for o in obs if o.status in ("stale", "missing")]


async def test_a_reconnect_of_a_few_seconds_is_recovered_too(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    """The minimum gap length is for the periodic scan; a known disconnect is recovered at any length."""
    clock = OffsetClock(T0)
    fake = server.fake
    market = Market(fake, clock)
    service = IngestionService(
        massive_settings(server, migrated_database, gap_min_seconds=30), registry, engine, clock=clock
    )
    await service.start()
    market.start()
    try:
        await wait_for(lambda: streaming(service), what="streaming")
        await asyncio.sleep(2.2)
        fake.silent = True
        await fake.drop_connections()
        await asyncio.sleep(2.6)  # far below the 30 s minimum
        fake.silent = False

        async def recovered() -> bool:
            async with engine.connect() as conn:
                return bool(await repo.list_gaps(conn, status="filled"))

        await wait_for(recovered, timeout=15, what="recovery of the short outage")
        await asyncio.sleep(1.0)
    finally:
        await market.stop()
        await service.stop()

    async with engine.connect() as conn:
        (gap,) = await repo.list_gaps(conn)
    assert (gap["kind"], gap["status"]) == ("feed_outage", "filled") and 3 <= gap["seconds"] <= 6
    obs = await fetch_all(engine, "SELECT ts, source, status FROM obs_1s ORDER BY ts")
    inside = [o for o in obs if gap["start_ms"] <= dt_to_ms(o.ts) < gap["end_ms"]]
    assert len(inside) == gap["seconds"] and {o.status for o in inside} == {"fresh"}
    assert not [o for o in obs if o.status in ("stale", "missing")]


# ------------------------------------------------------ whole-second provider timestamps ----
# Massive's /v3/quotes returned whole-second timestamps for EUR/USD when it was first run against
# production (15 November 2026).  Recovered quotes therefore cannot be ordered against live
# ones inside a second, and several quotes legitimately share one timestamp.


def tape_in_second(fake: FakeMassive, second_ms: int) -> list[dict[str, Any]]:
    return [q for q in fake.tape if second_ms <= q["t"] < second_ms + 1000]


def per_second(ticks: list[Any]) -> dict[int, int]:
    return dict(Counter(floor_ms(dt_to_ms(t.source_ts), SECOND_MS) for t in ticks))


async def test_outage_is_recovered_from_history_stamped_in_whole_seconds(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    clock = OffsetClock(T0)
    fake = server.fake
    fake.history_resolution_ms = 1000  # the stream keeps milliseconds
    market = Market(fake, clock)
    service = IngestionService(massive_settings(server, migrated_database), registry, engine, clock=clock)
    await service.start()
    market.start()
    try:
        await wait_for(lambda: streaming(service), what="streaming")
        await asyncio.sleep(3.2)
        fake.silent = True
        await fake.drop_connections()
        await asyncio.sleep(4.5)
        fake.silent = False

        async def gap_filled() -> bool:
            async with engine.connect() as conn:
                return bool(await repo.list_gaps(conn, status="filled"))

        await wait_for(gap_filled, timeout=15, what="gap backfill")
        await asyncio.sleep(1.5)
    finally:
        await market.stop()
        await service.stop()

    async with engine.connect() as conn:
        (gap,) = await repo.list_gaps(conn)
    assert (gap["kind"], gap["status"]) == ("feed_outage", "filled")
    assert gap["detail"]["rejected"] == 0 and gap["detail"]["accepted"] == gap["detail"]["provider_quotes"] > 20

    # the grid: contiguous, and every recovered second is exactly what the provider published in it
    obs = await fetch_all(
        engine, "SELECT ts, status, source, n_quotes, bid, ask, mid_high, mid_low FROM obs_1s ORDER BY ts"
    )
    seconds = [dt_to_ms(o.ts) for o in obs]
    assert seconds == list(range(seconds[0], seconds[0] + len(seconds) * 1000, 1000))
    assert not [o for o in obs if o.status in ("stale", "missing")]
    recovered = [o for o in obs if o.source == "backfill"]
    assert len(recovered) >= 3
    for o in recovered:
        tape = tape_in_second(fake, dt_to_ms(o.ts))
        assert o.status == "fresh" and o.n_quotes == len(tape) > 0
        assert (o.bid, o.ask) == (tape[-1]["b"], tape[-1]["a"])  # the last quote of the second, in publication order
        mids = [(q["b"] + q["a"]) / 2 for q in tape]
        assert o.mid_high == pytest.approx(max(mids)) and o.mid_low == pytest.approx(min(mids))

    # ticks: recovered ones carry the provider's whole-second stamps, live ones their milliseconds
    ticks = await fetch_all(engine, "SELECT source_ts, recv_ts, flags FROM quote_ticks ORDER BY source_ts")
    backfilled = [t for t in ticks if t.flags & 2]
    live = [t for t in ticks if not t.flags & 2]
    assert backfilled and all(dt_to_ms(t.source_ts) % 1000 == 0 and t.recv_ts is None for t in backfilled)
    assert any(dt_to_ms(t.source_ts) % 1000 for t in live)
    # a quote is stored once: a second holds live ticks or recovered ticks, never both, and a
    # recovered second holds exactly the quotes of the tape
    recovered_seconds, live_seconds = per_second(backfilled), per_second(live)
    assert not set(recovered_seconds) & set(live_seconds)
    assert set(recovered_seconds) == {dt_to_ms(o.ts) for o in recovered}
    for second, count in recovered_seconds.items():
        assert count == len(tape_in_second(fake, second))


async def test_restart_hole_is_recovered_from_history_stamped_in_whole_seconds(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    clock = OffsetClock(T0)
    fake = server.fake
    fake.history_resolution_ms = 1000
    market = Market(fake, clock)
    market.start()
    settings = massive_settings(server, migrated_database)
    try:
        first = IngestionService(settings, registry, engine, clock=clock)
        await first.start()
        await wait_for(lambda: streaming(first), what="streaming")
        await asyncio.sleep(3.2)
        await first.stop()
        await asyncio.sleep(4.2)
        second = IngestionService(settings, registry, engine, clock=clock)
        await second.start()
        try:

            async def hole_filled() -> bool:
                async with engine.connect() as conn:
                    return bool(await repo.list_gaps(conn, status="filled"))

            await wait_for(hole_filled, timeout=15, what="hole backfill")
            await asyncio.sleep(2.0)
        finally:
            await second.stop()
    finally:
        await market.stop()

    async with engine.connect() as conn:
        (gap,) = await repo.list_gaps(conn)
    assert (gap["kind"], gap["status"]) == ("hole", "filled")

    obs = await fetch_all(engine, "SELECT ts, source, status, n_quotes FROM obs_1s ORDER BY ts")
    seconds = [dt_to_ms(o.ts) for o in obs]
    assert seconds == list(range(seconds[0], seconds[0] + len(seconds) * 1000, 1000))
    in_hole = [o for o in obs if gap["start_ms"] <= dt_to_ms(o.ts) < gap["end_ms"]]
    assert len(in_hole) == gap["seconds"] and {o.status for o in in_hole} == {"fresh"}
    assert {o.source for o in in_hole[:-1]} == {"backfill"} and in_hole[-1].source == "live"
    for o in in_hole[:-1]:  # every recovered row counts all of the provider's quotes of its second
        assert o.n_quotes == len(tape_in_second(fake, dt_to_ms(o.ts)))

    ticks = await fetch_all(engine, "SELECT source_ts, flags FROM quote_ticks ORDER BY source_ts")
    backfilled = [t for t in ticks if t.flags & 2]
    live = [t for t in ticks if not t.flags & 2]
    assert backfilled and all(dt_to_ms(t.source_ts) % 1000 == 0 for t in backfilled)
    recovered_seconds, live_seconds = per_second(backfilled), per_second(live)
    # Never both kinds in one second, so no quote is stored twice.  The price of whole-second
    # stamps: in the second that was being captured when the process stopped, the quotes after
    # the stop cannot be told apart from the ones already on disk, so its ticks stay as captured
    # (its row is still rebuilt from all of the provider's quotes).
    assert not set(recovered_seconds) & set(live_seconds)
    for second, count in recovered_seconds.items():
        assert count == len(tape_in_second(fake, second))
    cut = floor_ms(gap["start_ms"], SECOND_MS)
    if cut in live_seconds:
        assert live_seconds[cut] <= len(tape_in_second(fake, cut))
    # everything else that was published is on disk exactly once (the first and the last second
    # of the run and the closing second of the gap are partial by nature)
    stored = recovered_seconds | live_seconds
    closing = gap["end_ms"] - 1000
    for second in range(min(stored) + 1000, max(stored), 1000):
        if second not in (cut, closing):
            assert stored.get(second, 0) == len(tape_in_second(fake, second)), second


async def test_stream_stamped_in_whole_seconds_is_accepted_in_full(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    """Quotes sharing a timestamp are neither late nor duplicates, and the gate stays open."""
    clock = OffsetClock(T0)
    fake = server.fake
    fake.stream_resolution_ms = fake.history_resolution_ms = 1000
    market = Market(fake, clock)
    service = IngestionService(massive_settings(server, migrated_database), registry, engine, clock=clock)
    await service.start()
    market.start()
    try:
        await wait_for(lambda: streaming(service), what="streaming")
        await asyncio.sleep(6.0)  # enough samples for the tracker to recognise the granularity
        before = service.status()["instruments"][0]
        fake.silent = True
        await fake.drop_connections()
        await asyncio.sleep(3.5)
        fake.silent = False

        async def gap_filled() -> bool:
            async with engine.connect() as conn:
                return bool(await repo.list_gaps(conn, status="filled"))

        await wait_for(gap_filled, timeout=15, what="gap backfill")
        await asyncio.sleep(1.5)
        after = service.status()["instruments"][0]
    finally:
        await market.stop()
        await service.stop()

    assert before["counters"]["received"] == before["counters"]["accepted"] > 50
    assert after["counters"]["received"] == after["counters"]["accepted"]
    assert before["signal_gate"] == {"allowed": True, "reason": "OK"}
    latency = before["latency"]
    assert latency["resolution_ms"] == 1000 and "1 s resolution" in latency["note"]
    assert latency["clock_skew_suspected"] is False and 0 <= latency["p50_ms"] <= 1000

    obs = await fetch_all(engine, "SELECT ts, status, source, n_quotes FROM obs_1s ORDER BY ts")
    seconds = [dt_to_ms(o.ts) for o in obs]
    assert seconds == list(range(seconds[0], seconds[0] + len(seconds) * 1000, 1000))
    assert not [o for o in obs if o.status in ("stale", "missing")]
    assert [o for o in obs if o.source == "backfill"]

    ticks = await fetch_all(engine, "SELECT source_ts, flags FROM quote_ticks ORDER BY source_ts")
    assert all(dt_to_ms(t.source_ts) % 1000 == 0 for t in ticks)
    recovered_seconds = per_second([t for t in ticks if t.flags & 2])
    live_seconds = per_second([t for t in ticks if not t.flags & 2])
    assert recovered_seconds and not set(recovered_seconds) & set(live_seconds)
    stored = recovered_seconds | live_seconds
    edges = {s for s in live_seconds if s - 1000 in recovered_seconds or s + 1000 in recovered_seconds}
    for second in range(min(stored) + 1000, max(stored), 1000):
        if second not in edges:  # the seconds in which the feed dropped and came back are partial
            assert stored.get(second, 0) == len(tape_in_second(fake, second)), second


async def test_live_capture_reconciles_with_the_provider_history(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    """`fxlab reconcile`: seconds captured live hold exactly the quotes of /v3/quotes, and a loss is named."""
    clock = OffsetClock(T0)
    fake = server.fake
    fake.history_resolution_ms = 1000
    settings = massive_settings(server, migrated_database)
    service = IngestionService(settings, registry, engine, clock=clock)
    levels = [(1.16000, 1.16008), (1.16001, 1.16009), (1.16002, 1.16010)]

    async def market() -> None:  # neighbours always differ in price: nothing can pass for a duplicate
        for n in range(10_000):
            bid, ask = levels[n % 3]
            await fake.publish(b=bid, a=ask, t=clock.now_ms())
            await asyncio.sleep(0.07)

    await service.start()
    quoting = asyncio.create_task(market())
    try:
        await wait_for(lambda: streaming(service), what="streaming")
        await asyncio.sleep(4.3)
    finally:
        quoting.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await quoting
        await service.stop()

    stamps = [dt_to_ms(t.source_ts) for t in await fetch_all(engine, "SELECT source_ts FROM quote_ticks")]
    start, end = floor_ms(min(stamps), SECOND_MS) + 1000, floor_ms(max(stamps), SECOND_MS)  # whole seconds only
    assert end - start >= 2000

    async def check(**window: int) -> dict[str, Any]:
        return await reconcile(settings, registry, engine, symbol="EURUSD", now_ms=clock.now_ms(), **window)

    report = await check(start_ms=start, end_ms=end)
    captured = report["seconds_captured_live"]
    assert captured == (end - start) // 1000 == report["seconds_identical"] == report["seconds_with_same_last_quote"]
    assert report["live_quotes"] == report["history_quotes_in_those_seconds"] == report["quotes_in_both"] > 20
    assert report["history_stamps_on_whole_seconds"] == 1.0 and report["live_stamps_on_whole_seconds"] < 0.5
    assert report["examples_of_differences"] == [] and report["recovered_ticks_in_window"] == 0
    text = format_reconciliation(report)
    assert "no differences" in text and "NOT Massive's production endpoints" in text and fake.api_key not in text

    # the provider's history loses a quote: the check names the second
    fake.tape.remove(next(q for q in fake.tape if start + 1000 <= q["t"] < start + 2000))
    report = await check(start_ms=start, end_ms=end)
    assert report["seconds_identical"] == captured - 1
    assert report["live_quotes"] == report["history_quotes_in_those_seconds"] + 1
    assert [e["second"] for e in report["examples_of_differences"]] == [iso(start + 1000)]

    # by default only complete, settled minutes are compared: a run of a few seconds has none
    assert (await check(minutes=1))["error"] == "no live ticks in this window"
    assert (
        "unknown instrument"
        in (await reconcile(settings, registry, engine, symbol="XXXYYY", now_ms=clock.now_ms()))["error"]
    )
    mock = Settings(_env_file=None, data_provider="mock", database_url=migrated_database)  # type: ignore[call-arg]
    assert (
        "needs the Massive provider"
        in (await reconcile(mock, registry, engine, symbol="EURUSD", now_ms=clock.now_ms()))["error"]
    )


async def test_slow_first_connection_is_recorded_and_backfilled_and_stale_jobs_are_closed(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    """If no quote arrives for a while after start-up, those seconds are an outage too."""
    clock = OffsetClock(T0)
    fake = server.fake
    async with engine.begin() as conn:  # a download that was running when the previous process died
        stale_job = await repo.create_job(conn, "aggregates", "EURUSD", "1m", T0 - 3_600_000, T0)
        await repo.update_job(conn, stale_job, status="running")
    market = Market(fake, clock)
    fake.silent = True  # connected and subscribed, but the socket delivers nothing yet
    market.start()
    service = IngestionService(massive_settings(server, migrated_database), registry, engine, clock=clock)
    await service.start()
    started = clock.now_ms()
    try:
        await asyncio.sleep(6.5)
        assert service.status()["instruments"][0]["signal_gate"]["reason"] == "NO DATA"
        fake.silent = False

        async def filled() -> bool:
            async with engine.connect() as conn:
                return bool(await repo.list_gaps(conn, status="filled"))

        await wait_for(filled, timeout=15, what="start-up outage backfill")
        await asyncio.sleep(1.0)
    finally:
        await market.stop()
        await service.stop()

    async with engine.connect() as conn:
        (gap,) = await repo.list_gaps(conn)
        job = await repo.get_job(conn, stale_job)
    assert gap["kind"] == "feed_outage" and gap["status"] == "filled" and gap["seconds"] >= 6
    assert abs(gap["start_ms"] - started) < 2000
    obs = await fetch_all(engine, "SELECT ts, status, source FROM obs_1s ORDER BY ts")
    assert not [o for o in obs if o.status == "missing"]  # the empty start-up seconds were replaced
    assert {o.source for o in obs[:5]} == {"backfill"}
    assert job is not None and job["status"] == "failed" and "restart" in job["error"]


async def test_a_second_pair_is_added_by_configuration_only(
    engine: AsyncEngine, migrated_database: str, server: FakeMassiveServer
) -> None:
    """Acceptance criterion 15: one line of YAML activates a pair; no code knows its name."""
    config = yaml.safe_load(INSTRUMENTS_YAML.read_text(encoding="utf-8"))
    next(i for i in config["instruments"] if i["symbol"] == "USDJPY")["active"] = True  # the only change
    registry = InstrumentRegistry.from_dict(config)
    clock = OffsetClock(T0)
    fake = server.fake
    service = IngestionService(massive_settings(server, migrated_database), registry, engine, clock=clock)
    await service.start()
    try:
        await wait_for(lambda: streaming(service) or fake.subscribe_messages, what="subscription")
        for _ in range(68):  # long enough for a 5-second bar of each pair to close
            now = clock.now_ms()
            await fake.publish(
                [
                    {"pair": "EUR/USD", "b": 1.16000, "a": 1.16008, "t": now},
                    {"pair": "USD/JPY", "b": 154.300, "a": 154.312, "t": now},
                ]
            )
            await asyncio.sleep(0.1)
        # 20 pips wide for a JPY pair (pip = 0.01): rejected by USD/JPY's own limit of 15 pips
        await fake.publish(pair="USD/JPY", b=154.300, a=154.500, t=clock.now_ms())
        await asyncio.sleep(1.4)
        status = service.status()
    finally:
        await service.stop()

    assert set(fake.subscribe_messages[0].split(",")) == {"C.EUR-USD", "C.USD-JPY"}
    by_symbol = {i["symbol"]: i for i in status["instruments"]}
    assert set(by_symbol) == {"EURUSD", "USDJPY"}
    assert by_symbol["EURUSD"]["spread_pips"] == 0.8 and by_symbol["USDJPY"]["spread_pips"] == 1.2
    assert by_symbol["USDJPY"]["counters"]["rejected:wide_spread"] == 1
    assert "rejected:wide_spread" not in by_symbol["EURUSD"]["counters"]
    assert all(i["signal_gate"]["allowed"] for i in by_symbol.values())

    grid = await fetch_all(
        engine,
        "SELECT i.symbol, count(*) AS n, min(o.bid) AS bid, max(o.ask) AS ask FROM obs_1s o"
        " JOIN instruments i ON i.id = o.instrument_id WHERE o.status = 'fresh' GROUP BY i.symbol",
    )
    stored = {r.symbol: r for r in grid}
    assert stored["EURUSD"].n >= 3 and (stored["EURUSD"].bid, stored["EURUSD"].ask) == (1.16, 1.16008)
    assert stored["USDJPY"].n >= 3 and (stored["USDJPY"].bid, stored["USDJPY"].ask) == (154.3, 154.312)
    bars = await fetch_all(engine, "SELECT DISTINCT i.symbol FROM bars b JOIN instruments i ON i.id = b.instrument_id")
    assert {r.symbol for r in bars} == {"EURUSD", "USDJPY"}


async def test_unsupported_instrument_is_marked_and_never_filled_with_data(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    server.fake.fx_tickers = {"C:GBPUSD"}  # the provider does not list EUR/USD
    service = IngestionService(massive_settings(server, migrated_database), registry, engine, clock=OffsetClock(T0))
    await service.start()
    try:
        await asyncio.sleep(1.6)
        status = service.status()
    finally:
        await service.stop()
    inst = status["instruments"][0]
    assert inst["provider_status"] == "unsupported" and inst["supported"] is False
    assert inst["signal_gate"] == {"allowed": False, "reason": "INSTRUMENT UNSUPPORTED"}
    assert server.fake.connection_count == 0  # nothing to subscribe to
    assert await fetch_all(engine, "SELECT 1 FROM obs_1s") == []
    stored = await fetch_all(engine, "SELECT provider_status FROM instruments WHERE symbol = 'EURUSD'")
    assert stored[0].provider_status == "unsupported"


async def test_closed_market_is_quiet_no_filler_rows_no_false_gaps(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    clock = OffsetClock(SATURDAY)
    fake = server.fake
    service = IngestionService(
        massive_settings(server, migrated_database, ws_silence_timeout_s=0.5), registry, engine, clock=clock
    )
    await service.start()
    try:
        await wait_for(lambda: streaming(service), what="streaming")
        await wait_for(lambda: bool(fake.clients), what="subscription")
        await fake.publish(b=1.16, a=1.16008, t=clock.now_ms())  # a stray weekend quote is real data: keep it
        await asyncio.sleep(3.0)
        status = service.status()
        queued = await service.scan_gaps()
    finally:
        await service.stop()
    obs = await fetch_all(engine, "SELECT status, n_quotes FROM obs_1s")
    assert [(o.status, o.n_quotes) for o in obs] == [("fresh", 1)]  # no carried/stale filler for the weekend
    assert queued == 0 and await fetch_all(engine, "SELECT 1 FROM data_gaps") == []
    assert fake.connection_count == 1  # the watchdog does not recycle a quiet weekend connection
    inst = status["instruments"][0]
    assert inst["market_open"] is False and inst["signal_gate"]["reason"] == "MARKET CLOSED"
    assert inst["next_open_ms"] == utc_ms(2026, 11, 15, 22)


async def test_history_job_downloads_provider_bars_with_provenance(
    engine: AsyncEngine, registry: InstrumentRegistry, migrated_database: str, server: FakeMassiveServer
) -> None:
    fake = server.fake
    base = utc_ms(2026, 11, 10, 9, 0, 0)
    await fake.publish(
        [{"b": 1.16 + (i % 7) * 0.0001, "a": 1.1601 + (i % 7) * 0.0001, "t": base + i * 5_000} for i in range(120)]
    )
    fake.silent = True  # this test is about REST only
    fake.aggs_page_size = 4  # force pagination
    service = IngestionService(massive_settings(server, migrated_database), registry, engine, clock=OffsetClock(T0))
    await service.start()
    try:
        history = service.history
        assert history is not None and history.available
        job_id = await history.create_job("EURUSD", "1m", base, base + 10 * 60_000)
        job = await history.run_job(job_id)
        assert (job["status"], job["rows_written"], job["pages"]) == ("done", 10, 3)

        with pytest.raises(ValueError, match="end must be after start"):
            await history.create_job("EURUSD", "1m", base, base)
        with pytest.raises(ValueError, match="unsupported timeframe"):
            await history.create_job("EURUSD", "2m", base, base + 1)
        with pytest.raises(ValueError, match="range too long"):
            await history.create_job("EURUSD", "1s", base, base + 30 * 86_400_000)

        fake.rest_fail_next = [404]
        failed = await history.run_job(await history.create_job("EURUSD", "5m", base, base + 3_600_000))
        assert failed["status"] == "failed" and "404" in failed["error"]
    finally:
        await service.stop()

    bars = await fetch_all(
        engine,
        "SELECT ts, open, high, low, close, n_quotes, source, price_basis, complete FROM bars WHERE timeframe = '1m' ORDER BY ts",
    )
    assert len(bars) == 10
    assert {(b.source, b.price_basis) for b in bars} == {("massive_agg", "bid")}  # provider bars are bid based
    assert [dt_to_ms(b.ts) for b in bars] == [base + i * 60_000 for i in range(10)]
    assert all(b.n_quotes == 12 and b.complete is None for b in bars)
    first_minute_bids = [1.16 + (i % 7) * 0.0001 for i in range(12)]
    assert bars[0].open == pytest.approx(first_minute_bids[0]) and bars[0].high == pytest.approx(max(first_minute_bids))
