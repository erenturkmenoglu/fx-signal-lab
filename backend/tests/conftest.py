from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.models import QuoteTick, RawQuote
from fxlab.timeutil import dt_to_ms

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTRUMENTS_YAML = REPO_ROOT / "config" / "instruments.yaml"


def utc_ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0, ms: int = 0) -> int:
    return dt_to_ms(datetime(year, month, day, hour, minute, second, ms * 1000, tzinfo=UTC))


#: a Thursday, London/New York overlap, market open (2026-11-12 14:00:00 UTC)
T0 = utc_ms(2026, 11, 12, 14, 0, 0)


@pytest.fixture(scope="session")
def registry() -> InstrumentRegistry:
    """The registry exactly as shipped in config/instruments.yaml."""
    return InstrumentRegistry.from_yaml(INSTRUMENTS_YAML)


def make_tick(
    ts_ms: int,
    bid: float = 1.16000,
    ask: float = 1.16008,
    *,
    symbol: str = "EURUSD",
    recv_ts_ms: int | None = None,
    exchange: int | None = 48,
) -> QuoteTick:
    return QuoteTick(
        symbol=symbol,
        bid=bid,
        ask=ask,
        source_ts_ms=ts_ms,
        recv_ts_ms=ts_ms if recv_ts_ms is None else recv_ts_ms,
        exchange=exchange,
    )


def make_raw(
    ts_ms: int | None,
    bid: float | None = 1.16000,
    ask: float | None = 1.16008,
    *,
    symbol: str = "EURUSD",
    recv_ts_ms: int | None = None,
    exchange: int | None = 48,
) -> RawQuote:
    recv = recv_ts_ms if recv_ts_ms is not None else (ts_ms if ts_ms is not None else T0)
    return RawQuote(symbol=symbol, bid=bid, ask=ask, source_ts_ms=ts_ms, recv_ts_ms=recv, exchange=exchange)


# --- database fixtures ----------------------------------------------------------------------
# Integration tests need a real PostgreSQL (TimescaleDB optional).  Point TEST_DATABASE_URL at
# a disposable database, e.g.
#   TEST_DATABASE_URL=postgresql+asyncpg://fxlab:fxlab@127.0.0.1:5432/fxlab_test
# The application's tables in that database are DROPPED and recreated by the test session.

import asyncio  # noqa: E402
import os  # noqa: E402
import threading  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402

from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from fxlab.storage.db import upgrade_database_sync  # noqa: E402

DATA_TABLES = (
    "quote_ticks",
    "obs_1s",
    "bars",
    "connection_events",
    "data_gaps",
    "history_jobs",
    "ingest_stats_1m",
    "app_meta",
    "instruments",
)


def _run_in_thread(fn: object, *args: object) -> None:
    """Run blocking/own-event-loop code without touching the test event loop."""
    error: list[BaseException] = []

    def target() -> None:
        try:
            fn(*args)  # type: ignore[operator]
        except BaseException as exc:
            error.append(exc)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if error:
        raise error[0]


def _reset_schema(url: str) -> None:
    """Drop this application's tables (and Alembic's bookkeeping) so migrations start from zero.

    Only our own tables are dropped - never the schema - so a TimescaleDB extension installed
    in the test database is left alone.
    """

    async def go() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            for table in (*DATA_TABLES, "alembic_version"):
                await conn.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        await engine.dispose()

    asyncio.run(go())


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not set - database integration tests skipped")
    return url


@pytest.fixture(scope="session")
def migrated_database(database_url: str) -> str:
    """A database freshly built by the real Alembic migrations."""
    _run_in_thread(_reset_schema, database_url)
    _run_in_thread(upgrade_database_sync, database_url)
    return database_url


@pytest.fixture()
async def engine(migrated_database: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_database, poolclass=NullPool)
    async with eng.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(DATA_TABLES)} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()
