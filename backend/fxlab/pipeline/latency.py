"""Latency, throughput and processing-time statistics.

Feed latency is ``local receipt time - provider source timestamp``.  That difference
is only meaningful when (a) the local clock is synchronised and (b) the provider
timestamps are fine-grained.  Both conditions are checked from the data itself and the
result says so explicitly instead of presenting misleading numbers:

* if (nearly) all source timestamps fall on whole seconds, the provider is stamping
  with 1-second resolution and every figure is an *upper bound* (the true latency is
  up to 1 000 ms lower);
* if a relevant share of samples is negative, the local clock is behind the provider's
  and the figures are not meaningful until the clock is fixed.

Zero latency is never reported: with no samples there is simply no figure.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from fxlab.timeutil import SECOND_MS


def percentile(sorted_values: Sequence[float], q: float) -> float:
    """Percentile ``q`` (0-100) of an ascending sequence using linear interpolation."""
    n = len(sorted_values)
    if n == 0:
        raise ValueError("percentile of empty sequence")
    if n == 1:
        return float(sorted_values[0])
    pos = (q / 100.0) * (n - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return float(sorted_values[lo])
    frac = pos - lo
    return float(sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac)


@dataclass(frozen=True)
class LatencySnapshot:
    n: int
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    min_ms: float | None
    max_ms: float | None
    mean_ms: float | None
    #: detected granularity of the provider's source timestamps (1 or 1000 ms)
    resolution_ms: int
    negative_share: float
    clock_skew_suspected: bool
    #: False when the percentiles should not be trusted (see ``note``)
    meaningful: bool
    note: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LatencyTracker:
    """Rolling-window feed latency statistics for one instrument (or the whole feed)."""

    MIN_SAMPLES = 20
    MIN_SAMPLES_FOR_RESOLUTION = 50
    WHOLE_SECOND_SHARE = 0.98
    NEGATIVE_SHARE_LIMIT = 0.05

    def __init__(self, window_ms: int = 300_000, max_samples: int = 20_000) -> None:
        self._window_ms = window_ms
        self._samples: deque[tuple[int, int, bool]] = deque(maxlen=max_samples)

    def add(self, recv_ts_ms: int, source_ts_ms: int) -> None:
        self._samples.append((recv_ts_ms, recv_ts_ms - source_ts_ms, source_ts_ms % SECOND_MS == 0))

    def _evict(self, now_ms: int) -> None:
        cutoff = now_ms - self._window_ms
        samples = self._samples
        while samples and samples[0][0] < cutoff:
            samples.popleft()

    def snapshot(self, now_ms: int) -> LatencySnapshot:
        self._evict(now_ms)
        n = len(self._samples)
        if n == 0:
            return LatencySnapshot(0, None, None, None, None, None, None, 1, 0.0, False, False, "no samples")

        values = sorted(s[1] for s in self._samples)
        whole_seconds = sum(1 for s in self._samples if s[2])
        negatives = sum(1 for v in values if v < 0)
        negative_share = negatives / n

        resolution = 1
        if n >= self.MIN_SAMPLES_FOR_RESOLUTION and whole_seconds / n >= self.WHOLE_SECOND_SHARE:
            resolution = SECOND_MS

        p50 = percentile(values, 50)
        skew = negative_share > self.NEGATIVE_SHARE_LIMIT or p50 < 0
        meaningful = True
        note: str | None = None
        if n < self.MIN_SAMPLES:
            meaningful, note = False, f"only {n} samples in window"
        elif skew:
            meaningful = False
            note = (
                "negative latency: the local clock is behind the provider's clock; "
                "synchronise NTP before trusting latency or data-age figures"
            )
        elif resolution == SECOND_MS:
            note = (
                "provider source timestamps have 1 s resolution: figures are upper bounds, "
                "true latency is up to 1000 ms lower"
            )
        elif p50 < 1:
            # never present this as "0 ms": it is the floor of the measurement, not of the latency
            note = (
                "below the 1 ms resolution of the timestamps: too small to measure, which is not "
                "zero (a local clock that runs slightly behind the provider's hides latency)"
            )

        return LatencySnapshot(
            n=n,
            p50_ms=p50,
            p95_ms=percentile(values, 95),
            p99_ms=percentile(values, 99),
            min_ms=float(values[0]),
            max_ms=float(values[-1]),
            mean_ms=sum(values) / n,
            resolution_ms=resolution,
            negative_share=negative_share,
            clock_skew_suspected=skew,
            meaningful=meaningful,
            note=note,
        )


class DurationStats:
    """Rolling sample of durations (e.g. per-message processing time in microseconds)."""

    def __init__(self, max_samples: int = 5_000) -> None:
        self._samples: deque[float] = deque(maxlen=max_samples)

    def add(self, value: float) -> None:
        self._samples.append(value)

    def snapshot(self) -> dict[str, float | int | None]:
        n = len(self._samples)
        if n == 0:
            return {"n": 0, "p50": None, "p95": None, "p99": None, "max": None}
        values = sorted(self._samples)
        return {
            "n": n,
            "p50": percentile(values, 50),
            "p95": percentile(values, 95),
            "p99": percentile(values, 99),
            "max": values[-1],
        }


class RateCounter:
    """Counts events in 1-second buckets and reports rates over trailing windows."""

    def __init__(self, horizon_s: int = 120) -> None:
        self._horizon_s = horizon_s
        self._buckets: deque[tuple[int, int]] = deque()  # (epoch second, count)
        self.total = 0

    def add(self, ts_ms: int, count: int = 1) -> None:
        sec = ts_ms // SECOND_MS
        self.total += count
        if self._buckets and self._buckets[-1][0] == sec:
            self._buckets[-1] = (sec, self._buckets[-1][1] + count)
        else:
            self._buckets.append((sec, count))
            cutoff = sec - self._horizon_s
            while self._buckets and self._buckets[0][0] < cutoff:
                self._buckets.popleft()

    def rate(self, now_ms: int, window_s: int) -> float:
        """Average events per second over the last ``window_s`` *completed* seconds."""
        now_sec = now_ms // SECOND_MS
        lo = now_sec - window_s
        total = sum(c for sec, c in self._buckets if lo <= sec < now_sec)
        return total / window_s
