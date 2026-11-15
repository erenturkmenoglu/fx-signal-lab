"""Gap bookkeeping and recovery.

A *gap* is a stretch of market-open time for which the 1-second grid has no
trustworthy live data:

``hole``         no ``obs_1s`` rows at all (the process was down or suspended)
``feed_outage``  rows exist but were produced while the feed was disconnected or
                 silent, so they only repeat the last quote known before the outage

Recovery replays the provider's **historical quotes** (REST ``/v3/quotes``) for the gap
through the very same normaliser and sampler the live path uses, with the quote's
source timestamp acting as the clock.  The rebuilt rows are tagged ``backfill`` - they
describe what the market did, not what this system knew at the time - and they never
overwrite a second that was genuinely captured live.

Outcomes are explicit (``data_gaps.status``):

``filled``            rebuilt from provider quotes
``no_provider_data``  the provider has no quotes in that window either (not an outage)
``unfillable``        cannot be recovered automatically (no REST access, plan without
                      historical quotes, gap longer than the auto-backfill limit)
``failed``            a transient provider/database error; retried by the periodic scan
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.clock import Clock
from fxlab.domain.calendar import MarketCalendar
from fxlab.domain.instruments import Instrument, InstrumentRegistry
from fxlab.domain.models import DataSource, Observation, ObsStatus, QuoteTick
from fxlab.logging import get_logger
from fxlab.marketdata.massive.rest import (
    MassiveRestClient,
    ProviderError,
    ProviderNotEntitledError,
    quote_result_to_raw,
)
from fxlab.marketdata.massive.symbols import rest_quotes_ticker
from fxlab.pipeline.normalizer import QuoteNormalizer, Verdict
from fxlab.pipeline.sampler import SecondSampler
from fxlab.storage import repo
from fxlab.storage import schema as s
from fxlab.timeutil import HOUR_MS, SECOND_MS, dt_to_ms, floor_ms, iso, ms_to_dt

log = get_logger(__name__)

MAX_ATTEMPTS = 5


@dataclass
class GapTask:
    symbol: str
    start_ms: int
    end_ms: int
    kind: str  # hole | feed_outage
    not_before_ms: int = 0
    gap_id: int | None = None  # set when an already recorded gap is retried
    force: bool = False  # ignore the auto-backfill length limit (manual fill)
    #: a known interruption (disconnect, restart): record it however short it is.  The minimum
    #: length only keeps the periodic scan from reporting one-second scheduling hiccups.
    any_length: bool = False


class GapService:
    def __init__(
        self,
        *,
        engine: AsyncEngine,
        registry: InstrumentRegistry,
        clock: Clock,
        instrument_ids: Mapping[str, int],
        calendars: Mapping[str, MarketCalendar],
        rest: MassiveRestClient | None,
        min_seconds: int = 5,
        max_backfill_hours: int = 72,
        no_rest_reason: str = "no REST provider configured",
        on_filled: Callable[[str, int, int], Awaitable[None]] | None = None,
    ) -> None:
        self._engine = engine
        self._registry = registry
        self._clock = clock
        self._ids = instrument_ids
        self._calendars = calendars
        self._rest = rest
        self._min_ms = min_seconds * SECOND_MS
        self._max_ms = max_backfill_hours * HOUR_MS
        self._max_hours = max_backfill_hours
        self._no_rest_reason = no_rest_reason
        self._on_filled = on_filled

    # -- detection helpers ---------------------------------------------------------------
    def open_pieces(
        self, symbol: str, start_ms: int, end_ms: int, *, min_ms: int | None = None
    ) -> list[tuple[int, int]]:
        """The market-open parts of ``[start_ms, end_ms)`` that are long enough to matter."""
        start_ms, end_ms = floor_ms(start_ms, SECOND_MS), floor_ms(end_ms, SECOND_MS)
        shortest = self._min_ms if min_ms is None else min_ms
        return [(a, b) for a, b in self._calendars[symbol].open_intervals(start_ms, end_ms) if b - a >= shortest]

    async def scan_holes(self, symbol: str, start_ms: int, end_ms: int) -> list[GapTask]:
        """Find missing stretches of the grid in ``[start_ms, end_ms)`` during market hours."""
        start_ms, end_ms = floor_ms(start_ms, SECOND_MS), floor_ms(end_ms, SECOND_MS)
        async with self._engine.connect() as conn:
            holes = await repo.find_holes(conn, self._ids[symbol], start_ms, end_ms)
        return [GapTask(symbol, a, b, "hole") for hole in holes for a, b in self.open_pieces(symbol, *hole)]

    # -- processing ----------------------------------------------------------------------
    async def register(self, task: GapTask) -> list[GapTask]:
        """Record the market-open parts of ``task`` as open gaps; one task per newly recorded gap.

        Recording happens when a gap is detected, before any waiting, so that a restart
        cannot forget it: unresolved records are picked up again by :meth:`retry_candidates`.
        """
        registered: list[GapTask] = []
        min_ms = None
        if task.any_length:
            # The second of the last quote before a disconnect and the second of the first quote
            # after it were captured live and stay as they are; anything shorter than the
            # seconds between them holds nothing to recover.
            min_ms = (3 if task.kind == "feed_outage" else 2) * SECOND_MS
        for a, b in self.open_pieces(task.symbol, task.start_ms, task.end_ms, min_ms=min_ms):
            async with self._engine.begin() as conn:
                gap_id = await repo.record_gap(conn, self._ids[task.symbol], a, b, task.kind)
            if gap_id is None:
                continue  # already known; the periodic scan decides about retries
            log.warning(
                "gap_detected",
                symbol=task.symbol,
                kind=task.kind,
                start=iso(a),
                end=iso(b),
                seconds=(b - a) // 1000,
            )
            registered.append(GapTask(task.symbol, a, b, task.kind, task.not_before_ms, gap_id, task.force))
        return registered

    async def process(self, task: GapTask) -> list[dict[str, Any]]:
        """Try to fill the gap(s) described by ``task``, recording them first if necessary."""
        tasks = [task] if task.gap_id is not None else await self.register(task)
        results: list[dict[str, Any]] = []
        for t in tasks:
            assert t.gap_id is not None
            results.append(await self._fill(t.gap_id, t.symbol, t.start_ms, t.end_ms, t.kind, t.force))
        return results

    async def retry_candidates(self) -> list[GapTask]:
        """Recorded gaps that still deserve an attempt (``open``, or ``failed`` with attempts left)."""
        tasks: list[GapTask] = []
        async with self._engine.connect() as conn:
            for status in ("open", "failed"):
                for gap in await repo.list_gaps(conn, status=status):
                    attempts = (gap["detail"] or {}).get("attempts", 0)
                    if gap["symbol"] in self._ids and attempts < MAX_ATTEMPTS:
                        tasks.append(
                            GapTask(gap["symbol"], gap["start_ms"], gap["end_ms"], gap["kind"], gap_id=gap["id"])
                        )
        return tasks

    async def _fill(
        self, gap_id: int, symbol: str, start_ms: int, end_ms: int, kind: str, force: bool
    ) -> dict[str, Any]:
        inst = self._registry.get(symbol)
        status: str
        detail: dict[str, Any]
        if self._rest is None:
            status, detail = "unfillable", {"reason": self._no_rest_reason}
        elif end_ms - start_ms > self._max_ms and not force:
            status = "unfillable"
            detail = {
                "reason": f"longer than the auto-backfill limit ({self._max_hours} h); "
                f"fill manually with: fxlab gaps fill --id {gap_id}"
            }
        else:
            try:
                detail = await self._fill_from_quotes(inst, start_ms, end_ms)
                status = "filled" if detail["provider_quotes"] > 0 else "no_provider_data"
            except ProviderNotEntitledError as exc:
                status = "unfillable"
                detail = {
                    "reason": "the subscription does not include historical quotes (/v3/quotes)",
                    "error": str(exc),
                }
            except ProviderError as exc:
                status, detail = "failed", {"error": str(exc)[:300]}
            except Exception as exc:
                log.exception("gap_fill_crashed", gap_id=gap_id)
                status, detail = "failed", {"error": f"{type(exc).__name__}: {str(exc)[:300]}"}

        async with self._engine.begin() as conn:
            if status == "failed":
                previous = await repo.get_gap(conn, gap_id)
                detail["attempts"] = ((previous or {}).get("detail") or {}).get("attempts", 0) + 1
            await repo.resolve_gap(conn, gap_id, status, detail)

        log.info(
            "gap_processed",
            gap_id=gap_id,
            symbol=symbol,
            kind=kind,
            status=status,
            **{k: v for k, v in detail.items() if k != "error"},
        )
        if status == "filled" and self._on_filled is not None:
            await self._on_filled(symbol, start_ms, end_ms)
        return {
            "id": gap_id,
            "symbol": symbol,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "kind": kind,
            "status": status,
            "detail": detail,
        }

    async def _fill_from_quotes(self, inst: Instrument, start_ms: int, end_ms: int) -> dict[str, Any]:
        assert self._rest is not None
        symbol = inst.symbol
        iid = self._ids[symbol]
        calendar = self._calendars[symbol]
        ticker = rest_quotes_ticker(inst)

        # Deterministic replay: fresh validator and sampler, seeded with the provider's
        # last quote before the gap so quiet seconds at its start can be carried.
        normalizer = QuoteNormalizer(self._registry)
        sampler = SecondSampler(
            symbol, inst.data_quality.stale_after_ms, start_ms, source=DataSource.BACKFILL, max_catchup_s=None
        )
        async for page in self._rest.iter_quotes(ticker, lt_ms=start_ms, order="desc", limit=1):
            if page:
                seed = normalizer.process(quote_result_to_raw(symbol, page[0]))
                if seed.verdict is Verdict.ACCEPT and seed.tick is not None:
                    sampler.seed(seed.tick)
            break

        async with self._engine.begin() as conn:
            t = s.obs_1s
            protected = {
                dt_to_ms(r.ts)
                for r in await conn.execute(
                    select(t.c.ts).where(
                        t.c.instrument_id == iid,
                        t.c.ts >= ms_to_dt(start_ms),
                        t.c.ts < ms_to_dt(end_ms),
                        t.c.source == DataSource.LIVE.value,
                        t.c.status == ObsStatus.FRESH.value,
                    )
                )
            }
            q = s.quote_ticks
            in_window = and_(
                q.c.instrument_id == iid, q.c.source_ts >= ms_to_dt(start_ms), q.c.source_ts < ms_to_dt(end_ms)
            )
            await conn.execute(  # idempotent re-runs: forget an earlier partial backfill of this window
                delete(q).where(in_window, q.c.flags.op("&")(s.TICK_FLAG_BACKFILL) != 0)
            )
            # Live ticks in a second that never got its live row: the second that was being
            # captured when the process stopped.  Its ticks reached the disk, its observation did
            # not.  Recovered ticks are written only after the last of them: a quote is stored once.
            second = func.date_trunc("second", q.c.source_ts)
            live_until = {
                dt_to_ms(r[0]): dt_to_ms(r[1])
                for r in await conn.execute(select(second, func.max(q.c.source_ts)).where(in_window).group_by(second))
                if dt_to_ms(r[0]) not in protected
            }

        totals = {
            "provider_quotes": 0,
            "accepted": 0,
            "rejected": 0,
            "ticks_written": 0,
            "observations_written": 0,
            "fresh_seconds": 0,
        }

        async def write(ticks: list[QuoteTick], observations: list[Observation]) -> None:
            tick_rows = [
                repo.tick_row(iid, tick, s.TICK_FLAG_BACKFILL)
                for tick in ticks
                if (sec := floor_ms(tick.source_ts_ms, SECOND_MS)) not in protected
                and tick.source_ts_ms > live_until.get(sec, -1)
            ]
            obs_rows = [
                repo.obs_row(iid, o)
                for o in observations
                if start_ms <= o.ts_ms < end_ms and (o.status is ObsStatus.FRESH or calendar.is_open(o.ts_ms))
            ]
            async with self._engine.begin() as conn:
                await repo.insert_ticks(conn, tick_rows)
                await repo.upsert_observations(conn, obs_rows, replace="unless_live_fresh")
            totals["ticks_written"] += len(tick_rows)
            totals["observations_written"] += len(obs_rows)
            totals["fresh_seconds"] += sum(1 for o in observations if o.status is ObsStatus.FRESH)

        async for page in self._rest.iter_quotes(ticker, gte_ms=start_ms, lt_ms=end_ms, order="asc"):
            ticks: list[QuoteTick] = []
            observations: list[Observation] = []
            for item in page:
                totals["provider_quotes"] += 1
                result = normalizer.process(quote_result_to_raw(symbol, item))
                if result.verdict is Verdict.ACCEPT and result.tick is not None:
                    totals["accepted"] += 1
                    observations.extend(sampler.on_quote(result.tick))
                    ticks.append(result.tick)
                elif result.verdict is Verdict.REJECT:
                    totals["rejected"] += 1
            await write(ticks, observations)

        if totals["provider_quotes"] > 0:
            await write([], sampler.advance(end_ms))
        return totals
