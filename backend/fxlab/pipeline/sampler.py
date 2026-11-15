"""The 1-second observation sampler.

Turns an irregular stream of validated quotes into a regular grid with exactly one
:class:`~fxlab.domain.models.Observation` per second, **without ever inventing a
market update**:

* a second in which quotes arrived is ``FRESH`` and records how many arrived and the
  open/high/low of the mid inside that second;
* a quiet second repeats the quote in force and says so (``CARRIED``);
* once that quote is older than the instrument's stale threshold the observation is
  ``STALE``; if no quote is known at all it is ``MISSING``.

The class is pure and deterministic: it has no clock and performs no I/O.  Time only
advances through the timestamps passed to :meth:`on_quote` and :meth:`advance`, so the
exact same code samples the live stream (driven by receipt time) and rebuilds history
from provider quotes (driven by their source time).

Causality: a quote is assigned to the window that is open when it is *processed*;
windows that are already closed are never reopened, so an observation can never
contain information that arrived after it was emitted.
"""

from __future__ import annotations

from fxlab.domain.models import DataSource, Observation, ObsStatus, QuoteTick
from fxlab.timeutil import SECOND_MS, floor_ms


class SecondSampler:
    def __init__(
        self,
        symbol: str,
        stale_after_ms: int,
        start_ms: int,
        *,
        source: DataSource = DataSource.LIVE,
        max_catchup_s: int | None = 900,
    ) -> None:
        """
        :param stale_after_ms: quote age (at the end of a window) beyond which the
            observation is STALE.
        :param start_ms: the first window is the second containing ``start_ms``.
        :param max_catchup_s: if the sampler falls further behind than this (process
            suspended, clock jump) it does not flood the grid with thousands of rows;
            it skips ahead and reports the skipped range via :meth:`pop_skipped` so the
            gap detector can take over.  ``None`` disables the limit (replays).
        """
        self.symbol = symbol
        self._stale_after_ms = stale_after_ms
        self._source = source
        self._max_catchup_ms = None if max_catchup_s is None else max_catchup_s * SECOND_MS
        self._win_start = floor_ms(start_ms, SECOND_MS)
        self._last: QuoteTick | None = None
        self._n = 0
        self._open: float | None = None
        self._high: float | None = None
        self._low: float | None = None
        self._skipped: list[tuple[int, int]] = []

    # -- state ---------------------------------------------------------------------
    @property
    def window_start_ms(self) -> int:
        """Start of the window currently being accumulated."""
        return self._win_start

    @property
    def last_quote(self) -> QuoteTick | None:
        return self._last

    def seed(self, tick: QuoteTick) -> None:
        """Install the quote in force (after a restart / before a replay) without counting it."""
        self._last = tick

    def pop_skipped(self) -> list[tuple[int, int]]:
        """Return and clear the ``[start, end)`` ranges skipped by the catch-up limit."""
        out, self._skipped = self._skipped, []
        return out

    # -- input ---------------------------------------------------------------------
    def on_quote(self, tick: QuoteTick) -> list[Observation]:
        """Feed a validated quote.  Returns the observations closed by its arrival."""
        closed = self.advance(tick.recv_ts_ms)
        mid = tick.mid
        if self._n == 0:
            self._open = self._high = self._low = mid
        else:
            assert self._high is not None and self._low is not None
            self._high = max(self._high, mid)
            self._low = min(self._low, mid)
        self._n += 1
        self._last = tick
        return closed

    def advance(self, now_ms: int) -> list[Observation]:
        """Close every window that ended at or before ``now_ms``."""
        out: list[Observation] = []
        if self._max_catchup_ms is not None and now_ms - self._win_start > self._max_catchup_ms + SECOND_MS:
            out.append(self._close_window())  # keep whatever the open window had collected
            resume = floor_ms(now_ms, SECOND_MS)
            if resume > self._win_start:
                self._skipped.append((self._win_start, resume))
                self._win_start = resume
            return out
        while self._win_start + SECOND_MS <= now_ms:
            out.append(self._close_window())
        return out

    # -- internals -------------------------------------------------------------------
    def _close_window(self) -> Observation:
        start = self._win_start
        end = start + SECOND_MS
        last = self._last
        if last is None:
            obs = Observation(
                symbol=self.symbol,
                ts_ms=start,
                status=ObsStatus.MISSING,
                bid=None,
                ask=None,
                mid_open=None,
                mid_high=None,
                mid_low=None,
                n_quotes=0,
                quote_ts_ms=None,
                age_ms=None,
                source=self._source,
            )
        else:
            age = max(0, end - last.source_ts_ms)
            if age > self._stale_after_ms:
                status = ObsStatus.STALE
            elif self._n > 0:
                status = ObsStatus.FRESH
            else:
                status = ObsStatus.CARRIED
            has_updates = self._n > 0
            obs = Observation(
                symbol=self.symbol,
                ts_ms=start,
                status=status,
                bid=last.bid,
                ask=last.ask,
                mid_open=self._open if has_updates else None,
                mid_high=self._high if has_updates else None,
                mid_low=self._low if has_updates else None,
                n_quotes=self._n,
                quote_ts_ms=last.source_ts_ms,
                age_ms=age,
                source=self._source,
            )
        self._n = 0
        self._open = self._high = self._low = None
        self._win_start = end
        return obs
