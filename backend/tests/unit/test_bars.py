"""Multi-timeframe bar construction from 1-second observations."""

from __future__ import annotations

import random

import pytest

from fxlab.domain.calendar import WeeklyCalendar
from fxlab.domain.instruments import SessionConfig
from fxlab.domain.models import Bar, DataSource, Observation, ObsStatus, PriceBasis
from fxlab.domain.timeframes import BAR_TIMEFRAMES, get_timeframe
from fxlab.pipeline.bars import BarAggregator, build_bars
from fxlab.pipeline.sampler import SecondSampler
from tests.conftest import T0, make_tick, utc_ms

CAL = WeeklyCalendar(SessionConfig().schedule())
TF5 = (get_timeframe("5s"),)


def fresh(
    ts: int, o: float, h: float, lo: float, c: float, n: int = 3, source: DataSource = DataSource.LIVE
) -> Observation:
    return Observation("EURUSD", ts, ObsStatus.FRESH, c - 0.00004, c + 0.00004, o, h, lo, n, ts + 900, 100, source)


def carried(ts: int, mid: float) -> Observation:
    return Observation(
        "EURUSD", ts, ObsStatus.CARRIED, mid - 0.00004, mid + 0.00004, None, None, None, 0, ts - 2000, 3000
    )


def unusable(ts: int, status: ObsStatus) -> Observation:
    bid = None if status is ObsStatus.MISSING else 1.16
    ask = None if status is ObsStatus.MISSING else 1.1601
    return Observation("EURUSD", ts, status, bid, ask, None, None, None, 0, None, None)


def test_five_fresh_seconds_make_a_complete_bar() -> None:
    agg = BarAggregator("EURUSD", CAL, TF5)
    out: list[Bar] = []
    out += agg.on_observation(fresh(T0, 1.1600, 1.1603, 1.1599, 1.1602))
    out += agg.on_observation(fresh(T0 + 1000, 1.1602, 1.1607, 1.1601, 1.1606))
    out += agg.on_observation(fresh(T0 + 2000, 1.1606, 1.1606, 1.1595, 1.1596))
    out += agg.on_observation(fresh(T0 + 3000, 1.1596, 1.1600, 1.1596, 1.1599))
    assert out == []  # nothing is emitted before the bar's last second
    out += agg.on_observation(fresh(T0 + 4000, 1.1599, 1.1601, 1.1598, 1.1600))
    (bar,) = out
    assert (bar.timeframe, bar.ts_ms) == ("5s", T0)
    assert (bar.open, bar.high, bar.low) == (1.1600, 1.1607, 1.1595)
    assert bar.close == pytest.approx(1.1600)
    assert (bar.n_quotes, bar.n_fresh, bar.n_valid, bar.n_expected) == (15, 5, 5, 5)
    assert bar.complete is True
    assert bar.source is DataSource.LIVE and bar.price_basis is PriceBasis.MID
    assert bar.bid_close == pytest.approx(1.1600 - 0.00004) and bar.ask_close == pytest.approx(1.1600 + 0.00004)
    assert bar.n_backfilled == 0


def test_quiet_bar_is_flat_and_says_so() -> None:
    agg = BarAggregator("EURUSD", CAL, TF5)
    out: list[Bar] = []
    for i in range(5):
        out += agg.on_observation(carried(T0 + i * 1000, 1.1600))
    (bar,) = out
    assert bar.open == bar.high == bar.low == bar.close == 1.1600
    assert (bar.n_quotes, bar.n_fresh, bar.n_valid) == (0, 0, 5)
    assert bar.complete is True


def test_stale_and_missing_seconds_contribute_no_price_and_break_completeness() -> None:
    agg = BarAggregator("EURUSD", CAL, TF5)
    out: list[Bar] = []
    out += agg.on_observation(fresh(T0, 1.1600, 1.1602, 1.1599, 1.1601))
    out += agg.on_observation(unusable(T0 + 1000, ObsStatus.STALE))
    out += agg.on_observation(unusable(T0 + 2000, ObsStatus.MISSING))
    out += agg.on_observation(carried(T0 + 3000, 1.1601))
    out += agg.on_observation(fresh(T0 + 4000, 1.1601, 1.1605, 1.1601, 1.1604))
    (bar,) = out
    assert (bar.open, bar.high, bar.low) == (1.1600, 1.1605, 1.1599)
    assert (bar.n_valid, bar.n_expected, bar.complete) == (3, 5, False)


def test_bar_without_any_usable_second_is_not_emitted() -> None:
    agg = BarAggregator("EURUSD", CAL, TF5)
    out: list[Bar] = []
    for i in range(5):
        out += agg.on_observation(unusable(T0 + i * 1000, ObsStatus.MISSING if i % 2 else ObsStatus.STALE))
    assert out == []
    assert agg.partial_bars() == {}


def test_hole_in_the_grid_finalises_previous_bar_as_incomplete() -> None:
    agg = BarAggregator("EURUSD", CAL, TF5)
    assert agg.on_observation(fresh(T0, 1.1600, 1.1602, 1.1599, 1.1601)) == []
    # the process was down: the next observation is 30 seconds later
    out = agg.on_observation(fresh(T0 + 30_000, 1.1610, 1.1612, 1.1609, 1.1611))
    (bar,) = out
    assert bar.ts_ms == T0 and bar.n_valid == 1 and bar.complete is False
    # the new bar is in progress and reported as partial
    partial = agg.partial_bars()["5s"]
    assert partial.ts_ms == T0 + 30_000 and partial.complete is False


def test_out_of_order_observations_are_ignored() -> None:
    agg = BarAggregator("EURUSD", CAL, TF5)
    agg.on_observation(fresh(T0 + 2000, 1.16, 1.16, 1.16, 1.16))
    assert agg.on_observation(fresh(T0 + 1000, 1.20, 1.20, 1.20, 1.20)) == []
    assert agg.on_observation(fresh(T0 + 2000, 1.20, 1.20, 1.20, 1.20)) == []
    assert agg.out_of_order == 2
    assert agg.partial_bars()["5s"].high == 1.16


def test_backfilled_seconds_are_counted() -> None:
    agg = BarAggregator("EURUSD", CAL, TF5)
    out: list[Bar] = []
    for i in range(5):
        src = DataSource.BACKFILL if i in (1, 2) else DataSource.LIVE
        out += agg.on_observation(fresh(T0 + i * 1000, 1.16, 1.16, 1.16, 1.16, source=src))
    assert out[0].n_backfilled == 2 and out[0].source is DataSource.LIVE


def test_daily_and_4h_bars_follow_the_new_york_rollover() -> None:
    agg = BarAggregator("EURUSD", CAL, (get_timeframe("4h"), get_timeframe("1d")))
    # last second of the New York trading day (21:59:59 UTC in winter) ...
    last = utc_ms(2026, 11, 12, 21, 59, 59)
    agg.on_observation(fresh(last - 1000, 1.1600, 1.1601, 1.1599, 1.1600))
    out = agg.on_observation(fresh(last, 1.1600, 1.1602, 1.1600, 1.1602))
    by_tf = {b.timeframe: b for b in out}
    assert by_tf["1d"].ts_ms == utc_ms(2026, 11, 11, 22) and by_tf["1d"].n_expected == 86_400
    assert by_tf["4h"].ts_ms == utc_ms(2026, 11, 12, 18) and by_tf["4h"].n_expected == 14_400
    assert by_tf["1d"].complete is False  # only two seconds of the day were observed


def _random_observations(n_seconds: int, seed: int = 11) -> list[Observation]:
    """A realistic grid produced by the real sampler, including quiet and stale stretches."""
    rng = random.Random(seed)
    start = utc_ms(2026, 11, 12, 13, 58, 7)
    s = SecondSampler("EURUSD", 5000, start)
    out: list[Observation] = []
    t, px = start, 1.16
    while t < start + n_seconds * 1000:
        gap = rng.choice([1, 1, 1, 1, 2, 9]) * int(rng.expovariate(1 / 350) + 1)
        t += gap
        px += rng.gauss(0, 0.00003)
        out.extend(s.on_quote(make_tick(t - 20, px, px + 0.00008, recv_ts_ms=t)))
    return out


def test_streaming_equals_batch_and_warm_start_equals_uninterrupted_run() -> None:
    obs = _random_observations(1500)
    stream = BarAggregator("EURUSD", CAL)
    streamed = [b for o in obs for b in stream.on_observation(o)]
    batch, _ = build_bars(obs, "EURUSD", CAL)
    assert streamed == batch

    # restart in the middle: replay history into a new aggregator, then continue live
    cut = 777
    _, resumed = build_bars(obs[:cut], "EURUSD", CAL)
    after_restart = [b for o in obs[cut:] for b in resumed.on_observation(o)]
    uninterrupted_tail = [b for b in streamed if b.ts_ms + get_timeframe(b.timeframe).ms > obs[cut].ts_ms]
    assert after_restart == uninterrupted_tail


def test_higher_timeframes_are_consistent_with_lower_ones() -> None:
    obs = _random_observations(1900)
    bars, _ = build_bars(obs, "EURUSD", CAL)
    five = [b for b in bars if b.timeframe == "5s"]
    for minute_bar in (b for b in bars if b.timeframe == "1m"):
        parts = [b for b in five if minute_bar.ts_ms <= b.ts_ms < minute_bar.ts_ms + 60_000]
        assert parts, "a 1m bar must be made of 5s bars"
        assert minute_bar.open == parts[0].open and minute_bar.close == parts[-1].close
        assert minute_bar.high == max(p.high for p in parts)
        assert minute_bar.low == min(p.low for p in parts)
        assert minute_bar.n_quotes == sum(p.n_quotes or 0 for p in parts)
        assert minute_bar.n_valid == sum(p.n_valid or 0 for p in parts)
    # price sanity on every bar
    for b in bars:
        assert b.low <= b.open <= b.high and b.low <= b.close <= b.high
        assert (b.n_fresh or 0) <= (b.n_valid or 0) <= (b.n_expected or 0)


def test_all_specified_bar_timeframes_are_produced() -> None:
    obs = _random_observations(400)
    agg = BarAggregator("EURUSD", CAL)
    completed = {b.timeframe for o in obs for b in agg.on_observation(o)}
    assert completed | set(agg.partial_bars()) == {tf.code for tf in BAR_TIMEFRAMES}
    assert {"5s", "15s", "30s", "1m", "5m"} <= completed
