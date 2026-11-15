"""The Massive WebSocket adapter against a local server speaking the documented protocol.

These tests use real sockets.  They verify protocol handling and recovery behaviour of
our code; they cannot verify Massive's production service (see tests/support/fake_massive.py).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from fxlab.clock import SystemClock
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.models import RawQuote
from fxlab.marketdata.backoff import ExponentialBackoff
from fxlab.marketdata.base import ConnectionEvent, ConnectionState
from fxlab.marketdata.massive.ws import MassiveWebSocketAdapter
from tests.support.fake_massive import FakeMassiveServer


class CollectingSink:
    def __init__(self) -> None:
        self.quotes: list[RawQuote] = []
        self.events: list[ConnectionEvent] = []
        self.fail_on_quote = False

    def on_raw_quote(self, raw: RawQuote) -> None:
        if self.fail_on_quote:
            raise RuntimeError("sink exploded")
        self.quotes.append(raw)

    def on_connection_event(self, event: ConnectionEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


async def wait_for(predicate: Callable[[], bool], timeout: float = 5.0, what: str = "condition") -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.01)


@pytest.fixture()
async def server() -> AsyncIterator[FakeMassiveServer]:
    srv = await FakeMassiveServer().start()
    yield srv
    await srv.stop()


class Harness:
    def __init__(self, server: FakeMassiveServer, registry: InstrumentRegistry, **kw: Any) -> None:
        self.sink = CollectingSink()
        self.market_open = True
        params: dict[str, Any] = dict(
            url=server.ws_url,
            api_key=server.fake.api_key,
            instruments=registry.active(),
            sink=self.sink,
            clock=SystemClock(),
            is_market_open=lambda _ts: self.market_open,
            backoff=ExponentialBackoff(base_s=0.05, max_s=0.2),
            connect_timeout_s=2,
            auth_timeout_s=1,
            silence_timeout_s=30,
            stable_after_s=30,
            auth_failed_retry_s=0.3,
            max_connections_retry_s=0.3,
            ping_interval_s=None,
        )
        params.update(kw)
        self.adapter = MassiveWebSocketAdapter(**params)
        self.task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Harness:
        self.task = asyncio.create_task(self.adapter.run())
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.adapter.stop()
        assert self.task is not None
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.task, timeout=5)

    async def streaming(self) -> None:
        await wait_for(lambda: self.adapter.status().state is ConnectionState.STREAMING, what="STREAMING")


async def test_connect_authenticate_subscribe_and_stream(
    server: FakeMassiveServer, registry: InstrumentRegistry
) -> None:
    fake = server.fake
    async with Harness(server, registry) as h:
        await h.streaming()
        await wait_for(lambda: bool(fake.clients), what="subscription")
        await fake.publish(b=1.16000, a=1.16008, t=1_763_128_800_123)
        await fake.publish([{"b": 1.16001, "a": 1.16009}, {"b": 1.16002, "a": 1.16010}])  # one bundled frame
        await wait_for(lambda: len(h.sink.quotes) == 3, what="3 quotes")

        first = h.sink.quotes[0]
        assert (first.symbol, first.bid, first.ask, first.source_ts_ms, first.exchange) == (
            "EURUSD",
            1.16,
            1.16008,
            1_763_128_800_123,
            48,
        )
        assert first.recv_ts_ms > 1_763_128_800_123
        assert fake.subscribe_messages == ["C.EUR-USD"]
        assert fake.auth_attempts == 1

        kinds = h.sink.kinds()
        for expected in (
            "connect_attempt",
            "connected",
            "auth_success",
            "subscribed",
            "subscription_ack",
            "first_data",
        ):
            assert expected in kinds
        assert kinds.index("auth_success") < kinds.index("subscribed") < kinds.index("first_data")

        status = h.adapter.status()
        assert status.quotes_received == 3 and status.sessions_established == 1
        assert status.last_data_ms is not None and status.endpoint == server.ws_url
        # the API key never leaks into events or the status object
        assert fake.api_key not in repr(h.sink.events) and fake.api_key not in repr(status)

    assert h.adapter.status().state is ConnectionState.STOPPED
    assert h.sink.kinds()[-1] == "stopped"


async def test_works_without_greeting_and_without_subscription_ack(
    server: FakeMassiveServer, registry: InstrumentRegistry
) -> None:
    """Only ``auth_success`` is required by the protocol handling; the rest is optional."""
    server.fake.send_connected = False
    server.fake.send_subscription_ack = False
    async with Harness(server, registry) as h:
        await h.streaming()
        await wait_for(lambda: bool(server.fake.clients), what="subscription")
        await server.fake.publish(b=1.16, a=1.1601)
        await wait_for(lambda: len(h.sink.quotes) == 1, what="quote")
        assert "subscription_ack" not in h.sink.kinds()


async def test_reconnects_and_resubscribes_after_server_drop(
    server: FakeMassiveServer, registry: InstrumentRegistry
) -> None:
    fake = server.fake
    async with Harness(server, registry) as h:
        await h.streaming()
        await wait_for(lambda: bool(fake.clients), what="subscription")
        await fake.publish(b=1.16000, a=1.16008)
        await wait_for(lambda: len(h.sink.quotes) == 1, what="first quote")

        await fake.drop_connections()
        await fake.publish(b=1.17000, a=1.17008)  # published while nobody is connected
        await wait_for(lambda: fake.connection_count == 2 and bool(fake.clients), what="reconnect")
        await h.streaming()
        await fake.publish(b=1.16010, a=1.16018)
        await wait_for(lambda: len(h.sink.quotes) == 2, what="quote after reconnect")

        # the protocol has no replay: the quote from the outage never arrives over the socket
        assert [q.bid for q in h.sink.quotes] == [1.16000, 1.16010]
        assert len(fake.tape) == 3
        assert fake.subscribe_messages == ["C.EUR-USD", "C.EUR-USD"]
        kinds = h.sink.kinds()
        assert kinds.count("disconnected") == 1 and kinds.count("reconnect_scheduled") == 1
        assert kinds.count("first_data") == 2
        disconnected = next(e for e in h.sink.events if e.kind == "disconnected")
        assert disconnected.detail["last_data_ms"] is not None
        assert h.adapter.status().sessions_established == 2


async def test_backs_off_while_server_is_down_and_recovers(registry: InstrumentRegistry) -> None:
    first = await FakeMassiveServer().start()
    port = first.port
    h = Harness(first, registry)
    async with h:
        await h.streaming()
        await first.stop()  # provider outage
        await wait_for(
            lambda: h.sink.kinds().count("reconnect_scheduled") >= 4,
            timeout=10,
            what="several failed attempts",
        )
        scheduled = [e.detail for e in h.sink.events if e.kind == "reconnect_scheduled"]
        assert [d["attempt"] for d in scheduled[:4]] == [1, 2, 3, 4]
        assert all(0.025 <= d["delay_s"] <= 0.2 for d in scheduled)  # base 0.05 doubling, capped at 0.2
        assert h.adapter.status().state in (ConnectionState.RECONNECT_WAIT, ConnectionState.CONNECTING)
        assert h.adapter.status().last_error is not None

        second = await FakeMassiveServer(port=port).start()  # provider is back
        try:
            await h.streaming()
            await wait_for(lambda: bool(second.fake.clients), what="resubscription")
            await second.fake.publish(b=1.16, a=1.1601)
            await wait_for(lambda: len(h.sink.quotes) == 1, what="quote after recovery")
        finally:
            await h.adapter.stop()
            await second.stop()


async def test_authentication_failure_is_surfaced_and_retried_slowly(
    server: FakeMassiveServer, registry: InstrumentRegistry
) -> None:
    fake = server.fake
    fake.reject_auth = True
    async with Harness(server, registry, auth_failed_retry_s=0.4) as h:
        await wait_for(lambda: h.adapter.status().state is ConnectionState.AUTH_FAILED, what="AUTH_FAILED")
        assert "auth_failed" in h.sink.kinds()
        assert "authentication failed" in (h.adapter.status().last_error or "")
        await asyncio.sleep(0.2)
        assert fake.auth_attempts == 1  # not hammering: still waiting for the slow retry
        fake.reject_auth = False  # the user fixed the key
        await h.streaming()
        assert fake.auth_attempts == 2


async def test_connection_limit_is_respected(server: FakeMassiveServer, registry: InstrumentRegistry) -> None:
    fake = server.fake
    fake.max_connections_mode = True
    async with Harness(server, registry, max_connections_retry_s=0.4) as h:
        await wait_for(lambda: "connection limit" in (h.adapter.status().last_error or ""), what="limit error")
        await asyncio.sleep(0.2)
        assert fake.connection_count == 1  # waiting, not fighting for the single allowed connection
        statuses = [e for e in h.sink.events if e.kind == "server_status"]
        assert statuses and statuses[0].detail["status"] == "max_connections"
        fake.max_connections_mode = False
        await h.streaming()


async def test_watchdog_reconnects_on_silence_only_while_market_is_open(
    server: FakeMassiveServer, registry: InstrumentRegistry
) -> None:
    fake = server.fake
    async with Harness(server, registry, silence_timeout_s=0.5) as h:
        h.market_open = False
        await h.streaming()
        await asyncio.sleep(2.2)  # silent, but the market is closed: nothing happens
        assert "watchdog_timeout" not in h.sink.kinds() and fake.connection_count == 1

        h.market_open = True  # silent while the market is open: the connection is recycled
        await wait_for(lambda: "watchdog_timeout" in h.sink.kinds(), what="watchdog")
        await wait_for(lambda: fake.connection_count >= 2, what="reconnect after watchdog")
        event = next(e for e in h.sink.events if e.kind == "disconnected")
        assert event.detail["code"] == 4000


async def test_garbage_frames_and_sink_failures_do_not_stop_the_stream(
    server: FakeMassiveServer, registry: InstrumentRegistry
) -> None:
    fake = server.fake
    async with Harness(server, registry) as h:
        await h.streaming()
        await wait_for(lambda: bool(fake.clients), what="subscription")
        await fake.send_raw("this is not json")
        await fake.send_raw('[{"ev":"XYZ"},{"ev":"C","a":1,"b":1,"t":1}]')
        await fake.send_status("slow_consumer", "you are reading too slowly")
        h.sink.fail_on_quote = True
        await fake.publish(b=1.16, a=1.1601)
        await wait_for(lambda: h.adapter.status().sink_errors == 1, what="sink error")
        h.sink.fail_on_quote = False
        await fake.publish(b=1.16001, a=1.16011)
        await wait_for(lambda: len(h.sink.quotes) == 1, what="quote after failures")

        st = h.adapter.status()
        assert (st.parse_errors, st.unknown_events, st.malformed_events) == (1, 1, 1)
        assert st.state is ConnectionState.STREAMING and fake.connection_count == 1
        slow = [e for e in h.sink.events if e.kind == "server_status"]
        assert slow and slow[0].detail["status"] == "slow_consumer"
        assert "slow_consumer" in (st.last_error or "")


async def test_no_instruments_means_no_connection(server: FakeMassiveServer, registry: InstrumentRegistry) -> None:
    h = Harness(server, registry, instruments=[])
    async with h:
        await asyncio.sleep(0.2)
        assert h.adapter.status().state is ConnectionState.IDLE
        assert server.fake.connection_count == 0


async def test_rest_client_against_the_fake_provider(server: FakeMassiveServer) -> None:
    """End-to-end sanity of the REST client over HTTP, including pagination and entitlement."""
    from fxlab.marketdata.massive.rest import MassiveRestClient, ProviderAuthError, ProviderNotEntitledError

    fake = server.fake
    base = 1_763_128_800_000
    await fake.publish([{"b": 1.16 + i * 0.0001, "a": 1.1601 + i * 0.0001, "t": base + i * 400} for i in range(10)])
    fake.quotes_page_size = 4

    client = MassiveRestClient(base_url=server.rest_url, api_key=fake.api_key)
    try:
        assert (await client.market_status())["currencies"]["fx"] == "open"
        assert await client.fx_ticker("C:EURUSD") is not None
        assert await client.fx_ticker("C:USDTRY") is None

        pages = [p async for p in client.iter_quotes("C:EUR-USD", gte_ms=base, lt_ms=base + 3600)]
        quotes = [q for p in pages for q in p]
        assert [len(p) for p in pages] == [4, 4, 1]  # t = 0, 400 ... 3200 are < 3600
        assert [q["participant_timestamp"] for q in quotes] == [(base + i * 400) * 1_000_000 for i in range(9)]

        bars = [
            b for p in [p async for p in client.iter_aggregates("C:EURUSD", 1, "second", base, base + 3999)] for b in p
        ]
        assert [b["t"] for b in bars] == [base, base + 1000, base + 2000, base + 3000]
        assert [b["n"] for b in bars] == [3, 2, 3, 2]
        assert bars[0]["o"] == 1.16 and bars[0]["c"] == pytest.approx(1.1602)  # bid side

        assert (await client.last_quote("EUR", "USD"))["last"]["bid"] == pytest.approx(1.1609)

        fake.quotes_entitled = False
        with pytest.raises(ProviderNotEntitledError):
            _ = [p async for p in client.iter_quotes("C:EUR-USD")]
        assert all(h == f"Bearer {fake.api_key}" for h in fake.rest_auth_headers)
    finally:
        await client.aclose()

    bad = MassiveRestClient(base_url=server.rest_url, api_key="wrong")
    try:
        with pytest.raises(ProviderAuthError):
            await bad.market_status()
    finally:
        await bad.aclose()
