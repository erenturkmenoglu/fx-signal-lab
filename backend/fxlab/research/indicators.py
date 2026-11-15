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
    """Exact rolling mean or population standard deviation over ``n`` values.

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
        out[start + n - 1 : start + n - 1 + len(block)] = block.mean(axis=1) if how == "mean" else block.std(axis=1)
    return out


def sma(values: npt.ArrayLike, n: int) -> Array:
    """Simple moving average of the last ``n`` values."""
    _check_period(n)
    return _rolling(_as_array(values), n, "mean")


def rolling_std(values: npt.ArrayLike, n: int) -> Array:
    """Population standard deviation of the last ``n`` values."""
    _check_period(n)
    return _rolling(_as_array(values), n, "std")


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
