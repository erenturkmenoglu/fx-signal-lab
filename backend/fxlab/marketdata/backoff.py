"""Reconnect back-off policy."""

from __future__ import annotations

import random


class ExponentialBackoff:
    """Exponential back-off with "equal jitter".

    Delay ``n`` is drawn uniformly from ``[d/2, d]`` with ``d = min(max_s, base_s * factor**n)``.
    The jitter avoids synchronised reconnect storms; the lower half-bound guarantees the
    delay really grows.
    """

    def __init__(
        self,
        base_s: float = 1.0,
        factor: float = 2.0,
        max_s: float = 60.0,
        rng: random.Random | None = None,
    ) -> None:
        if base_s <= 0 or factor < 1 or max_s < base_s:
            raise ValueError("invalid back-off parameters")
        self.base_s = base_s
        self.factor = factor
        self.max_s = max_s
        self._rng = rng or random.Random()
        self._attempt = 0

    @property
    def attempt(self) -> int:
        """Number of delays handed out since the last reset."""
        return self._attempt

    def next_delay(self) -> float:
        ceiling = min(self.max_s, self.base_s * self.factor**self._attempt)
        self._attempt += 1
        return ceiling / 2 + self._rng.uniform(0, ceiling / 2)

    def reset(self) -> None:
        self._attempt = 0
