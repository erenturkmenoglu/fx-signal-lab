"""The ingestion service: one process-wide object that owns the live data path.

::

    adapter --raw quotes--> normaliser --ticks--> 1-second sampler --observations--+
       |                        |                                                    |
       | connection events      | counters, latency                                  +--> bar aggregator
       v                        v                                                    |          |
    health / gap detection   System Health                                           v          v
                                                                              batch writer   live hub
                                                                              (PostgreSQL)   (dashboard)

Everything on the hot path (``on_raw_quote``) is synchronous, allocation-light and
free of I/O; the database is only touched by the batch writer and the recovery
services, in their own tasks.  Because the provider allows a single WebSocket
connection, exactly one ingestion service may run per account.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import Counter, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab import __version__
from fxlab.clock import Clock, SystemClock
from fxlab.config import Settings
from fxlab.domain.calendar import WeeklyCalendar, active_sessions
from fxlab.domain.instruments import Instrument, InstrumentRegistry
from fxlab.domain.models import DataSource, Observation, ObsStatus, QuoteTick, RawQuote
from fxlab.logging import get_logger
from fxlab.marketdata.backoff import ExponentialBackoff
from fxlab.marketdata.base import ConnectionEvent, ConnectionState, MarketDataAdapter
from fxlab.marketdata.massive.rest import MassiveRestClient
from fxlab.marketdata.massive.symbols import rest_aggs_ticker
from fxlab.marketdata.massive.ws import MassiveWebSocketAdapter
from fxlab.marketdata.mock.adapter import MockMarketDataAdapter
from fxlab.pipeline.bars import BarAggregator, build_bars
from fxlab.pipeline.hub import LiveHub
from fxlab.pipeline.latency import DurationStats, LatencyTracker, RateCounter
from fxlab.pipeline.normalizer import QuoteNormalizer, Verdict
from fxlab.pipeline.payloads import bar_payload, event_payload, obs_payload
from fxlab.pipeline.quality import evaluate_signal_gate
from fxlab.pipeline.sampler import SecondSampler
from fxlab.services.gaps import GapService, GapTask
from fxlab.services.history import HistoryService
from fxlab.storage import repo
from fxlab.storage import schema as s
from fxlab.storage.db import apply_storage_policies, timescale_available
from fxlab.storage.guard import enforce_data_mode
from fxlab.storage.writer import BatchWriter
from fxlab.timeutil import HOUR_MS, MINUTE_MS, SECOND_MS, floor_ms, ms_to_dt

log = get_logger(__name__)

RECENT_OBS = 3_600  # observations kept in memory per instrument (one hour)
AdapterFactory = Callable[["IngestionService", list[Instrument]], MarketDataAdapter | None]


@dataclass
class InstrumentRuntime:
    inst: Instrument
    instrument_id: int
    calendar: WeeklyCalendar
    sampler: SecondSampler
    bars: BarAggregator
    source: DataSource
    latency: LatencyTracker = field(default_factory=LatencyTracker)
    rate: RateCounter = field(default_factory=RateCounter)
    recent_obs: deque[Observation] = field(default_factory=lambda: deque(maxlen=RECENT_OBS))
    last_obs: Observation | None = None
    minute: Counter[str] = field(default_factory=Counter)
    provider_status: str = "unverified"
    supported: bool = True
    ref_mid: float | None = None
    ref_ts_ms: int | None = None
    ref_window_end_ms: int = 0
    #: after a restart: where the stretch without real updates began (None once it is handled)
    recovery_start_ms: int | None = None


class IngestionService:
    def __init__(
        self,
        settings: Settings,
        registry: InstrumentRegistry,
        engine: AsyncEngine,
        *,
        clock: Clock | None = None,
        adapter_factory: AdapterFactory | None = None,
    ) -> None:
        self.settings = settings
        self.registry = registry
        self.engine = engine
        self.clock: Clock = clock or SystemClock()
        self._adapter_factory = adapter_factory
        self.synthetic = settings.data_provider == "mock"

        self.hub = LiveHub()
        self.writer = BatchWriter(
            engine,
            self.clock,
            flush_interval_s=settings.writer_flush_interval_s,
            max_buffer_rows=settings.writer_max_buffer_rows,
        )
        self.rest: MassiveRestClient | None = None
        if settings.data_provider == "massive" and settings.api_key:
            self.rest = MassiveRestClient(
                base_url=settings.massive_rest_url,
                api_key=settings.api_key,
                max_calls_per_min=settings.massive_rest_max_calls_per_min,
                timeout_s=settings.massive_rest_timeout_s,
                max_retries=settings.massive_rest_max_retries,
            )

        self._normalizer = QuoteNormalizer(registry)
        self._rt: dict[str, InstrumentRuntime] = {}
        self.instrument_ids: dict[str, int] = {}
        self.adapter: MarketDataAdapter | None = None
        self.gaps: GapService | None = None
        self.history: HistoryService | None = None

        self._tasks: list[asyncio.Task[Any]] = []
        self._adapter_task: asyncio.Task[Any] | None = None
        self._writer_task: asyncio.Task[Any] | None = None
        self._background: set[asyncio.Task[Any]] = set()
        self._registering: set[asyncio.Task[None]] = set()
        self._gap_queue: asyncio.Queue[GapTask] = asyncio.Queue()
        self._queued_gap_ids: set[int] = set()
        self._stop = asyncio.Event()
        self._started_ms = self.clock.now_ms()
        self._events: deque[ConnectionEvent] = deque(maxlen=300)
        self._errors: deque[dict[str, Any]] = deque(maxlen=50)
        self._proc = DurationStats()
        self._rate = RateCounter()
        self._outage_start_ms: int | None = None
        self._outage_is_startup = False
        self._gap_counts: dict[str, int] = {}
        self._gaps_processed = 0
        self.storage_policies: dict[str, Any] = {}
        self.data_mode: dict[str, Any] = {}
        self.started = False

    # ======================================================================= lifecycle ==
    async def start(self) -> None:
        settings = self.settings
        now = self.clock.now_ms()
        self._started_ms = now
        async with self.engine.begin() as conn:
            self.instrument_ids = await repo.sync_instruments(conn, self.registry)
        self.data_mode = await enforce_data_mode(self.engine, settings)
        if settings.storage_policies_enabled:
            self.storage_policies = await apply_storage_policies(
                self.engine,
                compress_after_days=settings.compress_after_days,
                retention_ticks_days=settings.retention_ticks_days,
                retention_obs_days=settings.retention_obs_days,
            )
        else:
            log.warning("storage_policies_not_managed", reason="STORAGE_POLICIES_ENABLED is false")
            self.storage_policies = {"timescaledb": await timescale_available(self.engine), "applied": [], "errors": []}

        source = DataSource.MOCK if self.synthetic else DataSource.LIVE
        for inst in self.registry.active():
            calendar = inst.make_calendar(always_open=self.synthetic)
            self._rt[inst.symbol] = InstrumentRuntime(
                inst=inst,
                instrument_id=self.instrument_ids[inst.symbol],
                calendar=calendar,
                sampler=SecondSampler(
                    inst.symbol,
                    inst.data_quality.stale_after_ms,
                    now,
                    source=source,
                    max_catchup_s=settings.sampler_max_catchup_s,
                ),
                bars=BarAggregator(inst.symbol, calendar, source=source),
                source=source,
                provider_status="mock (synthetic)" if self.synthetic else "unverified",
            )

        calendars = {sym: rt.calendar for sym, rt in self._rt.items()}
        self.gaps = GapService(
            engine=self.engine,
            registry=self.registry,
            clock=self.clock,
            instrument_ids=self.instrument_ids,
            calendars=calendars,
            rest=self.rest,
            min_seconds=settings.gap_min_seconds,
            max_backfill_hours=settings.gap_backfill_max_hours,
            no_rest_reason=(
                "the mock provider has no history to recover from"
                if self.synthetic
                else "no API key configured: historical quotes are not available"
            ),
            on_filled=self._on_gap_filled,
        )
        self.history = HistoryService(
            engine=self.engine,
            registry=self.registry,
            rest=self.rest,
            instrument_ids=self.instrument_ids,
            clock=self.clock,
        )

        await self._check_provider_support()
        for rt in self._rt.values():
            await self._recover_state(rt)
        await self._refresh_gap_counts()

        subscribed = [rt.inst for rt in self._rt.values() if rt.supported]
        self.adapter = (
            self._adapter_factory(self, subscribed) if self._adapter_factory else self._make_adapter(subscribed)
        )

        if self.adapter is not None:
            # Until the first quote arrives the feed is, in effect, down.  Treating start-up as
            # an outage in progress means that the downtime before it and a slow first
            # connection are recorded and backfilled like any other outage.
            self._outage_start_ms = now
            self._outage_is_startup = True
        async with self.engine.begin() as conn:
            interrupted = await repo.fail_unfinished_jobs(conn, "interrupted by an application restart")
        if interrupted:
            log.warning("history_jobs_interrupted", count=interrupted)

        self._writer_task = asyncio.create_task(self.writer.run(), name="writer")
        self._tasks = [
            asyncio.create_task(self._supervise("timer", self._timer_loop), name="timer"),
            asyncio.create_task(self._supervise("stats", self._stats_loop), name="stats"),
            asyncio.create_task(self._supervise("gap-worker", self._gap_worker), name="gap-worker"),
            asyncio.create_task(self._supervise("gap-scan", self._gap_scan_loop), name="gap-scan"),
        ]
        if self.adapter is not None:
            self._adapter_task = asyncio.create_task(self._supervise("adapter", self.adapter.run), name="adapter")
        else:
            log.warning(
                "provider_not_configured",
                hint="set MASSIVE_API_KEY (or DATA_PROVIDER=mock for synthetic development data)",
            )
        self.started = True
        log.info(
            "ingestion_started",
            provider=settings.data_provider,
            synthetic=self.synthetic,
            instruments=[rt.inst.symbol for rt in self._rt.values()],
            subscribed=[i.symbol for i in subscribed],
        )

    async def stop(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        if self.adapter is not None:
            with contextlib.suppress(Exception):
                await self.adapter.stop()
        if self._adapter_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._adapter_task, timeout=10)
        if self._registering:  # gaps detected a moment ago must reach the database
            await asyncio.wait(list(self._registering), timeout=5)
        for task in [*self._tasks, *self._background, *self._registering]:
            task.cancel()
        for task in [*self._tasks, *self._background, *self._registering]:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await self.writer.stop()
        if self._writer_task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._writer_task, timeout=10)
        if self.rest is not None:
            await self.rest.aclose()
        self.started = False
        log.info("ingestion_stopped", written=self.writer.written, dropped=self.writer.dropped)

    async def _supervise(self, name: str, fn: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Keep a background loop alive: an unexpected crash is logged, surfaced and restarted."""
        while not self._stop.is_set():
            try:
                await fn()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(name, f"{type(exc).__name__}: {exc}")
                log.exception("background_task_crashed", task=name)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=2.0)

    def _spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _detected(self, task: GapTask) -> None:
        """A gap was detected on the hot path: record it in the background, at once."""
        registering = asyncio.create_task(self._register_gap(task), name="register-gap")
        self._registering.add(registering)
        registering.add_done_callback(self._registering.discard)

    async def _register_gap(self, task: GapTask) -> None:
        """Record a detected gap before waiting to fill it, so that a restart cannot forget it."""
        assert self.gaps is not None
        try:
            registered = await self.gaps.register(task)
        except Exception as exc:
            self._record_error("gaps", f"could not record a gap: {type(exc).__name__}: {exc}")
            log.exception("gap_register_failed", symbol=task.symbol, kind=task.kind)
            self._gap_queue.put_nowait(task)  # the worker records it when it gets there
            return
        for item in registered:
            self.queue_gap(item)
        await self._refresh_gap_counts()

    def _make_adapter(self, instruments: list[Instrument]) -> MarketDataAdapter | None:
        settings = self.settings
        if settings.data_provider == "mock":
            return MockMarketDataAdapter(
                instruments=instruments,
                sink=self,
                clock=self.clock,
                quotes_per_second=settings.mock_quotes_per_second,
                seed=settings.mock_seed,
                faults=settings.mock_faults,
            )
        if not settings.api_key:
            return None
        return MassiveWebSocketAdapter(
            url=settings.massive_ws_url,
            api_key=settings.api_key,
            instruments=instruments,
            sink=self,
            clock=self.clock,
            is_market_open=self._any_market_open,
            backoff=ExponentialBackoff(base_s=settings.ws_backoff_base_s, max_s=settings.ws_backoff_max_s),
            connect_timeout_s=settings.ws_connect_timeout_s,
            auth_timeout_s=settings.ws_auth_timeout_s,
            silence_timeout_s=settings.ws_silence_timeout_s,
            stable_after_s=settings.ws_stable_after_s,
            auth_failed_retry_s=settings.ws_auth_failed_retry_s,
            max_connections_retry_s=settings.ws_max_connections_retry_s,
        )

    def _any_market_open(self, ts_ms: int) -> bool:
        return any(rt.calendar.is_open(ts_ms) for rt in self._rt.values() if rt.supported)

    # ================================================================ start-up recovery ==
    async def _check_provider_support(self) -> None:
        """Ask the provider's reference API whether each active instrument exists.

        Availability is never assumed.  An instrument the provider does not list is marked
        ``unsupported`` and is not subscribed; if the check itself fails the instrument stays
        ``unverified`` and is still subscribed (a missing feed then shows up as NO DATA).
        """
        if self.rest is None:
            return
        for rt in self._rt.values():
            ticker = rest_aggs_ticker(rt.inst)
            detail: dict[str, Any] = {"ticker": ticker}
            try:
                record = await asyncio.wait_for(self.rest.fx_ticker(ticker), timeout=15)
            except Exception as exc:
                status = "unverified"
                detail["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                self._record_error("provider-check", f"{rt.inst.symbol}: {detail['error']}")
            else:
                status = "supported" if record is not None else "unsupported"
                if record is not None:
                    detail["name"] = record.get("name")
            rt.provider_status = status
            rt.supported = status != "unsupported"
            async with self.engine.begin() as conn:
                await repo.set_provider_status(conn, rt.inst.symbol, status, detail)
            log.info("provider_instrument_check", symbol=rt.inst.symbol, status=status)

    async def _recover_state(self, rt: InstrumentRuntime) -> None:
        symbol, iid = rt.inst.symbol, rt.instrument_id
        now = self.clock.now_ms()
        async with self.engine.connect() as conn:
            last_priced = await repo.last_observation(conn, symbol, iid, with_price=True)
            last_any = await repo.last_observation(conn, symbol, iid)
            recent = await repo.fetch_observations(conn, symbol, iid, now - RECENT_OBS * SECOND_MS, now + SECOND_MS)
        rt.recent_obs.extend(recent)
        if recent:
            rt.last_obs = recent[-1]

        # carry-forward source: the quote that was in force when the process stopped
        if (
            last_priced is not None
            and last_priced.bid is not None
            and last_priced.ask is not None
            and last_priced.quote_ts_ms is not None
        ):
            tick = QuoteTick(symbol, last_priced.bid, last_priced.ask, last_priced.quote_ts_ms, last_priced.quote_ts_ms)
            rt.sampler.seed(tick)
            self._normalizer.seed(tick)

        await self._rebuild_bars(rt, now, write=False)  # warm start of in-progress bars
        await self._refresh_reference(rt)

        # Downtime is the stretch between the last second that holds a real update and the first
        # new one.  Starting after the last *fresh* row, not simply after the last row, also
        # covers an outage that was still in progress when the process stopped or crashed.  The
        # gap is recorded when the first quote arrives (see on_connection_event), so that it
        # also covers the seconds between this start and that quote.
        if last_any is not None:
            horizon = now - self.settings.gap_backfill_max_hours * HOUR_MS
            async with self.engine.connect() as conn:
                tail = await repo.unfilled_tail_start(conn, iid, horizon)
            rt.recovery_start_ms = tail if tail is not None else last_any.ts_ms + SECOND_MS

    async def _rebuild_bars(self, rt: InstrumentRuntime, from_ms: int, *, write: bool) -> int:
        """Rebuild bars from the observation grid, starting with the trading day of ``from_ms``,
        and adopt the result as the live aggregator state.

        Used for the warm start (``write=False``: stored bars are already correct) and after a
        backfill (``write=True``: affected bars are re-emitted with the recovered data).
        """
        symbol = rt.inst.symbol
        now = self.clock.now_ms()
        load_from, _ = rt.calendar.rollover_window(min(from_ms, now))
        async with self.engine.connect() as conn:
            stored = await repo.fetch_observations(conn, symbol, rt.instrument_id, load_from, now + SECOND_MS)
        # CPU-heavy for multi-day ranges: keep it off the event loop so ingestion stays responsive
        bars, aggregator = await asyncio.to_thread(build_bars, stored, symbol, rt.calendar, source=rt.source)
        # --- synchronous from here: catch up with what arrived meanwhile, then swap ---------
        last_ts = aggregator.last_ts_ms if aggregator.last_ts_ms is not None else -1
        for obs in rt.recent_obs:
            if obs.ts_ms > last_ts:
                bars.extend(aggregator.on_observation(obs))
        rt.bars = aggregator
        if write:
            for bar in bars:
                self.writer.add_bar(repo.bar_row(rt.instrument_id, bar))
        return len(bars)

    async def _refresh_reference(self, rt: InstrumentRuntime) -> None:
        """Reference price for "change": the last quote before the current trading day began."""
        now = self.clock.now_ms()
        day_start, day_end = rt.calendar.rollover_window(now)
        rt.ref_window_end_ms = day_end
        async with self.engine.connect() as conn:
            ref = await repo.last_observation(
                conn, rt.inst.symbol, rt.instrument_id, before_ms=day_start, with_price=True
            )
        if ref is not None and ref.mid is not None and day_start - ref.ts_ms <= 4 * 24 * HOUR_MS:
            rt.ref_mid, rt.ref_ts_ms = ref.mid, ref.ts_ms
        else:
            rt.ref_mid, rt.ref_ts_ms = None, None

    # ============================================================= sink (the hot path) ==
    def on_raw_quote(self, raw: RawQuote) -> None:
        started = time.perf_counter_ns()
        result = self._normalizer.process(raw)
        rt = self._rt.get(raw.symbol)
        if rt is None:
            return
        self._rate.add(raw.recv_ts_ms)
        rt.minute["received"] += 1
        verdict = result.verdict
        if verdict is Verdict.ACCEPT and result.tick is not None:
            tick = result.tick
            rt.minute["accepted"] += 1
            rt.latency.add(tick.recv_ts_ms, tick.source_ts_ms)
            rt.rate.add(tick.recv_ts_ms)
            self._handle_closed(rt, rt.sampler.on_quote(tick))
            if self.settings.persist_raw_ticks:
                self.writer.add_tick(
                    repo.tick_row(rt.instrument_id, tick, s.TICK_FLAG_SYNTHETIC if self.synthetic else 0)
                )
        elif verdict is Verdict.DUPLICATE:
            rt.minute["duplicate"] += 1
        elif verdict is Verdict.LATE and result.tick is not None:
            rt.minute["late"] += 1
            if self.settings.persist_raw_ticks:
                flags = s.TICK_FLAG_LATE | (s.TICK_FLAG_SYNTHETIC if self.synthetic else 0)
                self.writer.add_tick(repo.tick_row(rt.instrument_id, result.tick, flags))
        else:
            rt.minute["rejected"] += 1
            if result.reason is not None:
                rt.minute[f"rejected:{result.reason.value}"] += 1
        self._proc.add((time.perf_counter_ns() - started) / 1000)

    def on_connection_event(self, event: ConnectionEvent) -> None:
        self._events.append(event)
        self.writer.add_event(repo.event_row(event))
        self.hub.publish({"type": "event", "event": event_payload(event)})
        if event.kind in ("error", "auth_failed", "watchdog_timeout", "server_status"):
            self._record_error("feed", f"{event.kind}: {event.detail}")

        if event.kind in ("disconnected", "watchdog_timeout"):
            if self._outage_start_ms is None:
                last_data = event.detail.get("last_data_ms")
                if isinstance(last_data, int):
                    self._outage_start_ms = last_data
        elif event.kind == "first_data" and self._outage_start_ms is not None:
            outage_start, end = self._outage_start_ms, event.ts_ms + SECOND_MS
            startup, self._outage_start_ms, self._outage_is_startup = self._outage_is_startup, None, False
            prompt = event.ts_ms - outage_start < self.settings.gap_min_seconds * SECOND_MS
            not_before = event.ts_ms + int(self.settings.gap_backfill_delay_s * 1000)
            # A disconnect or a restart is a known interruption: recover it however short it was.
            # Without a provider to recover from, short ones are not worth a record.
            any_length = self.rest is not None
            for rt in self._rt.values():
                if not rt.supported:
                    continue
                start, kind = outage_start, "feed_outage"
                if startup and rt.recovery_start_ms is not None:
                    # after a restart: everything since the last real update, up to this first quote
                    start, kind, rt.recovery_start_ms = rt.recovery_start_ms, "hole", None
                elif startup and prompt:
                    continue  # a first start, and the first quote arrived promptly: nothing was missed
                self._detected(
                    GapTask(rt.inst.symbol, start, end, kind, not_before_ms=not_before, any_length=any_length)
                )

    def _handle_closed(self, rt: InstrumentRuntime, observations: list[Observation]) -> None:
        for obs in observations:
            # While the market is closed only real updates are stored; carried/stale filler
            # for the weekend would be noise, not information.
            if obs.status is not ObsStatus.FRESH and not rt.calendar.is_open(obs.ts_ms):
                continue
            rt.last_obs = obs
            rt.recent_obs.append(obs)
            self.writer.add_observation(repo.obs_row(rt.instrument_id, obs))
            closed_bars = rt.bars.on_observation(obs)
            for bar in closed_bars:
                self.writer.add_bar(repo.bar_row(rt.instrument_id, bar))
            if self.hub.subscriber_count:
                self.hub.publish(
                    {
                        "type": "obs",
                        "symbol": rt.inst.symbol,
                        "obs": obs_payload(obs, rt.inst),
                        "partials": {tf: bar_payload(b) for tf, b in rt.bars.partial_bars().items()},
                        "closed": [bar_payload(b) for b in closed_bars],
                    }
                )

    # ================================================================ background loops ==
    async def _sleep(self, seconds: float) -> bool:
        """Sleep; returns True when the service is stopping."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=max(0.0, seconds))
        return self._stop.is_set()

    async def _timer_loop(self) -> None:
        """Close 1-second windows on time even when no quote arrives."""
        while True:
            now = self.clock.now_ms()
            if await self._sleep((SECOND_MS - now % SECOND_MS + 5) / 1000):
                return
            now = self.clock.now_ms()
            for rt in self._rt.values():
                if self.adapter is None or not rt.supported:
                    continue  # nothing is subscribed: do not fill the grid with empty rows
                closed = rt.sampler.advance(now)
                for start, end in rt.sampler.pop_skipped():
                    self._record_error(
                        "sampler",
                        f"{rt.inst.symbol}: fell behind, skipped {(end - start) // 1000}s of the grid",
                    )
                    delay_ms = int(self.settings.gap_backfill_delay_s * 1000)
                    self._detected(GapTask(rt.inst.symbol, start, end, "hole", not_before_ms=now + delay_ms))
                self._handle_closed(rt, closed)
                if now >= rt.ref_window_end_ms:
                    rt.ref_window_end_ms = now + MINUTE_MS  # do not spawn again before the refresh lands
                    self._spawn(self._refresh_reference(rt), "refresh-reference")
            if self.hub.subscriber_count:
                self.hub.publish({"type": "status", "status": self.status()})

    async def _stats_loop(self) -> None:
        """Persist per-minute ingest counters and latency percentiles."""
        while True:
            now = self.clock.now_ms()
            if await self._sleep((MINUTE_MS - now % MINUTE_MS + 50) / 1000):
                return
            now = self.clock.now_ms()
            minute_start = floor_ms(now, MINUTE_MS) - MINUTE_MS
            for rt in self._rt.values():
                counters, rt.minute = rt.minute, Counter()
                if not counters and not rt.calendar.is_open(minute_start):
                    continue
                lat = rt.latency.snapshot(now)
                self.writer.add_stats(
                    {
                        "instrument_id": rt.instrument_id,
                        "ts": ms_to_dt(minute_start),
                        "n_received": counters.get("received", 0),
                        "n_accepted": counters.get("accepted", 0),
                        "n_duplicate": counters.get("duplicate", 0),
                        "n_late": counters.get("late", 0),
                        "n_rejected": counters.get("rejected", 0),
                        "rejected": {k.split(":", 1)[1]: v for k, v in counters.items() if k.startswith("rejected:")},
                        "lat_n": lat.n,
                        "lat_p50_ms": lat.p50_ms,
                        "lat_p95_ms": lat.p95_ms,
                        "lat_p99_ms": lat.p99_ms,
                        "lat_max_ms": lat.max_ms,
                        "lat_resolution_ms": lat.resolution_ms,
                    }
                )

    async def _gap_worker(self) -> None:
        assert self.gaps is not None
        while True:
            task = await self._gap_queue.get()
            # Never touch seconds that are not final yet: the live rows up to the end of the gap
            # must be closed and on disk, otherwise the backfill would write a second (and its
            # ticks) that live capture is still producing.
            settle_ms = int((3 * self.settings.writer_flush_interval_s + 2) * 1000)
            ready_ms = max(task.not_before_ms, task.end_ms + settle_ms)
            if await self._sleep((ready_ms - self.clock.now_ms()) / 1000):
                return
            try:
                results = await self.gaps.process(task)
            except Exception as exc:
                self._record_error("gap-worker", f"{type(exc).__name__}: {exc}")
                log.exception("gap_task_failed", symbol=task.symbol, kind=task.kind)
            else:
                self._gaps_processed += len(results)
            finally:
                if task.gap_id is not None:
                    self._queued_gap_ids.discard(task.gap_id)
            await self._refresh_gap_counts()

    async def _gap_scan_loop(self) -> None:
        """Periodically look for holes in the grid and retry unresolved gaps."""
        assert self.gaps is not None
        first = True
        while True:
            # the first pass waits just long enough for the first batches to be on disk
            wait = 3 * self.settings.writer_flush_interval_s + 5 if first else self.settings.gap_scan_interval_s
            first = False
            if await self._sleep(wait):
                return
            await self.scan_gaps()

    async def scan_gaps(self) -> int:
        """One scan pass; returns the number of tasks queued."""
        assert self.gaps is not None
        now = self.clock.now_ms()
        # rows of the last few seconds may still be in the write buffer: not holes
        end = now - int((3 * self.settings.writer_flush_interval_s + 3) * 1000)
        start = max(self._started_ms, now - self.settings.gap_scan_lookback_hours * HOUR_MS)
        queued = 0
        for rt in self._rt.values():
            if self.adapter is None or not rt.supported:
                continue  # nothing is collected for this pair, so an empty grid is not a gap
            for task in await self.gaps.scan_holes(rt.inst.symbol, start, end):
                self._gap_queue.put_nowait(task)
                queued += 1
        for task in await self.gaps.retry_candidates():
            if task.gap_id is not None and task.gap_id not in self._queued_gap_ids:
                self._queued_gap_ids.add(task.gap_id)
                self._gap_queue.put_nowait(task)
                queued += 1
        await self._refresh_gap_counts()
        return queued

    async def _on_gap_filled(self, symbol: str, start_ms: int, end_ms: int) -> None:
        rt = self._rt.get(symbol)
        if rt is None:
            return
        # the in-memory hour may contain the rows that were just replaced: reload it
        now = self.clock.now_ms()
        async with self.engine.connect() as conn:
            stored = await repo.fetch_observations(
                conn, symbol, rt.instrument_id, now - RECENT_OBS * SECOND_MS, now + SECOND_MS
            )
        newest_stored = stored[-1].ts_ms if stored else -1
        tail = [o for o in rt.recent_obs if o.ts_ms > newest_stored]  # not flushed yet
        rt.recent_obs.clear()
        rt.recent_obs.extend([*stored, *tail])
        rebuilt = await self._rebuild_bars(rt, start_ms, write=True)
        log.info("bars_rebuilt_after_backfill", symbol=symbol, bars=rebuilt)
        self.hub.publish({"type": "resync"})

    async def _refresh_gap_counts(self) -> None:
        try:
            async with self.engine.connect() as conn:
                self._gap_counts = await repo.gap_counts(conn, self.clock.now_ms() - 24 * HOUR_MS)
        except Exception as exc:
            self._record_error("gaps", f"{type(exc).__name__}: {exc}")

    def _record_error(self, where: str, message: str) -> None:
        self._errors.appendleft({"ts_ms": self.clock.now_ms(), "where": where, "message": message[:400]})

    # ===================================================================== public API ==
    def runtime(self, symbol: str) -> InstrumentRuntime | None:
        return self._rt.get(symbol)

    def recent_observations(self, symbol: str, limit: int = 300) -> list[Observation]:
        rt = self._rt.get(symbol)
        if rt is None:
            return []
        items = list(rt.recent_obs)
        return items[-limit:]

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return [event_payload(e) for e in list(self._events)[-limit:]][::-1]

    def queue_gap(self, task: GapTask) -> None:
        """Queue a gap for (re)processing by the gap worker."""
        if task.gap_id is not None:
            self._queued_gap_ids.add(task.gap_id)
        self._gap_queue.put_nowait(task)

    def start_history_job(self, job_id: int) -> None:
        assert self.history is not None
        self._spawn(self.history.run_job(job_id), f"history-job-{job_id}")

    def provider_label(self) -> str:
        if self.synthetic:
            return "MOCK"
        if not self.settings.api_key:
            return "NOT CONFIGURED"
        return "LIVE" if self.settings.official_endpoints else "TEST ENDPOINT"

    def status(self) -> dict[str, Any]:
        now = self.clock.now_ms()
        settings = self.settings
        if self.adapter is not None:
            connection = self.adapter.status().as_dict()
        else:
            connection = {
                "provider": settings.data_provider,
                "state": ConnectionState.NOT_CONFIGURED.value,
                "state_since_ms": self._started_ms,
            }
        streaming = connection["state"] == ConnectionState.STREAMING.value
        configured = settings.provider_configured

        instruments: list[dict[str, Any]] = []
        for rt in self._rt.values():
            inst = rt.inst
            lat = rt.latency.snapshot(now)
            quote = rt.sampler.last_quote
            last = rt.last_obs
            market_open = rt.calendar.is_open(now)
            gate = evaluate_signal_gate(
                provider_configured=configured,
                instrument_supported=rt.supported,
                market_open=market_open,
                feed_streaming=streaming,
                last_status=last.status if last is not None and now - last.ts_ms < 5 * SECOND_MS else None,
                clock_skew_suspected=lat.clock_skew_suspected,
            )
            change = None
            if quote is not None and rt.ref_mid:
                diff = quote.mid - rt.ref_mid
                change = {
                    "ref_mid": rt.ref_mid,
                    "ref_ts_ms": rt.ref_ts_ms,
                    "abs": diff,
                    "pips": round(inst.to_pips(diff), 1),
                    "pct": diff / rt.ref_mid * 100,
                }
            instruments.append(
                {
                    "symbol": inst.symbol,
                    "description": inst.description,
                    "pip_size": inst.pip_size,
                    "price_precision": inst.price_precision,
                    "provider_status": rt.provider_status,
                    "supported": rt.supported,
                    "market_open": market_open,
                    "next_open_ms": None if market_open else rt.calendar.next_open_ms(now),
                    "next_close_ms": rt.calendar.next_close_ms(now) if market_open and not self.synthetic else None,
                    "bid": quote.bid if quote else None,
                    "ask": quote.ask if quote else None,
                    "mid": quote.mid if quote else None,
                    "spread_pips": round(inst.to_pips(quote.spread), 2) if quote else None,
                    "quote_ts_ms": quote.source_ts_ms if quote else None,
                    "quote_age_ms": max(0, now - quote.source_ts_ms) if quote else None,
                    "status": last.status.value if last else None,
                    "last_obs_ts_ms": last.ts_ms if last else None,
                    "quotes_last_second": last.n_quotes if last else None,
                    "quotes_per_s_10s": rt.rate.rate(now, 10),
                    "quotes_per_s_60s": rt.rate.rate(now, 60),
                    "stale_after_ms": inst.data_quality.stale_after_ms,
                    "signal_gate": {"allowed": gate.signals_allowed, "reason": gate.reason.value},
                    "change": change,
                    "counters": self._normalizer.counters(inst.symbol),
                    "latency": lat.as_dict(),
                }
            )

        return {
            "server_time_ms": now,
            "started_ms": self._started_ms,
            "uptime_s": (now - self._started_ms) // 1000,
            "version": __version__,
            "provider": {
                "name": settings.data_provider,
                "label": self.provider_label(),
                "configured": configured,
                "synthetic": self.synthetic,
                "official_endpoint": settings.official_endpoints if not self.synthetic else False,
                "feed_host": None if self.synthetic else urlparse(settings.massive_ws_url).hostname,
                "rest_available": self.rest is not None,
            },
            "connection": connection,
            "sessions": active_sessions(now),
            "instruments": instruments,
            "throughput": {
                "quotes_per_s_1s": self._rate.rate(now, 1),
                "quotes_per_s_10s": self._rate.rate(now, 10),
                "quotes_per_s_60s": self._rate.rate(now, 60),
                "total_quotes": self._rate.total,
                "unknown_symbols": dict(self._normalizer.unknown_symbols),
            },
            "processing_us": self._proc.snapshot(),
            "storage": {
                **self.writer.stats(),
                "timescaledb": self.storage_policies.get("timescaledb", False),
                "policy_errors": self.storage_policies.get("errors", []),
                "policies_managed": settings.storage_policies_enabled,
                "persist_raw_ticks": settings.persist_raw_ticks,
            },
            "gaps": {
                "queued": self._gap_queue.qsize(),
                "processed": self._gaps_processed,
                "last_24h": self._gap_counts,
            },
            "live_clients": self.hub.subscriber_count,
            "errors": list(self._errors)[:15],
        }
