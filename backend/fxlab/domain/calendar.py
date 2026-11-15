"""Market calendars and trading-session labels.

Nothing here is specific to one currency pair: each instrument carries its own
session specification (see ``config/instruments.yaml``) and gets its own calendar.

The spot FX week runs from Sunday 17:00 to Friday 17:00 New York time, and the
conventional trading day rolls at 17:00 New York.  All wall-clock arithmetic is done
with ``zoneinfo`` so daylight-saving changes are handled correctly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from typing import Protocol
from zoneinfo import ZoneInfo

from fxlab.timeutil import DAY_MS, dt_to_ms, floor_ms, ms_to_dt

DAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def parse_hhmm(text: str) -> int:
    """'17:00' -> minute of day."""
    hh, mm = text.split(":")
    minute = int(hh) * 60 + int(mm)
    if not 0 <= minute < 24 * 60:
        raise ValueError(f"invalid time of day: {text!r}")
    return minute


class MarketCalendar(Protocol):
    def is_open(self, ts_ms: int) -> bool: ...

    def open_intervals(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]: ...

    def next_open_ms(self, ts_ms: int) -> int: ...

    def next_close_ms(self, ts_ms: int) -> int: ...

    def rollover_window(self, ts_ms: int) -> tuple[int, int]: ...


@dataclass(frozen=True)
class WeeklySchedule:
    """A market that is open continuously between a weekly open and a weekly close."""

    tz: str
    open_day: int  # 0 = Monday ... 6 = Sunday
    open_minute: int  # minute of day, local time
    close_day: int
    close_minute: int
    rollover_minute: int  # daily rollover, local time


class WeeklyCalendar:
    """24-hour market with one weekly closure (the spot FX model)."""

    def __init__(self, schedule: WeeklySchedule) -> None:
        self.schedule = schedule
        self._tz = ZoneInfo(schedule.tz)
        self._open_w = schedule.open_day * 86_400 + schedule.open_minute * 60
        self._close_w = schedule.close_day * 86_400 + schedule.close_minute * 60
        if self._open_w == self._close_w:
            raise ValueError("weekly open and close must differ")

    # -- helpers -----------------------------------------------------------------
    def _local(self, ts_ms: int) -> datetime:
        return ms_to_dt(ts_ms).astimezone(self._tz)

    def _at(self, day: date, minute: int) -> int:
        local = datetime.combine(day, dtime(minute // 60, minute % 60), tzinfo=self._tz)
        return dt_to_ms(local)

    def _next_occurrence(self, ts_ms: int, weekday: int, minute: int) -> int:
        """First instant strictly after ``ts_ms`` that falls on ``weekday`` at ``minute`` local."""
        local = self._local(ts_ms)
        target = local.date() + timedelta(days=(weekday - local.weekday()) % 7)
        candidate = self._at(target, minute)
        if candidate <= ts_ms:
            candidate = self._at(target + timedelta(days=7), minute)
        return candidate

    # -- MarketCalendar ----------------------------------------------------------
    def is_open(self, ts_ms: int) -> bool:
        local = self._local(ts_ms)
        w = local.weekday() * 86_400 + local.hour * 3_600 + local.minute * 60 + local.second
        if self._close_w < self._open_w:
            closed = self._close_w <= w < self._open_w
        else:  # the closure wraps around the end of the week
            closed = w >= self._close_w or w < self._open_w
        return not closed

    def next_open_ms(self, ts_ms: int) -> int:
        return self._next_occurrence(ts_ms, self.schedule.open_day, self.schedule.open_minute)

    def next_close_ms(self, ts_ms: int) -> int:
        return self._next_occurrence(ts_ms, self.schedule.close_day, self.schedule.close_minute)

    def open_intervals(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        """Half-open sub-intervals of ``[start_ms, end_ms)`` during which the market is open."""
        out: list[tuple[int, int]] = []
        cur = start_ms
        while cur < end_ms:
            if self.is_open(cur):
                close = self.next_close_ms(cur)
                out.append((cur, min(close, end_ms)))
                cur = close
            else:
                cur = self.next_open_ms(cur)
        return out

    def rollover_window(self, ts_ms: int) -> tuple[int, int]:
        """The trading day ``[start, end)`` containing ``ts_ms`` (rolls at the local rollover time)."""
        local = self._local(ts_ms)
        minute = self.schedule.rollover_minute
        local_minute = local.hour * 60 + local.minute
        start_day = local.date() if local_minute >= minute else local.date() - timedelta(days=1)
        return self._at(start_day, minute), self._at(start_day + timedelta(days=1), minute)


class AlwaysOpenCalendar(WeeklyCalendar):
    """Never closes.  Used for the synthetic mock feed, which runs 24/7."""

    def is_open(self, ts_ms: int) -> bool:
        return True

    def open_intervals(self, start_ms: int, end_ms: int) -> list[tuple[int, int]]:
        return [(start_ms, end_ms)] if start_ms < end_ms else []

    def next_open_ms(self, ts_ms: int) -> int:
        return ts_ms

    def next_close_ms(self, ts_ms: int) -> int:
        return ts_ms + 10 * 365 * DAY_MS


@dataclass(frozen=True)
class TradingSession:
    """A named regional session defined in the local time of its financial centre."""

    name: str
    tz: str
    start_minute: int
    end_minute: int

    def is_active(self, ts_ms: int) -> bool:
        local = ms_to_dt(ts_ms).astimezone(ZoneInfo(self.tz))
        if local.weekday() >= 5:  # local Saturday / Sunday
            return False
        m = local.hour * 60 + local.minute
        if self.start_minute <= self.end_minute:
            return self.start_minute <= m < self.end_minute
        return m >= self.start_minute or m < self.end_minute


DEFAULT_SESSIONS: tuple[TradingSession, ...] = (
    TradingSession("Sydney", "Australia/Sydney", parse_hhmm("07:00"), parse_hhmm("16:00")),
    TradingSession("Tokyo", "Asia/Tokyo", parse_hhmm("09:00"), parse_hhmm("18:00")),
    TradingSession("London", "Europe/London", parse_hhmm("08:00"), parse_hhmm("17:00")),
    TradingSession("New York", "America/New_York", parse_hhmm("08:00"), parse_hhmm("17:00")),
)


def active_sessions(ts_ms: int, sessions: tuple[TradingSession, ...] = DEFAULT_SESSIONS) -> list[str]:
    """Names of the regional sessions active at ``ts_ms`` (several during overlaps)."""
    return [s.name for s in sessions if s.is_active(ts_ms)]


def utc_day_start(ts_ms: int) -> int:
    return floor_ms(ts_ms, DAY_MS)
