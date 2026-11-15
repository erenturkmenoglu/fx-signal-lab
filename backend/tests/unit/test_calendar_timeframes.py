"""Session calendar, trading-day rollover and bar-window alignment (including DST)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from fxlab.domain.calendar import AlwaysOpenCalendar, WeeklyCalendar, active_sessions
from fxlab.domain.instruments import InstrumentRegistry, SessionConfig
from fxlab.domain.timeframes import BAR_TIMEFRAMES, TIMEFRAMES, bar_window, get_timeframe
from fxlab.timeutil import DAY_MS, HOUR_MS, dt_to_ms, iso, ms_to_dt, parse_time
from tests.conftest import utc_ms


@pytest.fixture()
def cal() -> WeeklyCalendar:
    return WeeklyCalendar(SessionConfig().schedule())


# --- time helpers ---------------------------------------------------------------------


def test_ms_datetime_roundtrip_is_exact() -> None:
    for ms in (0, 1, 999, 1_763_128_800_123, 1_999_999_999_999):
        assert dt_to_ms(ms_to_dt(ms)) == ms


def test_dt_to_ms_rejects_naive() -> None:
    with pytest.raises(ValueError):
        dt_to_ms(datetime(2026, 1, 1))


def test_parse_time_variants() -> None:
    expected = utc_ms(2026, 11, 12, 14, 0, 0)
    assert parse_time("2026-11-12T14:00:00Z") == expected
    assert parse_time("2026-11-12T14:00:00") == expected  # naive => UTC
    assert parse_time("2026-11-12T17:00:00+03:00") == expected
    assert parse_time(expected) == expected  # epoch ms
    assert parse_time(expected // 1000) == expected  # epoch seconds
    assert parse_time(str(expected)) == expected
    assert parse_time(datetime(2026, 11, 12, 14, tzinfo=UTC)) == expected
    assert iso(expected) == "2026-11-12T14:00:00.000Z"
    assert iso(None) is None


# --- weekly open/close --------------------------------------------------------------


def test_week_close_and_open_in_winter_time(cal: WeeklyCalendar) -> None:
    # New York is UTC-5 in November: 17:00 local == 22:00 UTC
    assert cal.is_open(utc_ms(2026, 11, 13, 21, 59, 59))  # Friday, one second before the close
    assert not cal.is_open(utc_ms(2026, 11, 13, 22, 0, 0))  # Friday close
    assert not cal.is_open(utc_ms(2026, 11, 14, 12, 0, 0))  # Saturday
    assert not cal.is_open(utc_ms(2026, 11, 15, 21, 59, 59))  # Sunday, one second before the open
    assert cal.is_open(utc_ms(2026, 11, 15, 22, 0, 0))  # Sunday open
    assert cal.is_open(utc_ms(2026, 11, 12, 3, 0, 0))  # mid-week


def test_week_close_and_open_in_summer_time(cal: WeeklyCalendar) -> None:
    # New York is UTC-4 in July: 17:00 local == 21:00 UTC
    assert cal.is_open(utc_ms(2026, 7, 10, 20, 59, 59))
    assert not cal.is_open(utc_ms(2026, 7, 10, 21, 0, 0))
    assert not cal.is_open(utc_ms(2026, 7, 12, 20, 59, 59))
    assert cal.is_open(utc_ms(2026, 7, 12, 21, 0, 0))


def test_next_open_and_close(cal: WeeklyCalendar) -> None:
    saturday = utc_ms(2026, 11, 14, 12)
    assert cal.next_open_ms(saturday) == utc_ms(2026, 11, 15, 22)
    assert cal.next_close_ms(saturday) == utc_ms(2026, 11, 20, 22)
    thursday = utc_ms(2026, 11, 12, 14)
    assert cal.next_close_ms(thursday) == utc_ms(2026, 11, 13, 22)
    # strictly after: asking exactly at the close returns the following week's close
    assert cal.next_close_ms(utc_ms(2026, 11, 13, 22)) == utc_ms(2026, 11, 20, 22)


def test_open_intervals_over_one_week(cal: WeeklyCalendar) -> None:
    start, end = utc_ms(2026, 11, 9), utc_ms(2026, 11, 16)  # Monday 00:00 UTC .. Monday 00:00 UTC
    assert cal.open_intervals(start, end) == [
        (start, utc_ms(2026, 11, 13, 22)),
        (utc_ms(2026, 11, 15, 22), end),
    ]
    # fully inside the weekend
    assert cal.open_intervals(utc_ms(2026, 11, 14, 1), utc_ms(2026, 11, 14, 5)) == []
    # open seconds in a normal week: 5 days
    total = sum(b - a for a, b in cal.open_intervals(utc_ms(2026, 11, 15, 22), utc_ms(2026, 11, 22, 22)))
    assert total == 5 * DAY_MS


def test_always_open_calendar() -> None:
    always = AlwaysOpenCalendar(SessionConfig().schedule())
    saturday = utc_ms(2026, 11, 14, 12)
    assert always.is_open(saturday)
    assert always.open_intervals(saturday, saturday + 1000) == [(saturday, saturday + 1000)]
    # the trading-day rollover is still the configured one
    assert always.rollover_window(saturday) == (utc_ms(2026, 11, 13, 22), utc_ms(2026, 11, 14, 22))


def test_instrument_builds_matching_calendar(registry: InstrumentRegistry) -> None:
    eurusd = registry.get("EURUSD")
    assert not eurusd.make_calendar().is_open(utc_ms(2026, 11, 14, 12))
    assert eurusd.make_calendar(always_open=True).is_open(utc_ms(2026, 11, 14, 12))


# --- daily rollover -------------------------------------------------------------------


def test_rollover_window_winter_and_summer(cal: WeeklyCalendar) -> None:
    assert cal.rollover_window(utc_ms(2026, 11, 12, 10)) == (
        utc_ms(2026, 11, 11, 22),
        utc_ms(2026, 11, 12, 22),
    )
    assert cal.rollover_window(utc_ms(2026, 11, 12, 22)) == (
        utc_ms(2026, 11, 12, 22),
        utc_ms(2026, 11, 13, 22),
    )
    assert cal.rollover_window(utc_ms(2026, 7, 8, 10)) == (utc_ms(2026, 7, 7, 21), utc_ms(2026, 7, 8, 21))


def test_rollover_window_across_dst_changes(cal: WeeklyCalendar) -> None:
    # US clocks go back on Sunday 2026-11-01: that New York "day" lasts 25 hours
    start, end = cal.rollover_window(utc_ms(2026, 11, 1, 12))
    assert (start, end) == (utc_ms(2026, 10, 31, 21), utc_ms(2026, 11, 1, 22))
    assert end - start == 25 * HOUR_MS
    # US clocks go forward on Sunday 2026-03-08: 23 hours
    start, end = cal.rollover_window(utc_ms(2026, 3, 8, 12))
    assert (start, end) == (utc_ms(2026, 3, 7, 22), utc_ms(2026, 3, 8, 21))
    assert end - start == 23 * HOUR_MS
    # both changes fall inside the weekend closure, so every traded day is 24 hours
    assert not cal.is_open(utc_ms(2026, 11, 1, 12))
    assert not cal.is_open(utc_ms(2026, 3, 8, 12))


# --- sessions -------------------------------------------------------------------------


def test_active_sessions() -> None:
    assert active_sessions(utc_ms(2026, 11, 12, 14)) == ["London", "New York"]  # overlap
    assert active_sessions(utc_ms(2026, 11, 12, 3)) == ["Sydney", "Tokyo"]
    assert active_sessions(utc_ms(2026, 11, 12, 10)) == ["London"]
    assert active_sessions(utc_ms(2026, 11, 14, 12)) == []  # Saturday everywhere


# --- bar windows ----------------------------------------------------------------------


def test_timeframe_catalogue_matches_specification() -> None:
    assert list(TIMEFRAMES) == ["1s", "5s", "15s", "30s", "1m", "5m", "15m", "30m", "1h", "4h", "1d"]
    assert [tf.code for tf in BAR_TIMEFRAMES] == list(TIMEFRAMES)[1:]
    with pytest.raises(ValueError):
        get_timeframe("2m")


def test_utc_aligned_windows(cal: WeeklyCalendar) -> None:
    ts = utc_ms(2026, 11, 12, 14, 7, 42, 500)
    assert bar_window(ts, get_timeframe("5s"), cal) == (
        utc_ms(2026, 11, 12, 14, 7, 40),
        utc_ms(2026, 11, 12, 14, 7, 45),
    )
    assert bar_window(ts, get_timeframe("1m"), cal) == (
        utc_ms(2026, 11, 12, 14, 7),
        utc_ms(2026, 11, 12, 14, 8),
    )
    assert bar_window(ts, get_timeframe("15m"), cal) == (
        utc_ms(2026, 11, 12, 14, 0),
        utc_ms(2026, 11, 12, 14, 15),
    )
    assert bar_window(ts, get_timeframe("1h"), cal) == (utc_ms(2026, 11, 12, 14), utc_ms(2026, 11, 12, 15))


def test_rollover_anchored_windows(cal: WeeklyCalendar) -> None:
    ts = utc_ms(2026, 11, 12, 10, 30)
    assert bar_window(ts, get_timeframe("1d"), cal) == (utc_ms(2026, 11, 11, 22), utc_ms(2026, 11, 12, 22))
    # 4h bars start at 22:00, 02:00, 06:00, 10:00 ... UTC in winter
    assert bar_window(ts, get_timeframe("4h"), cal) == (utc_ms(2026, 11, 12, 10), utc_ms(2026, 11, 12, 14))
    # ... and at 21:00, 01:00, 05:00, 09:00 ... UTC in summer
    summer = utc_ms(2026, 7, 8, 10, 30)
    assert bar_window(summer, get_timeframe("4h"), cal) == (utc_ms(2026, 7, 8, 9), utc_ms(2026, 7, 8, 13))
    # a full FX week therefore has exactly five daily bars
    days = {
        bar_window(t, get_timeframe("1d"), cal)[0]
        for a, b in cal.open_intervals(utc_ms(2026, 11, 15, 22), utc_ms(2026, 11, 20, 22))
        for t in range(a, b, HOUR_MS)
    }
    assert len(days) == 5
