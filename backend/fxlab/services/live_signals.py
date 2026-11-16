"""Live signals for the dashboard: the seconds of the trading day in memory, the rule on them,
the trend channels.

One :class:`LiveSignals` belongs to the ingestion service.  For an instrument that somebody
looks at it keeps the seconds of the trading day on the one-second grid (and some before it,
so that the indicators are warm when the day starts), the signals of the day, and a stepper
that takes the rule on as each new second arrives.  A signal raised by a new second is
published to the dashboard at once.

What is in memory is what was seen: the seconds as the ingestion handed them over, and for
each whether the data-quality gate allowed signals at that moment.  The memory is filled from
the database when an instrument is first asked for, at the start of a new trading day and
after a repaired gap.  The gate of past seconds is not stored there, so after such a reload a
second counts by its own quality alone: fresh or carried, and inside market hours.

Nothing here sends an order.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import numpy.typing as npt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.clock import Clock
from fxlab.domain.calendar import WeeklyCalendar
from fxlab.domain.instruments import Instrument
from fxlab.domain.models import Observation
from fxlab.logging import get_logger
from fxlab.pipeline.hub import LiveHub
from fxlab.research.bars import PROVIDER_SOURCE
from fxlab.research.live import LiveError, LiveSettings, Signal, Stepper, channel, replay
from fxlab.research.live import describe as describe_settings
from fxlab.research.live import payload as signal_payload
from fxlab.storage import repo
from fxlab.storage import schema as s
from fxlab.timeutil import DAY_MS, MINUTE_MS, SECOND_MS, dt_to_ms, ms_to_dt

log = get_logger(__name__)

Floats = npt.NDArray[np.float64]
Ints = npt.NDArray[np.int64]
Bools = npt.NDArray[np.bool_]

#: where the settings are kept (``app_meta``)
SETTINGS_KEY = "live_signals"
#: seconds before the start of the day that are kept to warm the indicators up: at least this many bars ...
MIN_WARMUP_BARS = 600
#: ... and never more than this many seconds
MAX_WARMUP_S = 6 * 3600
CHANNEL_NAMES = ("short", "medium", "long")
USABLE = ("fresh", "carried")


def warmup_seconds(settings: LiveSettings) -> int:
    """Seconds kept before the start of the trading day, a whole number of minutes."""
    bars = max(MIN_WARMUP_BARS, 20 * settings.first_signal_at)
    return min(MAX_WARMUP_S, math.ceil(bars * settings.bar_seconds / 60) * 60)


@dataclass(frozen=True)
class Feed:
    """What the ingestion knows about one instrument and the live signals need."""

    inst: Instrument
    instrument_id: int
    calendar: WeeklyCalendar
    #: the newest seconds, some of them not in the database yet
    recent: Iterable[Observation]


@dataclass
class _Day:
    """The seconds of one trading day on the grid; position 0 is ``base_ms``, before the day starts."""

    start_ms: int
    end_ms: int
    base_ms: int
    bid: Floats
    ask: Floats
    seen: Bools  # a second was stored at this position
    priced: Bools  # it carries a price fit to be drawn (fresh or carried)
    usable: Bools  # and that price may feed signals (market open, gate allowed)
    stepper: Stepper
    count: int = 0  # one past the newest stored second
    signals: list[Signal] = field(default_factory=list)
    #: one-minute bid closes before ``base_ms`` from the database: (bar start, close)
    older: tuple[Ints, Floats] | None = None

    @property
    def first(self) -> int:
        """Position of the first second of the day."""
        return (self.start_ms - self.base_ms) // SECOND_MS

    def stored(self) -> tuple[Ints, Floats, Floats, Bools]:
        """The stored seconds in time order: (start of the second, bid, ask, usable)."""
        at = np.flatnonzero(self.seen[: self.count])
        return self.base_ms + at * SECOND_MS, self.bid[at], self.ask[at], self.usable[at]


class LiveSignals:
    def __init__(self, engine: AsyncEngine, clock: Clock, hub: LiveHub) -> None:
        self._engine = engine
        self._clock = clock
        self._hub = hub
        self._settings: LiveSettings | None = None
        self._days: dict[str, _Day] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._pending: dict[str, list[tuple[Observation, bool]]] = {}
        self._version = 0  # goes up with every change of the settings
        self.raised = 0  # signals published since the start

    # ---------------------------------------------------------------------------- settings
    async def settings(self) -> LiveSettings:
        if self._settings is None:
            async with self._engine.connect() as conn:
                stored = await repo.get_meta(conn, SETTINGS_KEY)
            try:
                self._settings = LiveSettings.from_mapping((stored or {}).get("settings", {}))
            except LiveError as exc:  # written by another version: say so and go on with the defaults
                log.warning("live_settings_unusable", error=str(exc))
                self._settings = LiveSettings()
        return self._settings

    async def describe(self) -> dict[str, Any]:
        return {"settings": (await self.settings()).as_dict(), **describe_settings()}

    async def save_settings(self, data: Any) -> LiveSettings:
        """Keep new settings and work the day out again with them.  What is left out stays as it is."""
        if not isinstance(data, dict):
            raise LiveError("the settings must be an object of names and values")
        current = await self.settings()
        settings = LiveSettings.from_mapping({**current.as_dict(), **data})
        async with self._engine.begin() as conn:
            await repo.set_meta(conn, SETTINGS_KEY, {"settings": settings.as_dict(), "saved_ms": self._clock.now_ms()})
        self._settings = settings
        self._version += 1
        self._days.clear()  # the warm-up may have another length now: read again when next asked
        self._hub.publish({"type": "resync"})  # every open page asks again
        log.info("live_settings_saved", **settings.as_dict())
        return settings

    # ------------------------------------------------------------------------ from the feed
    def watching(self, symbol: str) -> bool:
        """True when new seconds of this instrument are wanted (somebody has asked for it)."""
        return symbol in self._days or symbol in self._pending

    def on_observation(self, feed: Feed, obs: Observation, allowed: bool) -> None:
        """A new second from the ingestion.  ``allowed``: the data-quality gate at this moment."""
        symbol = feed.inst.symbol
        pending = self._pending.get(symbol)
        if pending is not None:  # the day is being read from the database: taken in afterwards
            pending.append((obs, allowed))
            return
        day = self._days.get(symbol)
        if day is not None:
            self._days[symbol] = self._take(feed, day, obs, allowed, publish=True)

    def invalidate(self, symbol: str) -> None:
        """Stored seconds were replaced (a repaired gap): read the day again when next asked."""
        self._days.pop(symbol, None)

    def _take(self, feed: Feed, day: _Day, obs: Observation, allowed: bool, *, publish: bool) -> _Day:
        if obs.ts_ms >= day.end_ms:
            day = self._next_day(feed, day, obs.ts_ms)
        at = (obs.ts_ms - day.base_ms) // SECOND_MS
        if at < day.count or at >= len(day.seen):
            return day  # older than what is stored, or not of this day
        priced = obs.status.usable and obs.bid is not None and obs.ask is not None
        usable = priced and allowed and feed.calendar.is_open(obs.ts_ms)
        bid, ask = (obs.bid or 0.0, obs.ask or 0.0) if priced else (math.nan, math.nan)
        day.seen[at], day.priced[at], day.usable[at], day.bid[at], day.ask[at] = True, priced, usable, bid, ask
        day.count = at + 1
        for signal in day.stepper.push(obs.ts_ms, bid, ask, usable):
            if signal.ts_ms < day.start_ms:
                continue
            day.signals.append(signal)
            if publish:
                self.raised += 1
                shown = signal_payload(signal, feed.inst.price_precision)
                self._hub.publish({"type": "signal", "symbol": feed.inst.symbol, "signal": shown})
        return day

    # ------------------------------------------------------------------------------ the day
    @staticmethod
    def window(calendar: WeeklyCalendar, now_ms: int) -> tuple[int, int]:
        """The trading day to show: today's, or the last one that traded while the market is closed."""
        if calendar.is_open(now_ms):
            return calendar.rollover_window(now_ms)
        traded = calendar.open_intervals(now_ms - 7 * DAY_MS, now_ms)
        return calendar.rollover_window(traded[-1][1] - 1) if traded else calendar.rollover_window(now_ms)

    def _empty(self, settings: LiveSettings, start: int, end: int) -> _Day:
        base = start - warmup_seconds(settings) * SECOND_MS
        size = (end - base) // SECOND_MS
        return _Day(
            start_ms=start,
            end_ms=end,
            base_ms=base,
            bid=np.full(size, np.nan),
            ask=np.full(size, np.nan),
            seen=np.zeros(size, dtype=bool),
            priced=np.zeros(size, dtype=bool),
            usable=np.zeros(size, dtype=bool),
            stepper=Stepper(settings),
        )

    @staticmethod
    def _work(day: _Day) -> None:
        """The signals of the day and the stepper that goes on from its newest second."""
        signals, day.stepper = replay(*day.stored(), day.stepper.settings)
        day.signals = [signal for signal in signals if signal.ts_ms >= day.start_ms]

    def _next_day(self, feed: Feed, old: _Day, ts_ms: int) -> _Day:
        """A second of a later trading day arrived: start that day, with the newest seconds as its warm-up."""
        day = self._empty(old.stepper.settings, *feed.calendar.rollover_window(ts_ms))
        skip = (day.base_ms - old.base_ms) // SECOND_MS
        if 0 <= skip < old.count:
            n = old.count - skip
            for name in ("bid", "ask", "seen", "priced", "usable"):
                getattr(day, name)[:n] = getattr(old, name)[skip : old.count]
            day.count = n
        self._work(day)
        return day

    async def _load(self, feed: Feed) -> _Day:
        settings = await self.settings()
        day = self._empty(settings, *self.window(feed.calendar, self._clock.now_ms()))
        base, end = day.base_ms, day.end_ms
        t = s.obs_1s
        query = (
            select(t.c.ts, t.c.status, t.c.bid, t.c.ask)
            .where(t.c.instrument_id == feed.instrument_id, t.c.ts >= ms_to_dt(base), t.c.ts < ms_to_dt(end))
            .order_by(t.c.ts)
        )
        async with self._engine.connect() as conn:
            result = await conn.stream(query)
            async for batch in result.partitions(20_000):
                at = np.fromiter(
                    ((dt_to_ms(r[0]) - base) // SECOND_MS for r in batch), dtype=np.int64, count=len(batch)
                )
                priced = np.fromiter(
                    (r[1] in USABLE and r[2] is not None and r[3] is not None for r in batch),
                    dtype=bool,
                    count=len(batch),
                )
                day.seen[at] = True
                day.priced[at] = priced
                day.bid[at[priced]] = [r[2] for r, ok in zip(batch, priced.tolist(), strict=True) if ok]
                day.ask[at[priced]] = [r[3] for r, ok in zip(batch, priced.tolist(), strict=True) if ok]
        for obs in list(feed.recent):  # the newest seconds may not be in the database yet
            if base <= obs.ts_ms < end:
                at_one = (obs.ts_ms - base) // SECOND_MS
                ok = obs.status.usable and obs.bid is not None and obs.ask is not None
                day.seen[at_one], day.priced[at_one] = True, ok
                day.bid[at_one], day.ask[at_one] = (obs.bid or 0.0, obs.ask or 0.0) if ok else (math.nan, math.nan)
        open_now = np.zeros(len(day.seen), dtype=bool)
        for a, b in feed.calendar.open_intervals(base, end):
            open_now[(a - base) // SECOND_MS : -((base - b) // SECOND_MS)] = True
        day.usable = day.priced & open_now
        stored = np.flatnonzero(day.seen)
        day.count = int(stored[-1]) + 1 if len(stored) else 0
        self._work(day)
        log.info(
            "live_day_loaded",
            symbol=feed.inst.symbol,
            seconds=len(stored),
            signals=len(day.signals),
            warmup_s=(day.start_ms - base) // SECOND_MS,
        )
        return day

    async def _day(self, feed: Feed) -> _Day:
        symbol = feed.inst.symbol
        async with self._locks.setdefault(symbol, asyncio.Lock()):
            while True:
                day = self._days.get(symbol)
                if day is not None:
                    return day
                version = self._version
                self._pending[symbol] = []
                try:
                    day = await self._load(feed)
                except BaseException:
                    self._pending.pop(symbol, None)
                    raise
                # the seconds that arrived while the database was read (no await from here on)
                for obs, allowed in self._pending.pop(symbol):
                    day = self._take(feed, day, obs, allowed, publish=False)
                if version == self._version:  # else the settings changed meanwhile: read again
                    self._days[symbol] = day
                    return day

    # ------------------------------------------------------------------------------ minutes
    @staticmethod
    def _minutes_in_memory(day: _Day) -> tuple[Ints, Floats]:
        """The completed minutes of the stored seconds: bid at the last priced second of each."""
        whole = day.count // 60
        if not whole:
            return np.empty(0, dtype=np.int64), np.empty(0)
        priced = day.priced[: whole * 60].reshape(whole, 60)
        last = 59 - np.argmax(priced[:, ::-1], axis=1)
        has = priced.any(axis=1)
        rows = np.flatnonzero(has)
        close = day.bid[: whole * 60].reshape(whole, 60)[rows, last[rows]]
        return day.base_ms + rows * MINUTE_MS, close

    async def _older_minutes(self, feed: Feed, day: _Day, bars: int) -> tuple[Ints, Floats]:
        """One-minute bid closes before the seconds in memory, from the stored bars.

        The provider's bars are bid bars; the bars this system builds are mid bars that carry
        their bid close.  Where both exist for a minute the provider's is taken.
        """
        if day.older is None:
            calendar_days = math.ceil(bars / 1440 * 7 / 5) + 3  # weekends, and a margin
            b = s.bars
            query = (
                select(b.c.ts, b.c.source, b.c.close, b.c.bid_close)
                .where(
                    b.c.instrument_id == feed.instrument_id,
                    b.c.timeframe == "1m",
                    b.c.ts >= ms_to_dt(day.base_ms - calendar_days * DAY_MS),
                    b.c.ts < ms_to_dt(day.base_ms),
                )
                .order_by(b.c.ts)
            )
            values: dict[int, float] = {}
            async with self._engine.connect() as conn:
                result = await conn.stream(query)
                async for batch in result.partitions(20_000):
                    for ts, source, close, bid_close in batch:
                        minute = dt_to_ms(ts)
                        if source == PROVIDER_SOURCE:
                            values[minute] = close
                        elif bid_close is not None and minute not in values:
                            values[minute] = bid_close
            stamps = np.fromiter(sorted(values), dtype=np.int64, count=len(values))
            day.older = (stamps, np.fromiter((values[int(m)] for m in stamps), dtype=np.float64, count=len(values)))
        return day.older

    async def _minutes(self, feed: Feed, day: _Day) -> tuple[Ints, Floats]:
        """One-minute bid closes up to the newest completed minute, oldest first."""
        bars = day.stepper.settings.channel_bars()[2]
        old_ts, old_close = await self._older_minutes(feed, day, bars)
        new_ts, new_close = self._minutes_in_memory(day)
        return np.concatenate((old_ts, new_ts)), np.concatenate((old_close, new_close))

    @staticmethod
    def _channels(day: _Day, ts: Ints, close: Floats) -> list[dict[str, Any]]:
        settings = day.stepper.settings
        out: list[dict[str, Any]] = []
        for name, bars in zip(CHANNEL_NAMES, settings.channel_bars(), strict=True):
            found = channel(ts, close, bars, settings.channel_width, day.start_ms)
            out.append({"name": name, "asked": bars, "channel": found})
        return out

    # ----------------------------------------------------------------------------- answers
    async def channels(self, feed: Feed) -> dict[str, Any]:
        """The three trend channels as they stand now (asked for again every minute)."""
        day = await self._day(feed)
        ts, close = await self._minutes(feed, day)
        return {
            "symbol": feed.inst.symbol,
            "bar_ms": MINUTE_MS,
            "minutes": len(ts),
            "channels": self._channels(day, ts, close),
        }

    async def view(self, feed: Feed, *, history: bool = False, seconds: bool = True) -> dict[str, Any]:
        """Everything the page shows: the seconds of the day, its signals, the channels, the settings.

        ``history`` adds the one-minute closes the channels were fitted to.  A page that draws
        those has no use for the day's seconds and leaves them out with ``seconds=False``.
        """
        day = await self._day(feed)
        settings = day.stepper.settings
        precision = feed.inst.price_precision
        first, count = day.first, max(day.first, day.count)
        bid = (
            [
                round(value, precision) if ok else None
                for value, ok in zip(day.bid[first:count].tolist(), day.priced[first:count].tolist(), strict=True)
            ]
            if seconds
            else []
        )
        ts, close = await self._minutes(feed, day)
        answer: dict[str, Any] = {
            "symbol": feed.inst.symbol,
            "price_precision": precision,
            "pip_size": feed.inst.pip_size,
            "day": {"start": day.start_ms, "end": day.end_ms},
            "newest": day.base_ms + (day.count - 1) * SECOND_MS if day.count > day.first else None,
            "warmup_s": (day.start_ms - day.base_ms) // SECOND_MS,
            "settings": settings.as_dict(),
            "seconds": {"start": day.start_ms, "bid": bid},
            "signals": [signal_payload(signal, precision) for signal in day.signals],
            "bar_ms": MINUTE_MS,
            "minutes": len(ts),
            "channels": self._channels(day, ts, close),
            "history": None,
        }
        if history:
            keep = settings.channel_bars()[2]
            answer["history"] = {
                "t": ts[-keep:].tolist(),
                "bid": [round(value, precision) for value in close[-keep:].tolist()],
            }
        return answer
