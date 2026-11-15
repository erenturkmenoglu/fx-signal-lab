"""Core domain records flowing through the pipeline.

All timestamps are epoch milliseconds (UTC).  All intervals are start-labelled and
half-open, see :mod:`fxlab.timeutil`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ObsStatus(StrEnum):
    """Quality class of a 1-second observation.

    The four classes are mutually exclusive and are what the specification asks the
    system to distinguish explicitly:

    * ``FRESH``   - at least one valid quote was *received during this second*.
    * ``CARRIED`` - no quote arrived in this second; the last valid quote is carried
      forward and is still younger than the instrument's stale threshold.
    * ``STALE``   - the quote in force is older than the stale threshold.  The price is
      kept for diagnostics but must not be used to generate signals.
    * ``MISSING`` - no valid quote is known at all (prices are ``None``).
    """

    FRESH = "fresh"
    CARRIED = "carried"
    STALE = "stale"
    MISSING = "missing"

    @property
    def usable(self) -> bool:
        """True when the observation carries a price that may feed bars/signals."""
        return self in (ObsStatus.FRESH, ObsStatus.CARRIED)


class DataSource(StrEnum):
    """Provenance of a stored row.  Never mix these up silently."""

    LIVE = "live"  # sampled by this system from the live provider quote stream
    #: observations rebuilt after the fact from the provider's historical quotes.  Bars
    #: built by this system keep source ``live`` and count such seconds in ``n_backfilled``.
    BACKFILL = "backfill"
    MASSIVE_AGG = "massive_agg"  # provider-computed aggregate bars (REST), BID based
    MOCK = "mock"  # synthetic data from the mock adapter - never market data


class PriceBasis(StrEnum):
    MID = "mid"
    BID = "bid"


#: price basis implied by each source
SOURCE_PRICE_BASIS: dict[DataSource, PriceBasis] = {
    DataSource.LIVE: PriceBasis.MID,
    DataSource.BACKFILL: PriceBasis.MID,
    DataSource.MOCK: PriceBasis.MID,
    DataSource.MASSIVE_AGG: PriceBasis.BID,
}


@dataclass(frozen=True, slots=True)
class RawQuote:
    """A quote exactly as parsed from a provider message - not yet validated."""

    symbol: str
    bid: float | None
    ask: float | None
    source_ts_ms: int | None
    recv_ts_ms: int
    exchange: int | None = None


@dataclass(frozen=True, slots=True)
class QuoteTick:
    """A validated best bid/offer quote."""

    symbol: str
    bid: float
    ask: float
    source_ts_ms: int  # provider timestamp of the quote
    recv_ts_ms: int  # local wall-clock receipt time (== source_ts_ms in replays)
    exchange: int | None = None

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class Observation:
    """One cell of the 1-second observation grid for one instrument.

    ``ts_ms`` is the start of the window ``[ts_ms, ts_ms + 1000)``.  ``bid``/``ask`` are
    the quote in force at the *end* of the window.  ``mid_open/high/low`` describe the
    path of the mid price inside the window and exist only when quotes actually arrived
    (``n_quotes > 0``) - no price movement is ever invented for quiet seconds.
    """

    symbol: str
    ts_ms: int
    status: ObsStatus
    bid: float | None
    ask: float | None
    mid_open: float | None
    mid_high: float | None
    mid_low: float | None
    n_quotes: int
    quote_ts_ms: int | None  # source timestamp of the quote in force
    age_ms: int | None  # age of that quote at the end of the window
    source: DataSource = DataSource.LIVE

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def end_ms(self) -> int:
        return self.ts_ms + 1000


@dataclass(frozen=True, slots=True)
class Bar:
    """An OHLC bar built from 1-second observations (or supplied by the provider).

    For bars built by this system the prices are mid prices and the completeness
    counters are populated.  For provider aggregates (``source = massive_agg``) the
    prices are BID prices and the counters other than ``n_quotes`` are ``None``.
    """

    symbol: str
    timeframe: str
    ts_ms: int  # bar start
    open: float
    high: float
    low: float
    close: float
    source: DataSource
    price_basis: PriceBasis
    bid_close: float | None = None
    ask_close: float | None = None
    n_quotes: int | None = None  # quote updates inside the bar
    n_fresh: int | None = None  # seconds with at least one update
    n_valid: int | None = None  # seconds with a usable price (fresh or carried)
    n_expected: int | None = None  # seconds the bar spans
    n_backfilled: int | None = None  # usable seconds that were rebuilt from historical quotes
    complete: bool | None = None  # n_valid == n_expected
