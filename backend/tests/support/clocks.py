from __future__ import annotations

import time


class OffsetClock:
    """Real time shifted so that it starts at a chosen instant.

    Lets real-time integration tests (sockets, asyncio timers) run "on a Thursday afternoon"
    or "on a Saturday" regardless of the day the test-suite is executed.
    """

    def __init__(self, start_ms: int) -> None:
        self._offset = start_ms - time.time_ns() // 1_000_000

    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000 + self._offset
