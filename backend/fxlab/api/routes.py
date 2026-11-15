"""REST API consumed by the dashboard (and usable with curl).

All timestamps in responses are epoch milliseconds (UTC).  Time parameters accept
ISO-8601 strings, epoch seconds or epoch milliseconds.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from fxlab.domain.models import DataSource
from fxlab.domain.timeframes import TIMEFRAMES, get_timeframe
from fxlab.pipeline.ingestion import IngestionService
from fxlab.pipeline.payloads import bar_payload, obs_payload
from fxlab.services.gaps import GapTask
from fxlab.storage import repo
from fxlab.storage import schema as s
from fxlab.timeutil import HOUR_MS, SECOND_MS, dt_to_ms, ms_to_dt, parse_time

router = APIRouter()


def get_service(request: Request) -> IngestionService:
    service: IngestionService | None = getattr(request.app.state, "service", None)
    error: str | None = getattr(request.app.state, "startup_error", None)
    if service is None or not service.started:
        raise HTTPException(status_code=503, detail=error or "the ingestion service is not running")
    return service


def _parse(value: str | int | float | None, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return parse_time(value)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=400, detail=f"invalid time for '{name}': {value!r}") from None


def _runtime(service: IngestionService, symbol: str) -> Any:
    rt = service.runtime(symbol.upper())
    if rt is None:
        raise HTTPException(status_code=404, detail=f"unknown or inactive instrument {symbol!r}")
    return rt


# --- health -------------------------------------------------------------------------------


@router.get("/health/live")
async def health_live() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


@router.get("/health/ready")
async def health_ready(request: Request) -> JSONResponse:
    """Readiness: started, and the database accepted the last write."""
    service: IngestionService | None = getattr(request.app.state, "service", None)
    error: str | None = getattr(request.app.state, "startup_error", None)
    problems: list[str] = []
    if error:
        problems.append(error)
    if service is None or not service.started:
        problems.append("ingestion service not started")
    elif service.writer.db_ok is False:
        problems.append(f"database writes failing: {service.writer.last_error}")
    return JSONResponse({"ready": not problems, "problems": problems}, status_code=200 if not problems else 503)


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """Full system snapshot: provider, connection, instruments, latency, storage, gaps, errors."""
    service: IngestionService | None = getattr(request.app.state, "service", None)
    error: str | None = getattr(request.app.state, "startup_error", None)
    if service is None or not service.started:
        return {"startup_error": error or "the ingestion service is not running"}
    return service.status()


# --- reference ----------------------------------------------------------------------------


@router.get("/instruments")
async def instruments(request: Request) -> list[dict[str, Any]]:
    """The configured instrument registry with the provider-availability check result."""
    service = get_service(request)
    async with service.engine.connect() as conn:
        rows = {r["symbol"]: r for r in await repo.list_instruments(conn)}
    out = []
    for inst in service.registry.all():
        row = rows.get(inst.symbol, {})
        checked = row.get("provider_checked_at")
        out.append(
            {
                **inst.model_dump(mode="json"),
                "provider_status": row.get("provider_status", "unverified"),
                "provider_checked_ms": dt_to_ms(checked) if checked else None,
                "provider_detail": row.get("provider_detail"),
            }
        )
    return out


@router.get("/timeframes")
async def timeframes() -> list[dict[str, Any]]:
    return [{"code": tf.code, "seconds": tf.seconds, "anchor": tf.anchor} for tf in TIMEFRAMES.values()]


# --- market data --------------------------------------------------------------------------


@router.get("/observations")
async def observations(
    request: Request,
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(600, ge=1, le=20_000),
) -> dict[str, Any]:
    """1-second observations (ascending).  Without a range: the most recent ``limit`` seconds."""
    service = get_service(request)
    rt = _runtime(service, symbol)
    start_ms, end_ms = _parse(start, "start"), _parse(end, "end")
    if start_ms is None and end_ms is None and limit <= len(rt.recent_obs):
        items = service.recent_observations(rt.inst.symbol, limit)
    else:
        now = service.clock.now_ms()
        hi = end_ms if end_ms is not None else now + SECOND_MS
        lo = start_ms if start_ms is not None else 0
        async with service.engine.connect() as conn:
            items = await repo.fetch_observations(
                conn, rt.inst.symbol, rt.instrument_id, lo, hi, limit=limit, newest_first=start_ms is None
            )
        if start_ms is None:
            items.reverse()
    return {
        "symbol": rt.inst.symbol,
        "synthetic": service.synthetic,
        "observations": [obs_payload(o, rt.inst) for o in items],
    }


@router.get("/bars")
async def bars(
    request: Request,
    symbol: str,
    timeframe: str = "1m",
    source: Literal["live", "provider"] = "live",
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(500, ge=1, le=20_000),
) -> dict[str, Any]:
    """OHLC bars (ascending).

    ``source=live``     bars built by this system from its own 1-second grid (mid prices).
                        The bar still in progress is appended with ``"partial": true``.
    ``source=provider`` aggregates downloaded from the provider (BID prices, UTC-aligned).
    """
    service = get_service(request)
    rt = _runtime(service, symbol)
    try:
        tf = get_timeframe(timeframe)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    if tf.code == "1s" and source == "live":
        raise HTTPException(status_code=400, detail="the 1s series is the observation grid: use /api/observations")
    start_ms, end_ms = _parse(start, "start"), _parse(end, "end")
    now = service.clock.now_ms()
    stored_source = rt.source.value if source == "live" else DataSource.MASSIVE_AGG.value
    async with service.engine.connect() as conn:
        items = await repo.fetch_bars(
            conn,
            rt.inst.symbol,
            rt.instrument_id,
            tf.code,
            stored_source,
            start_ms if start_ms is not None else 0,
            end_ms if end_ms is not None else now + SECOND_MS,
            limit=limit,
            newest_first=start_ms is None,
        )
    if start_ms is None:
        items.reverse()
    payload = [bar_payload(b) for b in items]
    if source == "live" and end_ms is None:
        partial = rt.bars.partial_bars().get(tf.code)
        if partial is not None and (not payload or partial.ts_ms > payload[-1]["ts"]):
            payload.append({**bar_payload(partial), "partial": True})
    return {
        "symbol": rt.inst.symbol,
        "timeframe": tf.code,
        "source": stored_source,
        "price_basis": "mid" if source == "live" else "bid",
        "synthetic": service.synthetic,
        "bars": payload,
    }


@router.get("/stats/ingest")
async def ingest_stats(request: Request, symbol: str, hours: int = Query(6, ge=1, le=168)) -> dict[str, Any]:
    """Per-minute ingest counters and latency percentiles."""
    service = get_service(request)
    rt = _runtime(service, symbol)
    t = s.ingest_stats_1m
    since = ms_to_dt(service.clock.now_ms() - hours * HOUR_MS)
    async with service.engine.connect() as conn:
        rows = await conn.execute(
            select(t).where(t.c.instrument_id == rt.instrument_id, t.c.ts >= since).order_by(t.c.ts)
        )
        out = [
            {
                "ts": dt_to_ms(r.ts),
                "received": r.n_received,
                "accepted": r.n_accepted,
                "duplicate": r.n_duplicate,
                "late": r.n_late,
                "rejected": r.n_rejected,
                "rejected_by_reason": r.rejected or {},
                "latency_p50_ms": r.lat_p50_ms,
                "latency_p95_ms": r.lat_p95_ms,
                "latency_p99_ms": r.lat_p99_ms,
                "latency_max_ms": r.lat_max_ms,
            }
            for r in rows
        ]
    return {"symbol": rt.inst.symbol, "minutes": out}


# --- monitoring ---------------------------------------------------------------------------


@router.get("/events")
async def events(request: Request, limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    """Connection lifecycle events, newest first (persisted across restarts)."""
    service = get_service(request)
    async with service.engine.connect() as conn:
        stored = await repo.recent_events(conn, limit)
    recent = service.recent_events(limit)
    known = {(e["ts_ms"], e["kind"]) for e in stored}
    merged = [e for e in recent if (e["ts_ms"], e["kind"]) not in known] + stored  # not yet flushed + stored
    return merged[:limit]


@router.get("/gaps")
async def gaps(
    request: Request,
    status: str | None = None,
    hours: int = Query(168, ge=1, le=24 * 365),
) -> list[dict[str, Any]]:
    """Recorded data gaps, newest first."""
    service = get_service(request)
    async with service.engine.connect() as conn:
        return await repo.list_gaps(conn, status=status, since_ms=service.clock.now_ms() - hours * HOUR_MS)


@router.post("/gaps/scan")
async def scan_gaps(request: Request) -> dict[str, int]:
    """Run a hole scan now instead of waiting for the periodic one."""
    service = get_service(request)
    return {"queued": await service.scan_gaps()}


@router.post("/gaps/{gap_id}/fill", status_code=202)
async def fill_gap(request: Request, gap_id: int) -> dict[str, Any]:
    """Queue a (re)fill of one gap from the provider's historical quotes, ignoring the length limit."""
    service = get_service(request)
    async with service.engine.connect() as conn:
        gap = await repo.get_gap(conn, gap_id)
    if gap is None:
        raise HTTPException(status_code=404, detail=f"gap {gap_id} does not exist")
    if service.runtime(gap["symbol"]) is None:
        raise HTTPException(status_code=409, detail=f"instrument {gap['symbol']} is not active")
    service.queue_gap(GapTask(gap["symbol"], gap["start_ms"], gap["end_ms"], gap["kind"], gap_id=gap_id, force=True))
    return {"queued": True, "gap": gap}


# --- historical retrieval -----------------------------------------------------------------


class HistoryRequest(BaseModel):
    symbol: str = Field(examples=["EURUSD"])
    timeframe: str = Field("1m", examples=["1m"])
    start: str | int | float = Field(description="ISO-8601 or epoch (s or ms)", examples=["2026-11-12T00:00:00Z"])
    end: str | int | float = Field(description="ISO-8601 or epoch (s or ms)", examples=["2026-11-13T00:00:00Z"])


@router.post("/history/jobs", status_code=202)
async def create_history_job(request: Request, body: HistoryRequest) -> dict[str, Any]:
    """Download provider aggregate bars for a range (runs in the background)."""
    service = get_service(request)
    history = service.history
    if history is None or not history.available:
        raise HTTPException(
            status_code=409,
            detail="historical retrieval needs the Massive REST API (API key not configured or mock mode)",
        )
    start_ms, end_ms = _parse(body.start, "start"), _parse(body.end, "end")
    if start_ms is None or end_ms is None:
        raise HTTPException(status_code=400, detail="start and end are required")
    try:
        job_id = await history.create_job(body.symbol.upper(), body.timeframe, start_ms, end_ms)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    service.start_history_job(job_id)
    async with service.engine.connect() as conn:
        job = await repo.get_job(conn, job_id)
    assert job is not None
    return job


@router.get("/history/jobs")
async def list_history_jobs(request: Request, limit: int = Query(20, ge=1, le=200)) -> list[dict[str, Any]]:
    service = get_service(request)
    async with service.engine.connect() as conn:
        return await repo.list_jobs(conn, limit)


@router.get("/history/jobs/{job_id}")
async def get_history_job(request: Request, job_id: int) -> dict[str, Any]:
    service = get_service(request)
    async with service.engine.connect() as conn:
        job = await repo.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id} does not exist")
    return job
