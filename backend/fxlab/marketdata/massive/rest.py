"""Massive REST client (historical data, reference data, market status).

Endpoints used - all taken from Massive's documentation (see docs/MASSIVE_API_NOTES.md):

====================================================================  =======================
``GET /v2/aggs/ticker/{C:EURUSD}/range/{mult}/{timespan}/{from}/{to}``  aggregate bars (BID based)
``GET /v3/quotes/{C:EUR-USD}``                                         historical BBO quotes
``GET /v1/last_quote/currencies/{from}/{to}``                          last quote
``GET /v3/reference/tickers``                                          instrument availability
``GET /v1/marketstatus/now``                                           market status, server time
====================================================================  =======================

Authentication uses the ``Authorization: Bearer`` header (never the query string, so
the key cannot end up in URLs or logs).  Paginated responses carry a ``next_url``
that is followed with the same header, and only if it points at the configured host.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

import httpx

from fxlab.domain.models import Bar, DataSource, PriceBasis, RawQuote
from fxlab.logging import get_logger

log = get_logger(__name__)

#: statuses worth retrying (same list as the official client, which adds 413 and 499)
RETRYABLE_STATUS = frozenset({413, 429, 499, 500, 502, 503, 504})

MAX_PAGE_LIMIT = 50_000


class ProviderError(Exception):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderAuthError(ProviderError):
    """401 - the API key is missing or invalid."""


class ProviderNotEntitledError(ProviderError):
    """403 - the subscription plan does not include this data."""


class ProviderRateLimitError(ProviderError):
    """429 - still rate limited after all retries."""


class ProviderUnavailableError(ProviderError):
    """Network failure or 5xx after all retries."""


class MassiveRestClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        max_calls_per_min: int = 0,
        timeout_s: float = 30.0,
        max_retries: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._host = urlparse(self._base_url).hostname
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "fx-signal-lab/0.1",
            },
            timeout=timeout_s,
            transport=transport,
        )
        self._min_interval_s = 60.0 / max_calls_per_min if max_calls_per_min > 0 else 0.0
        self._max_retries = max_retries
        self._sleep = sleep
        self._next_call_at = 0.0
        self._throttle_lock = asyncio.Lock()
        self.calls = 0
        self.retries = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- low level ---------------------------------------------------------------------
    async def _throttle(self) -> None:
        if not self._min_interval_s:
            return
        async with self._throttle_lock:
            loop = asyncio.get_running_loop()
            wait = self._next_call_at - loop.time()
            if wait > 0:
                await self._sleep(wait)
            self._next_call_at = max(loop.time(), self._next_call_at) + self._min_interval_s

    def _check_same_host(self, url: str) -> None:
        if url.startswith("/"):
            return
        host = urlparse(url).hostname
        if host != self._host:
            raise ProviderError(f"refusing to follow a URL on a different host: {host!r}")

    async def get_json(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET with throttling and retries.  ``url`` is a path or a ``next_url``."""
        self._check_same_host(url)
        attempt = 0
        while True:
            await self._throttle()
            self.calls += 1
            retry_after: float | None = None
            failure: ProviderError
            try:
                resp = await self._client.get(url, params=params)
            except httpx.TransportError as exc:
                failure = ProviderUnavailableError(f"{type(exc).__name__}: {exc}")
            else:
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError as exc:
                        raise ProviderError(f"invalid JSON in response: {exc}", 200) from exc
                    if not isinstance(data, dict):
                        raise ProviderError("unexpected response shape (not a JSON object)", 200)
                    return data
                detail = _error_detail(resp)
                if resp.status_code == 401:
                    raise ProviderAuthError(detail, 401)
                if resp.status_code == 403:
                    raise ProviderNotEntitledError(detail, 403)
                if resp.status_code not in RETRYABLE_STATUS:
                    raise ProviderError(detail, resp.status_code)
                retry_after = _retry_after_seconds(resp)
                failure = (
                    ProviderRateLimitError(detail, 429)
                    if resp.status_code == 429
                    else ProviderUnavailableError(detail, resp.status_code)
                )

            attempt += 1
            if attempt > self._max_retries:
                raise failure
            self.retries += 1
            delay = retry_after if retry_after is not None else min(60.0, 0.5 * 2 ** (attempt - 1))
            delay += random.uniform(0, 0.25 * delay)
            log.warning(
                "rest_retry",
                url=_safe_url(url),
                attempt=attempt,
                delay_s=round(delay, 2),
                error=str(failure)[:200],
            )
            await self._sleep(delay)

    async def _paginate(self, path: str, params: dict[str, Any]) -> AsyncIterator[list[dict[str, Any]]]:
        url: str | None = path
        query: dict[str, Any] | None = params
        while url:
            data = await self.get_json(url, query)
            results = data.get("results") or []
            if not isinstance(results, list):
                raise ProviderError("unexpected 'results' shape (not a list)")
            yield results
            next_url = data.get("next_url")
            url = next_url if isinstance(next_url, str) and next_url else None
            query = None  # the cursor URL already carries every parameter

    # -- endpoints ---------------------------------------------------------------------
    async def market_status(self) -> dict[str, Any]:
        return await self.get_json("/v1/marketstatus/now")

    async def fx_ticker(self, ticker: str) -> dict[str, Any] | None:
        """Reference record of an FX ticker (e.g. ``C:EURUSD``) or ``None`` if Massive does not list it."""
        data = await self.get_json(
            "/v3/reference/tickers", {"ticker": ticker, "market": "fx", "active": "true", "limit": 10}
        )
        for item in data.get("results") or []:
            if isinstance(item, dict) and item.get("ticker") == ticker:
                return item
        return None

    async def last_quote(self, base: str, quote: str) -> dict[str, Any]:
        return await self.get_json(f"/v1/last_quote/currencies/{base}/{quote}")

    def iter_aggregates(
        self,
        ticker: str,
        multiplier: int,
        timespan: str,
        start_ms: int,
        end_ms: int,
        *,
        limit: int = MAX_PAGE_LIMIT,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Pages of aggregate bars whose window starts in ``[start_ms, end_ms]`` (ascending)."""
        path = f"/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{start_ms}/{end_ms}"
        return self._paginate(path, {"adjusted": "true", "sort": "asc", "limit": limit})

    def iter_quotes(
        self,
        ticker: str,
        *,
        gte_ms: int | None = None,
        lt_ms: int | None = None,
        order: str = "asc",
        limit: int = MAX_PAGE_LIMIT,
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Pages of historical BBO quotes.  Timestamps are sent in nanoseconds as documented."""
        params: dict[str, Any] = {"order": order, "sort": "timestamp", "limit": limit}
        if gte_ms is not None:
            params["timestamp.gte"] = str(gte_ms * 1_000_000)
        if lt_ms is not None:
            params["timestamp.lt"] = str(lt_ms * 1_000_000)
        return self._paginate(f"/v3/quotes/{ticker}", params)


# -- response conversion -----------------------------------------------------------------


def aggregate_to_bar(symbol: str, timeframe: str, result: dict[str, Any]) -> Bar | None:
    """Convert one aggregates ``results[]`` item into a provider bar.

    Massive builds FX aggregates from the **bid** side only and ``n``/``v`` are quote
    counts, not volume - hence ``price_basis = bid`` and no completeness counters.
    """
    try:
        ts = int(result["t"])
        o, h, lo, c = float(result["o"]), float(result["h"]), float(result["l"]), float(result["c"])
    except (KeyError, TypeError, ValueError):
        return None
    n = result.get("n", result.get("v"))
    return Bar(
        symbol=symbol,
        timeframe=timeframe,
        ts_ms=ts,
        open=o,
        high=h,
        low=lo,
        close=c,
        source=DataSource.MASSIVE_AGG,
        price_basis=PriceBasis.BID,
        n_quotes=int(n) if isinstance(n, (int, float)) else None,
    )


def quote_result_to_raw(symbol: str, result: dict[str, Any]) -> RawQuote:
    """Convert one ``/v3/quotes`` item.  ``recv_ts_ms`` is set to the source time (replay)."""
    ts_ns = result.get("participant_timestamp")
    ts_ms = ts_ns // 1_000_000 if isinstance(ts_ns, int) and not isinstance(ts_ns, bool) else None
    bid, ask = result.get("bid_price"), result.get("ask_price")
    exchange = result.get("bid_exchange")
    return RawQuote(
        symbol=symbol,
        bid=float(bid) if isinstance(bid, (int, float)) and not isinstance(bid, bool) else None,
        ask=float(ask) if isinstance(ask, (int, float)) and not isinstance(ask, bool) else None,
        source_ts_ms=ts_ms,
        recv_ts_ms=ts_ms if ts_ms is not None else 0,
        exchange=exchange if isinstance(exchange, int) and not isinstance(exchange, bool) else None,
    )


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, min(300.0, float(value)))
    except ValueError:
        return None


def _error_detail(resp: httpx.Response) -> str:
    message = ""
    try:
        body = resp.json()
        if isinstance(body, dict):
            message = str(body.get("message") or body.get("error") or body.get("status") or "")
    except ValueError:
        message = resp.text[:200]
    return f"HTTP {resp.status_code}: {message}".strip()


def _safe_url(url: str) -> str:
    """URL without query string (cursors are long; keys are never in URLs anyway)."""
    return url.split("?", 1)[0]
