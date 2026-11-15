"""Schema migrations, repositories, the batch writer and the data-mode guard on real PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.clock import ManualClock
from fxlab.config import Settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.models import Bar, DataSource, Observation, ObsStatus, PriceBasis
from fxlab.marketdata.base import ConnectionEvent
from fxlab.storage import repo
from fxlab.storage import schema as s
from fxlab.storage.db import apply_storage_policies, timescale_available
from fxlab.storage.guard import DataModeMismatchError, enforce_data_mode
from fxlab.storage.writer import BatchWriter
from tests.conftest import T0, make_tick, utc_ms

pytestmark = pytest.mark.db


def obs(
    ts: int,
    status: ObsStatus = ObsStatus.FRESH,
    bid: float | None = 1.16,
    source: DataSource = DataSource.LIVE,
) -> Observation:
    fresh = status is ObsStatus.FRESH
    ask = None if bid is None else bid + 0.00008
    mid = None if bid is None else bid + 0.00004
    return Observation(
        "EURUSD",
        ts,
        status,
        bid,
        ask,
        mid if fresh else None,
        mid if fresh else None,
        mid if fresh else None,
        2 if fresh else 0,
        ts + 500 if bid else None,
        500 if bid else None,
        source,
    )


async def ids(engine: AsyncEngine, registry: InstrumentRegistry) -> dict[str, int]:
    async with engine.begin() as conn:
        return await repo.sync_instruments(conn, registry)


async def test_migration_matches_declared_schema(engine: AsyncEngine) -> None:
    """The Alembic migration and storage/schema.py must describe the same database."""

    def diff(sync_conn: Any) -> list[Any]:
        return compare_metadata(MigrationContext.configure(sync_conn), s.metadata)

    async with engine.connect() as conn:
        differences = await conn.run_sync(diff)
        tables = {r[0] for r in await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
    assert differences == []
    assert {t.name for t in s.metadata.sorted_tables} <= tables


async def test_instruments_are_mirrored_from_the_registry(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    first = await ids(engine, registry)
    second = await ids(engine, registry)  # idempotent
    assert first == second and set(first) == {i.symbol for i in registry.all()}
    async with engine.begin() as conn:
        rows = {r["symbol"]: r for r in await repo.list_instruments(conn)}
        assert rows["EURUSD"]["active"] is True and rows["USDJPY"]["active"] is False
        assert rows["USDJPY"]["pip_size"] == 0.01
        assert rows["EURUSD"]["provider_status"] == "unverified"  # availability is never assumed
        assert rows["USDTRY"]["config"]["data_quality"]["stale_after_ms"] == 60000
        await repo.set_provider_status(conn, "USDTRY", "unsupported", {"reason": "not listed"})
        rows = {r["symbol"]: r for r in await repo.list_instruments(conn)}
        assert rows["USDTRY"]["provider_status"] == "unsupported" and rows["USDTRY"]["provider_checked_at"] is not None


async def test_observation_roundtrip_and_conflict_rules(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    iid = (await ids(engine, registry))["EURUSD"]
    live = [
        obs(T0),
        obs(T0 + 1000, ObsStatus.CARRIED),
        obs(T0 + 2000, ObsStatus.STALE),
        obs(T0 + 3000, ObsStatus.MISSING, bid=None),
    ]
    async with engine.begin() as conn:
        await repo.upsert_observations(conn, [repo.obs_row(iid, o) for o in live])
        assert await repo.fetch_observations(conn, "EURUSD", iid, T0, T0 + 10_000) == live

        # live path: an existing row always wins
        await repo.upsert_observations(conn, [repo.obs_row(iid, obs(T0, bid=9.99))], replace="never")
        assert (await repo.fetch_observations(conn, "EURUSD", iid, T0, T0 + 1000))[0].bid == 1.16

        # backfill path: replaces carried/stale/missing rows, never a captured live update
        rebuilt = [obs(T0 + i * 1000, ObsStatus.FRESH, 1.17, DataSource.BACKFILL) for i in range(5)]
        await repo.upsert_observations(conn, [repo.obs_row(iid, o) for o in rebuilt], replace="unless_live_fresh")
        after = await repo.fetch_observations(conn, "EURUSD", iid, T0, T0 + 10_000)
        assert [(o.source, o.bid) for o in after] == [
            (DataSource.LIVE, 1.16),  # the real capture is untouched
            (DataSource.BACKFILL, 1.17),
            (DataSource.BACKFILL, 1.17),
            (DataSource.BACKFILL, 1.17),
            (DataSource.BACKFILL, 1.17),  # the hole is filled
        ]
        last = await repo.last_observation(conn, "EURUSD", iid)
        assert last is not None and last.ts_ms == T0 + 4000
        assert (await repo.last_observation(conn, "EURUSD", iid, before_ms=T0 + 1000)).ts_ms == T0  # type: ignore[union-attr]
        with pytest.raises(Exception, match="status_valid"):
            await conn.execute(
                s.obs_1s.insert().values(instrument_id=iid, ts=repo.ms_to_dt(T0 + 99_000), status="bogus", n_quotes=0)
            )


async def test_ticks_and_bars_roundtrip(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    iid = (await ids(engine, registry))["EURUSD"]
    live_bar = Bar(
        "EURUSD",
        "1m",
        T0,
        1.16,
        1.1605,
        1.1598,
        1.1602,
        DataSource.LIVE,
        PriceBasis.MID,
        1.16016,
        1.16024,
        120,
        55,
        60,
        60,
        0,
        True,
    )
    provider_bar = Bar(
        "EURUSD",
        "1m",
        T0,
        1.1599,
        1.1604,
        1.1597,
        1.1601,
        DataSource.MASSIVE_AGG,
        PriceBasis.BID,
        n_quotes=118,
    )
    async with engine.begin() as conn:
        await repo.insert_ticks(
            conn,
            [
                repo.tick_row(iid, make_tick(T0 + 10, recv_ts_ms=T0 + 45)),
                repo.tick_row(iid, make_tick(T0 + 20), s.TICK_FLAG_BACKFILL),
            ],
        )
        ticks = (await conn.execute(select(s.quote_ticks).order_by(s.quote_ticks.c.source_ts))).all()
        assert repo.dt_to_ms(ticks[0].recv_ts) == T0 + 45 and ticks[0].flags == 0
        assert ticks[1].recv_ts is None and ticks[1].flags == s.TICK_FLAG_BACKFILL  # no receipt time is invented

        await repo.upsert_bars(conn, [repo.bar_row(iid, live_bar), repo.bar_row(iid, provider_bar)])
        # same key, different source: our mid bar and the provider's bid bar live side by side
        assert await repo.fetch_bars(conn, "EURUSD", iid, "1m", "live", T0, T0 + 60_000) == [live_bar]
        assert await repo.fetch_bars(conn, "EURUSD", iid, "1m", "massive_agg", T0, T0 + 60_000) == [provider_bar]
        # a re-emitted bar replaces the stored one
        revised = Bar(
            "EURUSD",
            "1m",
            T0,
            1.16,
            1.1609,
            1.1598,
            1.1602,
            DataSource.LIVE,
            PriceBasis.MID,
            1.16016,
            1.16024,
            130,
            56,
            60,
            60,
            4,
            True,
        )
        await repo.upsert_bars(conn, [repo.bar_row(iid, revised)])
        assert await repo.fetch_bars(conn, "EURUSD", iid, "1m", "live", T0, T0 + 60_000) == [revised]


async def test_find_holes(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    iid = (await ids(engine, registry))["EURUSD"]
    async with engine.begin() as conn:
        assert await repo.find_holes(conn, iid, T0, T0 + 60_000) == [(T0, T0 + 60_000)]  # nothing stored at all
        present = [0, 1, 2, 5, 6, 10, 11, 12, 13]
        await repo.upsert_observations(conn, [repo.obs_row(iid, obs(T0 + sec * 1000)) for sec in present])
        assert await repo.find_holes(conn, iid, T0, T0 + 20_000) == [
            (T0 + 3000, T0 + 5000),
            (T0 + 7000, T0 + 10_000),
            (T0 + 14_000, T0 + 20_000),
        ]
        assert await repo.find_holes(conn, iid, T0 - 4000, T0 + 3000) == [(T0 - 4000, T0)]
        assert await repo.find_holes(conn, iid, T0, T0 + 3000) == []
        assert await repo.find_holes(conn, iid, T0 + 5000, T0 + 5000) == []


async def test_gap_bookkeeping(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    iid = (await ids(engine, registry))["EURUSD"]
    async with engine.begin() as conn:
        gid = await repo.record_gap(conn, iid, T0, T0 + 30_000, "hole", {"cause": "restart"})
        assert gid is not None
        assert await repo.record_gap(conn, iid, T0, T0 + 30_000, "hole") is None  # already known
        assert await repo.record_gap(conn, iid, T0, T0 + 20_000, "hole") is None  # a part of a known gap
        other = await repo.record_gap(conn, iid, T0, T0 + 30_000, "feed_outage")  # different kind is distinct
        assert other is not None and other != gid
        open_gaps = await repo.list_gaps(conn, status="open")
        assert {g["id"] for g in open_gaps} == {gid, other}
        assert open_gaps[0]["symbol"] == "EURUSD" and open_gaps[0]["seconds"] == 30
        await repo.resolve_gap(conn, gid, "filled", {"quotes": 42})
        assert await repo.gap_counts(conn, T0 - 1000) == {"filled": 1, "open": 1}
        filled = (await repo.list_gaps(conn, status="filled"))[0]
        assert filled["resolved_ms"] is not None and filled["detail"] == {"quotes": 42}

        # the same downtime seen again, longer (restarted before any new data arrived):
        # the record is extended and reopened instead of the extra time being forgotten
        assert await repo.record_gap(conn, iid, T0, T0 + 90_000, "hole") == gid
        grown = await repo.get_gap(conn, gid)
        assert grown is not None and (grown["status"], grown["seconds"], grown["resolved_ms"]) == ("open", 90, None)
        assert grown["detail"] is None and len(await repo.list_gaps(conn)) == 2


async def test_unfilled_tail_start(engine: AsyncEngine, registry: InstrumentRegistry) -> None:
    """Where recovery has to begin after a restart: after the last second with a real update."""
    iid = (await ids(engine, registry))["EURUSD"]
    async with engine.begin() as conn:
        assert await repo.unfilled_tail_start(conn, iid, T0 - 60_000) is None  # nothing stored
        rows = [obs(T0), obs(T0 + 1000), obs(T0 + 2000, ObsStatus.CARRIED)]
        await repo.upsert_observations(conn, [repo.obs_row(iid, o) for o in rows])
        assert await repo.unfilled_tail_start(conn, iid, T0 - 60_000) == T0 + 2000
        # the feed died: carried, then stale seconds were written until the process stopped
        tail = [obs(T0 + 3000, ObsStatus.STALE), obs(T0 + 4000, ObsStatus.MISSING, bid=None)]
        await repo.upsert_observations(conn, [repo.obs_row(iid, o) for o in tail])
        assert await repo.unfilled_tail_start(conn, iid, T0 - 60_000) == T0 + 2000
        assert await repo.unfilled_tail_start(conn, iid, T0 + 3500) == T0 + 4000  # no fresh row in range
        assert await repo.unfilled_tail_start(conn, iid, T0 + 60_000) is None  # range is empty
        # a recovered (backfilled) second is a real update too
        await repo.upsert_observations(
            conn, [repo.obs_row(iid, obs(T0 + 4000, source=DataSource.BACKFILL))], replace="unless_live_fresh"
        )
        assert await repo.unfilled_tail_start(conn, iid, T0 - 60_000) is None


async def test_history_jobs(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        jid = await repo.create_job(conn, "aggregates", "EURUSD", "1m", T0, T0 + 3_600_000)
        job = await repo.get_job(conn, jid)
        assert job is not None and job["status"] == "queued" and job["rows_written"] == 0
        await repo.update_job(conn, jid, status="done", rows_written=60, pages=1)
        assert (await repo.list_jobs(conn))[0]["rows_written"] == 60
        assert await repo.get_job(conn, 999_999) is None


async def test_writer_persists_batches_and_survives_database_failures(
    engine: AsyncEngine, registry: InstrumentRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    iid = (await ids(engine, registry))["EURUSD"]
    writer = BatchWriter(engine, ManualClock(T0), flush_interval_s=0.05, max_buffer_rows=1000)
    for i in range(2500):  # more than one INSERT chunk
        writer.add_tick(repo.tick_row(iid, make_tick(T0 + i)))
    writer.add_observation(repo.obs_row(iid, obs(T0)))
    bar = Bar(
        "EURUSD",
        "5s",
        T0,
        1.16,
        1.16,
        1.16,
        1.16,
        DataSource.LIVE,
        PriceBasis.MID,
        n_valid=5,
        n_expected=5,
        complete=True,
    )
    writer.add_bar(repo.bar_row(iid, bar))
    writer.add_bar(repo.bar_row(iid, bar))  # same bar twice is written once
    writer.add_event(repo.event_row(ConnectionEvent(T0, "massive", "connected", {"x": 1})))

    # buffer limit is 1000 rows: the oldest ticks are dropped and counted, observations are kept
    assert await writer.flush() is True
    assert writer.dropped == {"ticks": 1501, "obs": 0}
    assert writer.written == {"ticks": 999, "obs": 1, "bars": 1, "events": 1, "stats": 0}
    assert writer.db_ok is True and writer.buffered["ticks"] == 0

    # database failure: nothing is lost, rows stay buffered and are retried
    real_insert = repo.insert_ticks
    calls = {"n": 0}

    async def failing_insert(conn: Any, rows: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("database is down")
        await real_insert(conn, rows)

    monkeypatch.setattr(repo, "insert_ticks", failing_insert)
    writer.add_tick(repo.tick_row(iid, make_tick(T0 + 5000)))
    writer.add_observation(repo.obs_row(iid, obs(T0 + 1000)))
    assert await writer.flush() is False
    assert writer.db_ok is False and "database is down" in (writer.last_error or "")
    assert writer.buffered["ticks"] == 1 and writer.buffered["obs"] == 1
    writer.add_tick(repo.tick_row(iid, make_tick(T0 + 5001)))  # ingestion continues during the outage
    assert await writer.flush() is True
    assert writer.db_ok is True and writer.consecutive_failures == 0

    async with engine.connect() as conn:
        counts = {
            t: (await conn.execute(text(f"SELECT count(*) FROM {t}"))).scalar()
            for t in ("quote_ticks", "obs_1s", "bars", "connection_events")
        }
    # the failed transaction was rolled back completely, so no row is duplicated
    assert counts == {"quote_ticks": 1001, "obs_1s": 2, "bars": 1, "connection_events": 1}


async def test_data_mode_guard_refuses_to_mix_sources(engine: AsyncEngine) -> None:
    real = Settings(_env_file=None, data_provider="massive", massive_api_key="k" * 12)  # type: ignore[call-arg]
    mock = Settings(_env_file=None, data_provider="mock")  # type: ignore[call-arg]
    test_endpoint = Settings(
        _env_file=None,
        data_provider="massive",
        massive_api_key="k" * 12,
        massive_ws_url="ws://127.0.0.1:9911/forex",
    )  # type: ignore[call-arg]

    mode = await enforce_data_mode(engine, real)
    assert mode == {"provider": "massive", "synthetic": False, "feed_host": "socket.massive.com"}
    assert await enforce_data_mode(engine, real) == mode  # same mode: fine
    with pytest.raises(DataModeMismatchError, match="Refusing to mix"):
        await enforce_data_mode(engine, mock)  # synthetic data must not enter a real database
    with pytest.raises(DataModeMismatchError):
        await enforce_data_mode(engine, test_endpoint)  # nor data from a non-production endpoint


async def test_storage_policies_degrade_gracefully_without_timescaledb(engine: AsyncEngine) -> None:
    summary = await apply_storage_policies(engine, compress_after_days=7, retention_ticks_days=30, retention_obs_days=0)
    if await timescale_available(engine):
        assert summary["timescaledb"] is True and summary["errors"] == []
    else:
        assert summary == {"timescaledb": False, "applied": [], "errors": []}


# ---------------------------------------------------------------------- TimescaleDB only ----
# These run in CI (TimescaleDB image) and are skipped on plain PostgreSQL.

OLD = utc_ms(2026, 10, 1, 12, 0, 0)  # old enough to be "compress" territory on any run date


async def _policy_jobs(engine: AsyncEngine) -> dict[tuple[str, str], dict[str, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT proc_name, hypertable_name, config FROM timescaledb_information.jobs"
                " WHERE hypertable_name IN ('quote_ticks', 'obs_1s', 'bars')"
            )
        )
        return {(r.proc_name, r.hypertable_name): dict(r.config) for r in rows}


async def _compress_all_chunks(engine: AsyncEngine) -> None:
    """Compress every chunk now instead of waiting for the policy's background job.

    That job starts as soon as the policy exists and may be compressing the very same chunk;
    losing that race raises "already compressed", which is the outcome wanted here anyway.
    """
    for table in ("quote_ticks", "obs_1s", "bars"):
        async with engine.connect() as conn:
            chunks = [r[0] for r in await conn.execute(text(f"SELECT c::text FROM show_chunks('{table}') c"))]
        for chunk in chunks:
            try:
                async with engine.begin() as conn:
                    await conn.execute(
                        text("SELECT compress_chunk(CAST(:chunk AS regclass), if_not_compressed => true)"),
                        {"chunk": chunk},
                    )
            except DBAPIError as exc:
                if "already compressed" not in str(exc):
                    raise


async def test_timescaledb_hypertables_and_policies_follow_the_settings(engine: AsyncEngine) -> None:
    if not await timescale_available(engine):
        pytest.skip("TimescaleDB is not installed in the test database")
    async with engine.connect() as conn:
        dims = await conn.execute(
            text("SELECT hypertable_name, column_name, time_interval FROM timescaledb_information.dimensions")
        )
        chunking = {r.hypertable_name: (r.column_name, r.time_interval.days) for r in dims}
    assert chunking == {"quote_ticks": ("source_ts", 1), "obs_1s": ("ts", 1), "bars": ("ts", 30)}

    first = await apply_storage_policies(engine, compress_after_days=7, retention_ticks_days=30, retention_obs_days=0)
    assert first["errors"] == [] and first["hypertables"] == ["bars", "obs_1s", "quote_ticks"]
    jobs = await _policy_jobs(engine)
    assert jobs[("policy_compression", "obs_1s")]["compress_after"] == "7 days"
    assert jobs[("policy_compression", "quote_ticks")]["compress_after"] == "7 days"
    assert jobs[("policy_compression", "bars")]["compress_after"] == "30 days"  # bars: never sooner than 30 days
    assert jobs[("policy_retention", "quote_ticks")]["drop_after"] == "30 days"
    assert ("policy_retention", "obs_1s") not in jobs and ("policy_retention", "bars") not in jobs

    # changed settings take effect at the next start instead of being ignored
    second = await apply_storage_policies(
        engine, compress_after_days=14, retention_ticks_days=0, retention_obs_days=400
    )
    assert second["errors"] == []
    jobs = await _policy_jobs(engine)
    assert jobs[("policy_compression", "obs_1s")]["compress_after"] == "14 days"
    assert jobs[("policy_compression", "bars")]["compress_after"] == "30 days"
    assert ("policy_retention", "quote_ticks") not in jobs  # 0 = keep forever
    assert jobs[("policy_retention", "obs_1s")]["drop_after"] == "400 days"
    assert len(jobs) == 4  # no duplicates left behind


async def test_timescaledb_compressed_chunks_take_backfill_and_survive_a_restart(
    engine: AsyncEngine, registry: InstrumentRegistry
) -> None:
    if not await timescale_available(engine):
        pytest.skip("TimescaleDB is not installed in the test database")
    iid = (await ids(engine, registry))["EURUSD"]
    policies = dict(compress_after_days=7, retention_ticks_days=0, retention_obs_days=0)  # nothing is dropped here
    assert (await apply_storage_policies(engine, **policies))["errors"] == []

    # ten minutes of live capture with a one-minute outage in it, plus ticks and provider bars
    outage = range(120, 180)
    live = [obs(OLD + i * 1000, ObsStatus.MISSING, None) if i in outage else obs(OLD + i * 1000) for i in range(600)]
    provider_bar = Bar("EURUSD", "1m", OLD, 1.1599, 1.1604, 1.1597, 1.1601, DataSource.MASSIVE_AGG, PriceBasis.BID)
    async with engine.begin() as conn:
        await repo.upsert_observations(conn, [repo.obs_row(iid, o) for o in live])
        await repo.insert_ticks(
            conn, [repo.tick_row(iid, make_tick(OLD + i * 500, recv_ts_ms=OLD + i * 500 + 30)) for i in range(240)]
        )
        await repo.upsert_bars(conn, [repo.bar_row(iid, provider_bar)])
    await _compress_all_chunks(engine)
    async with engine.connect() as conn:
        chunks = await conn.execute(text("SELECT hypertable_name, is_compressed FROM timescaledb_information.chunks"))
        assert sorted(tuple(r) for r in chunks) == [("bars", True), ("obs_1s", True), ("quote_ticks", True)]

    # a restart re-applies the policies: no complaints about chunks that are already compressed
    assert (await apply_storage_policies(engine, **policies))["errors"] == []

    rebuilt = [obs(OLD + i * 1000, ObsStatus.FRESH, 1.17, DataSource.BACKFILL) for i in range(110, 190)]
    revised = Bar(
        "EURUSD", "1m", OLD, 1.1599, 1.1609, 1.1597, 1.1603, DataSource.MASSIVE_AGG, PriceBasis.BID, n_quotes=7
    )
    async with engine.begin() as conn:
        # gap recovery and a repeated history download both write into compressed chunks
        await repo.upsert_observations(conn, [repo.obs_row(iid, o) for o in rebuilt], replace="unless_live_fresh")
        await repo.insert_ticks(conn, [repo.tick_row(iid, make_tick(OLD + 150_000), s.TICK_FLAG_BACKFILL)])
        await repo.upsert_bars(conn, [repo.bar_row(iid, revised)])
    async with engine.connect() as conn:
        after = await repo.fetch_observations(conn, "EURUSD", iid, OLD, OLD + 600_000, limit=1000)
        assert len(after) == 600 and not [o for o in after if o.status is ObsStatus.MISSING]
        assert [o.ts_ms for o in after if o.source is DataSource.BACKFILL] == [OLD + i * 1000 for i in outage]
        assert await repo.find_holes(conn, iid, OLD - 5000, OLD + 605_000) == [
            (OLD - 5000, OLD),
            (OLD + 600_000, OLD + 605_000),
        ]
        assert await repo.fetch_bars(conn, "EURUSD", iid, "1m", "massive_agg", OLD, OLD + 60_000) == [revised]
        assert await conn.scalar(text("SELECT count(*) FROM quote_ticks")) == 241
