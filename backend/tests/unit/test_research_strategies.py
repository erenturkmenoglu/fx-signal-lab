"""Strategies: what makes each of them signal, and that none of them can see the future."""

from __future__ import annotations

import numpy as np
import pytest

from fxlab.research import indicators as ind
from fxlab.research.bars import Bars, resample
from fxlab.research.engine import Signals
from fxlab.research.strategies import STRATEGIES, Param, get_strategy
from fxlab.timeutil import MINUTE_MS
from tests.conftest import T0, utc_ms

PIP = 0.0001


def five_minute_bars(close: list[float] | np.ndarray, start: int, spread: float = 0.0002) -> Bars:
    """Bars whose high and low sit ``spread`` around the close; each opens where the last one closed."""
    c = np.asarray(close, dtype=float)
    o = np.concatenate((c[:1], c[:-1]))
    ts = start + np.arange(len(c), dtype=np.int64) * 5 * MINUTE_MS
    return Bars(ts, o, np.maximum(o, c) + spread, np.minimum(o, c) - spread, c, 300)


def random_bars(n: int, seed: int, start: int | None = None) -> Bars:
    rng = np.random.default_rng(seed)
    close = np.round(1.16 + np.cumsum(rng.normal(0, 0.0004, n)), 5)
    return five_minute_bars(close, start if start is not None else utc_ms(2026, 11, 9, 0, 0))


def events(sig: Signals) -> list[tuple[int, int]]:
    """(bar, direction) of every signal."""
    return [(int(i), int(sig.direction[i])) for i in np.flatnonzero(sig.direction)]


# --- no look-ahead -------------------------------------------------------------------------


@pytest.mark.parametrize("key", list(STRATEGIES))
@pytest.mark.parametrize("seed", [1, 2])
def test_no_strategy_looks_ahead(key: str, seed: int) -> None:
    """Cutting the bars short anywhere leaves every earlier answer exactly as it was."""
    strategy = get_strategy(key)
    bars = random_bars(2600, seed)  # nine days of five-minute bars
    whole = strategy.signals(bars, PIP)
    assert len(events(whole)) >= 6, "the series must make the strategy signal for this test to mean anything"
    for cut in (1, 2, 50, 97, 288, 333, 577, 600, 899, 1500, 2011, 2599):
        short = Bars(bars.ts[:cut], bars.open[:cut], bars.high[:cut], bars.low[:cut], bars.close[:cut], 300)
        part = strategy.signals(short, PIP)
        assert np.array_equal(part.direction, whole.direction[:cut])
        assert np.array_equal(part.potential_pips, whole.potential_pips[:cut], equal_nan=True)


@pytest.mark.parametrize("key", list(STRATEGIES))
def test_every_signal_comes_with_a_potential_and_only_signals_do(key: str) -> None:
    sig = get_strategy(key).signals(random_bars(900, 4), PIP)
    assert sig.direction.dtype == np.int8 and set(np.unique(sig.direction)) <= {-1, 0, 1}
    assert not np.isnan(sig.potential_pips[sig.direction != 0]).any()
    assert np.isnan(sig.potential_pips[sig.direction == 0]).all()


@pytest.mark.parametrize("key", list(STRATEGIES))
def test_strategies_cope_with_no_bars_and_with_too_few(key: str) -> None:
    strategy = get_strategy(key)
    assert len(strategy.signals(random_bars(0, 1), PIP).direction) == 0
    assert not strategy.signals(random_bars(3, 1), PIP).direction.any()


# --- RSI -----------------------------------------------------------------------------------


def test_rsi_reversion_signals_once_when_the_rsi_enters_a_zone() -> None:
    # falls for a while, recovers, rallies hard, comes back
    close = [1.1600 - 0.0002 * i for i in range(30)]
    close += [close[-1] + 0.0003 * i for i in range(1, 60)]
    close += [close[-1] - 0.0003 * i for i in range(1, 30)]
    bars = five_minute_bars(close, utc_ms(2026, 11, 9, 8, 0))
    strategy = get_strategy("rsi_reversion")
    sig = strategy.signals(bars, PIP, {"rsi_period": 5, "mean_period": 10})
    rsi = ind.rsi(bars.close, 5)
    mean = ind.sma(bars.close, 10)
    got = events(sig)
    assert got, "the series must cross both zones"
    for i, side in got:
        if side > 0:  # a buy: the RSI has just fallen below 30
            assert rsi[i] < 30 <= rsi[i - 1]
            assert sig.potential_pips[i] == pytest.approx((mean[i] - bars.close[i]) / PIP)
        else:  # a sell: it has just risen above 70
            assert rsi[i] > 70 >= rsi[i - 1]
            assert sig.potential_pips[i] == pytest.approx((bars.close[i] - mean[i]) / PIP)
    assert {side for _, side in got} == {1, -1}
    # while the RSI stays in the zone there is no second signal
    in_zone = int(np.count_nonzero(rsi < 30)) + int(np.count_nonzero(rsi > 70))
    assert in_zone > len(got)


def test_rsi_reversion_can_wait_until_the_rsi_leaves_the_zone() -> None:
    bars = random_bars(900, 6)
    strategy = get_strategy("rsi_reversion")
    enter = strategy.signals(bars, PIP, {"trigger": "enter"})
    leave = strategy.signals(bars, PIP, {"trigger": "leave"})
    rsi = ind.rsi(bars.close, 14)
    for i, side in events(leave):
        assert (rsi[i] > 30 >= rsi[i - 1]) if side > 0 else (rsi[i] < 70 <= rsi[i - 1])
    # each buy on leaving follows a buy on entering, never the other way round
    first_enter = min(i for i, side in events(enter) if side > 0)
    first_leave = min(i for i, side in events(leave) if side > 0)
    assert first_leave > first_enter


def test_the_distance_to_the_average_can_be_against_the_trade() -> None:
    # a slow slide: the RSI goes oversold while the price is still above a long average ... never.
    # But a short average can already be below the price when the RSI turns down from a spike.
    bars = random_bars(900, 6)
    sig = get_strategy("rsi_reversion").signals(bars, PIP, {"mean_period": 2})
    potentials = sig.potential_pips[sig.direction != 0]
    assert (potentials < 6).any()  # such signals exist, and the engine's minimum is what drops them


# --- Bollinger -----------------------------------------------------------------------------


def test_bollinger_reversion_signals_on_the_bar_that_closes_outside_the_band() -> None:
    close = [1.1600 + 0.0001 * ((-1) ** i) for i in range(40)]  # quiet
    close += [1.1570, 1.1565, 1.1580, 1.1600, 1.1640, 1.1645, 1.1600]  # a drop, then a spike
    bars = five_minute_bars(close, utc_ms(2026, 11, 9, 8, 0))
    sig = get_strategy("bollinger_reversion").signals(bars, PIP, {"period": 20, "width": 2.0})
    middle, upper, lower = ind.bollinger(bars.close, 20, 2.0)
    got = events(sig)
    assert got[0] == (40, 1)  # the first bar of the drop closes below the lower band
    assert (41, 1) not in got  # the next one is still below: no second signal
    assert sig.potential_pips[40] == pytest.approx((middle[40] - bars.close[40]) / PIP)
    sells = [i for i, side in got if side < 0]
    assert sells and all(bars.close[i] > upper[i] and bars.close[i - 1] <= upper[i - 1] for i in sells)
    assert all(bars.close[i] < lower[i] for i, side in got if side > 0)


def test_wider_bands_signal_less_often() -> None:
    bars = random_bars(900, 8)
    strategy = get_strategy("bollinger_reversion")
    narrow = len(events(strategy.signals(bars, PIP, {"width": 1.5})))
    wide = len(events(strategy.signals(bars, PIP, {"width": 3.0})))
    assert narrow > wide


# --- EMA momentum --------------------------------------------------------------------------


def test_ema_momentum_signals_when_the_fast_average_crosses_the_slow_one() -> None:
    close = [1.1600 - 0.0002 * i for i in range(60)] + [1.1482 + 0.0004 * i for i in range(60)]
    bars = five_minute_bars(close, utc_ms(2026, 11, 9, 8, 0))
    sig = get_strategy("ema_momentum").signals(bars, PIP, {"fast": 5, "slow": 20, "atr_multiple": 2.0})
    gap = ind.ema(bars.close, 5) - ind.ema(bars.close, 20)
    got = events(sig)
    assert [side for _, side in got] == [1]  # one cross up; the fall at the start is before the averages exist
    i = got[0][0]
    assert gap[i] > 0 >= gap[i - 1]
    reach = 2.0 * ind.atr(bars.high, bars.low, bars.close, 14)[i] / PIP
    assert sig.potential_pips[i] == pytest.approx(reach)


def test_ema_momentum_can_require_a_trend() -> None:
    bars = random_bars(900, 9)
    strategy = get_strategy("ema_momentum")
    every = events(strategy.signals(bars, PIP, {"fast": 5, "slow": 20}))
    trending = events(strategy.signals(bars, PIP, {"fast": 5, "slow": 20, "adx_min": 25}))
    adx = ind.adx(bars.high, bars.low, bars.close, 14)[0]
    assert 0 < len(trending) < len(every)
    assert set(trending) <= set(every) and all(adx[i] >= 25 for i, _ in trending)
    assert all(not adx[i] >= 25 for i, _ in set(every) - set(trending))


# --- session breakout ----------------------------------------------------------------------


def london_day(start_utc: int, moves: dict[int, float], n: int = 120) -> Bars:
    """Five-minute bars from ``start_utc``: flat at 1.1600 (+- 2 pips) except closes set in ``moves``."""
    close = [1.1600] * n
    for i, value in moves.items():
        close[i] = value
    c = np.asarray(close)
    ts = start_utc + np.arange(n, dtype=np.int64) * 5 * MINUTE_MS
    return Bars(ts, c.copy(), c + 0.0002, c - 0.0002, c, 300)


def test_session_breakout_trades_the_first_close_outside_the_range_before_the_open() -> None:
    # Wednesday 11 November 2026 (winter: London = UTC).  Bars from 03:00; the open is 08:00 = bar 60.
    start = utc_ms(2026, 11, 11, 3, 0)
    bars = london_day(
        start,
        {
            10: 1.1610,  # inside the range hours: makes the top 1.1612 (high = close + 2 pips)
            20: 1.1590,  # ... and the bottom 1.1588
            59: 1.1630,  # the last bar before the open still belongs to the range: top 1.1632
            62: 1.1631,  # after the open, but not above 1.1632
            65: 1.1633,  # the first close above: buy
            66: 1.1640,  # still above: no second buy
            70: 1.1580,  # the first close below 1.1588: sell
            75: 1.1570,
        },
    )
    sig = get_strategy("session_breakout").signals(
        bars, PIP, {"session": "london", "range_hours": 5, "window_hours": 3}
    )
    assert events(sig) == [(65, 1), (70, -1)]
    assert sig.potential_pips[65] == pytest.approx(44.0)  # the height of the range: 1.1632 - 1.1588
    assert sig.potential_pips[70] == pytest.approx(44.0)


def test_session_breakout_stops_looking_when_its_window_is_over() -> None:
    start = utc_ms(2026, 11, 11, 3, 0)
    # the window is 08:00 to 09:00, bars 60..71; a breakout at bar 72 comes too late
    late = london_day(start, {72: 1.1650})
    in_time = london_day(start, {71: 1.1650})
    strategy = get_strategy("session_breakout")
    values = {"session": "london", "range_hours": 5, "window_hours": 1}
    assert events(strategy.signals(late, PIP, values)) == []
    assert events(strategy.signals(in_time, PIP, values)) == [(71, 1)]


def test_session_breakout_follows_the_clock_of_the_place() -> None:
    # Wednesday 1 July 2026: London is on summer time, so 08:00 there is 07:00 UTC.
    start = utc_ms(2026, 7, 1, 2, 0)  # 03:00 in London
    bars = london_day(start, {60: 1.1650})  # bar 60 starts at 07:00 UTC = 08:00 London: the first bar of the window
    values = {"session": "london", "range_hours": 5, "window_hours": 3}
    assert events(get_strategy("session_breakout").signals(bars, PIP, values)) == [(60, 1)]
    # the same bars read as New York's morning: 08:00 there is 12:00 UTC = bar 120, beyond these bars
    assert events(get_strategy("session_breakout").signals(bars, PIP, {**values, "session": "new_york"})) == []
    tokyo = london_day(utc_ms(2026, 6, 30, 19, 0), {60: 1.1650})  # 04:00 in Tokyo; 09:00 = 00:00 UTC = bar 60
    assert events(get_strategy("session_breakout").signals(tokyo, PIP, {**values, "session": "tokyo"})) == [(60, 1)]


def test_session_breakout_needs_enough_bars_to_call_something_a_range() -> None:
    start = utc_ms(
        2026, 11, 11, 7, 0
    )  # the data only starts an hour before the open: 12 bars of the 60 of a full range
    bars = london_day(start, {14: 1.1650}, n=40)
    strategy = get_strategy("session_breakout")
    values = {"session": "london", "range_hours": 5, "window_hours": 3}
    assert events(strategy.signals(bars, PIP, {**values, "min_coverage": 0.2})) == [(14, 1)]
    assert events(strategy.signals(bars, PIP, {**values, "min_coverage": 0.21})) == []
    assert events(strategy.signals(bars, PIP, values)) == []  # by default half the range must be there
    # the share does not depend on the length of the bars: the same hour as four quarter-hour bars of twenty
    quarter = resample(bars, 900)
    assert len(events(strategy.signals(quarter, PIP, {**values, "min_coverage": 0.2}))) == 1
    assert events(strategy.signals(quarter, PIP, {**values, "min_coverage": 0.21})) == []


def test_session_breakout_trades_every_day_on_its_own_range() -> None:
    bars = random_bars(288 * 10, 12, start=utc_ms(2026, 11, 9, 0, 0))  # ten days in a row
    sig = get_strategy("session_breakout").signals(bars, PIP)
    days = {(int(bars.ts[i]) // 86_400_000, side) for i, side in events(sig)}
    assert len(days) == len(events(sig))  # at most one buy and one sell a day
    assert len({day for day, _ in days}) >= 5
    for i, _ in events(sig):
        day_start = int(bars.ts[i]) - int(bars.ts[i]) % 86_400_000
        minutes = (int(bars.ts[i]) - day_start) // 60_000
        assert 8 * 60 <= minutes < 11 * 60  # between the London open and three hours later (winter: UTC)
        # the range is that of 03:00 to 08:00 of the same day, and the close is outside it
        quiet = (bars.ts >= day_start + 3 * 3_600_000) & (bars.ts < day_start + 8 * 3_600_000)
        top, bottom = bars.high[quiet].max(), bars.low[quiet].min()
        assert bars.close[i] > top or bars.close[i] < bottom
        assert sig.potential_pips[i] == pytest.approx((top - bottom) / PIP)


# --- parameters ----------------------------------------------------------------------------


def test_parameters_have_defaults_and_are_checked() -> None:
    strategy = get_strategy("rsi_reversion")
    assert strategy.resolve() == {
        "rsi_period": 14,
        "oversold": 30.0,
        "overbought": 70.0,
        "trigger": "enter",
        "mean_period": 20,
    }
    assert strategy.resolve({"rsi_period": "7", "oversold": 25})["rsi_period"] == 7  # a form sends text
    assert strategy.resolve({"oversold": "25"})["oversold"] == 25.0
    for bad, message in (
        ({"rsi_period": 1}, "at least 2"),
        ({"rsi_period": 2.5}, "not a valid value"),
        ({"rsi_period": "many"}, "not a valid value"),
        ({"oversold": 60}, "at most 49"),
        ({"trigger": "sometimes"}, "one of: enter, leave"),
        ({"period": 14}, "no parameter 'period'"),
        ({"oversold": float("nan")}, "not a valid value"),
    ):
        with pytest.raises(ValueError, match=message):
            strategy.resolve(bad)


def test_parameters_that_do_not_go_together_are_refused() -> None:
    with pytest.raises(ValueError, match="fast must be shorter than slow"):
        get_strategy("ema_momentum").resolve({"fast": 50, "slow": 20})
    with pytest.raises(ValueError, match="fast must be shorter than slow"):
        get_strategy("ema_momentum").resolve({"fast": 48})  # against the default slow of 48
    assert get_strategy("session_breakout").resolve({"range_hours": 12, "window_hours": 12})["range_hours"] == 12


def test_strategies_describe_themselves_for_a_form() -> None:
    assert list(STRATEGIES) == [
        "rsi_reversion",
        "bollinger_reversion",
        "ema_momentum",
        "session_breakout",
        "stochastic_reversion",
        "cci_reversion",
        "macd_cross",
        "dmi_cross",
        "supertrend_flip",
        "donchian_breakout",
        "squeeze_breakout",
        "pivot_reversion",
        "round_number",
        "channel_reversion",
        "channel_break",
        "session_drift",
        "random_entry",
    ]
    shorts = [s.short for s in STRATEGIES.values()]
    assert len(set(shorts)) == len(shorts) and all(1 < len(short) <= 4 for short in shorts)  # marks on a chart
    assert {s.family for s in STRATEGIES.values()} == {"reversion", "trend", "breakout", "level", "time", "benchmark"}
    for strategy in STRATEGIES.values():
        described = strategy.describe()
        assert described["key"] == strategy.key and described["title"] and described["summary"]
        assert described["short"] == strategy.short and described["family"] == strategy.family
        assert described["style"] == strategy.style
        assert described["timeframe"] == "5m"
        for param in described["params"]:
            assert param["kind"] in ("int", "float", "choice") and param["label"]
            assert (param["kind"] == "choice") == bool(param["choices"])
        assert strategy.resolve() == {p.name: p.default for p in strategy.params}
    with pytest.raises(ValueError, match="unknown strategy 'martingale'; there are: rsi_reversion"):
        get_strategy("martingale")
    assert Param("n", "N", 3, "int").coerce("4") == 4


# --- the strategies added from the survey of indicators --------------------------------------


def bars_from(rows: list[tuple[float, float, float, float]], start: int) -> Bars:
    """Five-minute bars from (open, high, low, close) rows."""
    ts = start + np.arange(len(rows), dtype=np.int64) * 5 * MINUTE_MS
    o, h, lo, c = (np.array([r[i] for r in rows], dtype=float) for i in range(4))
    return Bars(ts, o, h, lo, c, 300)


def test_stochastic_reversion_needs_the_turn_inside_the_zone() -> None:
    bars = random_bars(1500, 21)
    sig = get_strategy("stochastic_reversion").signals(bars, PIP)
    k, d = ind.stochastic(bars.high, bars.low, bars.close, 14, 3, 3)
    middle = (ind.rolling_max(bars.high, 14) + ind.rolling_min(bars.low, 14)) / 2
    got = events(sig)
    assert {side for _, side in got} == {1, -1} and len(got) > 20
    for i, side in got:
        if side > 0:
            assert k[i] > d[i] and k[i - 1] <= d[i - 1] and k[i - 1] < 20
            assert sig.potential_pips[i] == pytest.approx((middle[i] - bars.close[i]) / PIP)
        else:
            assert k[i] < d[i] and k[i - 1] >= d[i - 1] and k[i - 1] > 80
            assert sig.potential_pips[i] == pytest.approx((bars.close[i] - middle[i]) / PIP)
    # a turn in the middle of the scale is not a signal: there are many more crosses than signals
    crosses = int(np.count_nonzero((k[1:] > d[1:]) != (k[:-1] > d[:-1])))
    assert crosses > 2 * len(got)


def test_cci_reversion_signals_on_the_way_back_or_on_the_way_out() -> None:
    bars = random_bars(1500, 22)
    strategy = get_strategy("cci_reversion")
    cci = ind.cci(bars.high, bars.low, bars.close, 20)
    mean = ind.sma((bars.high + bars.low + bars.close) / 3, 20)
    leave = strategy.signals(bars, PIP)
    for i, side in events(leave):
        assert (cci[i] > -100 >= cci[i - 1]) if side > 0 else (cci[i] < 100 <= cci[i - 1])
        expected = (mean[i] - bars.close[i]) / PIP if side > 0 else (bars.close[i] - mean[i]) / PIP
        assert leave.potential_pips[i] == pytest.approx(expected)
    enter = strategy.signals(bars, PIP, {"trigger": "enter"})
    for i, side in events(enter):
        assert (cci[i] < -100 <= cci[i - 1]) if side > 0 else (cci[i] > 100 >= cci[i - 1])
    assert len(events(leave)) > 10 and len(events(enter)) > 10
    assert len(events(strategy.signals(bars, PIP, {"level": 200}))) < len(events(leave))


def test_macd_cross_and_the_side_of_the_zero_line() -> None:
    bars = random_bars(1500, 23)
    strategy = get_strategy("macd_cross")
    line, signal, _ = ind.macd(bars.close, 12, 26, 9)
    every = events(strategy.signals(bars, PIP))
    for i, side in every:
        assert (
            (line[i] > signal[i] and line[i - 1] <= signal[i - 1])
            if side > 0
            else (line[i] < signal[i] and line[i - 1] >= signal[i - 1])
        )
    against = events(strategy.signals(bars, PIP, {"zero": "against"}))
    with_trend = events(strategy.signals(bars, PIP, {"zero": "with"}))
    assert all((line[i] < 0) == (side > 0) for i, side in against)
    assert all((line[i] > 0) == (side > 0) for i, side in with_trend)
    assert set(against) | set(with_trend) <= set(every) and not set(against) & set(with_trend)
    assert len(against) > 5 and len(with_trend) > 5
    sig = strategy.signals(bars, PIP, {"atr_multiple": 2.0})
    i = every[0][0]
    assert sig.potential_pips[i] == pytest.approx(2.0 * ind.atr(bars.high, bars.low, bars.close, 14)[i] / PIP)
    with pytest.raises(ValueError, match="fast must be shorter than slow"):
        strategy.resolve({"fast": 30})


def test_dmi_cross_only_counts_while_the_adx_shows_a_trend() -> None:
    bars = random_bars(1500, 24)
    strategy = get_strategy("dmi_cross")
    adx, plus, minus = ind.adx(bars.high, bars.low, bars.close, 14)
    filtered = events(strategy.signals(bars, PIP))
    unfiltered = events(strategy.signals(bars, PIP, {"adx_min": 0}))
    for i, side in unfiltered:
        assert (
            (plus[i] > minus[i] and plus[i - 1] <= minus[i - 1])
            if side > 0
            else (plus[i] < minus[i] and plus[i - 1] >= minus[i - 1])
        )
    assert set(filtered) < set(unfiltered) and all(adx[i] >= 20 for i, _ in filtered)
    assert all(not adx[i] >= 20 for i, _ in set(unfiltered) - set(filtered))


def test_supertrend_flip_signals_where_the_trend_changes_side() -> None:
    bars = random_bars(1500, 25)
    sig = get_strategy("supertrend_flip").signals(bars, PIP, {"multiple": 2.0})
    _, direction = ind.supertrend(bars.high, bars.low, bars.close, 10, 2.0)
    flips = [(i, int(direction[i])) for i in range(1, len(bars)) if direction[i] * direction[i - 1] < 0]
    assert events(sig) == flips and len(flips) > 10


def test_donchian_breakout_by_hand() -> None:
    start = utc_ms(2026, 11, 11, 8, 0)
    quiet = [(1.1600, 1.1605, 1.1595, 1.1600)] * 6
    bars = bars_from(
        [
            *quiet,  # the channel of these bars: 1.1595 to 1.1605
            (1.1600, 1.1604, 1.1598, 1.1603),  # inside
            (1.1603, 1.1609, 1.1602, 1.1608),  # the first close above 1.1605: buy
            (
                1.1608,
                1.1612,
                1.1606,
                1.1611,
            ),  # still above the channel before it (top now 1.1609): a new high, no new signal
            (1.1611, 1.1611, 1.1590, 1.1592),  # falls through the bottom 1.1595: sell
            (1.1592, 1.1593, 1.1585, 1.1586),  # lower still: no second sell
        ],
        start,
    )
    sig = get_strategy("donchian_breakout").signals(bars, PIP, {"period": 6})
    assert events(sig) == [(7, 1), (9, -1)]
    assert sig.potential_pips[7] == pytest.approx(10.0)  # the height of the channel: 1.1605 - 1.1595
    assert sig.potential_pips[9] == pytest.approx(17.0)  # by then: 1.1612 - 1.1595


def test_squeeze_breakout_trades_the_end_of_a_quiet_stretch() -> None:
    start = utc_ms(2026, 11, 11, 8, 0)
    rng = np.random.default_rng(3)
    wild = 1.16 + np.cumsum(rng.normal(0, 0.0004, 60))  # bands wide
    quiet = wild[-1] + np.cumsum(rng.normal(0, 0.00002, 40))  # closes hardly move, bars keep their range
    burst = quiet[-1] + np.arange(1, 9) * 0.0007  # a run upward
    bars = five_minute_bars(np.round(np.concatenate((wild, quiet, burst)), 5), start)
    strategy = get_strategy("squeeze_breakout")
    sig = strategy.signals(bars, PIP)
    got = events(sig)
    in_burst = [(i, side) for i, side in got if i >= 100]
    assert in_burst == [(in_burst[0][0], 1)] and in_burst[0][0] <= 103  # one buy, as the quiet ends
    middle, upper, lower = ind.bollinger(bars.close, 20, 2.0)
    _, kc_upper, kc_lower = ind.keltner(bars.high, bars.low, bars.close, 20, 20, 1.5)
    i = in_burst[0][0]
    squeezed = (upper < kc_upper) & (lower > kc_lower)
    assert not squeezed[i] and squeezed[i - 6 : i].all() and bars.close[i] > middle[i]
    assert sig.potential_pips[i] == pytest.approx(3.0 * ind.atr(bars.high, bars.low, bars.close, 20)[i] / PIP)
    # asking for a longer quiet stretch than there was: no signal
    assert not [e for e in events(strategy.signals(bars, PIP, {"min_bars": 60})) if e[0] >= 100]


def test_pivot_reversion_by_hand() -> None:
    # Tuesday 10 November 2026 from 22:00 UTC is Wednesday's trading day in New York (17:00 there)
    day1 = utc_ms(2026, 11, 10, 22, 0)
    quiet = (1.1620, 1.1625, 1.1615, 1.1620)
    # one wide bar after midnight UTC makes the day's range: H 1.1640, L 1.1580; the day closes at 1.1620
    first = [quiet] * 30 + [(1.1600, 1.1640, 1.1580, 1.1620)] + [quiet] * 257
    # pivot (1.1640 + 1.1580 + 1.1620) / 3 = 1.1613333; S1 = 2P - H = 1.1586667; R1 = 2P - L = 1.1646667
    second = [
        (1.1620, 1.1622, 1.1600, 1.1602),
        (1.1602, 1.1603, 1.1585, 1.1590),  # dips to 1.1585, below S1, and closes back above it: buy
        (1.1590, 1.1592, 1.1584, 1.1591),  # again below and back: the close before was above S1 too -> buy again
        (1.1591, 1.1592, 1.1580, 1.1582),  # closes below S1: no signal
        (1.1582, 1.1590, 1.1581, 1.1589),  # comes back from below: not a dip from above, no signal
        (1.1589, 1.1650, 1.1588, 1.1640),  # pokes above R1 1.1646667 and closes back below: sell
    ]
    bars = bars_from(first + second, day1)
    strategy = get_strategy("pivot_reversion")
    sig = strategy.signals(bars, PIP)
    assert events(sig) == [(289, 1), (290, 1), (293, -1)]
    pivot = (1.1640 + 1.1580 + 1.1620) / 3
    assert sig.potential_pips[289] == pytest.approx((pivot - 1.1590) / PIP)
    assert sig.potential_pips[293] == pytest.approx((1.1640 - pivot) / PIP)
    assert not sig.direction[:288].any()  # the first day has no day before it
    # S2 = P - (H - L) = 1.1553: never reached; R2 = 1.1673: never reached
    assert events(strategy.signals(bars, PIP, {"level": 2})) == []
    # with days that end at midnight UTC the "day before" of those bars is the two quiet hours before
    # midnight (range 1.1615 to 1.1625), and nothing in the second day is a dip to that support and back
    other = strategy.signals(bars, PIP, {"day_end": "utc_midnight"})
    assert [e for e in events(other) if e[0] >= 288] == []
    assert not other.direction[:24].any()  # the hours before the first midnight have no day before them


def test_round_number_fade_and_break_by_hand() -> None:
    start = utc_ms(2026, 11, 11, 8, 0)
    bars = bars_from(
        [
            (1.1630, 1.1634, 1.1628, 1.1632),
            (1.1632, 1.1640, 1.1631, 1.1638),  # on the way up to 1.1650, still 10 pips short
            (1.1638, 1.1649, 1.1637, 1.1641),  # reaches within 2 pips of 1.1650 and closes back: sell (fade)
            (1.1641, 1.1649, 1.1640, 1.1642),  # again: but the bar before had been there already, no signal
            (1.1642, 1.1655, 1.1641, 1.1653),  # closes through 1.1650: buy (break)
            (1.1653, 1.1656, 1.1651, 1.1654),
            (
                1.1654,
                1.1655,
                1.1644,
                1.1646,
            ),  # back through 1.1650: sell (break); for the fade, the level below was 1.1650:
            # reached and not held, but the close is below it, so it is not a fade
            (
                1.1646,
                1.1647,
                1.1601,
                1.1606,
            ),  # down to 1.1601, within 2 pips of 1.1600, closes above the zone: buy (fade)
        ],
        start,
    )
    strategy = get_strategy("round_number")
    fade = strategy.signals(bars, PIP)
    assert events(fade) == [(2, -1), (7, 1)]
    assert fade.potential_pips[2] == 10.0 and fade.potential_pips[7] == 10.0
    broke = strategy.signals(bars, PIP, {"mode": "break", "expect_pips": 15})
    assert events(broke) == [(4, 1), (6, -1)] and broke.potential_pips[4] == 15.0
    # levels every 100 pips only: 1.1650 is not one
    assert events(strategy.signals(bars, PIP, {"step_pips": 100})) == [(7, 1)]
    assert events(strategy.signals(bars, PIP, {"mode": "break", "step_pips": 100})) == []
    # a close exactly on a level is on the upper side of it
    exact = bars_from([(1.1648, 1.1649, 1.1647, 1.1648), (1.1648, 1.1650, 1.1647, 1.1650)], start)
    assert events(strategy.signals(exact, PIP, {"mode": "break"})) == [(1, 1)]


def test_session_drift_trades_the_clock_of_each_place() -> None:
    strategy = get_strategy("session_drift")
    # Wednesday 11 November 2026, winter: 08:00 in Berlin is 07:00 UTC, 08:00 in New York is 13:00 UTC
    bars = london_day(utc_ms(2026, 11, 11, 6, 0), {}, n=120)  # five-minute bars from 06:00 to 16:00 UTC
    sig = strategy.signals(bars, PIP)
    # the bar from 06:55 is complete at 07:00: the sell is known then; the bar from 12:55 gives the buy
    assert events(sig) == [(11, -1), (83, 1)]
    assert sig.potential_pips[11] == 8.0 and sig.potential_pips[83] == 8.0
    # Wednesday 1 July 2026, summer: Berlin 08:00 = 06:00 UTC, New York 08:00 = 12:00 UTC
    summer = london_day(utc_ms(2026, 7, 1, 5, 0), {}, n=120)
    assert events(strategy.signals(summer, PIP)) == [(11, -1), (83, 1)]
    # other sessions: buy when Tokyo opens (09:00 there = 00:00 UTC), sell when London does (08:00 UTC in winter)
    night = london_day(utc_ms(2026, 11, 10, 22, 0), {}, n=140)
    swapped = strategy.signals(night, PIP, {"sell_session": "london", "buy_session": "tokyo", "expect_pips": 12})
    assert events(swapped) == [(23, 1), (119, -1)] and swapped.potential_pips[23] == 12.0
    # data that only starts after the open: the first bar is not taken for the open
    late = london_day(utc_ms(2026, 11, 11, 7, 30), {}, n=30)
    assert events(strategy.signals(late, PIP)) == []
    # a hole across the open: the first bar after it comes more than an hour late, and is not an open either
    holed = london_day(utc_ms(2026, 11, 11, 6, 0), {}, n=120)
    keep = np.r_[0:8, 30:120]  # nothing between 06:40 and 08:30
    cut = Bars(holed.ts[keep], holed.open[keep], holed.high[keep], holed.low[keep], holed.close[keep], 300)
    assert [side for _, side in events(strategy.signals(cut, PIP))] == [1]  # only the New York buy is left


def test_random_entry_is_the_same_draw_for_the_same_bar() -> None:
    bars = random_bars(6000, 30)
    strategy = get_strategy("random_entry")
    sig = strategy.signals(bars, PIP)
    got = events(sig)
    assert 6000 / 12 * 0.7 < len(got) < 6000 / 12 * 1.3  # about one bar in twelve
    buys = sum(1 for _, side in got if side > 0)
    assert 0.4 < buys / len(got) < 0.6  # about half of them buys
    assert (sig.potential_pips[sig.direction != 0] == 999.0).all()
    # the draw belongs to the bar's time, not to its position: a series that starts later agrees
    later = Bars(bars.ts[777:], bars.open[777:], bars.high[777:], bars.low[777:], bars.close[777:], 300)
    assert np.array_equal(strategy.signals(later, PIP).direction, sig.direction[777:])
    assert events(strategy.signals(bars, PIP, {"seed": 2})) != got
    assert len(events(strategy.signals(bars, PIP, {"every": 1}))) == 6000


# --- the two camps ---------------------------------------------------------------------------


def test_every_method_bets_on_a_return_or_on_a_continuation() -> None:
    styles = {key: strategy.style for key, strategy in STRATEGIES.items()}
    assert {key for key, style in styles.items() if style == "reversion"} == {
        "rsi_reversion",
        "bollinger_reversion",
        "stochastic_reversion",
        "cci_reversion",
        "pivot_reversion",
        "round_number",
        "channel_reversion",
    }
    assert {key for key, style in styles.items() if style == "momentum"} == {
        "ema_momentum",
        "macd_cross",
        "dmi_cross",
        "supertrend_flip",
        "session_breakout",
        "donchian_breakout",
        "squeeze_breakout",
        "channel_break",
    }
    # the clock and the dice bet on neither
    assert {key for key, style in styles.items() if style == "other"} == {"session_drift", "random_entry"}


# --- trend channel -----------------------------------------------------------------------------


def channel_bars(closes: list[float], lows: list[float] | None = None, highs: list[float] | None = None) -> Bars:
    """Hourly bars: with one hour of bars per hour, 'hours' is the number of bars the line is fitted to."""
    rows = []
    for i, close in enumerate(closes):
        low = lows[i] if lows else close
        high = highs[i] if highs else close
        rows.append((T0 + i * 3_600_000, close, max(high, close), min(low, close), close))
    return Bars.from_rows(rows, 3600)


def test_channel_break_is_the_first_close_outside_the_line_of_the_bars_before() -> None:
    # twenty hours on a line rising one pip an hour with a wiggle of a pip: a channel two pips either side
    line = [1.1000 + 0.0001 * i + (0.0001 if i % 2 else -0.0001) for i in range(20)]
    calm = [1.1020 + 0.0001 * i for i in range(3)]  # goes on along the line: inside
    bars = channel_bars([*line, *calm, 1.1040, 1.1050, 1.1024, 1.1000])
    out = get_strategy("channel_break").signals(bars, PIP, {"hours": 20})
    assert out.direction[:23].tolist() == [0] * 23  # nothing while the price stays in its channel
    # bar 23 closes far above the upper line: a buy.  Bar 24 is still above it: no second signal.
    assert out.direction[23:25].tolist() == [1, 0]
    # bar 26 closes below the lower line of the channel that by now includes the spike: a sell
    assert out.direction[26] == -1
    # what it expects is half the height of the channel it broke out of: two standard deviations of
    # the wiggle of a pip around the line
    middle, upper, _, _ = ind.regression_channel(bars.close, 20, 2.0, ahead=1)
    assert (
        out.potential_pips[23] == pytest.approx((upper[23] - middle[23]) / PIP) and 1.5 < out.potential_pips[23] < 2.5
    )
    assert np.isnan(out.potential_pips[22])


def test_channel_reversion_trades_back_from_the_edge_and_not_into_a_falling_channel() -> None:
    flat = [1.1000 + (0.0002 if i % 2 else -0.0002) for i in range(20)]  # a level channel four pips either side
    closes = [*flat, 1.1000, 1.1000]
    lows = [*flat, 1.0994, 1.1000]  # bar 20 dips below the lower line (1.0996) and closes back at the middle
    highs = [*flat, 1.1000, 1.1006]  # bar 21 pokes above the upper line and closes back
    level = channel_bars(closes, lows, highs)
    out = get_strategy("channel_reversion").signals(level, PIP, {"hours": 20})
    assert out.direction[20:].tolist() == [1, -1] and not out.direction[:20].any()
    # what it expects is the way back to the middle line; here it closed almost on it
    middle = ind.regression_channel(level.close, 20, 2.0, ahead=1)[0]
    assert out.potential_pips[20] == pytest.approx((middle[20] - 1.1000) / PIP) and abs(out.potential_pips[20]) < 0.5

    # the same dip in a channel that falls three pips an hour: with the channel only, there is no buying
    falling = [1.1000 - 0.0003 * i + (0.0002 if i % 2 else -0.0002) for i in range(20)]
    last = 1.1000 - 0.0003 * 20
    bars = channel_bars([*falling, last], [*falling, last - 0.0008], [*falling, last])
    assert get_strategy("channel_reversion").signals(bars, PIP, {"hours": 20}).direction[20] == 0
    assert get_strategy("channel_reversion").signals(bars, PIP, {"hours": 20, "side": "both"}).direction[20] == 1


def test_the_channel_is_measured_in_hours_whatever_the_bars() -> None:
    rng = np.random.default_rng(4)
    stamps = T0 + np.arange(600) * 300_000
    price = np.round(1.16 + np.cumsum(rng.normal(0, 0.0001, 600)), 5)
    five = Bars(stamps.astype(np.int64), price, price + 0.00005, price - 0.00005, price, 300)
    out = get_strategy("channel_break").signals(five, PIP, {"hours": 24})
    assert not out.direction[:288].any()  # a day of five-minute bars is 288 of them: nothing before that
    assert out.direction[288:].any()
    short = get_strategy("channel_break").signals(five, PIP, {"hours": 1})  # an hour would be 12 bars: 20 at least
    assert not short.direction[:20].any() and short.direction[20:60].any()
