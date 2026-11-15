"""Massive forex WebSocket adapter (live quotes).

Protocol (docs: websocket/quickstart, websocket/forex/quotes; official client v2.10):

1. connect to ``wss://socket.massive.com/forex``; the server greets with
   ``[{"ev":"status","status":"connected",...}]``
2. send ``{"action":"auth","params":"<API KEY>"}``; the server answers with status
   ``auth_success`` (or ``auth_failed``)
3. send ``{"action":"subscribe","params":"C.EUR-USD,C.GBP-USD"}``
4. quote events arrive as JSON arrays, possibly several per frame

Operational facts taken into account:

* only **one** concurrent connection per asset class is allowed by default - a second
  one triggers a ``max_connections`` status, so exactly one adapter instance may run;
* a client that reads too slowly is disconnected by the server - the read loop
  therefore does nothing but parse and hand over; persistence happens elsewhere;
* nothing in the protocol replays missed data after a reconnect - recovery is done
  by the gap detector through the REST API.

Reliability features: exponential back-off with jitter, protocol-level ping/pong
keep-alive, a data-silence watchdog that is aware of market hours, and explicit
handling of authentication failures (retried slowly and surfaced, never hammered).
The API key is never logged nor included in events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable, Sequence
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from fxlab.clock import Clock
from fxlab.domain.instruments import Instrument
from fxlab.logging import get_logger
from fxlab.marketdata.backoff import ExponentialBackoff
from fxlab.marketdata.base import AdapterStatus, ConnectionEvent, ConnectionState, MarketDataSink
from fxlab.marketdata.massive.parser import FrameParseError, parse_frame
from fxlab.marketdata.massive.symbols import ws_quote_channel

log = get_logger(__name__)

WATCHDOG_CLOSE_CODE = 4000


class AuthFailedError(Exception):
    """The provider rejected the API key."""


class MaxConnectionsError(Exception):
    """Another session is already using the account's connection for this asset class."""


class MassiveWebSocketAdapter:
    name = "massive"
    is_synthetic = False

    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        instruments: Sequence[Instrument],
        sink: MarketDataSink,
        clock: Clock,
        is_market_open: Callable[[int], bool],
        backoff: ExponentialBackoff | None = None,
        connect_timeout_s: float = 15.0,
        auth_timeout_s: float = 10.0,
        silence_timeout_s: float = 30.0,
        stable_after_s: float = 30.0,
        auth_failed_retry_s: float = 300.0,
        max_connections_retry_s: float = 60.0,
        ping_interval_s: float | None = 20.0,
        ping_timeout_s: float | None = 20.0,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._channels = sorted(ws_quote_channel(i) for i in instruments)
        self._sink = sink
        self._clock = clock
        self._is_market_open = is_market_open
        self._backoff = backoff or ExponentialBackoff()
        self._connect_timeout_s = connect_timeout_s
        self._auth_timeout_s = auth_timeout_s
        self._silence_timeout_ms = int(silence_timeout_s * 1000)
        self._stable_after_ms = int(stable_after_s * 1000)
        self._auth_failed_retry_s = auth_failed_retry_s
        self._max_connections_retry_s = max_connections_retry_s
        self._ping_interval_s = ping_interval_s
        self._ping_timeout_s = ping_timeout_s

        self._stop = asyncio.Event()
        self._ws: ClientConnection | None = None
        self._streaming_since_ms: int | None = None
        self._first_data_pending = False
        self._limit_hit = False  # the server reported max_connections during this session
        self._last_log_ms: dict[str, int] = {}
        self._status = AdapterStatus(
            provider=self.name,
            is_synthetic=False,
            state=ConnectionState.IDLE,
            state_since_ms=clock.now_ms(),
            endpoint=url,
            subscriptions=list(self._channels),
        )

    # -- public API --------------------------------------------------------------------
    def status(self) -> AdapterStatus:
        return self._status

    async def stop(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(Exception):
                await ws.close(code=1000, reason="client shutdown")

    async def run(self) -> None:
        if not self._channels:
            self._set_state(ConnectionState.IDLE)
            await self._stop.wait()
            self._set_state(ConnectionState.STOPPED)
            return

        while not self._stop.is_set():
            delay: float | None = None
            failed_state = ConnectionState.RECONNECT_WAIT
            self._streaming_since_ms = None
            self._limit_hit = False
            try:
                await self._session()
            except AuthFailedError as exc:
                failed_state = ConnectionState.AUTH_FAILED
                delay = self._auth_failed_retry_s
                self._record_error(f"authentication failed: {exc}")
                self._emit("auth_failed", message=str(exc))
            except MaxConnectionsError as exc:
                delay = self._max_connections_retry_s
                self._record_error(f"connection limit reached: {exc}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._record_error(f"{type(exc).__name__}: {exc}")
                self._emit("error", error=type(exc).__name__, message=str(exc)[:300])
            finally:
                self._ws = None

            if self._stop.is_set():
                break

            now = self._clock.now_ms()
            if self._streaming_since_ms is not None and now - self._streaming_since_ms >= self._stable_after_ms:
                self._backoff.reset()
            if delay is None and self._limit_hit:
                # someone else holds the account's single connection: do not fight for it
                delay = self._max_connections_retry_s
            if delay is None:
                delay = self._backoff.next_delay()
            self._status.next_retry_ms = now + int(delay * 1000)
            self._set_state(failed_state)
            self._emit("reconnect_scheduled", delay_s=round(delay, 3), attempt=self._backoff.attempt)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)

        self._status.next_retry_ms = None
        self._set_state(ConnectionState.STOPPED)
        self._emit("stopped")

    # -- one connection lifecycle ------------------------------------------------------
    async def _session(self) -> None:
        self._status.connect_attempts += 1
        self._status.next_retry_ms = None
        self._set_state(ConnectionState.CONNECTING)
        self._emit("connect_attempt", attempt=self._status.connect_attempts)

        async with connect(
            self._url,
            open_timeout=self._connect_timeout_s,
            ping_interval=self._ping_interval_s,
            ping_timeout=self._ping_timeout_s,
            close_timeout=5,
            max_size=4 * 1024 * 1024,
        ) as ws:
            self._ws = ws
            self._emit("connected")
            streamed = False
            try:
                if self._stop.is_set():
                    return
                self._set_state(ConnectionState.AUTHENTICATING)
                try:
                    await asyncio.wait_for(self._authenticate(ws), timeout=self._auth_timeout_s)
                except TimeoutError:
                    raise TimeoutError(f"no auth reply within {self._auth_timeout_s:g}s") from None
                self._emit("auth_success")

                self._set_state(ConnectionState.SUBSCRIBING)
                await ws.send(json.dumps({"action": "subscribe", "params": ",".join(self._channels)}))
                self._emit("subscribed", channels=list(self._channels))

                now = self._clock.now_ms()
                self._streaming_since_ms = now
                self._first_data_pending = True
                self._status.sessions_established += 1
                self._status.last_data_ms = None
                self._set_state(ConnectionState.STREAMING)
                streamed = True

                watchdog = asyncio.create_task(self._watchdog(ws), name="massive-ws-watchdog")
                try:
                    async for frame in ws:
                        self._on_frame(frame)
                except ConnectionClosed:
                    pass
                finally:
                    watchdog.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await watchdog
            finally:
                duration_s = (
                    (self._clock.now_ms() - self._streaming_since_ms) / 1000
                    if streamed and self._streaming_since_ms is not None
                    else 0.0
                )
                self._emit(
                    "disconnected",
                    code=ws.close_code,
                    reason=(ws.close_reason or "")[:200],
                    streamed_s=round(duration_s, 1),
                    last_data_ms=self._status.last_data_ms,
                )

    async def _authenticate(self, ws: ClientConnection) -> None:
        """Send the API key and wait for ``auth_success``."""
        try:
            await ws.send(json.dumps({"action": "auth", "params": self._api_key}))
            while True:
                if self._handshake_frame(await ws.recv()):
                    return
        except ConnectionClosed:
            # The server hung up during the handshake.  Frames it sent before closing are
            # still buffered and say why (e.g. max_connections): read them, then re-raise.
            await self._drain_handshake(ws)
            raise

    async def _drain_handshake(self, ws: ClientConnection) -> None:
        while True:
            try:
                frame = await asyncio.wait_for(ws.recv(), timeout=0.25)
            except (ConnectionClosed, TimeoutError):
                return
            self._handshake_frame(frame)

    def _handshake_frame(self, frame: str | bytes) -> bool:
        """Process a frame received before streaming starts.  True once authenticated."""
        now = self._clock.now_ms()
        self._status.frames_received += 1
        self._status.last_message_ms = now
        try:
            parsed = parse_frame(frame, now)
        except FrameParseError:
            self._status.parse_errors += 1
            return False
        authenticated = False
        for st in parsed.statuses:
            if st.status == "auth_success":
                authenticated = True
            elif st.status == "auth_failed":
                raise AuthFailedError(st.message or "auth_failed")
            elif st.status == "max_connections":
                self._emit("server_status", status=st.status, message=st.message[:300])
                raise MaxConnectionsError(st.message or "max_connections")
            elif st.status != "connected":
                self._emit("server_status", status=st.status, message=st.message[:300])
        return authenticated

    def _on_frame(self, frame: str | bytes) -> None:
        now = self._clock.now_ms()
        st = self._status
        st.frames_received += 1
        st.last_message_ms = now
        try:
            parsed = parse_frame(frame, now)
        except FrameParseError as exc:
            st.parse_errors += 1
            self._log_limited("parse_error", "unparseable websocket frame", error=str(exc)[:200])
            return

        for status in parsed.statuses:
            if status.status == "success":
                self._emit("subscription_ack", message=status.message[:300])
            elif status.status not in ("connected", "auth_success"):
                # e.g. error, max_connections, slow_consumer, force_disconnect
                if status.status == "max_connections":
                    self._limit_hit = True
                self._record_error(f"server status {status.status}: {status.message}")
                self._emit("server_status", status=status.status, message=status.message[:300])

        st.malformed_events += parsed.malformed
        st.unknown_events += parsed.unknown
        if parsed.malformed:
            self._log_limited("malformed", "malformed quote events", count=parsed.malformed)

        if not parsed.quotes:
            return
        st.last_data_ms = now
        st.quotes_received += len(parsed.quotes)
        if self._first_data_pending:
            self._first_data_pending = False
            self._emit("first_data")
        for quote in parsed.quotes:
            try:
                self._sink.on_raw_quote(quote)
            except Exception:
                st.sink_errors += 1
                self._log_limited("sink_error", "sink failed to handle a quote", exc_info=True)

    async def _watchdog(self, ws: ClientConnection) -> None:
        """Force a reconnect when the feed goes silent while the market is open."""
        while True:
            await asyncio.sleep(1.0)
            now = self._clock.now_ms()
            reference = self._status.last_data_ms or self._streaming_since_ms or now
            silent_ms = now - reference
            if silent_ms > self._silence_timeout_ms and self._is_market_open(now):
                self._record_error(f"feed silent for {silent_ms / 1000:.0f}s while market open")
                self._emit("watchdog_timeout", silent_s=round(silent_ms / 1000, 1))
                await ws.close(code=WATCHDOG_CLOSE_CODE, reason="watchdog: feed silent")
                return

    # -- helpers -------------------------------------------------------------------------
    def _set_state(self, state: ConnectionState) -> None:
        if self._status.state is not state:
            self._status.state = state
            self._status.state_since_ms = self._clock.now_ms()

    def _record_error(self, message: str) -> None:
        self._status.last_error = message[:500]
        self._status.last_error_ms = self._clock.now_ms()

    def _emit(self, kind: str, **detail: Any) -> None:
        event = ConnectionEvent(ts_ms=self._clock.now_ms(), provider=self.name, kind=kind, detail=detail)
        level = "warning" if kind in ("error", "auth_failed", "watchdog_timeout", "server_status") else "info"
        getattr(log, level)("feed_event", kind=kind, **detail)
        try:
            self._sink.on_connection_event(event)
        except Exception:
            self._status.sink_errors += 1
            log.exception("sink failed to handle a connection event", kind=kind)

    def _log_limited(self, key: str, message: str, *, period_ms: int = 10_000, **kw: Any) -> None:
        now = self._clock.now_ms()
        if now - self._last_log_ms.get(key, 0) >= period_ms:
            self._last_log_ms[key] = now
            log.warning(message, **kw)
