"""Time helpers.

Convention used across the whole pipeline
-----------------------------------------
* Inside the pipeline every instant is an ``int`` of **epoch milliseconds in UTC**
  (``*_ms``).  Integers cannot be naive/aware-confused, are exact and cheap.
* ``datetime`` objects (always timezone-aware, UTC) appear only at the edges:
  database rows and API payloads.
* Every interval is **labelled by its start** and is half-open ``[start, end)``.
  A 1-second observation with ``ts = 12:00:00`` describes ``[12:00:00, 12:00:01)``
  and only becomes known at ``12:00:01``.  Bars follow the same rule, which is
  also the convention of the provider's aggregates.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

SECOND_MS = 1_000
MINUTE_MS = 60 * SECOND_MS
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def now_ms() -> int:
    """Current wall-clock time in epoch milliseconds (UTC)."""
    return time.time_ns() // 1_000_000


def ms_to_dt(ms: int) -> datetime:
    """Exact conversion of epoch milliseconds to an aware UTC datetime."""
    return _EPOCH + timedelta(milliseconds=ms)


def dt_to_ms(dt: datetime) -> int:
    """Exact conversion of an aware datetime to epoch milliseconds (floors sub-ms)."""
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed; attach a timezone")
    delta = dt - _EPOCH
    return delta.days * DAY_MS + delta.seconds * SECOND_MS + delta.microseconds // 1_000


def floor_ms(ts_ms: int, step_ms: int) -> int:
    """Floor ``ts_ms`` to a multiple of ``step_ms`` since the epoch."""
    return ts_ms - (ts_ms % step_ms)


def iso(ms: int | None) -> str | None:
    """ISO-8601 UTC string with millisecond precision, or ``None``."""
    if ms is None:
        return None
    return ms_to_dt(ms).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str | int | float | datetime) -> int:
    """Parse user input (ISO string, epoch seconds/ms, datetime) into epoch ms.

    Numbers below 10^11 are treated as epoch seconds, larger ones as milliseconds.
    Naive ISO strings are interpreted as UTC.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return dt_to_ms(value)
    if isinstance(value, (int, float)):
        return int(value * 1000) if abs(value) < 1e11 else int(value)
    text = value.strip()
    if text.lstrip("-").isdigit():
        return parse_time(int(text))
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt_to_ms(dt)
