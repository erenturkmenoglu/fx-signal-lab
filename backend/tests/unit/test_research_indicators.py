"""Indicators: checked against plain loops written from the textbook definitions, and against look-ahead."""

from __future__ import annotations

import math

import numpy as np
import pytest

from fxlab.research import indicators as ind


def walk(n: int, seed: int = 7, start: float = 1.16, step: float = 0.0002) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return np.round(start + np.cumsum(rng.normal(0, step, n)), 5)


def ohlc(n: int, seed: int = 7) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    close = walk(n, seed)
    rng = np.random.default_rng(seed + 1)
    high = np.round(close + np.abs(rng.normal(0, 0.0001, n)), 5)
    low = np.round(close - np.abs(rng.normal(0, 0.0001, n)), 5)
    return high, low, close


def same(a: np.ndarray, b: np.ndarray, tolerance: float = 1e-12) -> bool:
    return bool(np.array_equal(np.isnan(a), np.isnan(b)) and np.allclose(a, b, rtol=0, atol=tolerance, equal_nan=True))


# --- plain references ----------------------------------------------------------------------


def ref_wilder(values: list[float], n: int, first: int) -> list[float]:
    out = [math.nan] * len(values)
    if len(values) - first < n:
        return out
    value = sum(values[first : first + n]) / n
    out[first + n - 1] = value
    for i in range(first + n, len(values)):
        value = (value * (n - 1) + values[i]) / n
        out[i] = value
    return out


def ref_rsi(close: list[float], n: int) -> list[float]:
    gains = [0.0] + [max(close[i] - close[i - 1], 0.0) for i in range(1, len(close))]
    losses = [0.0] + [max(close[i - 1] - close[i], 0.0) for i in range(1, len(close))]
    g, lo = ref_wilder(gains, n, 1), ref_wilder(losses, n, 1)
    out = []
    for a, b in zip(g, lo, strict=True):
        if math.isnan(a):
            out.append(math.nan)
        elif b == 0:
            out.append(100.0 if a > 0 else 50.0)
        else:
            out.append(100.0 - 100.0 / (1.0 + a / b))
    return out


def ref_true_range(high: list[float], low: list[float], close: list[float]) -> list[float]:
    out = [high[0] - low[0]]
    for i in range(1, len(high)):
        out.append(max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])))
    return out


# --- moving averages -----------------------------------------------------------------------


def test_sma_and_std_are_the_mean_and_spread_of_the_last_n_values() -> None:
    x = walk(300)
    for n in (1, 2, 20, 97):
        mean = np.full(len(x), np.nan)
        std = np.full(len(x), np.nan)
        for i in range(n - 1, len(x)):
            window = x[i - n + 1 : i + 1]
            mean[i] = sum(window) / n
            std[i] = math.sqrt(sum((v - mean[i]) ** 2 for v in window) / n)
        assert same(ind.sma(x, n), mean)
        assert same(ind.rolling_std(x, n), std, 1e-10)


def test_rolling_windows_give_the_same_answer_in_small_memory_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    x = walk(500)
    whole = ind.sma(x, 20), ind.rolling_std(x, 20)
    monkeypatch.setattr(ind, "_CHUNK_BYTES", 8 * 20 * 7)  # seven windows at a time
    assert same(ind.sma(x, 20), whole[0], 0) and same(ind.rolling_std(x, 20), whole[1], 0)


def test_ema_by_hand() -> None:
    # n = 3: alpha = 0.5, seed = mean(1, 2, 3) = 2, then 2 + .5*(4-2) = 3, 3 + .5*(5-3) = 4, 4 + .5*(11-4) = 7.5
    assert ind.ema([1, 2, 3, 4, 5, 11], 3).tolist() == pytest.approx([math.nan, math.nan, 2, 3, 4, 7.5], nan_ok=True)


def test_ema_starts_after_the_warm_up_of_its_input() -> None:
    x = np.array([np.nan, np.nan, 1, 2, 3, 4, 5, 11], dtype=float)
    assert ind.ema(x, 3).tolist() == pytest.approx([math.nan] * 4 + [2, 3, 4, 7.5], nan_ok=True)
    assert np.isnan(ind.ema([np.nan, 1.0, 2.0], 3)).all()  # not enough values yet
    assert np.isnan(ind.ema([np.nan, np.nan], 2)).all()


def test_too_short_a_series_is_all_warm_up() -> None:
    assert np.isnan(ind.sma([1.0, 2.0], 3)).all()
    assert np.isnan(ind.rsi([1.0, 2.0, 3.0], 3)).all()  # needs n changes, that is n + 1 prices
    assert np.isnan(ind.atr([2.0], [1.0], [1.5], 2)).all()
    assert all(np.isnan(a).all() for a in ind.adx([2.0, 3.0], [1.0, 2.0], [1.5, 2.5], 2))
    assert len(ind.sma([], 3)) == 0 and len(ind.rsi([], 3)) == 0


def test_periods_must_be_positive_and_series_one_dimensional() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ind.sma([1.0], 0)
    with pytest.raises(ValueError, match="one-dimensional"):
        ind.ema(np.zeros((2, 2)), 2)
    with pytest.raises(ValueError, match="shorter"):
        ind.macd([1.0] * 50, 26, 12)


# --- RSI -----------------------------------------------------------------------------------


def test_rsi_by_hand() -> None:
    # prices 10, 11, 10.5, 11.5, 11.5; n = 2
    # changes: +1, -.5, +1, 0.  First averages (2 changes): gain .5, loss .25 -> RS 2 -> 66.67
    # next: gain (.5 + 1)/2 = .75, loss (.25 + 0)/2 = .125 -> RS 6 -> 85.71
    # next: gain .375, loss .0625 -> RS 6 -> 85.71
    rsi = ind.rsi([10, 11, 10.5, 11.5, 11.5], 2)
    assert rsi.tolist() == pytest.approx([math.nan, math.nan, 100 - 100 / 3, 100 - 100 / 7, 100 - 100 / 7], nan_ok=True)


def test_rsi_matches_the_plain_definition() -> None:
    x = walk(400)
    for n in (2, 14, 50):
        assert same(ind.rsi(x, n), np.array(ref_rsi(x.tolist(), n)), 1e-9)


def test_rsi_extremes() -> None:
    assert ind.rsi([1, 2, 3, 4, 5, 6], 3)[-1] == 100.0  # only gains
    assert ind.rsi([6, 5, 4, 3, 2, 1], 3)[-1] == 0.0  # only losses
    assert ind.rsi([5, 5, 5, 5, 5, 5], 3)[-1] == 50.0  # no movement: neither
    values = ind.rsi(walk(500), 14)
    assert np.nanmin(values) >= 0 and np.nanmax(values) <= 100


# --- ranges --------------------------------------------------------------------------------


def test_true_range_includes_the_gap_from_the_previous_close() -> None:
    high, low, close = [10.0, 12.0, 9.0], [9.0, 11.5, 8.0], [9.5, 11.8, 8.5]
    # bar 2 gapped up: 12 - 9.5 = 2.5 is more than its own range .5; bar 3 gapped down: 11.8 - 8 = 3.8
    assert ind.true_range(high, low, close).tolist() == pytest.approx([1.0, 2.5, 3.8])


def test_atr_matches_the_plain_definition() -> None:
    high, low, close = ohlc(300)
    for n in (1, 14, 30):
        ref = ref_wilder(ref_true_range(high.tolist(), low.tolist(), close.tolist()), n, 0)
        assert same(ind.atr(high, low, close, n), np.array(ref), 1e-12)


def test_adx_matches_the_plain_definition() -> None:
    high, low, close = ohlc(400, seed=11)
    n = 14
    h, lo, c = high.tolist(), low.tolist(), close.tolist()
    plus_dm, minus_dm = [0.0], [0.0]
    for i in range(1, len(h)):
        up, down = h[i] - h[i - 1], lo[i - 1] - lo[i]
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)
    tr = ref_wilder(ref_true_range(h, lo, c), n, 1)
    pdm, mdm = ref_wilder(plus_dm, n, 1), ref_wilder(minus_dm, n, 1)
    plus_di = [math.nan if math.isnan(t) else (100 * p / t if t else 0.0) for p, t in zip(pdm, tr, strict=True)]
    minus_di = [math.nan if math.isnan(t) else (100 * m / t if t else 0.0) for m, t in zip(mdm, tr, strict=True)]
    dx = [
        math.nan if math.isnan(p) else (100 * abs(p - m) / (p + m) if p + m > 0 else 0.0)
        for p, m in zip(plus_di, minus_di, strict=True)
    ]
    adx, got_plus, got_minus = ind.adx(high, low, close, n)
    assert same(got_plus, np.array(plus_di), 1e-9) and same(got_minus, np.array(minus_di), 1e-9)
    assert same(adx, np.array(ref_wilder(dx, n, n)), 1e-9)
    assert np.isnan(adx[: 2 * n - 1]).all() and not np.isnan(adx[2 * n - 1 :]).any()


def test_adx_in_a_straight_line_up_is_all_plus() -> None:
    high = np.arange(1.0, 61.0)
    adx, plus_di, minus_di = ind.adx(high, high - 0.5, high - 0.25, 5)
    assert adx[-1] == pytest.approx(100.0) and minus_di[-1] == 0.0 and plus_di[-1] > 50


def test_adx_of_a_market_that_does_not_move_is_zero_not_an_error() -> None:
    flat = np.full(40, 1.16)
    adx, plus_di, minus_di = ind.adx(flat, flat, flat, 5)
    assert adx[-1] == 0.0 and plus_di[-1] == 0.0 and minus_di[-1] == 0.0


# --- bands and MACD ------------------------------------------------------------------------


def test_bollinger_bands_are_the_average_and_k_spreads_around_it() -> None:
    x = walk(200)
    middle, upper, lower = ind.bollinger(x, 20, 2.5)
    assert same(middle, ind.sma(x, 20), 0)
    assert same(upper - middle, 2.5 * ind.rolling_std(x, 20), 1e-15)
    assert same(middle - lower, 2.5 * ind.rolling_std(x, 20), 1e-15)


def test_macd_is_the_gap_between_two_averages_and_its_own_average() -> None:
    x = walk(300)
    line, signal, histogram = ind.macd(x, 12, 26, 9)
    assert same(line, ind.ema(x, 12) - ind.ema(x, 26), 0)
    assert same(signal, ind.ema(line, 9), 0) and same(histogram, line - signal, 0)
    assert np.isnan(line[:25]).all() and not np.isnan(line[25])
    assert np.isnan(signal[:33]).all() and not np.isnan(signal[33])  # 26 + 9 - 2


# --- the rule that matters most ------------------------------------------------------------


@pytest.mark.parametrize("cut", [1, 2, 13, 14, 15, 27, 28, 29, 60, 199])
def test_no_indicator_looks_ahead(cut: int) -> None:
    """A value never changes when later prices are added: cutting the series short leaves the past as it was."""
    high, low, close = ohlc(200, seed=3)
    h, lo, c = high[:cut], low[:cut], close[:cut]
    pairs = [
        (ind.sma(close, 14), ind.sma(c, 14)),
        (ind.rolling_std(close, 14), ind.rolling_std(c, 14)),
        (ind.ema(close, 14), ind.ema(c, 14)),
        (ind.rsi(close, 14), ind.rsi(c, 14)),
        (ind.true_range(high, low, close), ind.true_range(h, lo, c)),
        (ind.atr(high, low, close, 14), ind.atr(h, lo, c, 14)),
        *zip(ind.bollinger(close, 14), ind.bollinger(c, 14), strict=True),
        *zip(ind.macd(close, 5, 14, 3), ind.macd(c, 5, 14, 3), strict=True),
        *zip(ind.adx(high, low, close, 14), ind.adx(h, lo, c, 14), strict=True),
    ]
    for full, short in pairs:
        assert len(short) == cut
        assert np.array_equal(full[:cut], short, equal_nan=True)
