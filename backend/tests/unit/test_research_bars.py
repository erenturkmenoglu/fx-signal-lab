"""Bar series for research: validation, resampling, wall-clock offsets."""

from __future__ import annotations

import numpy as np
import pytest

from fxlab.research.bars import Bars, resample, utc_offsets_ms
from fxlab.timeutil import HOUR_MS, MINUTE_MS
from tests.conftest import T0, utc_ms


def minute_bars(rows: list[tuple[int, float, float, float, float]]) -> Bars:
    """Rows of (minutes after T0, open, high, low, close)."""
    return Bars.from_rows([(T0 + m * MINUTE_MS, o, h, lo, c) for m, o, h, lo, c in rows], 60)


def test_bars_must_be_ascending_and_of_equal_length() -> None:
    with pytest.raises(ValueError, match="ascending"):
        Bars.from_rows([(T0, 1, 1, 1, 1), (T0, 1, 1, 1, 1)], 60)
    one = np.array([1.0])
    with pytest.raises(ValueError, match="length"):
        Bars(np.array([T0, T0 + 1]), one, one, one, one, 60)
    with pytest.raises(ValueError, match="one second"):
        Bars(np.array([T0]), one, one, one, one, 0)


def test_an_empty_series_is_a_series() -> None:
    empty = Bars.from_rows([], 60)
    assert len(empty) == 0 and empty.span_ms == 60_000
    assert len(resample(empty, 300)) == 0 and resample(empty, 300).seconds == 300


def test_resample_builds_longer_bars_on_the_utc_clock() -> None:
    # T0 is 14:00:00.  Minutes 0..4 are one 5-minute bar, 5..9 the next.
    base = minute_bars(
        [
            (0, 1.10, 1.12, 1.09, 1.11),
            (1, 1.11, 1.15, 1.10, 1.14),
            (2, 1.14, 1.14, 1.05, 1.06),
            (4, 1.06, 1.07, 1.06, 1.07),  # minute 3 had no quote: a hole inside the bar
            (5, 1.07, 1.08, 1.07, 1.08),
            (9, 1.08, 1.20, 1.08, 1.19),
        ]
    )
    five = resample(base, 300)
    assert five.seconds == 300
    assert five.ts.tolist() == [T0, T0 + 5 * MINUTE_MS]
    assert (five.open.tolist(), five.high.tolist(), five.low.tolist(), five.close.tolist()) == (
        [1.10, 1.07],
        [1.15, 1.20],
        [1.05, 1.07],
        [1.07, 1.19],
    )


def test_resample_leaves_out_periods_without_any_bar() -> None:
    base = minute_bars([(0, 1, 1, 1, 1), (17, 2, 2, 2, 2)])
    assert resample(base, 300).ts.tolist() == [T0, T0 + 15 * MINUTE_MS]


def test_resample_to_the_same_length_changes_nothing_and_odd_lengths_are_refused() -> None:
    base = minute_bars([(0, 1, 2, 0.5, 1.5), (1, 1.5, 2, 1, 1.2)])
    again = resample(base, 60)
    assert again.ts.tolist() == base.ts.tolist() and again.close.tolist() == base.close.tolist()
    with pytest.raises(ValueError, match="cannot be built"):
        resample(base, 90)


def test_resample_buckets_do_not_depend_on_where_the_series_starts() -> None:
    rng = np.random.default_rng(5)
    rows = [(m, *sorted(rng.uniform(1.1, 1.2, 4))) for m in range(0, 180)]
    rows = [(m, o, max(o, h, lo, c), min(o, h, lo, c), c) for m, o, h, lo, c in rows]
    whole = resample(minute_bars(rows), 900)
    late = resample(minute_bars(rows[7:]), 900)  # starts in the middle of a quarter hour
    assert late.ts.tolist() == whole.ts.tolist()
    assert late.close.tolist() == whole.close.tolist()
    assert late.high[1:].tolist() == whole.high[1:].tolist()  # only the first, partial bar may differ


def test_utc_offsets_follow_daylight_saving() -> None:
    stamps = np.array(
        [
            utc_ms(2026, 1, 15, 12),  # winter
            utc_ms(2026, 3, 8, 6, 59),  # the minute before New York springs forward (02:00 EST = 07:00 UTC)
            utc_ms(2026, 3, 8, 7, 0),
            utc_ms(2026, 3, 20, 12),  # New York already on summer time, London not yet
            utc_ms(2026, 3, 29, 0, 59),  # the minute before London springs forward (01:00 UTC)
            utc_ms(2026, 3, 29, 1, 0),
            utc_ms(2026, 7, 1, 12),  # summer
            utc_ms(2026, 10, 25, 0, 59),  # London falls back at 01:00 UTC
            utc_ms(2026, 10, 25, 1, 0),
            utc_ms(2026, 11, 1, 5, 59),  # New York falls back at 02:00 EDT = 06:00 UTC
            utc_ms(2026, 11, 1, 6, 0),
        ]
    )
    new_york = (utc_offsets_ms(stamps, "America/New_York") // HOUR_MS).tolist()
    london = (utc_offsets_ms(stamps, "Europe/London") // HOUR_MS).tolist()
    assert new_york == [-5, -5, -4, -4, -4, -4, -4, -4, -4, -4, -5]
    assert london == [0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0]
    assert utc_offsets_ms(stamps, "Asia/Tokyo").tolist() == [9 * HOUR_MS] * len(stamps)
    assert utc_offsets_ms(stamps, "UTC").tolist() == [0] * len(stamps)
    assert len(utc_offsets_ms(np.empty(0, dtype=np.int64), "UTC")) == 0
