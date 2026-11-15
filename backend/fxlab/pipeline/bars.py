"""Multi-timeframe bar construction from the 1-second observation grid.

One implementation serves every use: the live stream, the warm start after a
restart and the rebuild after a backfill all feed observations through
:class:`BarAggregator`, so bars can never disagree depending on how they were made.

Rules
-----
* Only *usable* observations (``FRESH`` or ``CARRIED``) contribute a price.
  ``STALE`` and ``MISSING`` seconds contribute nothing and make the bar incomplete.
* A ``FRESH`` second contributes the real open/high/low/close of the mid inside that
  second.  A ``CARRIED`` second contributes one flat point (the quote in force).
  No movement is ever synthesised; ``n_fresh`` tells how many seconds really updated.
* A bar with no usable second is not emitted at all.
* ``complete`` is true only when every second of the bar had a usable price.
* Provenance is kept: ``n_backfilled`` counts the seconds whose observation was rebuilt
  from historical provider quotes rather than captured live.
* Bars are start-labelled; a bar is emitted when its last second has been processed
  or when an observation of a later window shows up.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from fxlab.domain.calendar import MarketCalendar
from fxlab.domain.models import SOURCE_PRICE_BASIS, Bar, DataSource, Observation, ObsStatus
from fxlab.domain.timeframes import BAR_TIMEFRAMES, Timeframe, bar_window
from fxlab.timeutil import SECOND_MS


@dataclass
class _Partial:
    start: int
    end: int
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    bid_close: float | None = None
    ask_close: float | None = None
    n_quotes: int = 0
    n_fresh: int = 0
    n_valid: int = 0
    n_backfilled: int = 0


class BarAggregator:
    def __init__(
        self,
        symbol: str,
        calendar: MarketCalendar,
        timeframes: Sequence[Timeframe] = BAR_TIMEFRAMES,
        source: DataSource = DataSource.LIVE,
    ) -> None:
        self.symbol = symbol
        self._calendar = calendar
        self._timeframes = tuple(timeframes)
        self._source = source
        self._partials: dict[str, _Partial] = {}
        self._last_ts: int | None = None
        self.out_of_order = 0

    @property
    def last_ts_ms(self) -> int | None:
        """Start of the most recent observation consumed."""
        return self._last_ts

    def on_observation(self, obs: Observation) -> list[Bar]:
        """Consume one observation (strictly increasing ``ts_ms``) and return completed bars."""
        if self._last_ts is not None and obs.ts_ms <= self._last_ts:
            self.out_of_order += 1
            return []
        self._last_ts = obs.ts_ms

        out: list[Bar] = []
        for tf in self._timeframes:
            partial = self._partials.get(tf.code)
            if partial is not None and obs.ts_ms >= partial.end:
                bar = self._to_bar(tf, partial, final=True)
                if bar is not None:
                    out.append(bar)
                partial = None
            if partial is None:
                start, end = bar_window(obs.ts_ms, tf, self._calendar)
                partial = self._partials[tf.code] = _Partial(start, end)
            self._apply(partial, obs)
            if obs.ts_ms + SECOND_MS >= partial.end:
                bar = self._to_bar(tf, partial, final=True)
                if bar is not None:
                    out.append(bar)
                del self._partials[tf.code]
        return out

    def partial_bars(self) -> dict[str, Bar]:
        """Snapshots of the bars still being built (``complete`` is always False)."""
        out: dict[str, Bar] = {}
        for tf in self._timeframes:
            partial = self._partials.get(tf.code)
            if partial is None:
                continue
            bar = self._to_bar(tf, partial, final=False)
            if bar is not None:
                out[tf.code] = bar
        return out

    # -- internals -------------------------------------------------------------------
    @staticmethod
    def _apply(partial: _Partial, obs: Observation) -> None:
        if not obs.status.usable:
            return
        mid = obs.mid
        if mid is None:
            return
        if obs.status is ObsStatus.FRESH and obs.n_quotes > 0:
            o = obs.mid_open if obs.mid_open is not None else mid
            h = obs.mid_high if obs.mid_high is not None else mid
            lo = obs.mid_low if obs.mid_low is not None else mid
            partial.n_fresh += 1
            partial.n_quotes += obs.n_quotes
        else:
            o = h = lo = mid
        if partial.n_valid == 0:
            partial.open, partial.high, partial.low = o, h, lo
        else:
            partial.high = max(partial.high, h)
            partial.low = min(partial.low, lo)
        partial.close = mid
        partial.bid_close = obs.bid
        partial.ask_close = obs.ask
        partial.n_valid += 1
        if obs.source is DataSource.BACKFILL:
            partial.n_backfilled += 1

    def _to_bar(self, tf: Timeframe, partial: _Partial, *, final: bool) -> Bar | None:
        if partial.n_valid == 0:
            return None
        n_expected = (partial.end - partial.start) // SECOND_MS
        return Bar(
            symbol=self.symbol,
            timeframe=tf.code,
            ts_ms=partial.start,
            open=partial.open,
            high=partial.high,
            low=partial.low,
            close=partial.close,
            source=self._source,
            price_basis=SOURCE_PRICE_BASIS[self._source],
            bid_close=partial.bid_close,
            ask_close=partial.ask_close,
            n_quotes=partial.n_quotes,
            n_fresh=partial.n_fresh,
            n_valid=partial.n_valid,
            n_expected=n_expected,
            n_backfilled=partial.n_backfilled,
            complete=final and partial.n_valid == n_expected,
        )


def build_bars(
    observations: Iterable[Observation],
    symbol: str,
    calendar: MarketCalendar,
    timeframes: Sequence[Timeframe] = BAR_TIMEFRAMES,
    source: DataSource = DataSource.LIVE,
) -> tuple[list[Bar], BarAggregator]:
    """Batch helper: run observations through a fresh aggregator.

    Returns the completed bars and the aggregator (whose partial state can be adopted
    by the live pipeline for a warm start).
    """
    agg = BarAggregator(symbol, calendar, timeframes, source)
    bars: list[Bar] = []
    for obs in observations:
        bars.extend(agg.on_observation(obs))
    return bars, agg
