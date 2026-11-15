"""Historical data retrieval: provider aggregate bars -> ``bars`` table.

Provider aggregates are stored under ``source = massive_agg`` with
``price_basis = bid`` (Massive builds FX bars from the bid side only) so they can never
be mistaken for the mid-price bars this system builds from live quotes.

A request is recorded as a job (``history_jobs``) and executed in the background; the
dashboard and the CLI poll the job.  Requests are split into chunks that stay below
the provider's limit of 50 000 base aggregates per query, and ``next_url`` pagination
is followed on top of that.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.clock import Clock
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.timeframes import get_timeframe
from fxlab.logging import get_logger
from fxlab.marketdata.massive.rest import MassiveRestClient, aggregate_to_bar
from fxlab.marketdata.massive.symbols import TIMEFRAME_TO_RANGE, rest_aggs_ticker
from fxlab.storage import repo
from fxlab.timeutil import DAY_MS, MINUTE_MS, SECOND_MS

log = get_logger(__name__)

#: base aggregate behind each timespan (the provider's ``limit`` counts these)
_BASE_MS = {"second": SECOND_MS, "minute": MINUTE_MS, "hour": MINUTE_MS, "day": DAY_MS}
_CHUNK_BASE_BARS = 45_000  # safety margin below the documented maximum of 50 000

#: guard rails against an accidental huge download from the dashboard (CLI can loop)
MAX_RANGE_MS = {
    "second": 7 * DAY_MS,
    "minute": 366 * DAY_MS,
    "hour": 20 * 366 * DAY_MS,
    "day": 40 * 366 * DAY_MS,
}


class HistoryRequestError(ValueError):
    pass


class HistoryService:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        registry: InstrumentRegistry,
        rest: MassiveRestClient | None,
        instrument_ids: Mapping[str, int],
        clock: Clock,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._rest = rest
        self._ids = instrument_ids
        self._clock = clock

    @property
    def available(self) -> bool:
        return self._rest is not None

    def validate(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> None:
        if self._rest is None:
            raise HistoryRequestError("historical retrieval needs the Massive REST API (not available in this mode)")
        if symbol not in self._registry or symbol not in self._ids:
            raise HistoryRequestError(f"unknown instrument {symbol!r}")
        get_timeframe(timeframe)  # raises ValueError for unsupported codes
        if end_ms <= start_ms:
            raise HistoryRequestError("end must be after start")
        _, timespan = TIMEFRAME_TO_RANGE[timeframe]
        if end_ms - start_ms > MAX_RANGE_MS[timespan]:
            days = MAX_RANGE_MS[timespan] // DAY_MS
            raise HistoryRequestError(f"range too long for {timeframe} bars in one job (max {days} days)")

    async def create_job(self, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> int:
        self.validate(symbol, timeframe, start_ms, end_ms)
        async with self._engine.begin() as conn:
            return await repo.create_job(conn, "aggregates", symbol, timeframe, start_ms, end_ms)

    async def run_job(self, job_id: int) -> dict[str, Any]:
        """Execute a job.  Never raises: failures are recorded on the job."""
        async with self._engine.begin() as conn:
            job = await repo.get_job(conn, job_id)
            if job is None:
                raise HistoryRequestError(f"job {job_id} does not exist")
            await repo.update_job(conn, job_id, status="running", started_at=func.now(), error=None)
        try:
            rows, pages = await self._download(job_id, job["symbol"], job["timeframe"], job["start_ms"], job["end_ms"])
        except Exception as exc:
            log.error("history_job_failed", job_id=job_id, error=str(exc)[:300])
            async with self._engine.begin() as conn:
                await repo.update_job(
                    conn,
                    job_id,
                    status="failed",
                    error=f"{type(exc).__name__}: {str(exc)[:400]}",
                    finished_at=func.now(),
                )
        else:
            async with self._engine.begin() as conn:
                await repo.update_job(
                    conn, job_id, status="done", rows_written=rows, pages=pages, error=None, finished_at=func.now()
                )
            log.info("history_job_done", job_id=job_id, rows=rows, pages=pages)
        async with self._engine.connect() as conn:
            final = await repo.get_job(conn, job_id)
        assert final is not None
        return final

    async def _download(self, job_id: int, symbol: str, timeframe: str, start_ms: int, end_ms: int) -> tuple[int, int]:
        assert self._rest is not None
        inst = self._registry.get(symbol)
        iid = self._ids[symbol]
        multiplier, timespan = TIMEFRAME_TO_RANGE[timeframe]
        ticker = rest_aggs_ticker(inst)
        chunk_ms = _CHUNK_BASE_BARS * _BASE_MS[timespan]
        rows_written = pages = 0
        chunk_start = start_ms
        while chunk_start < end_ms:
            chunk_end = min(chunk_start + chunk_ms, end_ms)
            # the endpoint's range is inclusive; -1 ms keeps consecutive chunks disjoint
            async for page in self._rest.iter_aggregates(ticker, multiplier, timespan, chunk_start, chunk_end - 1):
                pages += 1
                bars = [b for b in (aggregate_to_bar(symbol, timeframe, item) for item in page) if b is not None]
                bars = [b for b in bars if start_ms <= b.ts_ms < end_ms]
                if bars:
                    async with self._engine.begin() as conn:
                        await repo.upsert_bars(conn, [repo.bar_row(iid, b) for b in bars])
                    rows_written += len(bars)
                async with self._engine.begin() as conn:
                    await repo.update_job(conn, job_id, rows_written=rows_written, pages=pages)
            chunk_start = chunk_end
        return rows_written, pages
