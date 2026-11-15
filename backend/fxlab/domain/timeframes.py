"""Supported timeframes and bar-window alignment.

* Timeframes up to one hour are aligned to the UTC epoch (``12:00:00, 12:05:00 ...``).
* ``4h`` and ``1d`` are anchored to the instrument's daily rollover (17:00 New York for
  spot FX) so that a daily bar is one FX trading day and a week has five of them.
  The provider's own daily aggregates follow the UTC day instead; they are stored
  under a different ``source`` and must not be confused with these.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from fxlab.domain.calendar import MarketCalendar
from fxlab.timeutil import floor_ms


@dataclass(frozen=True)
class Timeframe:
    code: str
    seconds: int
    anchor: Literal["utc", "rollover"] = "utc"

    @property
    def ms(self) -> int:
        return self.seconds * 1000


_ALL = [
    Timeframe("1s", 1),
    Timeframe("5s", 5),
    Timeframe("15s", 15),
    Timeframe("30s", 30),
    Timeframe("1m", 60),
    Timeframe("5m", 300),
    Timeframe("15m", 900),
    Timeframe("30m", 1800),
    Timeframe("1h", 3600),
    Timeframe("4h", 4 * 3600, "rollover"),
    Timeframe("1d", 24 * 3600, "rollover"),
]

TIMEFRAMES: dict[str, Timeframe] = {tf.code: tf for tf in _ALL}

#: timeframes materialised as bars.  ``1s`` is the observation grid itself.
BAR_TIMEFRAMES: tuple[Timeframe, ...] = tuple(tf for tf in _ALL if tf.code != "1s")


def get_timeframe(code: str) -> Timeframe:
    try:
        return TIMEFRAMES[code]
    except KeyError:
        raise ValueError(f"unsupported timeframe {code!r}; supported: {', '.join(TIMEFRAMES)}") from None


def bar_window(ts_ms: int, tf: Timeframe, calendar: MarketCalendar) -> tuple[int, int]:
    """Return the half-open bar window ``[start, end)`` that contains ``ts_ms``."""
    if tf.anchor == "utc":
        start = floor_ms(ts_ms, tf.ms)
        return start, start + tf.ms
    day_start, day_end = calendar.rollover_window(ts_ms)
    start = day_start + ((ts_ms - day_start) // tf.ms) * tf.ms
    return start, min(start + tf.ms, day_end)
