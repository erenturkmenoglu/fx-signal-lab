"""Clock abstraction so that time-dependent logic is deterministic in tests and replays."""

from __future__ import annotations

from typing import Protocol

from fxlab.timeutil import now_ms


class Clock(Protocol):
    def now_ms(self) -> int: ...


class SystemClock:
    """Wall clock (UTC epoch milliseconds)."""

    def now_ms(self) -> int:
        return now_ms()


class ManualClock:
    """A clock that only moves when told to. Used by tests and historical replay."""

    def __init__(self, start_ms: int = 0) -> None:
        self._now = start_ms

    def now_ms(self) -> int:
        return self._now

    def set(self, ts_ms: int) -> None:
        self._now = ts_ms

    def advance(self, delta_ms: int) -> int:
        self._now += delta_ms
        return self._now
