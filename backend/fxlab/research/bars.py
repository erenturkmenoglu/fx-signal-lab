"""Bar series for research: a few numpy arrays, resampling, loading from the database.

Prices are **bid** prices: Massive builds its historical FX bars from the bid side only.
The backtest engine adds the spread itself where a trade happens on the ask.

A series may have holes (seconds or minutes without a quote have no bar).  Everything here
works on timestamps, never on positions, so a hole is simply a longer step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from zoneinfo import ZoneInfo

import numpy as np
import numpy.typing as npt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.storage import schema as s
from fxlab.timeutil import SECOND_MS, dt_to_ms, ms_to_dt

Floats = npt.NDArray[np.float64]
Ints = npt.NDArray[np.int64]

#: source and basis of the provider's historical bars in the ``bars`` table
PROVIDER_SOURCE = "massive_agg"


@dataclass(frozen=True)
class Bars:
    """OHLC bars, oldest first.  ``ts`` is the start of each bar in epoch milliseconds (UTC)."""

    ts: Ints
    open: Floats
    high: Floats
    low: Floats
    close: Floats
    seconds: int

    def __post_init__(self) -> None:
        n = len(self.ts)
        if not (len(self.open) == len(self.high) == len(self.low) == len(self.close) == n):
            raise ValueError("bar arrays differ in length")
        if n > 1 and not bool(np.all(np.diff(self.ts) > 0)):
            raise ValueError("bars must be strictly ascending in time")
        if self.seconds < 1:
            raise ValueError("a bar lasts at least one second")

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def span_ms(self) -> int:
        return self.seconds * SECOND_MS

    @classmethod
    def from_rows(cls, rows: list[tuple[int, float, float, float, float]], seconds: int) -> Bars:
        """Rows of (start ms, open, high, low, close)."""
        if not rows:
            empty = np.empty(0, dtype=np.float64)
            return cls(np.empty(0, dtype=np.int64), empty, empty.copy(), empty.copy(), empty.copy(), seconds)
        data = np.asarray(rows, dtype=np.float64)
        return cls(
            np.asarray([r[0] for r in rows], dtype=np.int64),
            data[:, 1].copy(),
            data[:, 2].copy(),
            data[:, 3].copy(),
            data[:, 4].copy(),
            seconds,
        )


_OFFSET_STEP_MS = 15 * 60 * SECOND_MS  # every time zone changes its offset on a quarter hour of UTC


def utc_offsets_ms(ts: Ints, tz: str) -> Ints:
    """The offset of ``tz`` from UTC at each instant (daylight saving included), in milliseconds.

    ``ts + utc_offsets_ms(ts, tz)`` is the wall clock of that zone, as milliseconds since an
    epoch that counts local days.
    """
    zone = ZoneInfo(tz)
    slots, inverse = np.unique(ts // _OFFSET_STEP_MS, return_inverse=True)
    offsets = np.empty(len(slots), dtype=np.int64)
    for k, slot in enumerate(slots):
        offset = ms_to_dt(int(slot) * _OFFSET_STEP_MS).astimezone(zone).utcoffset()
        offsets[k] = (offset or timedelta(0)) // timedelta(milliseconds=1)
    return offsets[inverse]


def resample(base: Bars, seconds: int) -> Bars:
    """Aggregate to longer bars aligned to the UTC clock.  Buckets without a base bar do not exist."""
    if seconds % base.seconds != 0:
        raise ValueError(f"{seconds} s bars cannot be built from {base.seconds} s bars")
    if seconds == base.seconds or len(base) == 0:
        return Bars(base.ts, base.open, base.high, base.low, base.close, seconds)
    span = seconds * SECOND_MS
    bucket = base.ts - base.ts % span
    starts = np.flatnonzero(np.concatenate(([True], bucket[1:] != bucket[:-1])))
    ends = np.concatenate((starts[1:], [len(base)])) - 1
    return Bars(
        bucket[starts],
        base.open[starts],
        np.maximum.reduceat(base.high, starts),
        np.minimum.reduceat(base.low, starts),
        base.close[ends],
        seconds,
    )


async def load_provider_bars(
    engine: AsyncEngine, instrument_id: int, timeframe: str, seconds: int, start_ms: int, end_ms: int
) -> Bars:
    """The provider's historical bars of one instrument in ``[start_ms, end_ms)``, straight into arrays.

    Rows are streamed in batches: a few months of second bars are millions of rows, far too many
    to hold as Python objects on a small server.
    """
    b = s.bars
    query = (
        select(b.c.ts, b.c.open, b.c.high, b.c.low, b.c.close)
        .where(
            b.c.instrument_id == instrument_id,
            b.c.timeframe == timeframe,
            b.c.source == PROVIDER_SOURCE,
            b.c.ts >= ms_to_dt(start_ms),
            b.c.ts < ms_to_dt(end_ms),
        )
        .order_by(b.c.ts)
    )
    stamps: list[Ints] = []
    prices: list[Floats] = []
    async with engine.connect() as conn:
        result = await conn.stream(query)
        async for batch in result.partitions(50_000):
            stamps.append(np.fromiter((dt_to_ms(r[0]) for r in batch), dtype=np.int64, count=len(batch)))
            prices.append(np.asarray([r[1:] for r in batch], dtype=np.float64))
    if not stamps:
        return Bars.from_rows([], seconds)
    ts = np.concatenate(stamps)
    ohlc = np.concatenate(prices)
    return Bars(ts, ohlc[:, 0].copy(), ohlc[:, 1].copy(), ohlc[:, 2].copy(), ohlc[:, 3].copy(), seconds)
