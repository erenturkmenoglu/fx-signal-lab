"""Data access functions.

Plain async functions over an ``AsyncConnection``; no ORM and no hidden state.
Conversion between domain records (epoch-ms integers) and rows (aware datetimes)
happens here and nowhere else.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import and_, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.models import Bar, DataSource, Observation, ObsStatus, PriceBasis, QuoteTick
from fxlab.marketdata.base import ConnectionEvent
from fxlab.storage import schema as s
from fxlab.timeutil import SECOND_MS, dt_to_ms, ms_to_dt

#: rows per INSERT statement (asyncpg allows at most 32767 bind parameters per statement)
CHUNK_ROWS = 1000


def _chunks(rows: Sequence[dict[str, Any]], size: int = CHUNK_ROWS) -> Iterable[Sequence[dict[str, Any]]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


# --- row builders -------------------------------------------------------------------------


def tick_row(instrument_id: int, tick: QuoteTick, flags: int = 0) -> dict[str, Any]:
    backfilled = bool(flags & s.TICK_FLAG_BACKFILL)
    return {
        "instrument_id": instrument_id,
        "source_ts": ms_to_dt(tick.source_ts_ms),
        "recv_ts": None if backfilled else ms_to_dt(tick.recv_ts_ms),
        "bid": tick.bid,
        "ask": tick.ask,
        "exchange": tick.exchange,
        "flags": flags,
    }


def obs_row(instrument_id: int, obs: Observation) -> dict[str, Any]:
    return {
        "instrument_id": instrument_id,
        "ts": ms_to_dt(obs.ts_ms),
        "status": obs.status.value,
        "bid": obs.bid,
        "ask": obs.ask,
        "mid_open": obs.mid_open,
        "mid_high": obs.mid_high,
        "mid_low": obs.mid_low,
        "n_quotes": obs.n_quotes,
        "quote_ts": ms_to_dt(obs.quote_ts_ms) if obs.quote_ts_ms is not None else None,
        "age_ms": obs.age_ms,
        "source": obs.source.value,
    }


def bar_row(instrument_id: int, bar: Bar) -> dict[str, Any]:
    return {
        "instrument_id": instrument_id,
        "timeframe": bar.timeframe,
        "source": bar.source.value,
        "ts": ms_to_dt(bar.ts_ms),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "bid_close": bar.bid_close,
        "ask_close": bar.ask_close,
        "n_quotes": bar.n_quotes,
        "n_fresh": bar.n_fresh,
        "n_valid": bar.n_valid,
        "n_expected": bar.n_expected,
        "n_backfilled": bar.n_backfilled,
        "complete": bar.complete,
        "price_basis": bar.price_basis.value,
    }


def event_row(event: ConnectionEvent) -> dict[str, Any]:
    return {
        "ts": ms_to_dt(event.ts_ms),
        "provider": event.provider,
        "kind": event.kind,
        "detail": event.detail,
    }


def row_to_obs(symbol: str, row: Any) -> Observation:
    return Observation(
        symbol=symbol,
        ts_ms=dt_to_ms(row.ts),
        status=ObsStatus(row.status),
        bid=row.bid,
        ask=row.ask,
        mid_open=row.mid_open,
        mid_high=row.mid_high,
        mid_low=row.mid_low,
        n_quotes=row.n_quotes,
        quote_ts_ms=dt_to_ms(row.quote_ts) if row.quote_ts is not None else None,
        age_ms=row.age_ms,
        source=DataSource(row.source),
    )


def row_to_bar(symbol: str, row: Any) -> Bar:
    return Bar(
        symbol=symbol,
        timeframe=row.timeframe,
        ts_ms=dt_to_ms(row.ts),
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        source=DataSource(row.source),
        price_basis=PriceBasis(row.price_basis),
        bid_close=row.bid_close,
        ask_close=row.ask_close,
        n_quotes=row.n_quotes,
        n_fresh=row.n_fresh,
        n_valid=row.n_valid,
        n_expected=row.n_expected,
        n_backfilled=row.n_backfilled,
        complete=row.complete,
    )


# --- instruments --------------------------------------------------------------------------


async def sync_instruments(conn: AsyncConnection, registry: InstrumentRegistry) -> dict[str, int]:
    """Mirror the YAML registry into the ``instruments`` table; returns ``symbol -> id``."""
    for inst in registry.all():
        values = {
            "symbol": inst.symbol,
            "base": inst.base,
            "quote": inst.quote,
            "pip_size": inst.pip_size,
            "price_precision": inst.price_precision,
            "active": inst.active,
            "config": inst.model_dump(mode="json"),
        }
        stmt = pg_insert(s.instruments).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol"],
            set_={k: stmt.excluded[k] for k in values if k != "symbol"} | {"updated_at": func.now()},
        )
        await conn.execute(stmt)
    rows = await conn.execute(select(s.instruments.c.symbol, s.instruments.c.id))
    return {r.symbol: r.id for r in rows}


async def set_provider_status(
    conn: AsyncConnection, symbol: str, status: str, detail: dict[str, Any] | None = None
) -> None:
    await conn.execute(
        update(s.instruments)
        .where(s.instruments.c.symbol == symbol)
        .values(provider_status=status, provider_checked_at=func.now(), provider_detail=detail)
    )


async def list_instruments(conn: AsyncConnection) -> list[dict[str, Any]]:
    rows = await conn.execute(select(s.instruments).order_by(s.instruments.c.id))
    return [dict(r._mapping) for r in rows]


# --- writes -------------------------------------------------------------------------------


async def insert_ticks(conn: AsyncConnection, rows: Sequence[dict[str, Any]]) -> None:
    for chunk in _chunks(rows):
        await conn.execute(s.quote_ticks.insert(), list(chunk))


async def upsert_observations(
    conn: AsyncConnection,
    rows: Sequence[dict[str, Any]],
    *,
    replace: Literal["never", "unless_live_fresh"] = "never",
) -> None:
    """Insert observations.

    ``never``              - live path: an existing row always wins.
    ``unless_live_fresh``  - backfill path: replace what is there *unless* it is a
                             genuinely captured live update, which is never overwritten.
    """
    cols = [c.name for c in s.obs_1s.columns if c.name not in ("instrument_id", "ts")]
    for chunk in _chunks(rows):
        stmt = pg_insert(s.obs_1s).values(list(chunk))
        if replace == "never":
            stmt = stmt.on_conflict_do_nothing(index_elements=["instrument_id", "ts"])
        else:
            stmt = stmt.on_conflict_do_update(
                index_elements=["instrument_id", "ts"],
                set_={c: stmt.excluded[c] for c in cols},
                where=~and_(s.obs_1s.c.source == DataSource.LIVE.value, s.obs_1s.c.status == ObsStatus.FRESH.value),
            )
        await conn.execute(stmt)


async def upsert_bars(conn: AsyncConnection, rows: Sequence[dict[str, Any]]) -> None:
    cols = [c.name for c in s.bars.columns if c.name not in ("instrument_id", "timeframe", "source", "ts")]
    for chunk in _chunks(rows, 800):
        stmt = pg_insert(s.bars).values(list(chunk))
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "timeframe", "source", "ts"],
            set_={c: stmt.excluded[c] for c in cols},
        )
        await conn.execute(stmt)


async def insert_events(conn: AsyncConnection, rows: Sequence[dict[str, Any]]) -> None:
    if rows:
        await conn.execute(s.connection_events.insert(), list(rows))


async def upsert_ingest_stats(conn: AsyncConnection, rows: Sequence[dict[str, Any]]) -> None:
    for row in rows:
        stmt = pg_insert(s.ingest_stats_1m).values(**row)
        stmt = stmt.on_conflict_do_update(
            index_elements=["instrument_id", "ts"],
            set_={k: stmt.excluded[k] for k in row if k not in ("instrument_id", "ts")},
        )
        await conn.execute(stmt)


# --- reads --------------------------------------------------------------------------------


async def fetch_observations(
    conn: AsyncConnection,
    symbol: str,
    instrument_id: int,
    start_ms: int,
    end_ms: int,
    *,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[Observation]:
    """Observations with ``start_ms <= ts < end_ms`` (ascending unless ``newest_first``)."""
    t = s.obs_1s
    stmt = select(t).where(t.c.instrument_id == instrument_id, t.c.ts >= ms_to_dt(start_ms), t.c.ts < ms_to_dt(end_ms))
    stmt = stmt.order_by(t.c.ts.desc() if newest_first else t.c.ts)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [row_to_obs(symbol, r) for r in await conn.execute(stmt)]


async def last_observation(
    conn: AsyncConnection,
    symbol: str,
    instrument_id: int,
    *,
    before_ms: int | None = None,
    with_price: bool = False,
) -> Observation | None:
    t = s.obs_1s
    stmt = select(t).where(t.c.instrument_id == instrument_id)
    if before_ms is not None:
        stmt = stmt.where(t.c.ts < ms_to_dt(before_ms))
    if with_price:
        stmt = stmt.where(t.c.bid.is_not(None))
    row = (await conn.execute(stmt.order_by(t.c.ts.desc()).limit(1))).first()
    return row_to_obs(symbol, row) if row is not None else None


async def unfilled_tail_start(conn: AsyncConnection, instrument_id: int, since_ms: int) -> int | None:
    """Start of the trailing run of stored seconds that hold no real update.

    Looks at rows from ``since_ms`` on and returns the timestamp of the first row after the
    newest ``fresh`` one (the first row of that range when it holds no fresh row at all).
    ``None`` when the newest row is fresh or the range is empty.
    """
    t = s.obs_1s
    in_range = and_(t.c.instrument_id == instrument_id, t.c.ts >= ms_to_dt(since_ms))
    last_fresh = (
        await conn.execute(select(func.max(t.c.ts)).where(in_range, t.c.status == ObsStatus.FRESH.value))
    ).scalar()
    stmt = select(func.min(t.c.ts)).where(in_range)
    if last_fresh is not None:
        stmt = stmt.where(t.c.ts > last_fresh)
    first = (await conn.execute(stmt)).scalar()
    return dt_to_ms(first) if first is not None else None


async def fetch_bars(
    conn: AsyncConnection,
    symbol: str,
    instrument_id: int,
    timeframe: str,
    source: str,
    start_ms: int,
    end_ms: int,
    *,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[Bar]:
    t = s.bars
    stmt = select(t).where(
        t.c.instrument_id == instrument_id,
        t.c.timeframe == timeframe,
        t.c.source == source,
        t.c.ts >= ms_to_dt(start_ms),
        t.c.ts < ms_to_dt(end_ms),
    )
    stmt = stmt.order_by(t.c.ts.desc() if newest_first else t.c.ts)
    if limit is not None:
        stmt = stmt.limit(limit)
    return [row_to_bar(symbol, r) for r in await conn.execute(stmt)]


async def find_holes(conn: AsyncConnection, instrument_id: int, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
    """Maximal ranges ``[a, b)`` inside ``[start_ms, end_ms)`` that have **no** ``obs_1s`` row.

    Both bounds must be whole seconds.  Market hours are not considered here; the caller
    intersects the result with the instrument's calendar.
    """
    if start_ms >= end_ms:
        return []
    params = {"i": instrument_id, "a": ms_to_dt(start_ms), "b": ms_to_dt(end_ms)}
    bounds = (
        await conn.execute(
            text("SELECT min(ts) AS lo, max(ts) AS hi FROM obs_1s WHERE instrument_id = :i AND ts >= :a AND ts < :b"),
            params,
        )
    ).one()
    if bounds.lo is None:
        return [(start_ms, end_ms)]
    holes: list[tuple[int, int]] = []
    first, last = dt_to_ms(bounds.lo), dt_to_ms(bounds.hi)
    if first > start_ms:
        holes.append((start_ms, first))
    inner = await conn.execute(
        text(
            """
            SELECT ts, next_ts FROM (
                SELECT ts, lead(ts) OVER (ORDER BY ts) AS next_ts
                FROM obs_1s WHERE instrument_id = :i AND ts >= :a AND ts < :b
            ) q
            WHERE next_ts - ts > interval '1 second'
            ORDER BY ts
            """
        ),
        params,
    )
    holes.extend((dt_to_ms(r.ts) + SECOND_MS, dt_to_ms(r.next_ts)) for r in inner)
    if last + SECOND_MS < end_ms:
        holes.append((last + SECOND_MS, end_ms))
    return holes


async def recent_events(conn: AsyncConnection, limit: int = 100) -> list[dict[str, Any]]:
    t = s.connection_events
    rows = await conn.execute(select(t).order_by(t.c.id.desc()).limit(limit))
    return [{"ts_ms": dt_to_ms(r.ts), "provider": r.provider, "kind": r.kind, "detail": r.detail} for r in rows]


# --- gaps ---------------------------------------------------------------------------------


async def record_gap(
    conn: AsyncConnection,
    instrument_id: int,
    start_ms: int,
    end_ms: int,
    kind: str,
    detail: dict[str, Any] | None = None,
) -> int | None:
    """Record a gap as ``open``; returns its id, or ``None`` when it is already known.

    A gap seen again with the same start but a later end (the downtime grew: the process
    was restarted before any new data arrived) is extended and reopened, and its id returned.
    """
    stmt = pg_insert(s.data_gaps).values(
        instrument_id=instrument_id,
        start_ts=ms_to_dt(start_ms),
        end_ts=ms_to_dt(end_ms),
        kind=kind,
        detail=detail,
    )
    upsert = stmt.on_conflict_do_update(
        index_elements=["instrument_id", "start_ts", "kind"],
        set_={
            "end_ts": stmt.excluded.end_ts,
            "status": "open",
            "detected_at": func.now(),
            "resolved_at": None,
            "detail": stmt.excluded.detail,
        },
        where=s.data_gaps.c.end_ts < stmt.excluded.end_ts,
    ).returning(s.data_gaps.c.id)
    gap_id = (await conn.execute(upsert)).scalar()
    return int(gap_id) if gap_id is not None else None


async def resolve_gap(conn: AsyncConnection, gap_id: int, status: str, detail: dict[str, Any] | None = None) -> None:
    await conn.execute(
        update(s.data_gaps)
        .where(s.data_gaps.c.id == gap_id)
        .values(status=status, resolved_at=func.now(), detail=detail)
    )


def _gap_dict(r: Any) -> dict[str, Any]:
    return {
        "id": r.id,
        "symbol": r.symbol,
        "instrument_id": r.instrument_id,
        "start_ms": dt_to_ms(r.start_ts),
        "end_ms": dt_to_ms(r.end_ts),
        "seconds": (dt_to_ms(r.end_ts) - dt_to_ms(r.start_ts)) // SECOND_MS,
        "kind": r.kind,
        "status": r.status,
        "detected_ms": dt_to_ms(r.detected_at),
        "resolved_ms": dt_to_ms(r.resolved_at) if r.resolved_at else None,
        "detail": r.detail,
    }


async def get_gap(conn: AsyncConnection, gap_id: int) -> dict[str, Any] | None:
    g, i = s.data_gaps, s.instruments
    row = (
        await conn.execute(select(g, i.c.symbol).join(i, i.c.id == g.c.instrument_id).where(g.c.id == gap_id))
    ).first()
    return _gap_dict(row) if row is not None else None


async def list_gaps(
    conn: AsyncConnection, *, status: str | None = None, since_ms: int | None = None, limit: int = 200
) -> list[dict[str, Any]]:
    g, i = s.data_gaps, s.instruments
    stmt = select(g, i.c.symbol).join(i, i.c.id == g.c.instrument_id)
    if status is not None:
        stmt = stmt.where(g.c.status == status)
    if since_ms is not None:
        stmt = stmt.where(g.c.end_ts >= ms_to_dt(since_ms))
    rows = await conn.execute(stmt.order_by(g.c.start_ts.desc()).limit(limit))
    return [_gap_dict(r) for r in rows]


async def gap_counts(conn: AsyncConnection, since_ms: int) -> dict[str, int]:
    g = s.data_gaps
    rows = await conn.execute(
        select(g.c.status, func.count()).where(g.c.end_ts >= ms_to_dt(since_ms)).group_by(g.c.status)
    )
    return {r[0]: r[1] for r in rows}


# --- history jobs -------------------------------------------------------------------------


async def create_job(conn: AsyncConnection, kind: str, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> int:
    stmt = (
        s.history_jobs.insert()
        .values(
            kind=kind,
            symbol=symbol,
            timeframe=timeframe,
            start_ts=ms_to_dt(start_ms),
            end_ts=ms_to_dt(end_ms),
        )
        .returning(s.history_jobs.c.id)
    )
    return int((await conn.execute(stmt)).scalar_one())


async def update_job(conn: AsyncConnection, job_id: int, **values: Any) -> None:
    await conn.execute(update(s.history_jobs).where(s.history_jobs.c.id == job_id).values(**values))


async def fail_unfinished_jobs(conn: AsyncConnection, reason: str) -> int:
    """Mark jobs that were queued or running when the process stopped as failed."""
    t = s.history_jobs
    result = await conn.execute(
        update(t)
        .where(t.c.status.in_(("queued", "running")))
        .values(status="failed", error=reason, finished_at=func.now())
    )
    return int(result.rowcount or 0)


def _job_dict(r: Any) -> dict[str, Any]:
    def ms(v: datetime | None) -> int | None:
        return dt_to_ms(v) if v is not None else None

    return {
        "id": r.id,
        "kind": r.kind,
        "symbol": r.symbol,
        "timeframe": r.timeframe,
        "start_ms": dt_to_ms(r.start_ts),
        "end_ms": dt_to_ms(r.end_ts),
        "status": r.status,
        "rows_written": r.rows_written,
        "pages": r.pages,
        "error": r.error,
        "created_ms": ms(r.created_at),
        "started_ms": ms(r.started_at),
        "finished_ms": ms(r.finished_at),
    }


async def get_job(conn: AsyncConnection, job_id: int) -> dict[str, Any] | None:
    row = (await conn.execute(select(s.history_jobs).where(s.history_jobs.c.id == job_id))).first()
    return _job_dict(row) if row is not None else None


async def list_jobs(conn: AsyncConnection, limit: int = 50) -> list[dict[str, Any]]:
    rows = await conn.execute(select(s.history_jobs).order_by(s.history_jobs.c.id.desc()).limit(limit))
    return [_job_dict(r) for r in rows]


# --- meta ---------------------------------------------------------------------------------


async def get_meta(conn: AsyncConnection, key: str) -> Any | None:
    return (await conn.execute(select(s.app_meta.c.value).where(s.app_meta.c.key == key))).scalar()


async def set_meta(conn: AsyncConnection, key: str, value: Any) -> None:
    stmt = pg_insert(s.app_meta).values(key=key, value=value)
    stmt = stmt.on_conflict_do_update(
        index_elements=["key"], set_={"value": stmt.excluded.value, "updated_at": func.now()}
    )
    await conn.execute(stmt)
