# Massive API notes

What this integration relies on, where each fact comes from, and what could **not** be
checked without a live API key. Documentation was read on **2026-11-14**.

Massive is the new name of Polygon.io; hosts moved from `*.polygon.io` to `*.massive.com`.

> **Status of the integration:** implemented from the documentation below and tested
> end to end against a local server that speaks the documented protocol
> (`backend/tests/support/fake_massive.py`). `fxlab verify-provider` ran against
> Massive's production service once, on 15 November 2026 with the market closed:
> section 4 says what that settled. No live quote has been received yet.

## 1. Verified from documentation

### WebSocket (live data)

| Fact | Value | Source |
|---|---|---|
| Forex endpoint | `wss://socket.massive.com/forex` | [WS forex quotes][ws-quotes], [per-second aggregates][ws-cas] |
| Greeting | `[{"ev":"status","status":"connected","message":"Connected Successfully"}]` | [WS quickstart][ws-quick] |
| Authentication | send `{"action":"auth","params":"<API KEY>"}`; success reply `[{"ev":"status","status":"auth_success","message":"authenticated"}]`; subscribe only after it | [WS quickstart][ws-quick], [per-second aggregates][ws-cas] |
| Subscribe | `{"action":"subscribe","params":"C.EUR-USD,C.GBP-USD"}`; pair format `{from}-{to}`; `C.*` for all pairs | [WS forex quotes][ws-quotes] |
| Quote event | `{"ev":"C","p":"USD/CNH","x":"44","a":6.83366,"b":6.83363,"t":1536036818784}`: `a` ask, `b` bid, `t` Unix ms, `x` exchange id | [WS forex quotes][ws-quotes] |
| Framing | every frame is a JSON array; several events may be bundled in one frame | [WS quickstart][ws-quick] |
| Connection limit | one concurrent connection per asset class by default | [WS quickstart][ws-quick] |
| Slow consumers | the server may disconnect a client that reads too slowly | [WS quickstart][ws-quick], [KB: disconnects][kb-disc] |
| Other channels | `CA.` per-minute and `CAS.` per-second aggregates (derived from quotes), `FMV` (Business plan only) | [WS forex overview][ws-over], [FMV][ws-fmv] |

From the official Python client (`massive` 2.10.0 on PyPI, read as a reference, not used
at runtime): it reads the greeting, sends `auth`, treats status `auth_failed` as an
authentication error, treats `connected` / `auth_success` / `success` as normal and logs
every other status (`error`, `max_connections`, `slow_consumer`, `force_disconnect`) as a
server problem; unsubscribe is `{"action":"unsubscribe","params":...}`.

### REST (history, reference)

| Fact | Value | Source |
|---|---|---|
| Base URL, auth | `https://api.massive.com`; `Authorization: Bearer <key>` header (or `?apiKey=`) | [REST quickstart][rest-quick] |
| Aggregate bars | `GET /v2/aggs/ticker/{C:EURUSD}/range/{multiplier}/{timespan}/{from}/{to}`; `from`/`to` as `YYYY-MM-DD` or Unix ms; `adjusted`, `sort`, `limit` (default 5000, max 50000, counts base aggregates); `t` is the window start in Unix ms; windows in UTC; no bar when no quote | [Custom bars][rest-aggs], [KB: limit][kb-limit] |
| Bars are bid-based | o/h/l/c are the bid of the first / highest / lowest / last quote; `n` and `v` are both the quote count; `vw` is the plain average bid | [KB: forex bars][kb-bars] |
| Day bars | follow the UTC day (00:00 to 23:59 UTC) | [REST forex overview][rest-over] |
| Historical quotes | `GET /v3/quotes/{C:EUR-USD}`; `timestamp.gte/gt/lte/lt` (nanoseconds), `order`, `sort`, `limit` (default 1000, max 50000); fields `bid_price`, `ask_price`, `bid_exchange`, `ask_exchange`, `participant_timestamp` (ns); `next_url` pagination | [Quotes][rest-quotes] |
| Pagination | follow `next_url` unchanged, re-sending the key (header or `apiKey`) | [KB: paging][kb-page] |
| Last quote | `GET /v1/last_quote/currencies/{from}/{to}` | [Last quote][rest-last] |
| Reference tickers | `GET /v3/reference/tickers` with `ticker`, `market=fx`, `active` | [All tickers][rest-tickers] |
| Market status | `GET /v1/marketstatus/now` returns `currencies.fx` and `serverTime` | [Market status][rest-status] |
| FX hours | Sunday 17:00 to Friday 17:00 Eastern Time | [KB: forex hours][kb-hours] |

### Plans (Currencies)

| Capability | Basic (free) | Starter ($49/mo) | Business |
|---|---|---|---|
| REST calls | 5 per minute | unlimited | unlimited |
| Aggregate bars (REST) | yes, end-of-day recency, 2 years | yes, real-time, all history (from 2009-09-25) | yes |
| Historical quotes `/v3/quotes`, last quote | no | yes | yes |
| WebSocket quotes and aggregates | no | yes, real-time | yes |
| Fair Market Value | no | no | yes |

Sources: [pricing][pricing], plan tables on the endpoint pages above.

**Consequence:** live ingestion and outage recovery need **Currencies Starter or higher**.
On Basic the application can only download end-of-day aggregates.

## 2. How the facts shape the design

* **Quotes, not aggregates, are the live source.** Provider aggregates use the bid side
  only and no parameter changes that. The 1-second grid is therefore built here, from
  bid/ask quotes, as mid prices with the spread preserved.
* **Provider bars are stored separately** (`source = massive_agg`, `price_basis = bid`)
  and are never mixed with the system's own mid bars. Their daily bars follow the UTC
  day; ours follow the 17:00 New York rollover.
* **Bad quotes exist in the feed.** The knowledge base shows a quote with a spread of
  about 14% of its mid setting a bar's low. The normaliser rejects quotes wider than a
  per-pair limit and unconfirmed jumps, and counts every rejection.
* **Timestamps may be coarse.** The knowledge-base example shows quotes stamped on whole
  seconds. Latency statistics detect this from the data and then report figures as
  upper bounds instead of pretending to millisecond accuracy.
* **One connection.** Exactly one ingestion process may run per account; a
  `max_connections` status is handled by waiting, not by reconnecting in a loop.
* **No replay after a reconnect.** Missed data is recovered from `/v3/quotes`.

## 3. Not verified (needs a live key)

Each item is handled defensively; `fxlab verify-provider` reports on most of them.

1. That the production service accepts the documented handshake and channel names
   exactly as written (`C.EUR-USD`), and what the `p` field looks like in practice
   (`EUR/USD` vs `EUR-USD`; the parser accepts both).
2. Resolution and meaning of the WebSocket timestamp `t` (true milliseconds or whole
   seconds) and how far it lags real time.
3. Real quote rate for EUR/USD, and whether quotes arrive during the weekend closure.
4. Exact bodies and status codes for a rejected key (handled: 401), a plan without
   access (handled: 403) and rate limiting (handled: 429 with optional `Retry-After`).
5. Whether `/v2/aggs` with `timespan=second` counts seconds as base aggregates (requests
   are chunked to 45 000 seconds and `next_url` is followed, so either answer works).
6. Whether the server confirms subscriptions with a `success` status (accepted but not
   required) and what it sends before closing an idle or slow connection.
7. Availability of each planned pair, especially USD/ZAR, USD/PLN and USD/TRY, on your
   subscription. They are marked `unverified` until the reference check runs.
8. How quickly `/v3/quotes` contains quotes that were just published (gap backfill waits
   60 s after a reconnect before asking).

## 4. First run against production (15 November 2026, market closed)

`make verify` on the server, probe pair EUR/USD, window: the last 30 minutes before the
weekly close of Friday 13 November (21:30 to 22:00 UTC).

Settled:

* **Points 1 and 6, the handshake.** `wss://socket.massive.com/forex` greeted, accepted
  the key, accepted the subscription to `C.EUR-USD` and confirmed it with a `success`
  status. What the `p` field of a quote looks like is still open.
* **Point 7, the pairs.** The reference endpoint lists all eight configured pairs,
  USD/TRY, USD/ZAR and USD/PLN included.
* **REST shapes.** Market status, minute and second bars, historical quotes and the
  last quote came back in the documented shapes: bars as `v, vw, o, c, h, l, t, n` with
  `v` equal to `n`, quotes as `ask_exchange, ask_price, bid_exchange, bid_price,
  participant_timestamp`, the last quote as `ask, bid, exchange, timestamp`.
* **Point 5, in part.** Second bars work on the Starter plan: 113 bars for the last 120
  seconds.

New facts:

* **Historical quotes are stamped in whole seconds.** All 109 quotes of the last minute
  had a `participant_timestamp` on a full second, and so had the last quote. Section 3,
  point 2 (the stream's `t`) is still open, but whole seconds are now the likely answer.
* **Quote rate at a quiet time (point 3, in part).** 109 quotes in the last minute
  before the close, 83 in the minute at 21:30, a quote in 113 of the last 120 seconds:
  1.4 to 1.8 quotes a second. Busy hours are not known yet. No quote arrived on the
  stream in 20 seconds on a Sunday morning, as expected with the market closed.

Still open: points 2, 4 and 8, the `p` field, the quote rate in busy hours, and whether
the stream carries the same quotes as `/v3/quotes` (to be compared once live ticks exist).

[ws-quotes]: https://massive.com/docs/websocket/forex/quotes
[ws-cas]: https://massive.com/docs/websocket/forex/aggregates-per-second.md
[ws-quick]: https://massive.com/docs/websocket/quickstart.md
[ws-over]: https://massive.com/docs/websocket/forex/overview
[ws-fmv]: https://massive.com/docs/websocket/forex/fair-market-value.md
[rest-quick]: https://massive.com/docs/rest/quickstart.md
[rest-aggs]: https://massive.com/docs/rest/forex/aggregates/custom-bars.md
[rest-over]: https://massive.com/docs/rest/forex/overview.md
[rest-quotes]: https://massive.com/docs/rest/forex/quotes/quotes.md
[rest-last]: https://massive.com/docs/rest/forex/quotes/last-quote.md
[rest-tickers]: https://massive.com/docs/rest/forex/tickers/all-tickers.md
[rest-status]: https://massive.com/docs/rest/forex/market-operations/market-status.md
[kb-bars]: https://massive.com/knowledge-base/article/how-are-massives-forex-bars-built-and-what-do-v-and-n-mean-on-a-forex-bar
[kb-limit]: https://massive.com/knowledge-base/article/what-is-the-function-of-the-limit-parameter-in-massives-aggregate-endpoint
[kb-page]: https://massive.com/knowledge-base/article/how-do-i-page-through-all-the-trades-or-quotes-for-a-day
[kb-hours]: https://massive.com/knowledge-base/article/what-are-the-forex-market-hours
[kb-disc]: https://massive.com/knowledge-base/article/why-do-massive-websocket-disconnects-happen
[pricing]: https://massive.com/currencies
