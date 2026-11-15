"""The backtest engine: every rule on bars written by hand, with fills and money worked out in comments.

Conventions of the examples: one-minute bid bars, the first at 14:00 UTC on a Thursday.  A
signal is placed on bar 0, which closes at 14:01:00; with the reaction delay at 0 the order is
filled at the open of bar 1.  1 pip = 0.0001, a clip is 1 000 000 EUR, so 1 pip = 100 USD a clip.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from fxlab.research.bars import Bars
from fxlab.research.engine import Execution, Outcome, Signals, Trade, next_clock_ms, run
from fxlab.timeutil import MINUTE_MS, dt_to_ms
from tests.conftest import T0, utc_ms

PIP = 0.0001
#: spread 1 pip, 5 pip target, 10 pip stop, clips 5 pips apart, no delay, no cooldown
EX = Execution(reaction_delay_s=0, cooldown_minutes=0, entry_start_utc="00:00", entry_end_utc="23:59")
ONE = replace(EX, max_clips=1)


def bars(rows: list[tuple[float, float, float, float]], start: int = T0, minutes: list[int] | None = None) -> Bars:
    """Rows of (open, high, low, close), one minute apart (or at the given minutes after ``start``)."""
    at = minutes if minutes is not None else list(range(len(rows)))
    return Bars.from_rows([(start + m * MINUTE_MS, *row) for m, row in zip(at, rows, strict=True)], 60)


def signals(n: int, events: dict[int, tuple[int, float]]) -> Signals:
    """``events``: bar index -> (direction, potential in pips before the spread)."""
    direction = np.zeros(n, dtype=np.int8)
    potential = np.full(n, np.nan)
    for i, (side, pips) in events.items():
        direction[i], potential[i] = side, pips
    return Signals(direction, potential)


def trade_on(rows: list[tuple[float, float, float, float]], side: int = 1, ex: Execution = EX) -> Trade:
    """Signal on bar 0, traded on the rest; exactly one trade is expected."""
    base = bars(rows)
    out = run(base, base, signals(len(base), {0: (side, 20.0)}), ex)
    assert len(out.trades) == 1, out.counters
    return out.trades[0]


def flat(price: float) -> tuple[float, float, float, float]:
    return (price, price, price, price)


def fills(trade: Trade) -> list[tuple[int, float]]:
    """(minutes after T0, price) of each clip."""
    return [((f.ts_ms - T0) // MINUTE_MS, f.price) for f in trade.fills]


# --- a long position -----------------------------------------------------------------------


def test_a_buy_is_filled_at_the_ask_of_the_next_bar_and_takes_its_profit_at_the_target() -> None:
    t = trade_on(
        [
            flat(1.1000),  # the signal bar: closes 14:01
            (1.1000, 1.1003, 1.0999, 1.1002),  # bought at the open: ask = 1.1000 + 1 pip = 1.1001
            (1.1002, 1.1006, 1.1001, 1.1005),  # target = 1.1001 + 5 pips = 1.1006 on the bid: reached
            flat(1.1010),
        ]
    )
    assert t.direction == 1 and t.signal_ms == T0 + MINUTE_MS
    assert fills(t) == [(1, 1.1001)]
    assert (t.reason, t.exit_price, t.exit_ms) == ("target", 1.1006, T0 + 2 * MINUTE_MS)
    assert t.pnl == pytest.approx(500.0)  # 5 pips on 1 mn EUR
    assert t.stop_price == 1.0971  # last clip that could be added: 1.1001 - 4 x 5 pips = 1.0981; 10 pips below
    assert t.potential_pips == pytest.approx(19.0)  # what the signal saw, less the spread
    assert t.adverse_pips == pytest.approx(2.0)  # the bid stood at 1.0999 at worst, against the 1.1001 paid


def test_the_target_needs_the_bid_to_get_there_not_the_ask() -> None:
    rows = [flat(1.1000), (1.1000, 1.10059, 1.1000, 1.1005), flat(1.1005), flat(1.1005)]
    # bought at 1.1001, target 1.1006; the bid tops at 1.10059: a tenth of a pip short
    base = bars(rows)
    t = run(base, base, signals(4, {0: (1, 20.0)}), replace(EX, max_hold_minutes=2)).trades[0]
    assert t.reason == "time"
    assert trade_on([flat(1.1000), (1.1000, 1.1006, 1.1000, 1.1005), flat(1.1005)]).reason == "target"


def test_a_stop_without_adding_loses_the_stop_distance() -> None:
    t = trade_on(
        [
            flat(1.1000),
            (1.1000, 1.1002, 1.0995, 1.0996),  # bought at 1.1001; stop = 1.1001 - 10 pips = 1.0991
            (1.0996, 1.0997, 1.0990, 1.0992),  # bid falls through 1.0991: sold there
            flat(1.0992),
        ],
        ex=ONE,
    )
    assert (t.reason, t.exit_price, t.stop_price) == ("stop", 1.0991, 1.0991)
    assert t.pnl == pytest.approx(-1000.0)
    assert t.adverse_pips == pytest.approx(10.0)


def test_slippage_makes_a_stop_worse_and_leaves_a_target_alone() -> None:
    ex = replace(ONE, slippage_pips=0.5)
    stopped = trade_on([flat(1.1000), (1.1000, 1.1002, 1.0995, 1.0996), (1.0996, 1.0997, 1.0990, 1.0992)], ex=ex)
    assert stopped.exit_price == 1.09905 and stopped.pnl == pytest.approx(-1050.0)
    won = trade_on([flat(1.1000), (1.1000, 1.1006, 1.1000, 1.1005)], ex=ex)
    assert won.exit_price == 1.1006 and won.pnl == pytest.approx(500.0)


def test_commission_is_paid_on_the_way_in_and_on_the_way_out() -> None:
    t = trade_on([flat(1.1000), (1.1000, 1.1006, 1.1000, 1.1005)], ex=replace(ONE, commission_per_million=25.0))
    assert t.pnl == pytest.approx(500.0 - 2 * 25.0)


def test_adding_lowers_the_average_and_with_it_the_target() -> None:
    t = trade_on(
        [
            flat(1.1000),
            (1.1000, 1.1001, 1.0996, 1.0997),  # bought at 1.1001; next clip when the ask is 1.0996
            (1.0997, 1.0998, 1.0995, 1.0996),  # bid 1.0995 -> ask 1.0996: second clip there
            # average 1.09985, target 1.10035 on the bid
            (1.0996, 1.1003, 1.0996, 1.1002),  # bid tops at 1.1003: not yet
            (1.1002, 1.1004, 1.1001, 1.1004),  # reached
            flat(1.1004),
        ]
    )
    assert fills(t) == [(1, 1.1001), (2, 1.0996)]
    assert (t.reason, t.exit_price, t.exit_ms) == ("target", 1.10035, T0 + 4 * MINUTE_MS)
    assert t.average_price == pytest.approx(1.09985) and t.units == 2_000_000
    assert t.pnl == pytest.approx(1000.0)  # 5 pips on 2 mn EUR


def test_a_clip_added_in_a_bar_cannot_take_its_new_target_in_the_same_bar() -> None:
    rows = [
        flat(1.1000),
        (1.1000, 1.1001, 1.0999, 1.1000),  # bought at 1.1001, target 1.1006
        # falls to 1.0995 (second clip at ask 1.0996) and is back at 1.1004 within the minute: above
        # the new target 1.10035, but nobody knows whether the high came before the low
        (1.1000, 1.1004, 1.0995, 1.1004),
        (1.1004, 1.1004, 1.1002, 1.1002),  # so the profit is only taken here, where 1.10035 is touched again
        flat(1.1002),
    ]
    t = trade_on(rows)
    assert fills(t) == [(1, 1.1001), (2, 1.0996)]
    assert (t.reason, t.exit_ms) == ("target", T0 + 3 * MINUTE_MS)


def test_target_and_add_level_in_one_bar_the_smaller_position_takes_the_profit() -> None:
    t = trade_on(
        [
            flat(1.1000),
            (1.1000, 1.1006, 1.0995, 1.1000),  # bought 1.1001; the bar touches target 1.1006 and add level
            flat(1.1000),
        ]
    )
    assert fills(t) == [(1, 1.1001)]  # no second clip
    assert (t.reason, t.pnl) == ("target", pytest.approx(500.0))


def test_the_whole_ladder_and_then_the_stop() -> None:
    t = trade_on(
        [
            flat(1.1000),
            (1.1000, 1.1001, 1.0994, 1.0995),  # bought 1.1001; the ask falls to 1.0995: clip 2 at 1.0996
            (1.0995, 1.0995, 1.0984, 1.0985),  # ask 1.0985: clips 3 (1.0991) and 4 (1.0986)
            (1.0985, 1.0986, 1.0980, 1.0981),  # ask 1.0981: clip 5 at 1.0981
            (1.0981, 1.0982, 1.0972, 1.0973),  # not yet: the stop is 1.0981 - 10 pips = 1.0971
            (1.0973, 1.0974, 1.0970, 1.0970),  # bid 1.0970: stopped at 1.0971
            flat(1.0970),
        ]
    )
    assert fills(t) == [(1, 1.1001), (1, 1.0996), (2, 1.0991), (2, 1.0986), (3, 1.0981)]
    assert t.exit_ms == T0 + 5 * MINUTE_MS and t.target_price == 1.0996  # 5 pips above the average 1.0991
    assert (t.reason, t.exit_price, t.stop_price) == ("stop", 1.0971, 1.0971)
    # lost 30 + 25 + 20 + 15 + 10 = 100 pips of 1 mn EUR
    assert t.units == 5_000_000 and t.pnl == pytest.approx(-10_000.0)
    assert t.adverse_pips == pytest.approx(30.0)


def test_a_bar_that_falls_to_the_stop_bought_every_clip_on_the_way() -> None:
    t = trade_on([flat(1.1000), (1.1000, 1.1002, 1.0960, 1.0965), flat(1.0965)])
    assert fills(t) == [(1, 1.1001), (1, 1.0996), (1, 1.0991), (1, 1.0986), (1, 1.0981)]
    assert (t.reason, t.exit_price, t.pnl) == ("stop", 1.0971, pytest.approx(-10_000.0))


def test_stop_and_target_in_one_bar_the_stop_wins() -> None:
    t = trade_on([flat(1.1000), (1.1000, 1.1010, 1.0990, 1.1005), flat(1.1005)], ex=ONE)
    assert (t.reason, t.exit_price) == ("stop", 1.0991)


def test_a_bar_that_opens_beyond_the_stop_is_sold_at_its_open() -> None:
    t = trade_on(
        [
            flat(1.1000),
            (1.1000, 1.1001, 1.0997, 1.0998),  # bought 1.1001, stop 1.0971
            (1.0950, 1.0955, 1.0945, 1.0950),  # the next bar opens 21 pips below the stop
            flat(1.0950),
        ]
    )
    # the four clips still to come are taken at their levels, the worst they could have been filled at
    assert fills(t) == [(1, 1.1001), (2, 1.0996), (2, 1.0991), (2, 1.0986), (2, 1.0981)]
    assert (t.reason, t.exit_price) == ("stop", 1.0950)
    assert t.pnl == pytest.approx(-(51 + 46 + 41 + 36 + 31) * 100.0)


def test_a_target_is_never_filled_better_than_its_level() -> None:
    t = trade_on([flat(1.1000), (1.1000, 1.1001, 1.0999, 1.1000), (1.1020, 1.1025, 1.1015, 1.1020)], ex=ONE)
    assert (t.reason, t.exit_price, t.pnl) == ("target", 1.1006, pytest.approx(500.0))


def test_the_entry_bar_itself_can_end_the_trade_but_not_by_a_gap() -> None:
    won = trade_on([flat(1.1000), (1.1000, 1.1006, 1.1000, 1.1006)], ex=ONE)
    assert (won.reason, won.exit_ms, won.adverse_pips) == ("target", T0 + MINUTE_MS, pytest.approx(1.0))
    lost = trade_on([flat(1.1000), (1.1000, 1.1000, 1.0991, 1.0991)], ex=ONE)
    assert (lost.reason, lost.exit_price) == ("stop", 1.0991)


# --- a short position: everything mirrored -------------------------------------------------


def test_a_sell_is_filled_at_the_bid_and_bought_back_at_the_ask() -> None:
    t = trade_on(
        [
            flat(1.1000),
            (1.1000, 1.1001, 1.0997, 1.0998),  # sold at the bid 1.1000; target: ask 1.0995, that is bid 1.0994
            (1.0998, 1.0998, 1.09945, 1.0995),  # bid 1.09945: the ask is still 1.09955
            (1.0995, 1.0996, 1.0994, 1.0995),  # bid 1.0994: bought back at 1.0995
            flat(1.0995),
        ],
        side=-1,
    )
    assert fills(t) == [(1, 1.1000)]
    assert (t.reason, t.exit_price, t.exit_ms) == ("target", 1.0995, T0 + 3 * MINUTE_MS)
    assert t.pnl == pytest.approx(500.0)
    assert t.stop_price == 1.1030  # last clip 1.1000 + 20 pips, 10 pips above, on the ask
    assert t.adverse_pips == pytest.approx(2.0)  # the ask stood at 1.1002 at worst


def test_a_short_adds_on_the_way_up_and_is_stopped_on_the_ask() -> None:
    t = trade_on(
        [
            flat(1.1000),
            (1.1000, 1.1006, 1.1000, 1.1005),  # sold 1.1000; bid 1.1005: second clip at 1.1005
            (1.1005, 1.1021, 1.1005, 1.1020),  # bid 1.1010, 1.1015, 1.1020: clips 3, 4, 5
            (1.1020, 1.10289, 1.1019, 1.1028),  # ask 1.10299: a hair below the stop 1.1030
            (1.1028, 1.1029, 1.1027, 1.1029),  # ask 1.1030: stopped
            flat(1.1029),
        ],
        side=-1,
    )
    assert fills(t) == [(1, 1.1000), (1, 1.1005), (2, 1.1010), (2, 1.1015), (2, 1.1020)]
    assert (t.reason, t.exit_price, t.exit_ms) == ("stop", 1.1030, T0 + 4 * MINUTE_MS)
    assert t.pnl == pytest.approx(-10_000.0)


def test_a_short_gapped_through_its_stop_buys_back_at_the_ask_of_the_open() -> None:
    t = trade_on([flat(1.1000), (1.1000, 1.1001, 1.0999, 1.1000), (1.1050, 1.1052, 1.1049, 1.1050)], side=-1, ex=ONE)
    # stop 1.1010 on the ask; the bar opens at bid 1.1050, ask 1.1051
    assert (t.reason, t.exit_price, t.pnl) == ("stop", 1.1051, pytest.approx(-5100.0))


def test_a_short_target_after_adding() -> None:
    t = trade_on(
        [
            flat(1.1000),
            (1.1000, 1.1005, 1.1000, 1.1004),  # sold 1.1000 and 1.1005: average 1.10025, target ask 1.09975
            (1.1004, 1.1004, 1.09966, 1.0997),  # bid 1.09966 -> ask 1.09976: not yet
            (1.0997, 1.0997, 1.09965, 1.0997),  # bid 1.09965 -> ask 1.09975: done
        ],
        side=-1,
    )
    assert fills(t) == [(1, 1.1000), (1, 1.1005)]
    assert (t.reason, t.exit_price, t.exit_ms) == ("target", 1.09975, T0 + 3 * MINUTE_MS)
    assert t.pnl == pytest.approx(1000.0)


# --- time ----------------------------------------------------------------------------------


def test_the_order_waits_for_the_reaction_delay() -> None:
    rows = [flat(1.1000), flat(1.1010), flat(1.1020), flat(1.1030), flat(1.1030)]
    base = bars(rows)
    sig = signals(5, {0: (1, 20.0)})
    # the signal is known at 14:01:00.  No delay: the bar that opens at 14:01.  Five seconds: the
    # first bar that opens at or after 14:01:05 is the one at 14:02.
    assert fills(run(base, base, sig, replace(ONE, max_hold_minutes=1)).trades[0]) == [(1, 1.1011)]
    assert fills(run(base, base, sig, replace(ONE, max_hold_minutes=1, reaction_delay_s=5)).trades[0]) == [(2, 1.1021)]


def test_a_signal_with_no_price_soon_after_it_is_dropped() -> None:
    rows = [flat(1.1000), flat(1.1000), flat(1.1000)]
    base = bars(rows, minutes=[0, 4, 5])  # nothing between 14:01 and 14:04
    out = run(base, base, signals(3, {0: (1, 20.0)}), ONE)  # waits at most 120 s
    assert not out.trades and out.counters["skipped: no price after the signal"] == 1
    assert len(run(base, base, signals(3, {0: (1, 20.0)}), replace(ONE, max_wait_s=180)).trades) == 1
    last = run(base, base, signals(3, {2: (1, 20.0)}), ONE)  # a signal on the last bar: the data ends
    assert not last.trades and last.counters["skipped: no price after the signal"] == 1


def test_a_position_is_closed_after_the_longest_holding_time() -> None:
    rows = [flat(1.1000), *[(1.1000, 1.1002, 1.0999, 1.1001)] * 6, (1.1003, 1.1004, 1.1003, 1.1004), flat(1.1004)]
    base = bars(rows)
    t = run(base, base, signals(len(rows), {0: (1, 20.0)}), replace(ONE, max_hold_minutes=6)).trades[0]
    # in at 14:01, out at the open of the 14:07 bar: bid 1.1003 against 1.1001 paid
    assert (t.reason, t.exit_ms, t.exit_price) == ("time", T0 + 7 * MINUTE_MS, 1.1003)
    assert t.pnl == pytest.approx(200.0)
    slipped = run(base, base, signals(len(rows), {0: (1, 20.0)}), replace(ONE, max_hold_minutes=6, slippage_pips=1))
    assert slipped.trades[0].exit_price == 1.1002


def test_the_bar_at_the_deadline_is_not_traded_any_more() -> None:
    rows = [flat(1.1000), (1.1000, 1.1002, 1.0999, 1.1001), (1.1001, 1.1050, 1.0900, 1.1001), flat(1.1001)]
    base = bars(rows)
    t = run(base, base, signals(4, {0: (1, 20.0)}), replace(ONE, max_hold_minutes=1)).trades[0]
    # held from 14:01 to 14:02: the wild 14:02 bar is where it is sold at the open, not stopped or paid out
    assert (t.reason, t.exit_price) == ("time", 1.1001)


def test_every_position_is_closed_before_the_trading_day_ends() -> None:
    # Thursday 12 November 2026, winter time: 16:45 in New York is 21:45 UTC
    start = utc_ms(2026, 11, 12, 21, 40)
    rows = [flat(1.1000), *[(1.1000, 1.1002, 1.0999, 1.1001)] * 4, (1.1002, 1.1003, 1.1002, 1.1003), flat(1.1003)]
    base = bars(rows, start=start)
    t = run(base, base, signals(len(rows), {0: (1, 20.0)}), ONE).trades[0]
    assert (t.reason, t.exit_ms, t.exit_price) == ("end_of_day", utc_ms(2026, 11, 12, 21, 45), 1.1002)
    # a signal whose order could only be filled at or after that time is not taken at all:
    # known at 21:44 with a five-second delay (filled 21:45), known at 21:45, known at 21:46
    skipped = "skipped: too close to the end of the day"
    delayed = run(base, base, signals(len(rows), {3: (1, 20.0)}), replace(ONE, reaction_delay_s=5))
    assert not delayed.trades and delayed.counters[skipped] == 1
    for bar in (4, 5):
        late = run(base, base, signals(len(rows), {bar: (1, 20.0)}), ONE)
        assert not late.trades and late.counters[skipped] == 1


def test_trading_starts_again_when_the_next_trading_day_begins() -> None:
    # 17:00 in New York is 22:00 UTC in winter: the bar that closes then gives the first signal of the new day
    start = utc_ms(2026, 11, 12, 21, 58)
    base = bars([flat(1.1000)] * 5, start=start)
    skipped = "skipped: too close to the end of the day"
    assert run(base, base, signals(5, {0: (1, 20.0)}), ONE).counters[skipped] == 1  # known 21:59
    first = run(base, base, signals(5, {1: (1, 20.0)}), ONE).trades  # known 22:00:00
    assert [t.entry_ms for t in first] == [utc_ms(2026, 11, 12, 22, 0)]


def test_wall_clock_times_follow_daylight_saving() -> None:
    new_york = ZoneInfo("America/New_York")
    assert next_clock_ms(utc_ms(2026, 7, 2, 14), "16:45", new_york) == utc_ms(2026, 7, 2, 20, 45)  # summer
    assert next_clock_ms(utc_ms(2026, 11, 12, 14), "16:45", new_york) == utc_ms(2026, 11, 12, 21, 45)  # winter
    # exactly at that time: that moment; a millisecond later: tomorrow's
    assert next_clock_ms(utc_ms(2026, 11, 12, 21, 45), "16:45", new_york) == utc_ms(2026, 11, 12, 21, 45)
    assert next_clock_ms(utc_ms(2026, 11, 12, 21, 45) + 1, "16:45", new_york) == utc_ms(2026, 11, 13, 21, 45)
    assert next_clock_ms(utc_ms(2026, 11, 12, 23), "16:45", new_york) == utc_ms(2026, 11, 13, 21, 45)
    # across the night the clocks change (Sunday 1 November 2026): Saturday 20:45 UTC -> Sunday 21:45 UTC
    assert next_clock_ms(utc_ms(2026, 10, 31, 21), "16:45", new_york) == utc_ms(2026, 11, 1, 21, 45)
    assert next_clock_ms(utc_ms(2026, 3, 7, 22), "16:45", new_york) == utc_ms(2026, 3, 8, 20, 45)
    assert next_clock_ms(utc_ms(2026, 7, 2, 14), "09:00", ZoneInfo("Asia/Tokyo")) == utc_ms(2026, 7, 3, 0, 0)


def test_a_position_still_open_when_the_data_ends_is_valued_at_the_last_price() -> None:
    t = trade_on([flat(1.1000), (1.1000, 1.1002, 1.0999, 1.1001), (1.1001, 1.1003, 1.1000, 1.1002)], ex=ONE)
    assert (t.reason, t.exit_price, t.exit_ms) == ("end_of_data", 1.1002, T0 + 2 * MINUTE_MS)
    assert t.pnl == pytest.approx(100.0)


# --- which signals are traded --------------------------------------------------------------


def test_a_signal_must_see_enough_potential_after_the_spread() -> None:
    rows = [flat(1.1000)] * 4
    base = bars(rows)

    def taken(potential: float, ex: Execution = ONE) -> int:
        return len(run(base, base, signals(4, {0: (1, potential)}), ex).trades)

    assert taken(6.0) == 1  # 6 pips before the spread of 1: exactly the 5 required
    assert taken(5.9) == 0
    assert taken(float("nan")) == 0  # a strategy that cannot say how far is not followed
    assert taken(5.9, replace(ONE, spread_pips=0.5)) == 1
    assert taken(3.0, replace(ONE, min_potential_pips=0)) == 1
    out = run(base, base, signals(4, {0: (1, 2.0)}), ONE)
    assert out.counters == {"signals": 1, "traded": 0, "skipped: potential below the minimum": 1}


def test_directions_can_be_switched_off() -> None:
    base = bars([flat(1.1000)] * 4)
    sig = signals(4, {0: (1, 20.0), 2: (-1, 20.0)})
    only_short = run(base, base, sig, replace(ONE, long=False, max_hold_minutes=1))
    assert [t.direction for t in only_short.trades] == [-1]
    assert only_short.counters["skipped: direction switched off"] == 1
    only_long = run(base, base, sig, replace(ONE, short=False, max_hold_minutes=1))
    assert [t.direction for t in only_long.trades] == [1]


def test_entries_only_inside_the_entry_hours() -> None:
    rows = [flat(1.1000)] * 4
    sig = signals(4, {0: (1, 20.0)})
    for start, expected in (("14:01", 1), ("14:02", 0)):  # the signal is known at 14:01:00
        out = run(bars(rows), bars(rows), sig, replace(ONE, entry_start_utc=start, entry_end_utc="20:00"))
        assert len(out.trades) == expected
    for end, expected in (("14:01", 0), ("14:02", 1)):  # the end is exclusive
        out = run(bars(rows), bars(rows), sig, replace(ONE, entry_start_utc="06:00", entry_end_utc=end))
        assert len(out.trades) == expected
    overnight = replace(ONE, entry_start_utc="22:00", entry_end_utc="15:00")  # a window across midnight
    assert len(run(bars(rows), bars(rows), sig, overnight).trades) == 1
    assert len(run(bars(rows), bars(rows), sig, replace(overnight, entry_end_utc="14:00")).trades) == 0


def test_one_position_at_a_time_then_a_pause() -> None:
    rows = [flat(1.1000)] * 12
    base = bars(rows)
    sig = signals(12, {i: (1, 20.0) for i in range(11)})  # a signal on every bar
    out = run(base, base, sig, replace(ONE, max_hold_minutes=3, cooldown_minutes=2))
    # in 14:01, out 14:04 (time); pause until 14:06: the bar closing at 14:06 is bar 5, filled 14:06; out 14:09 ...
    assert [((t.entry_ms - T0) // MINUTE_MS, (t.exit_ms - T0) // MINUTE_MS) for t in out.trades] == [
        (1, 4),
        (6, 9),
        (11, 11),
    ]
    assert out.counters["skipped: a position was open or cooling down"] == 8
    assert out.counters["traded"] == 3 and out.counters["signals"] == 11


def test_a_new_position_never_starts_in_the_bar_the_last_one_was_stopped_in() -> None:
    rows = [
        flat(1.1000),
        (1.1000, 1.1002, 1.0999, 1.1000),  # bought 1.1001
        (1.1000, 1.1006, 1.1000, 1.1006),  # target, somewhere inside this bar
        flat(1.1006),
        flat(1.1006),
    ]
    base = bars(rows)
    # the second signal is known at 14:02:00, the open of the very bar the first trade ends in
    out = run(base, base, signals(5, {0: (1, 20.0), 1: (1, 20.0), 2: (-1, 20.0)}), ONE)
    assert [(t.direction, (t.entry_ms - T0) // MINUTE_MS) for t in out.trades] == [(1, 1), (-1, 3)]
    assert out.trades[0].reason == "target" and out.trades[0].exit_ms == T0 + 2 * MINUTE_MS
    assert out.counters["skipped: a position was open or cooling down"] == 1


def test_a_position_closed_at_the_open_of_a_bar_frees_that_bar() -> None:
    base = bars([flat(1.1000)] * 5)
    out = run(base, base, signals(5, {0: (1, 20.0), 1: (-1, 20.0)}), replace(ONE, max_hold_minutes=1))
    # first: in 14:01, out at the open of 14:02; the second signal (known 14:02:00) is filled at that same open
    assert [(t.direction, (t.entry_ms - T0) // MINUTE_MS) for t in out.trades] == [(1, 1), (-1, 2)]


def test_signals_on_longer_bars_are_filled_after_those_bars_close() -> None:
    base = bars([flat(1.1000 + i * 0.0001) for i in range(12)])
    five = Bars.from_rows([(T0, 1.1, 1.1, 1.1, 1.1), (T0 + 5 * MINUTE_MS, 1.1, 1.1, 1.1, 1.1)], 300)
    out = run(base, five, signals(2, {0: (1, 20.0)}), replace(ONE, max_hold_minutes=1))
    # the 14:00-14:05 bar closes at 14:05: filled at the open of the 14:05 minute, not before
    assert fills(out.trades[0]) == [(5, 1.1006)] and out.trades[0].signal_ms == T0 + 5 * MINUTE_MS


def test_settings_are_checked() -> None:
    for bad in (
        {"max_clips": 0},
        {"stop_pips": 0},
        {"target_pips": -1},
        {"spread_pips": -0.1},
        {"clip_units": 0},
        {"entry_start_utc": "25:00"},
        {"flat_time": "noon"},
        {"flat_time": "12:60"},
        {"session_tz": "Mars/Olympus"},
        {"add_step_pips": float("nan")},
    ):
        with pytest.raises(ValueError):
            replace(EX, **bad)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="differ in length"):
        run(bars([flat(1.1)] * 3), bars([flat(1.1)] * 3), signals(2, {}), EX)


def test_settings_from_a_form() -> None:
    ex = Execution.from_mapping(
        {"max_clips": "3", "spread_pips": "0.5", "long": "false", "short": True, "flat_time": "16:30"}, pip_size=0.01
    )
    assert (ex.max_clips, ex.spread_pips, ex.long, ex.short, ex.flat_time, ex.pip_size) == (
        3,
        0.5,
        False,
        True,
        "16:30",
        0.01,
    )
    for bad in (
        {"max_clips": "2.5"},
        {"spread_pips": "wide"},
        {"long": "maybe"},
        {"leverage": 10},
        {"stop_pips": "inf"},
    ):
        with pytest.raises(ValueError):
            Execution.from_mapping(bad)


# --- the engine against a plain bar-by-bar simulation --------------------------------------
# The engine jumps from event to event with array searches and works in floating point.  The
# reference below walks every bar and counts in whole units of a ten-millionth, straight from
# the rules in the engine's module text.  On thousands of random bars the two must agree on
# every fill, every exit and every dollar.

UNIT = 10_000_000  # prices as whole numbers of 1e-7
NEW_YORK = ZoneInfo("America/New_York")


def new_york_time(ts_ms: int) -> datetime:
    return (datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC")) + timedelta(milliseconds=ts_ms)).astimezone(NEW_YORK)


def next_flat_time(ts_ms: int, hour: int, minute: int) -> int:
    day = new_york_time(ts_ms).date()
    for ahead in (-1, 0, 1, 2):
        d = day + timedelta(days=ahead)
        at = dt_to_ms(datetime(d.year, d.month, d.day, hour, minute, tzinfo=NEW_YORK))
        if at >= ts_ms:
            return at
    raise AssertionError


def after_the_flat_time(ts_ms: int, flat: str, day_end: str) -> bool:
    """Is the New York clock between the flat time and the end of the trading day?"""
    local = new_york_time(ts_ms)
    now = local.hour * 3600 + local.minute * 60 + local.second
    flat_s = int(flat[:2]) * 3600 + int(flat[3:]) * 60
    end_s = int(day_end[:2]) * 3600 + int(day_end[3:]) * 60
    return (now - flat_s) % 86_400 < (end_s - flat_s) % 86_400


def reference(base: Bars, signal_bars: Bars, sig: Signals, ex: Execution) -> list[dict[str, object]]:
    def units(pips: float) -> int:
        return round(pips * ex.pip_size * UNIT)

    ts = base.ts.tolist()
    o, h, lo, c = (np.rint(a * UNIT).astype(np.int64).tolist() for a in (base.open, base.high, base.low, base.close))
    spread, slip, step = units(ex.spread_pips), units(ex.slippage_pips), units(ex.add_step_pips)
    start = int(ex.entry_start_utc[:2]) * 3_600_000 + int(ex.entry_start_utc[3:]) * 60_000
    end = int(ex.entry_end_utc[:2]) * 3_600_000 + int(ex.entry_end_utc[3:]) * 60_000
    out: list[dict[str, object]] = []
    free_ms, free_bar = -1, 0
    for i in range(len(signal_bars)):
        d = int(sig.direction[i])
        if d == 0 or not (ex.long if d > 0 else ex.short):
            continue
        if not float(sig.potential_pips[i]) - ex.spread_pips >= ex.min_potential_pips:
            continue
        known = int(signal_bars.ts[i]) + signal_bars.seconds * 1000
        tod = known % 86_400_000
        inside = start <= tod < end if start <= end else (tod >= start or tod < end)
        if known < free_ms or not inside:
            continue
        planned = known + int(ex.reaction_delay_s * 1000)
        j = next((k for k in range(len(ts)) if ts[k] >= planned), None)
        flat_ms = next_flat_time(known, int(ex.flat_time[:2]), int(ex.flat_time[3:]))
        if j is None or ts[j] - planned > ex.max_wait_s * 1000 or after_the_flat_time(known, ex.flat_time, ex.day_end):
            continue
        if ts[j] >= flat_ms or j < free_bar:
            continue
        bought_at, sold_at = (spread, 0) if d > 0 else (0, spread)  # what is added to a bid: clips, exit
        entry = o[j] + bought_at
        clips = [(ts[j], entry)]
        ladder = [entry - d * k * step for k in range(1, ex.max_clips)]
        stop = entry - d * ((ex.max_clips - 1) * step + units(ex.stop_pips))
        hold_ms = ts[j] + int(ex.max_hold_minutes * 60_000)
        q = j
        while True:
            if q >= len(ts):
                result = ("end_of_data", len(ts) - 1, c[-1] + sold_at - d * slip)
                free_bar = len(ts)
                break
            if ts[q] >= min(hold_ms, flat_ms):
                result = ("end_of_day" if flat_ms <= hold_ms else "time", q, o[q] + sold_at - d * slip)
                free_bar = q
                break
            average = sum(p for _, p in clips) / len(clips)
            target = average + d * units(ex.target_pips)
            worst, best = (lo[q], h[q]) if d > 0 else (h[q], lo[q])
            gapped = q > j and d * (o[q] + sold_at - stop) <= 0
            if gapped or d * (worst + sold_at - stop) <= 0:
                clips += [(ts[q], level) for level in ladder[len(clips) - 1 :]]
                result = ("stop", q, (o[q] + sold_at if gapped else stop) - d * slip)
                free_bar = q + 1
                break
            if d * (best + sold_at - target) >= 0:
                result = ("target", q, target)
                free_bar = q + 1
                break
            while len(clips) < ex.max_clips and d * (worst + bought_at - ladder[len(clips) - 1]) <= 0:
                clips.append((ts[q], ladder[len(clips) - 1]))
            q += 1
        reason, at, price = result
        out.append(
            {
                "direction": d,
                "signal_ms": known,
                "fills": [(t, p / UNIT) for t, p in clips],
                "exit_ms": ts[at],
                "exit_price": price / UNIT,
                "reason": reason,
                "pnl": sum(d * (price - p) for _, p in clips) / UNIT * ex.clip_units
                - 2 * ex.commission_per_million * len(clips) * ex.clip_units / 1e6,
            }
        )
        free_ms = ts[at] + int(ex.cooldown_minutes * 60_000)
    return out


def random_market(seed: int, n: int = 1500) -> tuple[Bars, Bars, Signals]:
    """A jumpy market on the five-decimal grid with missing minutes, gaps and signals in both directions."""
    rng = np.random.default_rng(seed)
    minutes = np.cumsum(rng.choice([1, 1, 1, 1, 1, 1, 2, 3, 7], size=n))
    opens, highs, lows, closes = [], [], [], []
    price = 110_000  # in units of 1e-5
    for _ in range(n):
        price += int(rng.choice([0, 0, 0, 0, 0, -15, 15, 40, -40]))  # the open may gap away from the last close
        # most minutes are quiet; some are wild enough to touch a stop, a target and clips in one bar
        path = price + np.cumsum(rng.integers(-6, 7, size=6) * int(rng.choice([1, 1, 1, 1, 1, 1, 3, 8])))
        opens.append(price)
        highs.append(max(price, int(path.max())))
        lows.append(min(price, int(path.min())))
        price = int(path[-1])
        closes.append(price)
    start = utc_ms(2026, 11, 9, 0, 0)  # a Monday; the series runs for some days, across flat times
    base = Bars(
        start + minutes.astype(np.int64) * MINUTE_MS,
        np.array(opens) / 1e5,
        np.array(highs) / 1e5,
        np.array(lows) / 1e5,
        np.array(closes) / 1e5,
        60,
    )
    direction = rng.choice([0, 0, 0, 0, 0, 0, 1, -1], size=n).astype(np.int8)
    potential = rng.choice([np.nan, 2.0, 6.0, 9.0, 30.0], size=n)
    return base, base, Signals(direction, potential)


VARIANTS = [
    {},
    {"max_clips": 1},
    {"max_clips": 3, "add_step_pips": 2.0, "stop_pips": 3.0, "target_pips": 2.0},
    {"max_clips": 4, "add_step_pips": 7.5, "spread_pips": 0.8, "slippage_pips": 0.3},
    {"max_clips": 2, "spread_pips": 0.0, "reaction_delay_s": 5.0, "max_hold_minutes": 30.0},
    {"max_clips": 5, "add_step_pips": 1.0, "stop_pips": 2.0, "target_pips": 1.5, "cooldown_minutes": 5.0},
    {"spread_pips": 2.5, "commission_per_million": 20.0, "max_hold_minutes": 12.0, "min_potential_pips": 0.0},
    {"long": False, "entry_start_utc": "06:00", "entry_end_utc": "20:00", "reaction_delay_s": 61.0},
]


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("variant", range(len(VARIANTS)))
def test_the_engine_agrees_with_a_plain_bar_by_bar_simulation(seed: int, variant: int) -> None:
    base, signal_bars, sig = random_market(seed)
    ex = replace(EX, **VARIANTS[variant])  # type: ignore[arg-type]
    got = run(base, signal_bars, sig, ex).trades
    want = reference(base, signal_bars, sig, ex)
    assert len(got) == len(want) and len(got) > 5
    for g, w in zip(got, want, strict=True):
        assert (g.direction, g.signal_ms, g.exit_ms, g.reason) == (
            w["direction"],
            w["signal_ms"],
            w["exit_ms"],
            w["reason"],
        )
        assert [f.ts_ms for f in g.fills] == [t for t, _ in w["fills"]]  # type: ignore[attr-defined]
        assert [f.price for f in g.fills] == pytest.approx([p for _, p in w["fills"]], abs=1e-9)  # type: ignore[attr-defined]
        assert g.exit_price == pytest.approx(w["exit_price"], abs=1e-9)
        assert g.pnl == pytest.approx(w["pnl"], abs=1e-4)


def test_the_random_markets_exercise_every_kind_of_exit() -> None:
    reasons: set[str] = set()
    clips: set[int] = set()
    for seed in range(12):
        base, signal_bars, sig = random_market(seed)
        for t in (
            run(base, signal_bars, sig, EX).trades
            + run(base, signal_bars, sig, replace(EX, max_hold_minutes=12)).trades
        ):
            reasons.add(t.reason)
            clips.add(t.clips)
    assert reasons >= {"target", "stop", "time", "end_of_day"} and clips == {1, 2, 3, 4, 5}


# --- things that must hold for any input ---------------------------------------------------


def check_trades(out: Outcome, ex: Execution) -> None:
    previous_exit = -1
    for t in out.trades:
        assert t.entry_ms > t.signal_ms - 1 and t.exit_ms >= t.entry_ms >= previous_exit
        previous_exit = t.exit_ms
        assert 1 <= t.clips <= ex.max_clips
        steps = [round(t.direction * (a.price - b.price) / PIP, 6) for a, b in zip(t.fills, t.fills[1:], strict=False)]
        assert steps == [ex.add_step_pips] * (t.clips - 1)  # every clip one step further against the position
        assert t.pnl == pytest.approx(sum(t.direction * f.units * (t.exit_price - f.price) for f in t.fills))
        if t.reason == "target":
            assert t.exit_price == t.target_price
            assert t.pnl == pytest.approx(ex.target_pips * PIP * t.units, abs=0.06 * t.clips)
        if t.reason == "stop":
            assert t.direction * (t.exit_price - t.stop_price) <= 0 and t.pnl < 0
        assert t.adverse_pips >= 0


@pytest.mark.parametrize("seed", range(6))
def test_what_must_hold_for_every_trade(seed: int) -> None:
    base, signal_bars, sig = random_market(seed)
    for ex in (EX, ONE, replace(EX, max_clips=3, add_step_pips=3.0)):
        out = run(base, signal_bars, sig, ex)
        check_trades(out, ex)
        assert out.counters["signals"] == int(np.count_nonzero(sig.direction))
        assert out.counters["traded"] == len(out.trades)
        assert sum(v for k, v in out.counters.items() if k != "signals") == out.counters["signals"]


@pytest.mark.parametrize("cut", [200, 555, 900, 1499])
def test_trades_do_not_depend_on_prices_that_come_later(cut: int) -> None:
    """Running on the first part of the data gives the first part of the trades."""
    base, _, sig = random_market(3)
    short = Bars(base.ts[:cut], base.open[:cut], base.high[:cut], base.low[:cut], base.close[:cut], 60)
    early = run(short, short, Signals(sig.direction[:cut], sig.potential_pips[:cut]), EX).trades
    whole = run(base, base, sig, EX).trades
    closed = [t for t in early if t.reason != "end_of_data"]
    assert len(closed) >= len(early) - 1 and len(closed) > 3
    assert closed == whole[: len(closed)]
