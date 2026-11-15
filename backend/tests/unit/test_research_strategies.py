"""Strategies: what makes each of them signal, and that none of them can see the future."""

from __future__ import annotations

import numpy as np
import pytest

from fxlab.research import indicators as ind
from fxlab.research.bars import Bars
from fxlab.research.engine import Signals
from fxlab.research.strategies import STRATEGIES, Param, get_strategy
from fxlab.timeutil import MINUTE_MS
from tests.conftest import utc_ms

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
    start = utc_ms(2026, 11, 11, 7, 0)  # the data only starts an hour before the open: 12 bars of range
    bars = london_day(start, {14: 1.1650}, n=40)
    strategy = get_strategy("session_breakout")
    values = {"session": "london", "range_hours": 5, "window_hours": 3}
    assert events(strategy.signals(bars, PIP, {**values, "min_range_bars": 12})) == [(14, 1)]
    assert events(strategy.signals(bars, PIP, {**values, "min_range_bars": 13})) == []


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
    assert list(STRATEGIES) == ["rsi_reversion", "bollinger_reversion", "ema_momentum", "session_breakout"]
    for strategy in STRATEGIES.values():
        described = strategy.describe()
        assert described["key"] == strategy.key and described["title"] and described["summary"]
        assert described["timeframe"] == "5m"
        for param in described["params"]:
            assert param["kind"] in ("int", "float", "choice") and param["label"]
            assert (param["kind"] == "choice") == bool(param["choices"])
        assert strategy.resolve() == {p.name: p.default for p in strategy.params}
    with pytest.raises(ValueError, match="unknown strategy 'martingale'; there are: rsi_reversion"):
        get_strategy("martingale")
    assert Param("n", "N", 3, "int").coerce("4") == 4
