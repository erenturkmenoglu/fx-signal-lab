"""Compare live capture with the provider's own quote history.

Gap recovery rebuilds seconds from ``/v3/quotes``.  That is only as good as the assumption
behind it: that the history holds the quotes the stream delivered.  This check tests the
assumption on real data.  It takes a window of ticks that were captured live and asks the
provider for the same window, then compares the two second by second.

Read-only: one SELECT and one REST query.  It does not use the WebSocket connection, so it
can run while the application is collecting.

Nothing is judged against a threshold.  The report states what is equal and what differs,
with examples, and leaves the conclusion to the reader.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.config import Settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.marketdata.massive.rest import MassiveRestClient
from fxlab.marketdata.massive.symbols import rest_quotes_ticker
from fxlab.storage import schema as s
from fxlab.timeutil import MINUTE_MS, SECOND_MS, dt_to_ms, floor_ms, iso, ms_to_dt

#: (timestamp in ms, bid, ask)
Quote = tuple[int, float, float]

#: how long after the fact the history is trusted to be complete (not known for Massive yet)
SETTLE_MS = 3 * MINUTE_MS
MAX_EXAMPLES = 5


def _by_second(quotes: Sequence[Quote], start_ms: int, end_ms: int) -> dict[int, list[tuple[float, float]]]:
    out: dict[int, list[tuple[float, float]]] = {}
    for ts, bid, ask in quotes:
        if start_ms <= ts < end_ms:
            out.setdefault(floor_ms(ts, SECOND_MS), []).append((bid, ask))
    return out


def _whole_second_share(quotes: Sequence[Quote]) -> float | None:
    return sum(1 for q in quotes if q[0] % SECOND_MS == 0) / len(quotes) if quotes else None


def compare(live: Sequence[Quote], history: Sequence[Quote], start_ms: int, end_ms: int) -> dict[str, Any]:
    """Compare two quote lists inside ``[start_ms, end_ms)``; both in the order they were produced.

    Only seconds that hold at least one live tick are compared: a second without live capture
    (an outage, a restart) says nothing about whether stream and history agree.
    """
    live_s = _by_second(live, start_ms, end_ms)
    hist_s = _by_second(history, start_ms, end_ms)
    captured = sorted(live_s)

    same_count = same_last = same_quotes = 0
    live_total = hist_total = in_both = 0
    examples: list[dict[str, Any]] = []
    for second in captured:
        mine, theirs = live_s[second], hist_s.get(second, [])
        live_total += len(mine)
        hist_total += len(theirs)
        common = sum((Counter(mine) & Counter(theirs)).values())
        in_both += common
        equal_count = len(mine) == len(theirs)
        equal_last = bool(theirs) and mine[-1] == theirs[-1]
        equal_quotes = equal_count and common == len(mine)
        same_count += equal_count
        same_last += equal_last
        same_quotes += equal_quotes
        if not (equal_quotes and equal_last) and len(examples) < MAX_EXAMPLES:
            examples.append(
                {
                    "second": iso(second),
                    "live_quotes": len(mine),
                    "history_quotes": len(theirs),
                    "live_last": list(mine[-1]),
                    "history_last": list(theirs[-1]) if theirs else None,
                }
            )

    not_captured = sorted(set(hist_s) - set(live_s))
    in_window_live = [q for q in live if start_ms <= q[0] < end_ms]
    in_window_hist = [q for q in history if start_ms <= q[0] < end_ms]
    # ignoring time altogether: would the two agree if only the timestamps were shifted?
    anywhere = sum(
        (Counter((b, a) for _, b, a in in_window_live) & Counter((b, a) for _, b, a in in_window_hist)).values()
    )
    return {
        "seconds_in_window": (end_ms - start_ms) // SECOND_MS,
        "seconds_captured_live": len(captured),
        "seconds_only_in_history": len(not_captured),
        "live_quotes": live_total,
        "history_quotes_in_those_seconds": hist_total,
        "quotes_in_both": in_both,
        "seconds_with_same_count": same_count,
        "seconds_with_same_last_quote": same_last,
        "seconds_identical": same_quotes,
        "history_quotes_in_window": len(in_window_hist),
        "quotes_in_both_ignoring_time": anywhere,
        "live_stamps_on_whole_seconds": _whole_second_share(in_window_live),
        "history_stamps_on_whole_seconds": _whole_second_share(in_window_hist),
        "examples_of_differences": examples,
    }


async def reconcile(
    settings: Settings,
    registry: InstrumentRegistry,
    engine: AsyncEngine,
    *,
    symbol: str,
    now_ms: int,
    minutes: int = 10,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict[str, Any]:
    """Fetch both sides for one window and compare them.  Never raises for missing data."""
    report: dict[str, Any] = {"symbol": symbol, "official_endpoints": settings.official_endpoints}
    if symbol not in registry:
        report["error"] = f"unknown instrument {symbol}"
        return report
    if settings.data_provider != "massive" or not settings.api_key:
        report["error"] = "this check needs the Massive provider and its API key"
        return report
    inst = registry.get(symbol)
    q, i = s.quote_ticks, s.instruments
    not_live = s.TICK_FLAG_BACKFILL | s.TICK_FLAG_SYNTHETIC

    async with engine.connect() as conn:
        iid = (await conn.execute(select(i.c.id).where(i.c.symbol == symbol))).scalar_one_or_none()
        if iid is None:
            report["error"] = f"{symbol} is not in the database yet (the application has not started with it)"
            return report
        is_live = (q.c.instrument_id == iid) & (q.c.flags.op("&")(not_live) == 0)
        if end_ms is None:
            latest = select(q.c.source_ts).where(is_live).order_by(q.c.source_ts.desc()).limit(1)
            newest = (await conn.execute(latest)).scalar_one_or_none()
            if newest is None:
                report["error"] = f"no live ticks stored for {symbol} yet"
                return report
            # complete minutes only, old enough for the history to have settled
            end_ms = floor_ms(min(now_ms - SETTLE_MS, dt_to_ms(newest)), MINUTE_MS)
        if start_ms is None:
            start_ms = end_ms - minutes * MINUTE_MS
        in_window = (q.c.source_ts >= ms_to_dt(start_ms)) & (q.c.source_ts < ms_to_dt(end_ms))
        rows = await conn.execute(
            select(q.c.source_ts, q.c.bid, q.c.ask).where(is_live, in_window).order_by(q.c.source_ts, q.c.recv_ts)
        )
        live: list[Quote] = [(dt_to_ms(r.source_ts), r.bid, r.ask) for r in rows]
        recovered = (
            await conn.execute(
                select(q.c.source_ts).where(
                    q.c.instrument_id == iid, in_window, q.c.flags.op("&")(s.TICK_FLAG_BACKFILL) != 0
                )
            )
        ).all()

    report["window"] = {"from": iso(start_ms), "to": iso(end_ms)}
    report["recovered_ticks_in_window"] = len(recovered)
    if not live:
        report["error"] = "no live ticks in this window"
        return report

    rest = MassiveRestClient(
        base_url=settings.massive_rest_url,
        api_key=settings.api_key,
        max_calls_per_min=settings.massive_rest_max_calls_per_min,
        timeout_s=settings.massive_rest_timeout_s,
        max_retries=settings.massive_rest_max_retries,
    )
    history: list[Quote] = []
    try:
        async for page in rest.iter_quotes(rest_quotes_ticker(inst), gte_ms=start_ms, lt_ms=end_ms, order="asc"):
            for item in page:
                ts, bid, ask = item.get("participant_timestamp"), item.get("bid_price"), item.get("ask_price")
                if isinstance(ts, int) and isinstance(bid, (int, float)) and isinstance(ask, (int, float)):
                    history.append((ts // 1_000_000, float(bid), float(ask)))
    except Exception as exc:  # the report must say why, not die
        report["error"] = f"the provider's history could not be read ({type(exc).__name__}: {exc})"
        return report
    finally:
        await rest.aclose()

    report.update(compare(live, history, start_ms, end_ms))
    return report


def _share(part: int, whole: int) -> str:
    return f"{part} of {whole}" + (f" ({part / whole * 100:.1f}%)" if whole else "")


def format_reconciliation(report: dict[str, Any]) -> str:
    """Human-readable rendering of :func:`reconcile`'s result."""
    lines = [f"FX Signal Lab - live capture against the provider's history, {report.get('symbol')}"]
    if not report.get("official_endpoints"):
        lines.append("  NOTE: these are NOT Massive's production endpoints - this run says nothing about Massive")
    window = report.get("window")
    if window:
        lines.append(f"  window: {window['from']} to {window['to']}")
    if report.get("error"):
        lines.append(f"\n  {report['error']}")
        return "\n".join(lines)

    captured = report["seconds_captured_live"]
    lines += [
        "",
        f"  seconds captured live            : {_share(captured, report['seconds_in_window'])}",
        f"  seconds only in the history      : {report['seconds_only_in_history']}"
        f"   (recovered ticks in the window: {report['recovered_ticks_in_window']})",
        f"  live quotes                      : {report['live_quotes']}",
        f"  history quotes, same seconds     : {report['history_quotes_in_those_seconds']}",
        f"  live quotes also in the history  : {_share(report['quotes_in_both'], report['live_quotes'])}",
        f"  seconds with the same count      : {_share(report['seconds_with_same_count'], captured)}",
        f"  seconds with the same last quote : {_share(report['seconds_with_same_last_quote'], captured)}",
        f"  seconds identical                : {_share(report['seconds_identical'], captured)}",
        f"  in both, ignoring the timestamps : {report['quotes_in_both_ignoring_time']}"
        f" (history has {report['history_quotes_in_window']} in the window)",
    ]
    for label, key in (("live", "live_stamps_on_whole_seconds"), ("history", "history_stamps_on_whole_seconds")):
        share = report.get(key)
        if share is not None:
            lines.append(f"  {label + ' stamps on whole seconds':33}: {share * 100:.0f}%")
    examples = report.get("examples_of_differences") or []
    if examples:
        lines.append("\n  first differences (bid, ask of the last quote in the second):")
        for e in examples:
            lines.append(
                f"    {e['second']}  live {e['live_quotes']} quotes, last {e['live_last']}"
                f"  |  history {e['history_quotes']} quotes, last {e['history_last']}"
            )
    elif captured:
        lines.append("\n  no differences: every second captured live holds the same quotes as the history")
    if report["live_quotes"] < report["history_quotes_in_those_seconds"]:
        lines.append(
            "\n  note: quotes the validator rejected or flagged as duplicates are not stored as ticks;"
            "\n        the counters on the dashboard say how many there were"
        )
    return "\n".join(lines)
