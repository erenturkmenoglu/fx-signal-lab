"""Technical indicators on numpy arrays.

Conventions, the same for every function:

* input and output are float64 arrays of equal length, oldest value first;
* element ``i`` of the output depends on inputs ``0..i`` only, never on later ones, so an
  indicator computed on a longer array has exactly the same values at the old positions
  (the tests check this for every indicator: it is what rules out look-ahead);
* positions where the indicator is not defined yet (its warm-up) hold ``nan``.

Definitions follow the usual textbook ones: Wilder's smoothing for RSI, ATR and ADX, an
EMA seeded with the simple mean of its first ``n`` values, Bollinger bands on the
population standard deviation.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

Array = npt.NDArray[np.float64]

_CHUNK_BYTES = 32 * 1024 * 1024  # memory used at a time by the rolling-window helpers


def _as_array(values: npt.ArrayLike) -> Array:
    out = np.asarray(values, dtype=np.float64)
    if out.ndim != 1:
        raise ValueError("indicators work on one-dimensional arrays")
    return out


def _check_period(n: int) -> None:
    if n < 1:
        raise ValueError("the period must be at least 1")


def _rolling(values: Array, n: int, how: str) -> Array:
    """Exact rolling mean, population standard deviation, maximum, minimum or mean absolute
    deviation over ``n`` values.

    Works on real windows (no running sums), so there is no cancellation error on prices
    that differ only in the fifth decimal, and no dependence on anything outside the window.
    """
    out = np.full(values.shape, np.nan)
    if len(values) < n:
        return out
    windows = np.lib.stride_tricks.sliding_window_view(values, n)
    step = max(1, _CHUNK_BYTES // (8 * n))
    for start in range(0, len(windows), step):
        block = windows[start : start + step]
        if how == "mean":
            result = block.mean(axis=1)
        elif how == "std":
            result = block.std(axis=1)
        elif how == "max":
            result = block.max(axis=1)
        elif how == "min":
            result = block.min(axis=1)
        else:  # mean absolute deviation from the window's own mean
            result = np.abs(block - block.mean(axis=1, keepdims=True)).mean(axis=1)
        out[start + n - 1 : start + n - 1 + len(block)] = result
    return out


def sma(values: npt.ArrayLike, n: int) -> Array:
    """Simple moving average of the last ``n`` values."""
    _check_period(n)
    return _rolling(_as_array(values), n, "mean")


def rolling_std(values: npt.ArrayLike, n: int) -> Array:
    """Population standard deviation of the last ``n`` values."""
    _check_period(n)
    return _rolling(_as_array(values), n, "std")


def rolling_max(values: npt.ArrayLike, n: int) -> Array:
    """Highest of the last ``n`` values (the current one included)."""
    _check_period(n)
    return _rolling(_as_array(values), n, "max")


def rolling_min(values: npt.ArrayLike, n: int) -> Array:
    """Lowest of the last ``n`` values (the current one included)."""
    _check_period(n)
    return _rolling(_as_array(values), n, "min")


def ema(values: npt.ArrayLike, n: int) -> Array:
    """Exponential moving average, ``alpha = 2 / (n + 1)``, seeded with the mean of the first ``n`` values.

    Leading ``nan`` values (the warm-up of another indicator) are skipped: the average starts
    with the first ``n`` defined values.
    """
    _check_period(n)
    x = _as_array(values)
    out = np.full(x.shape, np.nan)
    defined = np.flatnonzero(~np.isnan(x))
    if len(defined) == 0:
        return out
    first = int(defined[0])
    if len(x) - first < n:
        return out
    alpha = 2.0 / (n + 1.0)
    value = float(x[first : first + n].mean())
    out[first + n - 1] = value
    for i in range(first + n, len(x)):
        value += alpha * (x[i] - value)
        out[i] = value
    return out


def _wilder(values: Array, n: int, first: int) -> Array:
    """Wilder's smoothing of ``values[first:]``: seed = mean of ``n`` values, then (prev * (n-1) + x) / n."""
    out = np.full(values.shape, np.nan)
    if len(values) - first < n:
        return out
    value = float(values[first : first + n].mean())
    out[first + n - 1] = value
    for i in range(first + n, len(values)):
        value = (value * (n - 1) + values[i]) / n
        out[i] = value
    return out


def rsi(values: npt.ArrayLike, n: int = 14) -> Array:
    """Wilder's Relative Strength Index, 0 to 100.  Defined from position ``n``."""
    _check_period(n)
    x = _as_array(values)
    out = np.full(x.shape, np.nan)
    if len(x) <= n:
        return out
    change = np.diff(x, prepend=x[0])
    gain = _wilder(np.where(change > 0, change, 0.0), n, 1)
    loss = _wilder(np.where(change < 0, -change, 0.0), n, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        out = 100.0 - 100.0 / (1.0 + gain / loss)
    out[(loss == 0) & (gain > 0)] = 100.0
    out[(loss == 0) & (gain == 0)] = 50.0  # no movement at all: neither overbought nor oversold
    return out


def true_range(high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike) -> Array:
    """Range of each bar including the gap from the previous close (first bar: high - low)."""
    h, lo, c = _as_array(high), _as_array(low), _as_array(close)
    previous = np.concatenate(([c[0]], c[:-1])) if len(c) else c
    tr = np.maximum(h - lo, np.maximum(np.abs(h - previous), np.abs(lo - previous)))
    if len(tr):
        tr[0] = h[0] - lo[0]
    return tr


def atr(high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike, n: int = 14) -> Array:
    """Wilder's Average True Range.  Defined from position ``n - 1``."""
    _check_period(n)
    return _wilder(true_range(high, low, close), n, 0)


def bollinger(values: npt.ArrayLike, n: int = 20, k: float = 2.0) -> tuple[Array, Array, Array]:
    """Bollinger bands: (middle, upper, lower) = SMA and SMA +/- ``k`` population standard deviations."""
    middle = sma(values, n)
    width = k * rolling_std(values, n)
    return middle, middle + width, middle - width


def macd(values: npt.ArrayLike, fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[Array, Array, Array]:
    """MACD: (line, signal, histogram).  Line = EMA(fast) - EMA(slow); signal = EMA(line, signal)."""
    if fast >= slow:
        raise ValueError("the fast period must be shorter than the slow one")
    line = ema(values, fast) - ema(values, slow)
    signal_line = ema(line, signal)
    return line, signal_line, line - signal_line


def adx(high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike, n: int = 14) -> tuple[Array, Array, Array]:
    """Wilder's directional movement: (ADX, +DI, -DI), each 0 to 100.

    +DI and -DI are defined from position ``n``, ADX from position ``2n - 1``.
    """
    _check_period(n)
    h, lo = _as_array(high), _as_array(low)
    nan = np.full(h.shape, np.nan)
    if len(h) <= n:
        return nan, nan.copy(), nan.copy()
    up = np.diff(h, prepend=h[0])
    down = -np.diff(lo, prepend=lo[0])
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    tr = true_range(h, lo, close)
    # the first bar has no previous bar: directional movement starts at position 1
    smoothed_tr = _wilder(tr, n, 1)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * _wilder(plus_dm, n, 1) / smoothed_tr
        minus_di = 100.0 * _wilder(minus_dm, n, 1) / smoothed_tr
        total = plus_di + minus_di
        dx = np.where(total > 0, 100.0 * np.abs(plus_di - minus_di) / total, 0.0)
    flat = smoothed_tr == 0  # no range at all in the window: no direction either
    plus_di[flat] = 0.0
    minus_di[flat] = 0.0
    dx[np.isnan(smoothed_tr)] = np.nan
    return _wilder(dx, n, n), plus_di, minus_di


def stochastic(
    high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike, n: int = 14, smooth: int = 3, signal: int = 3
) -> tuple[Array, Array]:
    """Stochastic oscillator: (%K, %D), each 0 to 100.

    Raw %K is where the close stands in the range of the last ``n`` bars; %K is its simple
    average over ``smooth`` bars (1 = the fast stochastic) and %D the average of %K over
    ``signal`` bars.  A range of zero gives 50: the close is neither high nor low in it.
    """
    c = _as_array(close)
    top, bottom = rolling_max(high, n), rolling_min(low, n)
    span = top - bottom
    with np.errstate(divide="ignore", invalid="ignore"):
        raw = np.where(span > 0, 100.0 * (c - bottom) / span, 50.0)
    raw[np.isnan(span)] = np.nan
    k = sma(raw, smooth)
    return k, sma(k, signal)


def cci(high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike, n: int = 20) -> Array:
    """Lambert's Commodity Channel Index.

    The typical price (high + low + close) / 3 against its own average, in units of 0.015
    times its mean absolute deviation, so that most values fall between -100 and +100.
    No deviation at all gives 0.
    """
    typical = (_as_array(high) + _as_array(low) + _as_array(close)) / 3.0
    mean = sma(typical, n)
    deviation = _rolling(typical, n, "mad")
    with np.errstate(divide="ignore", invalid="ignore"):
        out = np.where(deviation > 0, (typical - mean) / (0.015 * deviation), 0.0)
    out[np.isnan(deviation)] = np.nan
    return out


def keltner(
    high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike, n: int = 20, atr_n: int = 10, k: float = 2.0
) -> tuple[Array, Array, Array]:
    """Keltner channel: (middle, upper, lower) = EMA of the close and EMA +/- ``k`` average true ranges."""
    middle = ema(close, n)
    width = k * atr(high, low, close, atr_n)
    return middle, middle + width, middle - width


def supertrend(
    high: npt.ArrayLike, low: npt.ArrayLike, close: npt.ArrayLike, n: int = 10, multiple: float = 3.0
) -> tuple[Array, Array]:
    """Supertrend: (line, direction).  Direction is +1 while the close is above the line, -1 below.

    The line trails the price at ``multiple`` average true ranges from the middle of the bar
    and only ever moves toward the price; the direction flips when a bar closes across it.
    """
    _check_period(n)
    h, lo, c = _as_array(high), _as_array(low), _as_array(close)
    line = np.full(c.shape, np.nan)
    direction = np.full(c.shape, np.nan)
    width = multiple * atr(h, lo, c, n)
    middle = (h + lo) / 2.0
    upper_raw, lower_raw = middle + width, middle - width
    start = n - 1
    if len(c) <= start:
        return line, direction
    upper, lower = float(upper_raw[start]), float(lower_raw[start])
    trend = 1.0 if c[start] >= middle[start] else -1.0
    line[start], direction[start] = (lower if trend > 0 else upper), trend
    for i in range(start + 1, len(c)):
        # a band only tightens, unless the last close already broke through it
        upper = float(upper_raw[i]) if (upper_raw[i] < upper or c[i - 1] > upper) else upper
        lower = float(lower_raw[i]) if (lower_raw[i] > lower or c[i - 1] < lower) else lower
        if trend > 0 and c[i] < lower:
            trend = -1.0
        elif trend < 0 and c[i] > upper:
            trend = 1.0
        line[i], direction[i] = (lower if trend > 0 else upper), trend
    return line, direction


#: up to this many values a regression channel is worked out on the windows themselves
_EXACT_WINDOW = 128


def regression_channel(
    values: npt.ArrayLike, n: int = 100, k: float = 2.0, ahead: int = 0
) -> tuple[Array, Array, Array, Array]:
    """Trend channel by linear regression: (middle, upper, lower, slope).

    A straight line is fitted to the last ``n`` values.  ``middle`` is that line at the newest
    of them, ``slope`` its rise per step, and the channel lies ``k`` standard deviations of the
    values around the line on either side (the population standard deviation, as for
    Bollinger bands).

    With ``ahead`` the line fitted to the ``n`` values that end ``ahead`` steps earlier is
    extended to the present: the channel as it could be drawn before the newest values were
    known, which is the one a price can break out of.

    Long windows (a day of minute bars is 1 440 values) are worked out with running sums of
    the values relative to the first of them; the error stays below a billionth of a price.
    """
    if n < 3:
        raise ValueError("a regression channel needs at least 3 values")
    if ahead < 0:
        raise ValueError("ahead must not be negative")
    y = _as_array(values)
    size = len(y)
    middle = np.full(y.shape, np.nan)
    slope = np.full(y.shape, np.nan)
    sigma = np.full(y.shape, np.nan)
    mean_x = (n - 1) / 2.0
    sxx = n * (n * n - 1) / 12.0
    if size >= n and n <= _EXACT_WINDOW:
        x = np.arange(n, dtype=np.float64) - mean_x
        windows = np.lib.stride_tricks.sliding_window_view(y, n)
        step = max(1, _CHUNK_BYTES // (8 * n))
        for first in range(0, len(windows), step):
            block = windows[first : first + step]
            mean_y = block.mean(axis=1)
            b = (block * x).sum(axis=1) / sxx  # row by row: a value must not depend on how many rows are worked on
            miss = block - mean_y[:, None] - b[:, None] * x
            rows = slice(first + n - 1, first + n - 1 + len(block))
            middle[rows] = mean_y + b * mean_x
            slope[rows] = b
            sigma[rows] = np.sqrt((miss * miss).mean(axis=1))
    elif size >= n:
        reference = y[0]
        z = y - reference
        position = np.arange(size, dtype=np.float64)
        sum_z = np.concatenate(([0.0], np.cumsum(z)))
        sum_iz = np.concatenate(([0.0], np.cumsum(position * z)))
        sum_zz = np.concatenate(([0.0], np.cumsum(z * z)))
        end = np.arange(n, size + 1)  # one past the newest value of each window
        start = end - n
        sy = sum_z[end] - sum_z[start]
        sxy = (sum_iz[end] - sum_iz[start]) - start * sy  # with x = 0 .. n-1 inside the window
        syy = sum_zz[end] - sum_zz[start]
        b = (sxy - mean_x * sy) / sxx
        mean_y = sy / n
        residual = syy - n * mean_y * mean_y - b * b * sxx
        middle[n - 1 :] = mean_y + b * mean_x + reference
        slope[n - 1 :] = b
        sigma[n - 1 :] = np.sqrt(np.maximum(residual, 0.0) / n)
    if ahead:
        moved_middle = np.full(y.shape, np.nan)
        moved_slope = np.full(y.shape, np.nan)
        moved_sigma = np.full(y.shape, np.nan)
        if size > ahead:
            moved_middle[ahead:] = middle[:-ahead] + ahead * slope[:-ahead]
            moved_slope[ahead:] = slope[:-ahead]
            moved_sigma[ahead:] = sigma[:-ahead]
        middle, slope, sigma = moved_middle, moved_slope, moved_sigma
    return middle, middle + k * sigma, middle - k * sigma, slope
