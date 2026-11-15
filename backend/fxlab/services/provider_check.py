"""Provider verification: probe what the configured Massive API key can really do.

``fxlab verify-provider`` runs these checks against the live service and prints a
capability report.  It is the tool that turns "implemented from the documentation"
into "verified against production" - nothing in this project claims the latter until
this report says so for your key and plan.

Checks (each one reports ``ok`` / ``failed`` / ``not_entitled`` / ``no_data`` / ``skipped``):

1. REST authentication + market status (also estimates the local clock offset)
2. instrument availability for every configured pair (reference tickers)
3. historical minute aggregates
4. historical second aggregates
5. historical quotes (needed for outage recovery) and their timestamp resolution
6. last quote
7. WebSocket: connect, authenticate, subscribe and receive live quotes
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime
from typing import Any

from fxlab.clock import SystemClock
from fxlab.config import Settings
from fxlab.domain.instruments import Instrument, InstrumentRegistry
from fxlab.domain.models import RawQuote
from fxlab.marketdata.backoff import ExponentialBackoff
from fxlab.marketdata.base import ConnectionEvent
from fxlab.marketdata.massive.rest import (
    MassiveRestClient,
    ProviderAuthError,
    ProviderError,
    ProviderNotEntitledError,
)
from fxlab.marketdata.massive.symbols import rest_aggs_ticker, rest_quotes_ticker, ws_quote_channel
from fxlab.marketdata.massive.ws import MassiveWebSocketAdapter
from fxlab.pipeline.latency import LatencyTracker
from fxlab.pipeline.normalizer import QuoteNormalizer, Verdict
from fxlab.timeutil import DAY_MS, HOUR_MS, MINUTE_MS, dt_to_ms, iso


def _check(name: str, status: str, **detail: Any) -> dict[str, Any]:
    return {"name": name, "status": status, "detail": detail}


def _classify(exc: Exception) -> tuple[str, dict[str, Any]]:
    if isinstance(exc, ProviderNotEntitledError):
        return "not_entitled", {"error": str(exc)}
    if isinstance(exc, ProviderAuthError):
        return "failed", {"error": str(exc), "hint": "the API key was rejected"}
    return "failed", {"error": f"{type(exc).__name__}: {exc}"}


def recent_open_window(inst: Instrument, now_ms: int, length_ms: int = 30 * MINUTE_MS) -> tuple[int, int]:
    """A window of market-open time that ended at least one hour ago (history has settled)."""
    calendar = inst.make_calendar()
    intervals = calendar.open_intervals(now_ms - 7 * DAY_MS, now_ms - HOUR_MS)
    if not intervals:
        return now_ms - HOUR_MS - length_ms, now_ms - HOUR_MS
    start, end = intervals[-1]
    return max(start, end - length_ms), end


class _ProbeSink:
    def __init__(self) -> None:
        self.quotes: list[RawQuote] = []
        self.events: list[ConnectionEvent] = []

    def on_raw_quote(self, raw: RawQuote) -> None:
        self.quotes.append(raw)

    def on_connection_event(self, event: ConnectionEvent) -> None:
        self.events.append(event)


async def _probe_websocket(
    settings: Settings, registry: InstrumentRegistry, inst: Instrument, seconds: float
) -> dict[str, Any]:
    clock = SystemClock()
    sink = _ProbeSink()
    assert settings.api_key is not None
    adapter = MassiveWebSocketAdapter(
        url=settings.massive_ws_url,
        api_key=settings.api_key,
        instruments=[inst],
        sink=sink,
        clock=clock,
        is_market_open=lambda _ts: False,  # a probe must not trigger watchdog reconnects
        backoff=ExponentialBackoff(base_s=30, max_s=30),  # one attempt is enough for a probe
        auth_failed_retry_s=3600,
        max_connections_retry_s=3600,
    )
    task = asyncio.create_task(adapter.run())
    try:
        deadline = asyncio.get_running_loop().time() + seconds
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.2)
            kinds = {e.kind for e in sink.events}
            if kinds & {"auth_failed", "error"} or any(
                e.kind == "server_status" and e.detail.get("status") == "max_connections" for e in sink.events
            ):
                break
            if len(sink.quotes) >= 200:
                break
    finally:
        await adapter.stop()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=5)

    seen = [e.kind for e in sink.events]
    detail: dict[str, Any] = {
        "endpoint": settings.massive_ws_url,
        "channel": ws_quote_channel(inst),
        "events": seen,
        "quotes_received": len(sink.quotes),
    }
    server_statuses = [e.detail for e in sink.events if e.kind == "server_status"]
    if server_statuses:
        detail["server_statuses"] = server_statuses
    if "auth_failed" in seen:
        return _check("websocket", "failed", **detail, hint="authentication failed: check the API key")
    if any(st.get("status") == "max_connections" for st in server_statuses):
        return _check(
            "websocket",
            "failed",
            **detail,
            hint="the account's single forex connection is in use - stop the running app (or other clients) and retry",
        )
    if "auth_success" not in seen:
        errors = [e.detail for e in sink.events if e.kind == "error"]
        return _check("websocket", "failed", **detail, errors=errors)
    if not sink.quotes:
        market_open = inst.make_calendar().is_open(clock.now_ms())
        hint = (
            "authenticated and subscribed but no quote arrived: on plans without real-time forex "
            "WebSocket access the server sends nothing"
            if market_open
            else "authenticated and subscribed; the FX market is closed right now, so no quotes are expected - "
            "re-run after the Sunday 17:00 New York open"
        )
        return _check("websocket", "no_data", **detail, market_open=market_open, hint=hint)

    # what does the live data look like?
    normalizer = QuoteNormalizer(registry)
    latency = LatencyTracker()
    verdicts: dict[str, int] = {}
    for raw in sink.quotes:
        result = normalizer.process(raw)
        key = result.verdict.value if result.reason is None else f"reject:{result.reason.value}"
        verdicts[key] = verdicts.get(key, 0) + 1
        if result.verdict is Verdict.ACCEPT and result.tick is not None:
            latency.add(result.tick.recv_ts_ms, result.tick.source_ts_ms)
    snap = latency.snapshot(clock.now_ms())
    first = sink.quotes[0]
    detail.update(
        verdicts=verdicts,
        sample={
            "symbol": first.symbol,
            "bid": first.bid,
            "ask": first.ask,
            "source_ts": iso(first.source_ts_ms),
            "exchange": first.exchange,
        },
        source_timestamp_resolution_ms=snap.resolution_ms,
        latency_ms={
            "p50": snap.p50_ms,
            "p95": snap.p95_ms,
            "p99": snap.p99_ms,
            "n": snap.n,
            "note": snap.note,
        },
        clock_skew_suspected=snap.clock_skew_suspected,
    )
    return _check("websocket", "ok", **detail)


async def verify_provider(
    settings: Settings,
    registry: InstrumentRegistry,
    *,
    ws_seconds: float = 20.0,
    include_ws: bool = True,
) -> dict[str, Any]:
    """Run all checks and return the capability report (never raises for provider errors)."""
    now = SystemClock().now_ms()
    report: dict[str, Any] = {
        "generated_at": iso(now),
        "rest_url": settings.massive_rest_url,
        "ws_url": settings.massive_ws_url,
        "official_endpoints": settings.official_endpoints,
        "checks": [],
        "instruments": [],
    }
    if not settings.api_key:
        report["error"] = "MASSIVE_API_KEY is not set"
        return report
    active = registry.active()
    probe = active[0] if active else (registry.all()[0] if registry.all() else None)
    if probe is None:
        report["error"] = "no instruments configured"
        return report
    report["probe_instrument"] = probe.symbol

    rest = MassiveRestClient(
        base_url=settings.massive_rest_url,
        api_key=settings.api_key,
        max_calls_per_min=settings.massive_rest_max_calls_per_min,
        timeout_s=settings.massive_rest_timeout_s,
        max_retries=1,
    )
    checks: list[dict[str, Any]] = report["checks"]
    try:
        # 1. authentication + market status + clock offset
        try:
            sent = SystemClock().now_ms()
            status = await rest.market_status()
            received = SystemClock().now_ms()
            detail: dict[str, Any] = {
                "fx_market": (status.get("currencies") or {}).get("fx"),
                "server_time": status.get("serverTime"),
            }
            try:
                server_ms = dt_to_ms(datetime.fromisoformat(str(status.get("serverTime"))))
                # serverTime has 1 s resolution: the offset is only good to about +/- 1 s
                detail["local_clock_offset_ms_approx"] = (sent + received) // 2 - server_ms - 500
            except (ValueError, TypeError):
                pass
            checks.append(_check("rest_auth_market_status", "ok", **detail))
        except Exception as exc:
            st, detail = _classify(exc)
            checks.append(_check("rest_auth_market_status", st, **detail))
            if isinstance(exc, ProviderAuthError):
                report["error"] = "the API key was rejected by the REST API; remaining checks skipped"
                return report

        # 2. instrument availability (every configured pair, active or not)
        for inst in registry.all():
            ticker = rest_aggs_ticker(inst)
            try:
                record = await rest.fx_ticker(ticker)
                entry = {
                    "symbol": inst.symbol,
                    "ticker": ticker,
                    "active": inst.active,
                    "status": "supported" if record else "unsupported",
                }
                if record:
                    entry["name"] = record.get("name")
            except ProviderError as exc:
                entry = {
                    "symbol": inst.symbol,
                    "ticker": ticker,
                    "active": inst.active,
                    "status": "unverified",
                    "error": str(exc),
                }
            report["instruments"].append(entry)
        supported = [i for i in report["instruments"] if i["status"] == "supported"]
        checks.append(
            _check(
                "instrument_availability",
                "ok" if any(i["symbol"] == probe.symbol for i in supported) else "failed",
                supported=[i["symbol"] for i in supported],
                unsupported=[i["symbol"] for i in report["instruments"] if i["status"] == "unsupported"],
                unverified=[i["symbol"] for i in report["instruments"] if i["status"] == "unverified"],
            )
        )

        start, end = recent_open_window(probe, now)

        # 3 + 4. historical aggregates
        for name, multiplier, timespan, a, b in (
            ("history_minute_aggregates", 1, "minute", start, end),
            ("history_second_aggregates", 1, "second", end - 2 * MINUTE_MS, end),
        ):
            window = {"from": iso(a), "to": iso(b)}  # the window this check asked for
            try:
                bars = [
                    bar
                    async for page in rest.iter_aggregates(rest_aggs_ticker(probe), multiplier, timespan, a, b - 1)
                    for bar in page
                ]
                if bars:
                    checks.append(
                        _check(
                            name,
                            "ok",
                            bars=len(bars),
                            first=iso(bars[0].get("t")),
                            last=iso(bars[-1].get("t")),
                            sample=bars[0],
                            window=window,
                            # "n" is the number of quotes in a bar: the provider's own count
                            quotes_in_window=sum(n for n in (bar.get("n") for bar in bars) if isinstance(n, int)),
                        )
                    )
                else:
                    checks.append(
                        _check(
                            name,
                            "no_data",
                            window=window,
                            hint="no bars returned for a recent market-open window",
                        )
                    )
            except Exception as exc:
                st, detail = _classify(exc)
                checks.append(_check(name, st, **detail))

        # 5. historical quotes
        window = {"from": iso(end - MINUTE_MS), "to": iso(end)}
        quotes_limit = 1000
        try:
            quotes: list[dict[str, Any]] = []
            async for page in rest.iter_quotes(
                rest_quotes_ticker(probe), gte_ms=end - MINUTE_MS, lt_ms=end, limit=quotes_limit
            ):
                quotes.extend(page)
                break  # one page is enough for a probe
            if quotes:
                stamps: list[int] = [t for t in (q.get("participant_timestamp") for q in quotes) if isinstance(t, int)]
                whole_seconds = sum(1 for t in stamps if t % 1_000_000_000 == 0)
                checks.append(
                    _check(
                        "history_quotes",
                        "ok",
                        quotes=len(quotes),
                        whole_window=len(quotes) < quotes_limit,  # False: only the first page was read
                        sample=quotes[0],
                        timestamp_resolution="1 second" if stamps and whole_seconds == len(stamps) else "sub-second",
                        window=window,
                    )
                )
            else:
                checks.append(_check("history_quotes", "no_data", window=window))
        except Exception as exc:
            st, detail = _classify(exc)
            if st == "not_entitled":
                detail["hint"] = (
                    "outage recovery (gap backfill) needs historical quotes; without them gaps stay 'unfillable'"
                )
            checks.append(_check("history_quotes", st, **detail))

        # 6. last quote
        try:
            last = await rest.last_quote(probe.base, probe.quote)
            checks.append(_check("last_quote", "ok", last=last.get("last"), symbol=last.get("symbol")))
        except Exception as exc:
            st, detail = _classify(exc)
            checks.append(_check("last_quote", st, **detail))
    finally:
        await rest.aclose()

    # 7. websocket
    if include_ws:
        try:
            checks.append(await _probe_websocket(settings, registry, probe, ws_seconds))
        except Exception as exc:
            checks.append(_check("websocket", "failed", error=f"{type(exc).__name__}: {exc}"))
    else:
        checks.append(_check("websocket", "skipped"))

    by_name = {c["name"]: c["status"] for c in checks}
    report["summary"] = {
        "live_feed_verified": by_name.get("websocket") == "ok",
        "history_verified": by_name.get("history_minute_aggregates") == "ok",
        "gap_recovery_possible": by_name.get("history_quotes") == "ok",
        "all": by_name,
    }
    return report


def format_report(report: dict[str, Any]) -> str:
    """Human-readable rendering of :func:`verify_provider`'s result."""
    lines = [
        "FX Signal Lab - Massive provider verification",
        f"  generated : {report.get('generated_at')}",
        f"  REST      : {report.get('rest_url')}",
        f"  WebSocket : {report.get('ws_url')}",
    ]
    if not report.get("official_endpoints"):
        lines.append(
            "  NOTE      : these are NOT Massive's production endpoints - this run verifies nothing about Massive"
        )
    if report.get("error"):
        lines.append(f"\nERROR: {report['error']}")
    marks = {"ok": "OK  ", "failed": "FAIL", "not_entitled": "PLAN", "no_data": "NODATA", "skipped": "SKIP"}
    lines.append("")
    for check in report.get("checks", []):
        lines.append(f"  [{marks.get(check['status'], check['status']):6}] {check['name']}")
        for key, value in check["detail"].items():
            text = str(value)
            lines.append(f"             {key}: {text if len(text) <= 300 else text[:300] + ' ...'}")
    if report.get("instruments"):
        lines.append("\n  Instruments:")
        for inst in report["instruments"]:
            flag = "active" if inst["active"] else "inactive"
            lines.append(f"    {inst['symbol']:8} {inst['ticker']:10} {inst['status']:12} ({flag})")
    summary = report.get("summary")
    if summary:
        lines.append("\n  Summary:")
        lines.append(f"    live feed verified      : {summary['live_feed_verified']}")
        lines.append(f"    history verified        : {summary['history_verified']}")
        lines.append(f"    gap recovery possible   : {summary['gap_recovery_possible']}")
    return "\n".join(lines)
