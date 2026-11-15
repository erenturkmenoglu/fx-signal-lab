"""Quote validation, de-duplication and outlier filtering."""

from __future__ import annotations

import math

import pytest

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.pipeline.normalizer import QuoteNormalizer, RejectReason, Verdict
from tests.conftest import T0, make_raw


@pytest.fixture()
def norm(registry: InstrumentRegistry) -> QuoteNormalizer:
    return QuoteNormalizer(registry)


def test_valid_quote_is_accepted(norm: QuoteNormalizer) -> None:
    res = norm.process(make_raw(T0, 1.16000, 1.16008, recv_ts_ms=T0 + 35))
    assert res.verdict is Verdict.ACCEPT
    assert res.tick is not None
    assert res.tick.mid == pytest.approx(1.16004)
    assert res.tick.source_ts_ms == T0 and res.tick.recv_ts_ms == T0 + 35
    assert norm.last("EURUSD") == res.tick
    assert norm.counters("EURUSD") == {"received": 1, "accepted": 1}


@pytest.mark.parametrize(
    "bid,ask",
    [
        (None, 1.1),
        (1.1, None),
        (math.nan, 1.1),
        (1.1, math.inf),
        (0.0, 1.1),
        (-1.0, 1.1),
        ("1.1", 1.2),
        (True, 1.2),
    ],
)
def test_invalid_prices_are_rejected(norm: QuoteNormalizer, bid: object, ask: object) -> None:
    res = norm.process(make_raw(T0, bid, ask))  # type: ignore[arg-type]
    assert (res.verdict, res.reason) == (Verdict.REJECT, RejectReason.INVALID_PRICE)
    assert norm.last("EURUSD") is None


@pytest.mark.parametrize("ts", [None, 0, 1_000_000_000, "1763128800000", 1763128800000.0])
def test_bad_timestamps_are_rejected(norm: QuoteNormalizer, ts: object) -> None:
    res = norm.process(make_raw(ts, recv_ts_ms=T0))  # type: ignore[arg-type]
    assert (res.verdict, res.reason) == (Verdict.REJECT, RejectReason.BAD_TIMESTAMP)


def test_crossed_quote_is_rejected_but_locked_is_accepted(norm: QuoteNormalizer) -> None:
    assert norm.process(make_raw(T0, 1.16010, 1.16000)).reason is RejectReason.CROSSED
    assert norm.process(make_raw(T0 + 1, 1.16000, 1.16000)).verdict is Verdict.ACCEPT


def test_wide_spread_uses_the_instrument_pip_size(norm: QuoteNormalizer) -> None:
    # EURUSD: limit is 15 pips = 0.0015
    assert norm.process(make_raw(T0, 1.16000, 1.16150)).verdict is Verdict.ACCEPT
    assert norm.process(make_raw(T0 + 1, 1.16000, 1.16160)).reason is RejectReason.WIDE_SPREAD
    # USDJPY: a pip is 0.01, so 0.10 (10 pips) passes and 0.20 (20 pips) does not
    assert norm.process(make_raw(T0, 154.300, 154.400, symbol="USDJPY")).verdict is Verdict.ACCEPT
    assert norm.process(make_raw(T0 + 1, 154.300, 154.500, symbol="USDJPY")).reason is RejectReason.WIDE_SPREAD


def test_the_documented_bad_quote_example_is_filtered(norm: QuoteNormalizer) -> None:
    """A quote like bid 1.50544 / ask 1.74033 (14% wide) must never reach the price series."""
    norm.process(make_raw(T0, 1.16000, 1.16008))
    res = norm.process(make_raw(T0 + 100, 1.50544, 1.74033))
    assert res.verdict is Verdict.REJECT and res.reason is RejectReason.WIDE_SPREAD
    assert norm.last("EURUSD").bid == 1.16000  # type: ignore[union-attr]


def test_future_timestamps(norm: QuoteNormalizer) -> None:
    # slightly ahead of the local clock (within the soft limit): plain accept
    assert norm.process(make_raw(T0 + 1000, recv_ts_ms=T0)).future_skew_ms == 0
    # beyond the soft limit: accepted but flagged, so health can report clock skew
    res = norm.process(make_raw(T0 + 5000, recv_ts_ms=T0 + 1))
    assert res.verdict is Verdict.ACCEPT and res.future_skew_ms == 4999
    assert norm.counters("EURUSD")["future_skew"] == 1
    # beyond the hard limit: garbage
    res = norm.process(make_raw(T0 + 120_000, recv_ts_ms=T0 + 2))
    assert res.reason is RejectReason.FUTURE_TIMESTAMP


def test_exact_duplicate_is_flagged_and_does_not_replace_state(norm: QuoteNormalizer) -> None:
    ts = T0 + 137
    first = norm.process(make_raw(ts, 1.16000, 1.16008))
    dup = norm.process(make_raw(ts, 1.16000, 1.16008, recv_ts_ms=ts + 3))
    assert dup.verdict is Verdict.DUPLICATE
    assert norm.last("EURUSD") is first.tick
    # same timestamp but a different price is a real update
    assert norm.process(make_raw(ts, 1.16001, 1.16009)).verdict is Verdict.ACCEPT
    # ... and going back to the earlier price is again a real update, not a duplicate
    assert norm.process(make_raw(ts, 1.16000, 1.16008)).verdict is Verdict.ACCEPT
    # a different venue id is not a duplicate either
    assert norm.process(make_raw(ts, 1.16000, 1.16008, exchange=7)).verdict is Verdict.ACCEPT


def test_equal_quotes_with_a_whole_second_timestamp_are_separate_quotes(norm: QuoteNormalizer) -> None:
    """Massive's historical quotes are stamped in whole seconds: a repeat is not a duplicate there."""
    assert T0 % 1000 == 0
    results = [norm.process(make_raw(T0, 1.16000, 1.16008, recv_ts_ms=T0 + i)) for i in range(3)]
    assert [r.verdict for r in results] == [Verdict.ACCEPT] * 3
    assert norm.counters("EURUSD") == {"received": 3, "accepted": 3}
    # one second later, still equal: a quote again
    assert norm.process(make_raw(T0 + 1000, 1.16000, 1.16008)).verdict is Verdict.ACCEPT


def test_out_of_order_quote_is_late_and_keeps_newer_state(norm: QuoteNormalizer) -> None:
    newer = norm.process(make_raw(T0 + 2000, 1.16010, 1.16018, recv_ts_ms=T0 + 2010))
    late = norm.process(make_raw(T0 + 1000, 1.16000, 1.16008, recv_ts_ms=T0 + 2020))
    assert late.verdict is Verdict.LATE and late.tick is not None
    assert norm.last("EURUSD") is newer.tick
    assert norm.counters("EURUSD")["late"] == 1


def test_single_spike_is_rejected_and_market_continues(norm: QuoteNormalizer) -> None:
    norm.process(make_raw(T0, 1.16000, 1.16008))
    spike = norm.process(make_raw(T0 + 100, 1.20000, 1.20008))  # +3.4% with a normal spread
    assert (spike.verdict, spike.reason) == (Verdict.REJECT, RejectReason.OUTLIER)
    back = norm.process(make_raw(T0 + 200, 1.16002, 1.16010))
    assert back.verdict is Verdict.ACCEPT and not back.level_shift
    # an isolated second spike later is again rejected: the first one left no memory behind
    assert norm.process(make_raw(T0 + 300, 1.20000, 1.20008)).reason is RejectReason.OUTLIER
    assert norm.counters("EURUSD")["rejected:outlier"] == 2


def test_real_level_shift_is_accepted_after_confirmation(norm: QuoteNormalizer) -> None:
    norm.process(make_raw(T0, 1.16000, 1.16008))
    r1 = norm.process(make_raw(T0 + 100, 1.17500, 1.17508))
    r2 = norm.process(make_raw(T0 + 200, 1.17510, 1.17518))
    r3 = norm.process(make_raw(T0 + 300, 1.17505, 1.17513))
    assert [r.verdict for r in (r1, r2, r3)] == [Verdict.REJECT, Verdict.REJECT, Verdict.ACCEPT]
    assert r3.level_shift
    # the new level is now the reference
    assert norm.process(make_raw(T0 + 400, 1.17506, 1.17514)).verdict is Verdict.ACCEPT
    assert norm.counters("EURUSD")["level_shift"] == 1


def test_unrelated_spikes_do_not_confirm_each_other(norm: QuoteNormalizer) -> None:
    norm.process(make_raw(T0, 1.16000, 1.16008))
    for i, level in enumerate((1.20, 1.10, 1.25, 1.05, 1.30)):
        res = norm.process(make_raw(T0 + 100 * (i + 1), level, level + 0.00008))
        assert res.reason is RejectReason.OUTLIER


def test_stale_reference_is_not_used_for_the_jump_filter(norm: QuoteNormalizer) -> None:
    """After a long silence (weekend, outage) the first quote is accepted as the new reference."""
    norm.process(make_raw(T0, 1.16000, 1.16008))
    after_gap = norm.process(make_raw(T0 + 3_600_000, 1.17500, 1.17508))
    assert after_gap.verdict is Verdict.ACCEPT and not after_gap.level_shift


def test_unknown_symbol_is_rejected_and_counted(norm: QuoteNormalizer) -> None:
    res = norm.process(make_raw(T0, symbol="XXXYYY"))
    assert (res.verdict, res.reason) == (Verdict.REJECT, RejectReason.UNKNOWN_SYMBOL)
    assert norm.unknown_symbols["XXXYYY"] == 1


def test_state_is_independent_per_symbol(norm: QuoteNormalizer) -> None:
    norm.process(make_raw(T0, 1.16000, 1.16008))
    res = norm.process(make_raw(T0, 154.300, 154.310, symbol="USDJPY"))
    assert res.verdict is Verdict.ACCEPT  # not compared against the EURUSD level
    assert norm.counters("USDJPY") == {"received": 1, "accepted": 1}


def test_every_quote_is_counted_exactly_once(norm: QuoteNormalizer) -> None:
    inputs = [
        make_raw(T0 + 5, 1.16000, 1.16008),
        make_raw(T0 + 5, 1.16000, 1.16008),  # duplicate
        make_raw(T0 - 500, 1.16000, 1.16009),  # late
        make_raw(T0 + 10, 1.16010, 1.16000),  # crossed
        make_raw(T0 + 20, None, 1.16000),  # invalid
        make_raw(T0 + 30, 1.16001, 1.16009),
    ]
    for raw in inputs:
        norm.process(raw)
    c = norm.counters("EURUSD")
    assert c["received"] == len(inputs)
    assert c["accepted"] + c["duplicate"] + c["late"] + c["rejected"] == c["received"]
