# Architecture

FX Signal Lab is a decision-support system for a discretionary FX trader. It collects
market data, turns it into a trustworthy research dataset and (in later phases)
computes indicators, backtests strategies and raises signals. It never places orders.

This document describes the system as built in Phase 1 and the seams left for later
phases.

## 1. Shape of the system

One Python process, one database, one dashboard.

```
                         Massive                               browser
                  WebSocket     REST                              ^
                      |           ^                               | HTTP + WebSocket
                      v           |                               |
+-------------------------------------------------------------------------------------+
| app process (fxlab serve, exactly one)                                              |
|                                                                                     |
|  marketdata/        pipeline/                                    api/               |
|  +-----------+      +-----------+   +---------+   +---------+   +----------------+  |
|  | adapter   |----->| normaliser|-->| 1-second|-->| bar     |-->| REST /api/...  |  |
|  | (massive  | raw  | validate, |tick sampler |obs|aggregator   | WS   /ws/live  |  |
|  |  or mock) |quote | dedupe,   |   +---------+   +---------+   | static files   |  |
|  +-----------+      | outliers  |        |             |        +----------------+  |
|        |            +-----------+        v             v                ^           |
|        | connection      |          +-----------------------+           |           |
|        | events          | counters,| batch writer          |   live hub (pub/sub)  |
|        v                 v latency  | (one tx per second)   |                       |
|  services/gaps  <---  ingestion service (orchestration, health)                     |
|  services/history     services/provider_check                                       |
+----------------------------------------------|--------------------------------------+
                                               v
                               PostgreSQL 16 + TimescaleDB
```

Why one process: the provider allows a single WebSocket connection per asset class,
the write volume is small (hundreds of rows per second at most), and one process has
no inter-service failure modes. The ingestion service is a self-contained object
(`IngestionService`), so it can be moved into its own process later if the API ever
needs to restart independently; nothing else would change.

No Redis, no message broker, no ORM: none is justified at this scale.

## 2. Modules and their contracts

| Package | Responsibility | Depends on |
|---|---|---|
| `fxlab.domain` | Records (`QuoteTick`, `Observation`, `Bar`), instrument registry, calendars, timeframes. Pure. | nothing |
| `fxlab.marketdata` | `MarketDataAdapter` / `MarketDataSink` protocols; Massive WebSocket and REST clients; mock adapter. | domain |
| `fxlab.pipeline` | Normaliser, sampler, bar aggregator, latency statistics, quality gate, live hub, ingestion service. | domain, marketdata, storage |
| `fxlab.storage` | Schema, migrations, repository functions, batch writer, data-mode guard. | domain |
| `fxlab.services` | Gap detection and recovery, historical download, provider verification. | all of the above |
| `fxlab.api` | FastAPI app: REST, live WebSocket, static dashboard. | pipeline, services |
| `fxlab.cli` | `serve`, `migrate`, `verify-provider`, `history`, `gaps`, `instruments`. | everything |
| `frontend/` | React dashboard. Talks only to `/api` and `/ws/live`. | the API |

Key interfaces:

* **`MarketDataAdapter`** (`run()`, `stop()`, `status()`, `is_synthetic`): owns a
  provider connection and pushes into a sink. Adding a provider means writing one
  adapter; nothing downstream changes.
* **`MarketDataSink`** (`on_raw_quote`, `on_connection_event`): synchronous, fast, no
  I/O. Implemented by the ingestion service.
* **`QuoteNormalizer.process(raw) -> NormalizeResult`**: the only place quotes are
  judged. Verdicts: accept, duplicate, late, reject (with reason).
* **`SecondSampler`** (`on_quote`, `advance`): pure and clock-free; the same code samples
  the live stream and replays history.
* **`BarAggregator.on_observation(obs) -> list[Bar]`**: the only bar-building code.
* **Repository functions** (`fxlab.storage.repo`): plain async functions over a
  connection; the only place rows are converted to and from domain records.
* **`evaluate_signal_gate(...)`**: one pure function deciding whether data is good
  enough for signals. Phase 1 displays it; Phase 4 must obey it.

Dependencies are injected through constructors (clock, adapter factory, REST transport),
which is what makes the real-time paths testable.

## 3. Time conventions

* Inside the pipeline every instant is an integer of **epoch milliseconds, UTC**.
  `datetime` objects exist only at the database and API edges, always timezone-aware.
* Every interval is **labelled by its start** and is half-open: `[start, end)`. A
  1-second observation labelled 12:00:00 covers 12:00:00.000 to 12:00:00.999 and
  becomes known at 12:00:01. The provider's aggregates use the same convention.
* A record is usable for a decision at time `now` only if `start + length <= now`. That
  rule is the whole look-ahead discipline.
* Bars up to one hour are aligned to the UTC epoch. `4h` and `1d` bars are anchored to
  the instrument's daily rollover (17:00 New York for spot FX), so a week has five
  daily bars. Daylight-saving changes fall inside the weekend closure.

## 4. The 1-second observation grid

For every active instrument and every second the sampler emits exactly one
observation with one of four statuses:

| Status | Meaning | Price fields |
|---|---|---|
| `fresh` | at least one valid quote was received during the second | last bid/ask, plus open/high/low of the mid inside the second and the quote count |
| `carried` | no quote arrived; the previous quote is still younger than the stale threshold | the carried bid/ask; no open/high/low |
| `stale` | the quote in force is older than the pair's stale threshold | kept for diagnostics, never used for bars or signals |
| `missing` | no valid quote is known at all | null |

Rules that keep the grid honest:

* A quote is assigned to the second in which it was **received** (live) and windows
  that have closed are never reopened, so an observation cannot contain information
  that arrived after it was emitted.
* Age is measured against the quote's **source** timestamp. A newly delivered quote
  that describes an old market is `stale`, not `fresh`.
* Nothing is interpolated. A quiet second repeats the last quote and says so.
* While the market is closed only real updates are stored; there is no filler for the
  weekend.
* If the process falls more than 15 minutes behind (suspended laptop, clock jump) it
  does not flood the grid; it skips ahead and hands the range to the gap detector.

## 5. Quote validation

Applied in order; every outcome is counted per instrument and shown on the dashboard.

1. unknown symbol
2. bid and ask must be finite positive numbers
3. source timestamp must be an integer after 2009 and not more than 60 s ahead of the
   local clock (1.5 s ahead is accepted but flags clock skew)
4. crossed quotes (ask below bid) are rejected; locked quotes are accepted
5. spread above the pair's `max_spread_pips` is rejected
6. an exact repeat of the previous accepted quote (same sub-second timestamp, prices
   and venue) is a duplicate. With a whole-second timestamp a repeat counts as a quote
   of its own: the timestamp cannot tell two quotes apart, and the provider counts both
7. a quote older than the one in force is `late`: stored as a tick, not applied
8. a mid more than `max_jump_pct` away from the last accepted mid is an outlier, unless
   `jump_confirm_quotes` consecutive quotes agree on the new level (a real level shift)
   or the reference is older than a minute

All thresholds are per instrument in `config/instruments.yaml`.

## 6. Bars

Bars are derived from the grid, never from a second code path:

* only `fresh` and `carried` seconds contribute a price; a `fresh` second contributes
  its real intra-second high and low, a `carried` second one flat point
* a bar without any usable second is not written
* `n_fresh`, `n_valid`, `n_expected` and `complete` record how much of the bar was
  really observed; `n_backfilled` records how many seconds were recovered later
* the live stream, the warm start after a restart and the rebuild after a backfill
  all run observations through the same `BarAggregator`

## 7. Storage

PostgreSQL 16 with TimescaleDB. On plain PostgreSQL the same schema works without
compression and retention (used by the tests); the dashboard says which one is in use.

| Table | Content | Key |
|---|---|---|
| `instruments` | mirror of the YAML registry plus the provider availability check | `symbol` |
| `quote_ticks` (hypertable) | every accepted or late quote; `flags` bit 1 late, 2 backfilled, 4 synthetic | index on instrument, time |
| `obs_1s` (hypertable) | the 1-second grid; `source` = live, backfill or mock | instrument, second |
| `bars` (hypertable) | bars for 5s to 1d; `source` = live, massive_agg or mock; `price_basis` = mid or bid | instrument, timeframe, source, start |
| `connection_events` | feed lifecycle events | id |
| `data_gaps` | detected gaps and what became of them | id |
| `history_jobs` | historical download jobs | id |
| `ingest_stats_1m` | per-minute quote counters and latency percentiles | instrument, minute |
| `app_meta` | data mode of this database | key |

Writes go through one batch writer: producers append to memory, one transaction per
second flushes. If the database is down, rows stay buffered and are retried; if the
buffer limit is hit, the oldest raw ticks are dropped first, then observations, and
every dropped row is counted and logged.

Retention defaults: raw ticks 30 days, observations and bars forever, compression after
7 days. Parquet archives are planned for Phase 3.

## 8. Provenance and the mock provider

* Every stored row says where it came from (see the table above).
* A database belongs to one data mode. The first start records the provider and feed
  host; a later start with a different mode is refused. Mock data cannot enter a
  database of market data, and data from a test endpoint cannot either.
* A missing API key never falls back to mock data. The application starts, reports
  "No data provider" and collects nothing.
* In mock mode the status payloads, the dashboard banner, the chart watermark and the
  stored rows all say so.

## 9. Failure handling

| Failure | Behaviour |
|---|---|
| Feed disconnects | reconnect with exponential back-off (1 s doubling to 60 s, jittered), resubscribe, record the outage, recover it from historical quotes |
| Feed silent while the market is open | watchdog closes the connection after 30 s and reconnects |
| API key rejected | state `auth_failed`, retry every 5 minutes, shown on the dashboard |
| Connection limit reached | wait 60 s instead of fighting for the connection |
| Process restart or crash | seed the sampler with the last stored quote, rebuild in-progress bars from the grid; when the first new quote arrives, record everything between the last second that holds a real update and that quote as one gap and backfill it (this covers the downtime, an outage that was still in progress when the process stopped, and the seconds it took to reconnect) |
| First connection slow on a first start | the seconds until the first quote count as an outage and are recovered the same way |
| Download job running when the process stops | marked `failed` ("interrupted by an application restart") at the next start; request it again |
| Database down | buffer in memory, retry, report; ingestion continues |
| Background task crash | logged, shown under "Recent problems", restarted after 2 s |
| Start-up failure (for example data-mode mismatch) | the API stays up in a degraded state and reports the reason |
| Shutdown (SIGTERM) | close the feed, flush buffers, exit |

Gap recovery replays the provider's historical quotes for the gap through the same
normaliser and sampler, with the source timestamp as the clock. Recovered rows are
tagged `backfill` and never overwrite a second that was captured live. Outcomes:
`filled`, `no_provider_data`, `unfillable`, `failed` (retried up to five times).

A gap is written to `data_gaps` as `open` the moment it is detected, before the wait
that precedes the backfill, so a restart cannot forget it: unresolved gaps are retried
by the scan that runs a few seconds after start-up and then every 15 minutes. Two kinds
exist: `feed_outage` (from the last quote before a disconnect to the first quote after
the reconnect) and `hole` (seconds absent from the grid, or left without a real update
around a restart). A gap found again with the same start and a later end is extended and
reopened. Recovery never starts before the last second of a gap is final and on disk, so
a second that live capture is still producing is not written twice.

A disconnect or a restart is a known interruption and is recovered however short it was,
as long as at least one whole second lies between the two seconds that were captured
live. `GAP_MIN_SECONDS` applies only to holes found by the periodic scan, where it keeps
one-second scheduling hiccups out of the record.

A quote is stored once. The second that was being captured when the process stopped has
its ticks on disk but never got its observation; it is rebuilt from the provider's
quotes, and the ticks recovered for it continue after the last one captured live.

## 10. Configuration

* `.env`: secrets and process settings (`fxlab/config.py` lists all of them).
* `config/instruments.yaml`: everything that differs between pairs.
* Nothing pair-specific is hardcoded; adding a pair is a YAML entry.

## 11. How later phases plug in

| Phase | Plugs into |
|---|---|
| 2 Indicators | consume `Bar` and `Observation` streams; one module per indicator; parameters in the database, editable from the dashboard |
| 3 Backtesting | an event loop that feeds stored observations and bars through the same strategy objects; Parquet export of the grid |
| 4 Live signals | subscribe to bar-close events from the aggregator; must call `evaluate_signal_gate` |
| 5 Strategy intelligence | regime classifier and walk-forward runner on top of 2 and 3 |
| 6 Paper trading | fills simulated against `quote_ticks` bid/ask |
| 7 More pairs | YAML entries, verified with `fxlab verify-provider` |
| 8 Hardening | authentication, scheduled backups, alerting |

## 12. Decisions worth knowing about

1. **Receipt-time sampling.** Live observations record what the system knew at each
   second. Backfilled observations record what the market did. The `source` column
   keeps the two apart.
2. **Edge seconds keep their live capture**, even if it is partial. The second of the
   last quote before a disconnect and the second of the first quote after it (or after
   a restart) are never rebuilt; only the seconds between them are. Live capture is
   first-hand, and the provider's history for the most recent seconds may still be
   incomplete when recovery runs.
3. **Bars are mid prices with bid and ask at the close.** Exact bid and ask paths are
   in `quote_ticks` for as long as ticks are retained.
4. **Duplicate detection is adjacent-only and needs sub-second timestamps.** Massive
   stamps historical quotes in whole seconds (seen on 15 November 2026). A wider window
   would delete real flip-flop quotes there, and dropping equal neighbours would make
   recovered seconds count fewer quotes than the provider's own bars do.
5. **The dashboard has no authentication yet** and is bound to localhost.
