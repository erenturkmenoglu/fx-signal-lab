"""Batched, failure-tolerant persistence.

The ingest path must never wait for the database: a slow consumer is disconnected
by the provider.  Producers therefore only append rows to in-memory buffers; this
writer flushes them in one transaction per interval.

Failure behaviour (nothing is dropped silently):

* if a flush fails the rows stay buffered and are retried on the next cycle; rows
  carry their own timestamps, so a database outage shorter than the buffer loses
  nothing;
* if the buffer limit is exceeded the oldest raw ticks are discarded first, then the
  oldest observations (which leaves holes the gap detector will find); every dropped
  row is counted and logged;
* ``stats()`` exposes database health, buffer depth, flush timing and drop counts
  to the System Health panel.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.clock import Clock
from fxlab.logging import get_logger
from fxlab.storage import repo

log = get_logger(__name__)


class BatchWriter:
    def __init__(
        self,
        engine: AsyncEngine,
        clock: Clock,
        *,
        flush_interval_s: float = 1.0,
        max_buffer_rows: int = 500_000,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._flush_interval_s = flush_interval_s
        self._max_buffer_rows = max_buffer_rows
        self._ticks: deque[dict[str, Any]] = deque()
        self._obs: deque[dict[str, Any]] = deque()
        self._bars: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._events: deque[dict[str, Any]] = deque()
        self._stats: deque[dict[str, Any]] = deque()
        self._stop = asyncio.Event()
        self._flush_lock = asyncio.Lock()
        # health
        self.db_ok: bool | None = None
        self.last_error: str | None = None
        self.last_error_ms: int | None = None
        self.last_flush_ms: int | None = None
        self.last_flush_duration_ms: float | None = None
        self.consecutive_failures = 0
        self.written = {"ticks": 0, "obs": 0, "bars": 0, "events": 0, "stats": 0}
        self.dropped = {"ticks": 0, "obs": 0}

    # -- producers (synchronous, never block) ------------------------------------------
    def add_tick(self, row: dict[str, Any]) -> None:
        self._ticks.append(row)

    def add_observation(self, row: dict[str, Any]) -> None:
        self._obs.append(row)

    def add_bar(self, row: dict[str, Any]) -> None:
        # a bar may be emitted again (rebuild); only the latest version needs writing
        self._bars[(row["instrument_id"], row["timeframe"], row["source"], row["ts"])] = row

    def add_event(self, row: dict[str, Any]) -> None:
        self._events.append(row)

    def add_stats(self, row: dict[str, Any]) -> None:
        self._stats.append(row)

    @property
    def buffered(self) -> dict[str, int]:
        return {
            "ticks": len(self._ticks),
            "obs": len(self._obs),
            "bars": len(self._bars),
            "events": len(self._events),
            "stats": len(self._stats),
        }

    # -- lifecycle -------------------------------------------------------------------------
    async def run(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._flush_interval_s)
            await self.flush()

    async def stop(self) -> None:
        """Stop the loop and make a final attempt to persist what is buffered."""
        self._stop.set()
        await self.flush()

    async def flush(self) -> bool:
        """Write everything buffered in one transaction.  Returns True on success."""
        async with self._flush_lock:
            self._enforce_limit()
            ticks, obs = list(self._ticks), list(self._obs)
            bars = list(self._bars.values())
            events, stats = list(self._events), list(self._stats)
            if not (ticks or obs or bars or events or stats):
                return True
            started = time.perf_counter()
            try:
                async with self._engine.begin() as conn:
                    if ticks:
                        await repo.insert_ticks(conn, ticks)
                    if obs:
                        await repo.upsert_observations(conn, obs, replace="never")
                    if bars:
                        await repo.upsert_bars(conn, bars)
                    if events:
                        await repo.insert_events(conn, events)
                    if stats:
                        await repo.upsert_ingest_stats(conn, stats)
            except Exception as exc:
                self.db_ok = False
                self.consecutive_failures += 1
                self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
                self.last_error_ms = self._clock.now_ms()
                log.error(
                    "db_flush_failed",
                    error=self.last_error,
                    failures=self.consecutive_failures,
                    buffered=self.buffered,
                )
                return False

            # success: remove exactly what was written (producers may have appended meanwhile)
            for _ in range(len(ticks)):
                self._ticks.popleft()
            for _ in range(len(obs)):
                self._obs.popleft()
            for row in bars:
                key = (row["instrument_id"], row["timeframe"], row["source"], row["ts"])
                if self._bars.get(key) is row:
                    del self._bars[key]
            for _ in range(len(events)):
                self._events.popleft()
            for _ in range(len(stats)):
                self._stats.popleft()

            self.written["ticks"] += len(ticks)
            self.written["obs"] += len(obs)
            self.written["bars"] += len(bars)
            self.written["events"] += len(events)
            self.written["stats"] += len(stats)
            if self.db_ok is False:
                log.info("db_recovered", after_failures=self.consecutive_failures)
            self.db_ok = True
            self.consecutive_failures = 0
            self.last_flush_ms = self._clock.now_ms()
            self.last_flush_duration_ms = (time.perf_counter() - started) * 1000
            return True

    def _enforce_limit(self) -> None:
        excess = len(self._ticks) + len(self._obs) - self._max_buffer_rows
        if excess <= 0:
            return
        drop_ticks = min(excess, len(self._ticks))
        for _ in range(drop_ticks):
            self._ticks.popleft()
        drop_obs = min(excess - drop_ticks, len(self._obs))
        for _ in range(drop_obs):
            self._obs.popleft()
        self.dropped["ticks"] += drop_ticks
        self.dropped["obs"] += drop_obs
        log.error(
            "write_buffer_overflow",
            dropped_ticks=drop_ticks,
            dropped_obs=drop_obs,
            limit=self._max_buffer_rows,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "db_ok": self.db_ok,
            "buffered": self.buffered,
            "written": dict(self.written),
            "dropped": dict(self.dropped),
            "last_flush_ms": self.last_flush_ms,
            "last_flush_duration_ms": self.last_flush_duration_ms,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "last_error_ms": self.last_error_ms,
        }
