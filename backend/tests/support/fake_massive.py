"""A local stand-in for Massive's forex API - TEST HARNESS ONLY.

It implements the WebSocket protocol and the REST endpoints **exactly as documented**
(see docs/MASSIVE_API_NOTES.md) so the real integration code can be exercised end to
end over real sockets: authentication, subscription, bundled frames, disconnects,
rate limiting, pagination, entitlement errors.

It proves that the code implements the documented protocol correctly.  It can *not*
prove that Massive's production service behaves as documented - that needs a real API
key (`fxlab verify-provider`).

The fake keeps a "tape" of every quote its simulated market produced, whether or not
a client was connected at the time, and serves that tape through the REST quotes and
aggregates endpoints.  That is what makes outage-recovery tests meaningful: quotes
published while the client was disconnected are only obtainable through REST.

Run standalone for manual end-to-end tests::

    python -m tests.support.fake_massive --port 9911 --rate 6
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import itertools
import json
import math
import random
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

UNIT_MS = {"second": 1_000, "minute": 60_000, "hour": 3_600_000, "day": 86_400_000}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


@dataclass(eq=False)
class _Client:
    ws: WebSocket
    authed: bool = False
    channels: set[str] = field(default_factory=set)


class FakeMassive:
    def __init__(self, api_key: str = "test-key") -> None:
        self.api_key = api_key
        self.base_url = ""  # set by the server once the port is known
        self.tape: list[dict[str, Any]] = []
        self.clients: list[_Client] = []
        self.fx_tickers = {"C:EURUSD", "C:GBPUSD", "C:USDJPY"}
        # behaviour switches
        self.send_connected = True
        self.send_subscription_ack = True
        self.reject_auth = False
        self.max_connections_mode = False
        self.quotes_entitled = True
        self.silent = False  # keep connections open but stop sending quotes
        self.rest_fail_next: list[int] = []
        self.quotes_page_size: int | None = None
        self.aggs_page_size: int | None = None
        # Granularity of the timestamps the provider hands out (the tape keeps the true time).
        # Massive's /v3/quotes returned whole seconds for EUR/USD on 15 November 2026.
        self.history_resolution_ms = 1
        self.stream_resolution_ms = 1
        # observations
        self.connection_count = 0
        self.auth_attempts = 0
        self.subscribe_messages: list[str] = []
        self.rest_calls: list[str] = []
        self.rest_auth_headers: list[str | None] = []
        self._cursors: dict[str, tuple[list[dict[str, Any]], int, int]] = {}
        self._cursor_ids = itertools.count(1)

    # ------------------------------------------------------------------ websocket ----
    async def _ws_endpoint(self, ws: WebSocket) -> None:
        await ws.accept()
        self.connection_count += 1
        client = _Client(ws)
        try:
            if self.send_connected:
                await self._send(ws, [{"ev": "status", "status": "connected", "message": "Connected Successfully"}])
            if self.max_connections_mode:
                await self._send(
                    ws,
                    [
                        {
                            "ev": "status",
                            "status": "max_connections",
                            "message": "Maximum number of websocket connections exceeded.",
                        }
                    ],
                )
                await ws.close(code=1008)
                return
            while True:
                data = json.loads(await ws.receive_text())
                action, params = data.get("action"), str(data.get("params", ""))
                if action == "auth":
                    self.auth_attempts += 1
                    if params == self.api_key and not self.reject_auth:
                        client.authed = True
                        await self._send(ws, [{"ev": "status", "status": "auth_success", "message": "authenticated"}])
                    else:
                        await self._send(
                            ws,
                            [{"ev": "status", "status": "auth_failed", "message": "authentication failed"}],
                        )
                        await ws.close(code=1008)
                        return
                elif action == "subscribe":
                    if not client.authed:
                        await self._send(ws, [{"ev": "status", "status": "error", "message": "not authenticated"}])
                        continue
                    self.subscribe_messages.append(params)
                    channels = [c for c in params.split(",") if c]
                    client.channels.update(channels)
                    if client not in self.clients:
                        self.clients.append(client)
                    if self.send_subscription_ack:
                        for ch in channels:
                            await self._send(
                                ws, [{"ev": "status", "status": "success", "message": f"subscribed to: {ch}"}]
                            )
                elif action == "unsubscribe":
                    client.channels.difference_update(params.split(","))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            if client in self.clients:
                self.clients.remove(client)

    @staticmethod
    async def _send(ws: WebSocket, events: list[dict[str, Any]]) -> None:
        await ws.send_text(json.dumps(events))

    @staticmethod
    def _channel(pair: str) -> str:
        return "C." + pair.replace("/", "-")

    async def publish(self, quotes: list[dict[str, Any]] | None = None, **one: Any) -> None:
        """Add quotes to the tape and stream them to subscribed clients as ONE frame.

        Each quote: ``{"pair": "EUR/USD", "b": bid, "a": ask, "t": ms}`` (``pair``/``t`` optional).
        """
        items = quotes if quotes is not None else [one]
        events_by_client: dict[_Client, list[dict[str, Any]]] = {}
        for q in items:
            quote = {
                "pair": q.get("pair", "EUR/USD"),
                "b": q["b"],
                "a": q["a"],
                "t": q.get("t", _now_ms()),
                "x": q.get("x", 48),
            }
            self.tape.append(quote)
            if self.silent:
                continue
            event = {
                "ev": "C",
                "p": quote["pair"],
                "x": quote["x"],
                "a": quote["a"],
                "b": quote["b"],
                "t": quote["t"] - quote["t"] % self.stream_resolution_ms,
            }
            for client in self.clients:
                if self._channel(quote["pair"]) in client.channels or "C.*" in client.channels:
                    events_by_client.setdefault(client, []).append(event)
        for client, events in events_by_client.items():
            with contextlib.suppress(Exception):
                await self._send(client.ws, events)

    async def send_raw(self, text: str) -> None:
        for client in list(self.clients):
            with contextlib.suppress(Exception):
                await client.ws.send_text(text)

    async def send_status(self, status: str, message: str = "") -> None:
        await self.send_raw(json.dumps([{"ev": "status", "status": status, "message": message}]))

    async def drop_connections(self, code: int = 1011) -> None:
        """Close every client connection from the server side."""
        for client in list(self.clients):
            with contextlib.suppress(Exception):
                if client.ws.application_state is WebSocketState.CONNECTED:
                    await client.ws.close(code=code)
        self.clients.clear()

    # ----------------------------------------------------------------------- REST ----
    def _guard(self, request: Request) -> JSONResponse | None:
        self.rest_calls.append(request.url.path)
        header = request.headers.get("authorization")
        self.rest_auth_headers.append(header)
        if self.rest_fail_next:
            code = self.rest_fail_next.pop(0)
            headers = {"Retry-After": "0"} if code == 429 else {}
            return JSONResponse({"status": "ERROR", "error": f"simulated {code}"}, status_code=code, headers=headers)
        if header != f"Bearer {self.api_key}" and request.query_params.get("apiKey") != self.api_key:
            return JSONResponse(
                {"status": "ERROR", "request_id": "fake", "error": "API Key was not provided"},
                status_code=401,
            )
        return None

    async def _market_status(self, request: Request) -> JSONResponse:
        if (denied := self._guard(request)) is not None:
            return denied
        return JSONResponse(
            {
                "afterHours": False,
                "currencies": {"crypto": "open", "fx": "open"},
                "earlyHours": False,
                "exchanges": {"nasdaq": "open", "nyse": "open", "otc": "closed"},
                "market": "open",
                "serverTime": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        )

    async def _tickers(self, request: Request) -> JSONResponse:
        if (denied := self._guard(request)) is not None:
            return denied
        wanted = request.query_params.get("ticker")
        results = [
            {
                "ticker": t,
                "name": f"{t[2:5]} - {t[5:]}",
                "market": "fx",
                "locale": "global",
                "active": True,
                "base_currency_symbol": t[2:5],
                "currency_symbol": t[5:],
                "last_updated_utc": "2026-11-13T00:00:00Z",
            }
            for t in sorted(self.fx_tickers)
            if not wanted or t == wanted
        ]
        return JSONResponse({"count": len(results), "request_id": "fake", "results": results, "status": "OK"})

    def _pair_from_ticker(self, ticker: str) -> str:
        body = ticker[2:].replace("-", "")
        return f"{body[:3]}/{body[3:]}"

    def _page(
        self, request: Request, rows: list[dict[str, Any]], page_size: int | None, extra: dict[str, Any]
    ) -> JSONResponse:
        cursor = request.query_params.get("cursor")
        if cursor is not None:
            rows, offset, size = self._cursors.pop(cursor)
        else:
            requested = int(request.query_params.get("limit", "5000"))
            offset, size = 0, min(requested, page_size or requested)
        chunk = rows[offset : offset + size]
        body: dict[str, Any] = {"request_id": "fake", "status": "OK", **extra}
        if chunk:
            body["results"] = chunk
        if "resultsCount" in extra or "queryCount" in extra:
            body["resultsCount"] = body["queryCount"] = len(chunk)
        if offset + size < len(rows):
            cid = str(next(self._cursor_ids))
            self._cursors[cid] = (rows, offset + size, size)
            body["next_url"] = f"{self.base_url}{request.url.path}?cursor={cid}"
        return JSONResponse(body)

    async def _quotes(self, request: Request) -> JSONResponse:
        if (denied := self._guard(request)) is not None:
            return denied
        if not self.quotes_entitled:
            return JSONResponse(
                {
                    "status": "NOT_AUTHORIZED",
                    "request_id": "fake",
                    "message": "You are not entitled to this data.",
                },
                status_code=403,
            )
        if "cursor" in request.query_params:
            return self._page(request, [], self.quotes_page_size, {})
        pair = self._pair_from_ticker(request.path_params["ticker"])
        qp = request.query_params
        res = self.history_resolution_ms

        def stamp(q: dict[str, Any]) -> int:
            return (q["t"] - q["t"] % res) * 1_000_000  # nanoseconds, at the provider's granularity

        rows = [q for q in self.tape if q["pair"] == pair]
        for name, keep in (
            ("timestamp.gte", lambda t, v: t >= v),
            ("timestamp.gt", lambda t, v: t > v),
            ("timestamp.lte", lambda t, v: t <= v),
            ("timestamp.lt", lambda t, v: t < v),
        ):
            if name in qp:
                bound = int(qp[name])
                rows = [q for q in rows if keep(stamp(q), bound)]
        # stable: quotes that share a timestamp stay in the order they were published, in both directions
        rows = sorted(rows, key=stamp, reverse=qp.get("order", "desc") == "desc")
        results = [
            {
                "ask_exchange": q["x"],
                "ask_price": q["a"],
                "bid_exchange": q["x"],
                "bid_price": q["b"],
                "participant_timestamp": stamp(q),
            }
            for q in rows
        ]
        return self._page(request, results, self.quotes_page_size, {})

    async def _aggs(self, request: Request) -> JSONResponse:
        if (denied := self._guard(request)) is not None:
            return denied
        ticker = request.path_params["ticker"]
        extra = {"ticker": ticker, "adjusted": True, "queryCount": 0, "resultsCount": 0}
        if "cursor" in request.query_params:
            return self._page(request, [], self.aggs_page_size, extra)
        pair = self._pair_from_ticker(ticker)
        window = int(request.path_params["mult"]) * UNIT_MS[request.path_params["span"]]
        start, end = int(request.path_params["start"]), int(request.path_params["end"])
        groups: dict[int, list[float]] = {}
        for q in sorted((q for q in self.tape if q["pair"] == pair), key=lambda q: q["t"]):
            bucket = q["t"] - q["t"] % window
            if start <= bucket <= end:
                groups.setdefault(bucket, []).append(q["b"])  # BID side only, as Massive does
        results = [
            {
                "t": t,
                "o": bids[0],
                "h": max(bids),
                "l": min(bids),
                "c": bids[-1],
                "n": len(bids),
                "v": len(bids),
                "vw": round(sum(bids) / len(bids), 4),
            }
            for t, bids in sorted(groups.items(), reverse=request.query_params.get("sort", "asc") == "desc")
        ]
        return self._page(request, results, self.aggs_page_size, extra)

    async def _last_quote(self, request: Request) -> JSONResponse:
        if (denied := self._guard(request)) is not None:
            return denied
        pair = f"{request.path_params['base']}/{request.path_params['quote']}"
        rows = [q for q in self.tape if q["pair"] == pair]
        if not rows:
            return JSONResponse({"status": "NOT_FOUND", "request_id": "fake"}, status_code=404)
        q = rows[-1]
        return JSONResponse(
            {
                "last": {"ask": q["a"], "bid": q["b"], "exchange": q["x"], "timestamp": q["t"]},
                "request_id": "fake",
                "status": "success",
                "symbol": pair,
            }
        )

    def app(self) -> Starlette:
        return Starlette(
            routes=[
                WebSocketRoute("/forex", self._ws_endpoint),
                Route("/v1/marketstatus/now", self._market_status),
                Route("/v3/reference/tickers", self._tickers),
                Route("/v3/quotes/{ticker}", self._quotes),
                Route("/v2/aggs/ticker/{ticker}/range/{mult}/{span}/{start}/{end}", self._aggs),
                Route("/v1/last_quote/currencies/{base}/{quote}", self._last_quote),
            ]
        )


class FakeMassiveServer:
    """Runs a :class:`FakeMassive` on a local TCP port inside the current event loop."""

    def __init__(self, fake: FakeMassive | None = None, port: int = 0) -> None:
        self.fake = fake or FakeMassive()
        self._requested_port = port
        self.port = 0
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> FakeMassiveServer:
        config = uvicorn.Config(
            self.fake.app(), host="127.0.0.1", port=self._requested_port, log_level="error", lifespan="off"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(500):
            if self._server.started:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("fake Massive server did not start")
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        self.fake.base_url = self.rest_url
        return self

    async def stop(self) -> None:
        if self._server is not None:
            await self.fake.drop_connections(code=1001)
            self._server.should_exit = True
        if self._task is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._task, timeout=10)

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/forex"

    @property
    def rest_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


async def run_market(
    fake: FakeMassive, rate: float, pairs: dict[str, float], drop_every_s: float | None, outage_s: float = 8.0
) -> None:
    """Simulate a market: random-walk quotes at ``rate`` per second per pair.

    With ``drop_every_s`` the fake periodically cuts every client connection and stays
    silent on the WebSocket for ``outage_s`` seconds while the market keeps moving, so
    those quotes exist only on the REST side - exactly what an outage looks like.
    """
    rng = random.Random()
    mids = dict(pairs)
    next_drop = time.monotonic() + drop_every_s if drop_every_s else math.inf
    silent_until = 0.0
    while True:
        await asyncio.sleep(rng.expovariate(rate * len(mids)))
        now = time.monotonic()
        fake.silent = now < silent_until
        pair = rng.choice(list(mids))
        mids[pair] *= math.exp(rng.gauss(0, 0.00002))
        half = mids[pair] * 0.00003
        digits = 3 if pair.endswith("JPY") else 5
        await fake.publish(pair=pair, b=round(mids[pair] - half, digits), a=round(mids[pair] + half, digits))
        if now >= next_drop:
            next_drop = now + (drop_every_s or 0)
            silent_until = now + outage_s
            fake.silent = True
            print("fake-massive: dropping client connections (simulated outage)", flush=True)
            await fake.drop_connections()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="Fake Massive forex API for manual end-to-end tests")
    parser.add_argument("--port", type=int, default=9911)
    parser.add_argument("--api-key", default="test-key")
    parser.add_argument("--rate", type=float, default=5.0, help="quotes per second per pair")
    parser.add_argument("--drop-every", type=float, default=None, help="drop client connections every N seconds")
    parser.add_argument("--history-resolution-ms", type=int, default=1, help="1000: /v3/quotes stamps whole seconds")
    parser.add_argument("--stream-resolution-ms", type=int, default=1, help="1000: the stream stamps whole seconds")
    args = parser.parse_args()
    server = await FakeMassiveServer(FakeMassive(args.api_key), args.port).start()
    server.fake.history_resolution_ms = args.history_resolution_ms
    server.fake.stream_resolution_ms = args.stream_resolution_ms
    print(f"fake-massive listening: WS {server.ws_url}  REST {server.rest_url}  key={args.api_key}", flush=True)
    await run_market(server.fake, args.rate, {"EUR/USD": 1.1000, "GBP/USD": 1.3000, "USD/JPY": 150.0}, args.drop_every)


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_main())
