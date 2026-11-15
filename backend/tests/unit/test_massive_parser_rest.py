"""Massive wire formats: WebSocket frame parsing, symbol spellings and the REST client.

Message and response samples below are the ones published in Massive's documentation.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.models import DataSource, PriceBasis
from fxlab.marketdata.massive.parser import FrameParseError, parse_frame
from fxlab.marketdata.massive.rest import (
    MassiveRestClient,
    ProviderAuthError,
    ProviderError,
    ProviderNotEntitledError,
    ProviderRateLimitError,
    ProviderUnavailableError,
    aggregate_to_bar,
    quote_result_to_raw,
)
from fxlab.marketdata.massive.symbols import (
    TIMEFRAME_TO_RANGE,
    canonical_symbol,
    rest_aggs_ticker,
    rest_quotes_ticker,
    ws_quote_channel,
)
from tests.conftest import T0

# --- symbols ----------------------------------------------------------------------------


def test_symbol_spellings_per_api(registry: InstrumentRegistry) -> None:
    eurusd = registry.get("EURUSD")
    assert ws_quote_channel(eurusd) == "C.EUR-USD"
    assert rest_aggs_ticker(eurusd) == "C:EURUSD"
    assert rest_quotes_ticker(eurusd) == "C:EUR-USD"


@pytest.mark.parametrize("text", ["EUR/USD", "EUR-USD", "C:EURUSD", "C:EUR-USD", "C.EUR-USD", "eur/usd", " EURUSD "])
def test_canonical_symbol_accepts_every_spelling(text: str) -> None:
    assert canonical_symbol(text) == "EURUSD"


@pytest.mark.parametrize("text", [None, 42, "", "EUR", "EUR/USDX", "EU1/USD"])
def test_canonical_symbol_rejects_garbage(text: object) -> None:
    assert canonical_symbol(text) is None


def test_every_timeframe_maps_to_an_aggregates_range() -> None:
    assert TIMEFRAME_TO_RANGE["1s"] == (1, "second")
    assert TIMEFRAME_TO_RANGE["15m"] == (15, "minute")
    assert TIMEFRAME_TO_RANGE["4h"] == (4, "hour")
    assert TIMEFRAME_TO_RANGE["1d"] == (1, "day")


# --- websocket frames -------------------------------------------------------------------


def test_documented_quote_sample() -> None:
    frame = '[{"ev":"C","p":"USD/CNH","x":"44","a":6.83366,"b":6.83363,"t":1536036818784}]'
    parsed = parse_frame(frame, T0)
    (q,) = parsed.quotes
    assert (q.symbol, q.bid, q.ask, q.source_ts_ms, q.exchange, q.recv_ts_ms) == (
        "USDCNH",
        6.83363,
        6.83366,
        1536036818784,
        44,
        T0,
    )
    assert not parsed.statuses and parsed.malformed == 0 and parsed.unknown == 0


def test_documented_status_samples() -> None:
    connected = parse_frame('[{"ev":"status","status":"connected","message":"Connected Successfully"}]', T0)
    assert [(s.status, s.message) for s in connected.statuses] == [("connected", "Connected Successfully")]
    authed = parse_frame('[{"ev":"status","status":"auth_success","message":"authenticated"}]', T0)
    assert authed.statuses[0].status == "auth_success"


def test_bundled_frame_with_mixed_events() -> None:
    frame = json.dumps(
        [
            {"ev": "C", "p": "EUR/USD", "x": 48, "a": 1.16008, "b": 1.16, "t": T0},
            {
                "ev": "CAS",
                "pair": "EUR/USD",
                "o": 1.16,
                "c": 1.16,
                "h": 1.16,
                "l": 1.16,
                "v": 2,
                "s": T0,
            },  # not consumed
            {"ev": "C", "x": 48, "a": 1.1, "b": 1.0, "t": T0},  # no pair
            {"ev": "C", "p": "EUR/USD", "x": 48, "a": "n/a", "b": None, "t": "soon"},  # unusable values
            "not-an-object",
            {"ev": "status", "status": "success", "message": "subscribed to: C.EUR-USD"},
        ]
    )
    parsed = parse_frame(frame, T0)
    assert len(parsed.quotes) == 2
    assert parsed.quotes[1].bid is None and parsed.quotes[1].ask is None and parsed.quotes[1].source_ts_ms is None
    assert parsed.unknown == 1 and parsed.malformed == 2
    assert parsed.statuses[0].status == "success"


def test_single_object_frame_is_accepted_and_garbage_raises() -> None:
    assert parse_frame('{"ev":"status","status":"connected","message":"x"}', T0).statuses
    assert parse_frame(b'[{"ev":"C","p":"EUR-USD","a":1.2,"b":1.1,"t":1763128800000}]', T0).quotes
    for bad in ("not json", "42", '"text"'):
        with pytest.raises(FrameParseError):
            parse_frame(bad, T0)


# --- REST client ------------------------------------------------------------------------


def make_client(handler: Any, **kw: Any) -> tuple[MassiveRestClient, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = MassiveRestClient(
        base_url="https://api.massive.com",
        api_key="SECRET-KEY",
        transport=httpx.MockTransport(handler),
        sleep=fake_sleep,
        **kw,
    )
    return client, sleeps


async def test_key_is_sent_as_bearer_header_never_in_the_url() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"market": "open", "currencies": {"fx": "open"}, "serverTime": "2026-11-12T09:00:00-05:00"},
        )

    client, _ = make_client(handler)
    status = await client.market_status()
    assert status["currencies"]["fx"] == "open"
    assert seen[0].url.path == "/v1/marketstatus/now"
    assert seen[0].headers["authorization"] == "Bearer SECRET-KEY"
    assert "SECRET-KEY" not in str(seen[0].url)
    await client.aclose()


async def test_aggregates_request_and_pagination() -> None:
    urls: list[str] = []
    page1 = {
        "ticker": "C:EURUSD",
        "results": [{"t": 1000, "o": 1.1, "h": 1.2, "l": 1.0, "c": 1.15, "n": 5, "v": 5, "vw": 1.1}],
        "status": "OK",
        "next_url": "https://api.massive.com/v2/aggs/ticker/C:EURUSD/range/1/minute/1000/9000?cursor=abc",
    }
    page2 = {
        "ticker": "C:EURUSD",
        "results": [{"t": 61000, "o": 1.15, "h": 1.25, "l": 1.1, "c": 1.2, "n": 3, "v": 3, "vw": 1.2}],
        "status": "OK",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        assert request.headers["authorization"] == "Bearer SECRET-KEY"  # also on the cursor URL
        return httpx.Response(200, json=page2 if "cursor" in str(request.url) else page1)

    client, _ = make_client(handler)
    pages = [page async for page in client.iter_aggregates("C:EURUSD", 1, "minute", 1000, 9000)]
    assert [len(p) for p in pages] == [1, 1]
    assert urls[0].startswith("https://api.massive.com/v2/aggs/ticker/C:EURUSD/range/1/minute/1000/9000?")
    assert "adjusted=true" in urls[0] and "sort=asc" in urls[0] and "limit=50000" in urls[0]
    assert urls[1].endswith("?cursor=abc")  # the cursor URL is followed verbatim
    await client.aclose()


async def test_empty_aggregates_response_without_results_key() -> None:
    client, _ = make_client(
        lambda r: httpx.Response(200, json={"ticker": "C:EURUSD", "resultsCount": 0, "status": "OK"})
    )
    assert [p async for p in client.iter_aggregates("C:EURUSD", 1, "minute", 0, 1)] == [[]]
    await client.aclose()


async def test_quotes_request_uses_nanosecond_bounds() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(dict(request.url.params))
        captured["path"] = request.url.path
        return httpx.Response(200, json={"results": [], "status": "OK"})

    client, _ = make_client(handler)
    _ = [p async for p in client.iter_quotes("C:EUR-USD", gte_ms=1_763_128_800_000, lt_ms=1_763_128_860_000)]
    assert captured["path"] == "/v3/quotes/C:EUR-USD"
    assert captured["timestamp.gte"] == "1763128800000000000"
    assert captured["timestamp.lt"] == "1763128860000000000"
    assert (captured["order"], captured["sort"], captured["limit"]) == ("asc", "timestamp", "50000")
    await client.aclose()


async def test_next_url_on_another_host_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"results": [{}], "next_url": "https://evil.example/v3/quotes/C:EUR-USD?cursor=1"}
        )

    client, _ = make_client(handler)
    with pytest.raises(ProviderError, match="different host"):
        _ = [p async for p in client.iter_quotes("C:EUR-USD")]
    await client.aclose()


async def test_rate_limit_is_retried_honouring_retry_after() -> None:
    responses = [
        httpx.Response(429, headers={"Retry-After": "2"}, json={"status": "ERROR"}),
        httpx.Response(503),
        httpx.Response(200, json={"market": "open"}),
    ]
    client, sleeps = make_client(lambda r: responses.pop(0))
    assert await client.market_status() == {"market": "open"}
    assert client.calls == 3 and client.retries == 2
    assert 2.0 <= sleeps[0] <= 2.5  # Retry-After (+ jitter)
    assert 1.0 <= sleeps[1] <= 1.25  # exponential: 0.5 * 2^(attempt-1) for attempt 2
    await client.aclose()


async def test_retries_are_bounded() -> None:
    client, sleeps = make_client(
        lambda r: httpx.Response(429, json={"status": "ERROR", "error": "too many"}), max_retries=3
    )
    with pytest.raises(ProviderRateLimitError) as exc:
        await client.market_status()
    assert exc.value.status_code == 429 and client.calls == 4 and len(sleeps) == 3
    await client.aclose()

    client, _ = make_client(lambda r: httpx.Response(502), max_retries=1)
    with pytest.raises(ProviderUnavailableError):
        await client.market_status()
    await client.aclose()


async def test_network_errors_are_retried() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("boom")
        return httpx.Response(200, json={"market": "open"})

    client, _ = make_client(handler)
    assert await client.market_status() == {"market": "open"}
    await client.aclose()


@pytest.mark.parametrize(
    "status,exc_type",
    [(401, ProviderAuthError), (403, ProviderNotEntitledError), (404, ProviderError), (400, ProviderError)],
)
async def test_client_errors_are_not_retried(status: int, exc_type: type[ProviderError]) -> None:
    client, sleeps = make_client(
        lambda r: httpx.Response(
            status, json={"status": "NOT_AUTHORIZED", "message": "You are not entitled to this data."}
        )
    )
    with pytest.raises(exc_type) as exc:
        await client.last_quote("EUR", "USD")
    assert exc.value.status_code == status and client.calls == 1 and sleeps == []
    assert "SECRET-KEY" not in str(exc.value)
    await client.aclose()


async def test_client_side_throttle_spaces_calls() -> None:
    client, sleeps = make_client(lambda r: httpx.Response(200, json={"market": "open"}), max_calls_per_min=5)
    for _ in range(3):
        await client.market_status()
    # 5 calls per minute => slots 12 s apart; the first call does not wait.  The injected
    # sleep does not advance the clock, so the waits are measured from "now": 12 s and 24 s.
    assert len(sleeps) == 2
    assert 11.5 <= sleeps[0] <= 12.0 and 23.5 <= sleeps[1] <= 24.0
    await client.aclose()


async def test_fx_ticker_lookup() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["market"] == "fx"
        wanted = request.url.params["ticker"]
        results = [{"ticker": "C:EURUSD", "market": "fx", "active": True}] if wanted == "C:EURUSD" else []
        return httpx.Response(200, json={"results": results, "status": "OK", "count": len(results)})

    client, _ = make_client(handler)
    assert (await client.fx_ticker("C:EURUSD"))["ticker"] == "C:EURUSD"  # type: ignore[index]
    assert await client.fx_ticker("C:USDTRY") is None
    await client.aclose()


# --- response conversion ----------------------------------------------------------------


def test_documented_aggregate_sample_becomes_a_bid_based_provider_bar() -> None:
    result = {
        "c": 1.17721,
        "h": 1.18305,
        "l": 1.1756,
        "n": 125329,
        "o": 1.17921,
        "t": 1626912000000,
        "v": 125329,
        "vw": 1.1789,
    }
    bar = aggregate_to_bar("EURUSD", "1d", result)
    assert bar is not None
    assert (bar.ts_ms, bar.open, bar.high, bar.low, bar.close) == (
        1626912000000,
        1.17921,
        1.18305,
        1.1756,
        1.17721,
    )
    assert bar.source is DataSource.MASSIVE_AGG and bar.price_basis is PriceBasis.BID
    assert bar.n_quotes == 125329
    assert bar.complete is None and bar.n_valid is None  # completeness is unknown for provider bars
    assert aggregate_to_bar("EURUSD", "1d", {"t": 1}) is None


def test_documented_quote_sample_conversion() -> None:
    result = {
        "ask_exchange": 48,
        "ask_price": 1.18565,
        "bid_exchange": 48,
        "bid_price": 1.18558,
        "participant_timestamp": 1625097600000000000,
    }
    raw = quote_result_to_raw("EURUSD", result)
    assert (raw.bid, raw.ask, raw.source_ts_ms, raw.exchange) == (1.18558, 1.18565, 1625097600000, 48)
    assert raw.recv_ts_ms == raw.source_ts_ms  # replay: no receipt time exists
    broken = quote_result_to_raw("EURUSD", {"bid_price": "x"})
    assert broken.bid is None and broken.source_ts_ms is None
