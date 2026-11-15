"""Latency statistics, the data-quality gate and the reconnect back-off policy."""

from __future__ import annotations

import random

import pytest

from fxlab.domain.models import ObsStatus
from fxlab.marketdata.backoff import ExponentialBackoff
from fxlab.pipeline.latency import DurationStats, LatencyTracker, RateCounter, percentile
from fxlab.pipeline.quality import GateReason, evaluate_signal_gate
from tests.conftest import T0

# --- percentiles ----------------------------------------------------------------------


def test_percentile_matches_linear_interpolation_definition() -> None:
    values = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert percentile(values, 0) == 10
    assert percentile(values, 50) == 30
    assert percentile(values, 100) == 50
    assert percentile(values, 25) == 20
    assert percentile(values, 95) == pytest.approx(48.0)
    assert percentile([7.0], 99) == 7.0
    with pytest.raises(ValueError):
        percentile([], 50)


# --- feed latency ---------------------------------------------------------------------


def test_no_samples_reports_no_figure_never_zero() -> None:
    snap = LatencyTracker().snapshot(T0)
    assert snap.n == 0 and snap.p50_ms is None and snap.p99_ms is None
    assert not snap.meaningful


def test_millisecond_resolution_latency() -> None:
    tracker = LatencyTracker()
    rng = random.Random(3)
    for i in range(1000):
        recv = T0 + i * 100
        tracker.add(recv, recv - rng.randint(20, 60) - (recv % 7))  # source stamps with ms granularity
    snap = tracker.snapshot(T0 + 100_000)
    assert snap.n == 1000 and snap.meaningful and snap.note is None
    assert snap.resolution_ms == 1
    assert 20 <= snap.min_ms <= snap.p50_ms <= snap.p95_ms <= snap.p99_ms <= snap.max_ms <= 66  # type: ignore[operator]
    assert not snap.clock_skew_suspected


def test_whole_second_source_timestamps_are_detected_and_flagged_as_upper_bound() -> None:
    tracker = LatencyTracker()
    for i in range(200):
        recv = T0 + i * 137 + 45
        tracker.add(recv, (recv // 1000) * 1000)  # provider floors its timestamps to the second
    snap = tracker.snapshot(T0 + 30_000)
    assert snap.resolution_ms == 1000
    assert snap.meaningful and snap.note is not None and "upper bound" in snap.note
    assert snap.min_ms >= 0 and snap.max_ms < 1000  # type: ignore[operator]


def test_negative_latency_means_clock_skew_and_is_not_presented_as_meaningful() -> None:
    tracker = LatencyTracker()
    for i in range(100):
        recv = T0 + i * 50
        tracker.add(recv, recv + 120 + (i % 9))  # source ahead of the local clock
    snap = tracker.snapshot(T0 + 6000)
    assert snap.clock_skew_suspected and not snap.meaningful
    assert snap.note is not None and "NTP" in snap.note
    assert snap.negative_share == 1.0


def test_sub_millisecond_latency_is_called_unmeasurable_not_zero() -> None:
    tracker = LatencyTracker()
    for i in range(200):
        recv = T0 + i * 37
        tracker.add(recv, recv - (1 if i % 10 == 0 else 0))  # same-millisecond receipt, as on a loopback feed
    snap = tracker.snapshot(T0 + 8000)
    assert snap.p50_ms == 0 and not snap.clock_skew_suspected
    assert snap.note is not None and "not zero" in snap.note and "too small to measure" in snap.note


def test_too_few_samples_is_not_meaningful() -> None:
    tracker = LatencyTracker()
    for i in range(5):
        tracker.add(T0 + i, T0 + i - 30)
    snap = tracker.snapshot(T0 + 10)
    assert snap.n == 5 and not snap.meaningful and snap.p50_ms == 30


def test_rolling_window_evicts_old_samples() -> None:
    tracker = LatencyTracker(window_ms=10_000)
    for i in range(100):
        tracker.add(T0 + i * 1000, T0 + i * 1000 - 25 - i)
    snap = tracker.snapshot(T0 + 100_000)
    assert snap.n == 10
    assert snap.min_ms == 25 + 90


def test_rate_counter() -> None:
    rc = RateCounter()
    for sec in range(10):
        for k in range(sec + 1):  # 1, 2, ... 10 events per second
            rc.add(T0 + sec * 1000 + k * 10)
    now = T0 + 10_000
    assert rc.total == 55
    assert rc.rate(now, 1) == 10
    assert rc.rate(now, 5) == pytest.approx((6 + 7 + 8 + 9 + 10) / 5)
    assert rc.rate(now + 60_000, 10) == 0


def test_duration_stats() -> None:
    ds = DurationStats()
    assert ds.snapshot()["p50"] is None
    for v in range(1, 101):
        ds.add(float(v))
    snap = ds.snapshot()
    assert snap["n"] == 100 and snap["max"] == 100 and snap["p50"] == pytest.approx(50.5)


# --- signal gate ----------------------------------------------------------------------

GOOD = dict(
    provider_configured=True,
    instrument_supported=True,
    market_open=True,
    feed_streaming=True,
    last_status=ObsStatus.FRESH,
    clock_skew_suspected=False,
)


def test_gate_allows_signals_only_when_everything_is_healthy() -> None:
    assert evaluate_signal_gate(**GOOD).signals_allowed  # type: ignore[arg-type]
    assert evaluate_signal_gate(**{**GOOD, "last_status": ObsStatus.CARRIED}).signals_allowed  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "override,reason",
    [
        ({"provider_configured": False}, GateReason.PROVIDER_NOT_CONFIGURED),
        ({"instrument_supported": False}, GateReason.INSTRUMENT_UNSUPPORTED),
        ({"market_open": False}, GateReason.MARKET_CLOSED),
        ({"feed_streaming": False}, GateReason.FEED_DISCONNECTED),
        ({"last_status": None}, GateReason.NO_DATA),
        ({"last_status": ObsStatus.MISSING}, GateReason.NO_DATA),
        ({"last_status": ObsStatus.STALE}, GateReason.DATA_STALE),
        ({"clock_skew_suspected": True}, GateReason.CLOCK_SKEW),
    ],
)
def test_gate_blocks_and_names_the_reason(override: dict[str, object], reason: GateReason) -> None:
    decision = evaluate_signal_gate(**{**GOOD, **override})  # type: ignore[arg-type]
    assert not decision.signals_allowed and decision.reason is reason


# --- back-off -------------------------------------------------------------------------


def test_backoff_grows_exponentially_with_jitter_and_is_capped() -> None:
    b = ExponentialBackoff(base_s=1, factor=2, max_s=60, rng=random.Random(1))
    delays = [b.next_delay() for _ in range(12)]
    ceilings = [min(60, 2**i) for i in range(12)]
    for delay, ceiling in zip(delays, ceilings, strict=True):
        assert ceiling / 2 <= delay <= ceiling
    assert max(delays) <= 60
    assert b.attempt == 12
    b.reset()
    assert b.attempt == 0 and b.next_delay() <= 1


def test_backoff_rejects_nonsense_parameters() -> None:
    with pytest.raises(ValueError):
        ExponentialBackoff(base_s=0)
    with pytest.raises(ValueError):
        ExponentialBackoff(base_s=10, max_s=1)
