"""Quote validation, de-duplication and outlier filtering.

Every raw provider quote goes through :class:`QuoteNormalizer` before anything else
sees it.  The outcome is always explicit and always counted - nothing is dropped
silently:

``ACCEPT``     a valid quote that becomes the new quote in force
``DUPLICATE``  identical to the previous accepted quote (same sub-second timestamp, prices,
               venue): the same message delivered twice
``LATE``       valid, but its source timestamp is older than the quote in force; it is
               stored as a tick but does not replace newer market state
``REJECT``     invalid (reason given): bad price, crossed, spread too wide, bad
               timestamp, or an unconfirmed price jump (outlier)

Why the spread and jump filters matter: the provider's FX feed consolidates quotes
from many contributors and occasionally contains quotes with absurd spreads or
levels.  Left in, a single such quote would create a fake high/low and a fake
breakout.  A real level shift is still accepted: once ``jump_confirm_quotes``
consecutive quotes agree on the new level the filter lets it through and reports a
``level_shift``.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.models import QuoteTick, RawQuote
from fxlab.timeutil import SECOND_MS

#: quotes stamped before this are certainly garbage (provider history starts in 2009)
MIN_VALID_TS_MS = 1_230_768_000_000  # 2009-01-01T00:00:00Z


class Verdict(StrEnum):
    ACCEPT = "accept"
    DUPLICATE = "duplicate"
    LATE = "late"
    REJECT = "reject"


class RejectReason(StrEnum):
    UNKNOWN_SYMBOL = "unknown_symbol"
    INVALID_PRICE = "invalid_price"
    BAD_TIMESTAMP = "bad_timestamp"
    FUTURE_TIMESTAMP = "future_timestamp"
    CROSSED = "crossed"
    WIDE_SPREAD = "wide_spread"
    OUTLIER = "outlier"


@dataclass(frozen=True, slots=True)
class NormalizeResult:
    verdict: Verdict
    tick: QuoteTick | None = None
    reason: RejectReason | None = None
    level_shift: bool = False
    #: source timestamp ahead of the local clock by this many ms (0 when not ahead)
    future_skew_ms: int = 0


@dataclass
class _SymbolState:
    last: QuoteTick | None = None
    pending_mid: float | None = None
    pending_count: int = 0
    counters: Counter[str] = field(default_factory=Counter)


def _is_price(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


class QuoteNormalizer:
    """Stateful validator. One instance serves all instruments; state is per symbol."""

    def __init__(self, registry: InstrumentRegistry) -> None:
        self._registry = registry
        self._state: dict[str, _SymbolState] = {}
        self.unknown_symbols: Counter[str] = Counter()

    def _st(self, symbol: str) -> _SymbolState:
        st = self._state.get(symbol)
        if st is None:
            st = self._state[symbol] = _SymbolState()
        return st

    # -- public API --------------------------------------------------------------
    def seed(self, tick: QuoteTick) -> None:
        """Install a reference quote (e.g. after a restart) without counting it."""
        self._st(tick.symbol).last = tick

    def last(self, symbol: str) -> QuoteTick | None:
        st = self._state.get(symbol)
        return st.last if st else None

    def counters(self, symbol: str) -> dict[str, int]:
        st = self._state.get(symbol)
        return dict(st.counters) if st else {}

    def process(self, raw: RawQuote) -> NormalizeResult:
        if raw.symbol not in self._registry:
            self.unknown_symbols[raw.symbol] += 1
            return NormalizeResult(Verdict.REJECT, reason=RejectReason.UNKNOWN_SYMBOL)

        inst = self._registry.get(raw.symbol)
        dq = inst.data_quality
        st = self._st(raw.symbol)
        st.counters["received"] += 1

        def reject(reason: RejectReason) -> NormalizeResult:
            st.counters["rejected"] += 1
            st.counters[f"rejected:{reason.value}"] += 1
            return NormalizeResult(Verdict.REJECT, reason=reason)

        bid, ask, ts = raw.bid, raw.ask, raw.source_ts_ms
        if not (_is_price(bid) and _is_price(ask)):
            return reject(RejectReason.INVALID_PRICE)
        assert bid is not None and ask is not None  # narrowed by _is_price
        if not isinstance(ts, int) or isinstance(ts, bool) or ts < MIN_VALID_TS_MS:
            return reject(RejectReason.BAD_TIMESTAMP)

        future_skew = ts - raw.recv_ts_ms
        if future_skew > dq.hard_future_skew_ms:
            return reject(RejectReason.FUTURE_TIMESTAMP)
        soft_skew = future_skew if future_skew > dq.soft_future_skew_ms else 0
        if soft_skew:
            st.counters["future_skew"] += 1

        if ask < bid:
            return reject(RejectReason.CROSSED)
        # tolerance: a spread of exactly the limit must not fail on binary float noise
        if (ask - bid) / inst.pip_size > dq.max_spread_pips + 1e-6:
            return reject(RejectReason.WIDE_SPREAD)

        tick = QuoteTick(
            symbol=raw.symbol,
            bid=float(bid),
            ask=float(ask),
            source_ts_ms=ts,
            recv_ts_ms=raw.recv_ts_ms,
            exchange=raw.exchange,
        )

        last = st.last
        if last is not None:
            # A repeat is only provably the same message when its timestamp is fine enough to
            # tell two quotes apart.  Massive stamps historical quotes in whole seconds: there,
            # equal consecutive quotes are separate quotes, and the provider counts them too.
            if (
                ts == last.source_ts_ms
                and ts % SECOND_MS != 0
                and tick.bid == last.bid
                and tick.ask == last.ask
                and tick.exchange == last.exchange
            ):
                st.counters["duplicate"] += 1
                return NormalizeResult(Verdict.DUPLICATE, tick=tick, future_skew_ms=soft_skew)
            if ts < last.source_ts_ms:
                st.counters["late"] += 1
                return NormalizeResult(Verdict.LATE, tick=tick, future_skew_ms=soft_skew)

        level_shift = False
        if last is not None and raw.recv_ts_ms - last.recv_ts_ms <= dq.jump_ref_max_age_ms:
            deviation_pct = abs(tick.mid - last.mid) / last.mid * 100.0
            if deviation_pct > dq.max_jump_pct:
                confirms_pending = (
                    st.pending_mid is not None
                    and abs(tick.mid - st.pending_mid) / st.pending_mid * 100.0 <= dq.max_jump_pct
                )
                st.pending_count = st.pending_count + 1 if confirms_pending else 1
                st.pending_mid = tick.mid
                if st.pending_count < dq.jump_confirm_quotes:
                    return reject(RejectReason.OUTLIER)
                level_shift = True
                st.counters["level_shift"] += 1

        st.pending_mid = None
        st.pending_count = 0
        st.last = tick
        st.counters["accepted"] += 1
        return NormalizeResult(Verdict.ACCEPT, tick=tick, level_shift=level_shift, future_skew_ms=soft_skew)
