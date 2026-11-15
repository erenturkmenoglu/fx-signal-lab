"""The comparison of live ticks with the provider's history (pure part)."""

from __future__ import annotations

from fxlab.services.reconcile import Quote, compare, format_reconciliation
from tests.conftest import T0

A, B, C = (1.16000, 1.16008), (1.16001, 1.16009), (1.16002, 1.16010)


def live(*items: tuple[int, tuple[float, float]]) -> list[Quote]:
    return [(T0 + ms, *price) for ms, price in items]


def history(*items: tuple[int, tuple[float, float]]) -> list[Quote]:
    """Stamped in whole seconds, as Massive's /v3/quotes was on 15 November 2026."""
    return [(T0 + ms - ms % 1000, *price) for ms, price in items]


def report(mine: list[Quote], theirs: list[Quote], seconds: int = 3) -> dict:
    return compare(mine, theirs, T0, T0 + seconds * 1000) | {
        "symbol": "EURUSD",
        "official_endpoints": True,
        "window": {"from": "a", "to": "b"},
        "recovered_ticks_in_window": 0,
    }


def test_identical_streams_agree_even_when_only_one_side_has_milliseconds() -> None:
    quotes = ((120, A), (480, B), (1310, B), (2050, C), (2900, A))
    r = report(live(*quotes), history(*quotes))
    assert r["seconds_captured_live"] == r["seconds_identical"] == r["seconds_with_same_last_quote"] == 3
    assert r["live_quotes"] == r["history_quotes_in_those_seconds"] == r["quotes_in_both"] == 5
    assert r["live_stamps_on_whole_seconds"] == 0.0 and r["history_stamps_on_whole_seconds"] == 1.0
    assert r["examples_of_differences"] == []
    assert "no differences" in format_reconciliation(r)


def test_a_quote_missing_from_the_stream_is_reported_with_its_second() -> None:
    theirs = ((120, A), (480, B), (1310, B), (1700, C), (2050, C))
    mine = ((120, A), (480, B), (1310, B), (2050, C))  # 1700 never arrived
    r = report(live(*mine), history(*theirs))
    assert (r["seconds_identical"], r["seconds_with_same_count"], r["seconds_with_same_last_quote"]) == (2, 2, 2)
    assert (r["live_quotes"], r["history_quotes_in_those_seconds"], r["quotes_in_both"]) == (4, 5, 4)
    (example,) = r["examples_of_differences"]
    assert example["live_quotes"] == 1 and example["history_quotes"] == 2
    assert example["live_last"] == list(B) and example["history_last"] == list(C)
    text = format_reconciliation(r)
    assert "first differences" in text and "not stored as ticks" in text


def test_same_quotes_in_another_order_differ_only_in_the_last_quote() -> None:
    r = report(live((100, A), (600, B)), history((600, B), (100, A)), seconds=1)
    assert r["seconds_with_same_count"] == 1 and r["seconds_identical"] == 1
    assert r["seconds_with_same_last_quote"] == 0 and len(r["examples_of_differences"]) == 1


def test_seconds_without_live_capture_are_counted_but_not_compared() -> None:
    theirs = ((120, A), (1310, B), (2050, C))
    r = report(live((120, A), (2050, C)), history(*theirs))  # second 1: an outage
    assert r["seconds_captured_live"] == 2 and r["seconds_only_in_history"] == 1
    assert r["seconds_identical"] == 2 and r["history_quotes_in_those_seconds"] == 2
    assert r["history_quotes_in_window"] == 3


def test_quotes_outside_the_window_are_ignored_and_a_shift_shows_up() -> None:
    # the stream stamps one second later than the history: nothing matches second by second ...
    mine = live((1100, A), (2100, B))
    theirs = history((100, A), (1100, B), (5000, C))
    r = report(mine, theirs)
    assert r["seconds_identical"] == 0 and r["quotes_in_both"] == 0
    # ... but ignoring the timestamps both quotes are there, which points at a shift, not at a loss
    assert r["quotes_in_both_ignoring_time"] == 2 and r["history_quotes_in_window"] == 2


def test_nothing_captured_is_not_an_error_of_the_comparison() -> None:
    r = report([], history((100, A)))
    assert r["seconds_captured_live"] == 0 and r["live_stamps_on_whole_seconds"] is None
    assert "0 of 3" in format_reconciliation(r)


def test_a_report_with_an_error_prints_the_error_only() -> None:
    text = format_reconciliation({"symbol": "EURUSD", "official_endpoints": False, "error": "no live ticks yet"})
    assert "no live ticks yet" in text and "NOT Massive's production endpoints" in text
