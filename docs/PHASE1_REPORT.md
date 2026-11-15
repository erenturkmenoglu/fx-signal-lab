# Phase 1 report: foundation and data infrastructure

FX Signal Lab, 14 November 2026. Covers Phase 1 of the specification (section 17) in the
hand-over format of section 19.

## 1. Summary

Phase 1 is built and tested up to the provider's door. **It has not been run against
Massive.** The build environment had no API key and no network route to
`api.massive.com`, so the Massive integration is "implemented from the documentation and
tested against a local stand-in that speaks the documented protocol". It is not yet
"working" in the sense of specification section 19, and this report does not claim it is.

Three things have never been executed:

1. Anything against Massive's production service.
2. The Docker image and the Compose stack (no Docker daemon was available).
3. The CI workflow (the repository has not been pushed anywhere).

Everything else was executed, including the database layer on TimescaleDB 2.26.1, the
version of the pinned image.

Items 2 and 3 have changed since this report was written; see the addendum at the end.

One command turns the first item from "unverified" into a report:
`make verify` (it runs `fxlab verify-provider` with your key). It needs a Massive
**Currencies Starter** plan and an open market (Sunday 17:00 to Friday 17:00 New York).

## 2. What was verified, and how

| Area | How it was checked | Status |
|---|---|---|
| Quote validation, 1-second grid, bars, calendars, latency statistics, signal gate | 140 unit tests on pure logic that does not depend on any provider | Verified |
| Storage: schema, migration, repositories, batch writer, data-mode guard | integration tests on PostgreSQL 16 | Verified |
| TimescaleDB: hypertables, compression and retention policies, writes into compressed chunks | the whole suite plus two specific tests on TimescaleDB 2.26.1 built from source | Verified, though not with the Docker image itself |
| Massive WebSocket client: connect, authenticate, subscribe, parse, reconnect, watchdog, connection limit, rejected key | real sockets against the local stand-in | Stand-in only |
| Massive REST client: bars, quotes, last quote, reference tickers, market status, paging, throttling, retries | same | Stand-in only |
| Recovery after a feed outage, a restart, a crash, a slow start | same | Stand-in only |
| Historical bar download | same | Stand-in only |
| `verify-provider` | same | Stand-in only |
| API and live stream | integration tests over HTTP and WebSocket | Verified |
| Dashboard | type check, unit tests of the formatting code, production build; viewed with mock and stand-in data at desktop and phone widths | Verified by eye; no automated browser tests |
| Docker image and Compose stack | Compose file parsed with `docker compose config`; the container's layout was reproduced from the pinned lock file and run | Not executed |
| CI workflow | runs the commands that passed here | Not executed |
| Massive production service | nothing | Not executed |

The stand-in is `backend/tests/support/fake_massive.py`. It implements what Massive's
documentation describes and nothing more, so it can confirm that the code does what the
documentation says. It cannot confirm that the documentation matches the service.
[MASSIVE_API_NOTES.md](MASSIVE_API_NOTES.md) lists every fact used, its source, and the
eight points that only a live key can settle.

## 3. Implemented features

**Market data**

* Massive WebSocket adapter: greeting, authentication, subscription to quote channels
  (`C.EUR-USD`), bundled frames, keep-alive, reconnect with jittered exponential
  back-off (1 s to 60 s), a silence watchdog that is aware of market hours, slow retry
  after a rejected key, waiting instead of fighting when the connection limit is hit.
* Massive REST client: aggregate bars, historical quotes, last quote, reference tickers,
  market status; pagination; retries that honour `Retry-After`; an optional client-side
  rate limit for the free plan.
* A mock adapter for development. It is synthetic, labelled as such everywhere (banner,
  chart watermark, API payloads, every stored row) and confined to its own database.
* `fxlab verify-provider`: seven checks of what a key can actually do, including which
  of the configured pairs the subscription offers.
* Without a key the application collects nothing and says so. It never falls back to
  synthetic prices on its own.

**Data quality**

* Every quote is validated: unknown symbol, non-positive or non-finite price, impossible
  timestamp, timestamp in the future, crossed quote, spread above a per-pair limit,
  exact duplicate, out-of-order quote, unconfirmed price jump. Every outcome is counted.
* The 1-second grid: each second is `fresh` (it received a quote), `carried` (last quote
  still valid), `stale` (last quote older than the pair's threshold) or `missing` (no
  quote known). Seconds without a quote never get an invented price movement.
* Bars from 5 s to 1 day are built from that grid by one aggregator used for live data,
  warm starts and rebuilds. Each bar records how many of its seconds were valid, fresh
  and recovered, and whether it is complete. 4 h and 1 day bars are anchored to the
  17:00 New York rollover.
* A signal gate (`evaluate_signal_gate`) that later phases must obey: no signals without
  a provider, on an unsupported pair, when the market is closed, when the feed is down,
  on stale data, or when the clock is suspect.

**Storage**

* PostgreSQL with TimescaleDB: raw ticks, 1-second observations, bars, connection
  events, gaps, download jobs, per-minute ingest statistics. Versioned migration.
* Provenance on every row: live, backfilled, provider aggregate or mock; mid or bid.
* Batched writes once per second, retried, with a bounded buffer whose overflow is
  counted and shown.
* Compression and retention policies set from configuration at every start.
* A guard that refuses to mix mock, test-endpoint and production data in one database.

**Reliability**

* Outages are recorded from the last quote before the disconnect to the first quote
  after it, then rebuilt from the provider's historical quotes through the same
  validation and sampling code. Rebuilt seconds are tagged and never overwrite a second
  that was captured live.
* A gap is stored when it is detected, so a restart cannot forget it. After a restart or
  crash, recovery runs from the last second that holds a real update to the first new
  quote, which also covers an outage that was still in progress when the process stopped.
* A disconnect or restart is recovered however short it was. A quote is stored once,
  including in the second that was being captured when the process stopped.
* Supervised background tasks, a start-up that degrades instead of crash-looping (the
  API stays up and reports the reason), graceful shutdown on SIGTERM.

**Monitoring**

* Feed latency (p50, p95, p99) over a rolling five minutes, data age, throughput,
  processing time per quote. The latency figures say when they cannot be trusted:
  whole-second provider timestamps (then they are upper bounds) or a local clock that
  runs behind (then they are meaningless). With no quotes there is no figure, and a
  value below one millisecond is shown as "<1 ms", never as zero.
* Connection log, validation counters, storage state, recent problems.

**API and dashboard**

* REST: health, status, instruments, timeframes, observations, bars, ingest statistics,
  events, gaps (list, scan, fill), history jobs. WebSocket: live stream.
* Dashboard: market monitor, dealing-style quote, chart (1 s line or 5 s to 1 day
  candles, own data or provider history), the last 120 seconds one cell per second,
  system health, connection log, data gaps, historical download.

**Configuration and operations**

* `config/instruments.yaml` holds everything that differs between pairs. EUR/USD is
  active; seven more pairs are defined and inactive. Adding a pair is a YAML edit.
* Secrets only from the environment; the API key is redacted from logs.
* Docker Compose deployment, Makefile, CI workflow, runbook.

## 4. Files created

Everything is new; no existing file was changed. About 15,000 lines.

| Path | Contents | Lines |
|---|---|---|
| `backend/fxlab/domain/` | records, instrument registry, calendars, timeframes | 576 |
| `backend/fxlab/marketdata/` | adapter interfaces, back-off, Massive WebSocket and REST clients, parser, symbol mapping, mock adapter | 1,163 |
| `backend/fxlab/pipeline/` | validation, 1-second sampler, bar aggregator, latency statistics, signal gate, live hub, ingestion service | 1,691 |
| `backend/fxlab/storage/` | schema, repository, batch writer, engine and policies, data-mode guard | 1,123 |
| `backend/fxlab/services/` | gap recovery, history download, provider verification | 860 |
| `backend/fxlab/api/`, `cli.py`, `config.py`, `logging.py`, `clock.py`, `timeutil.py` | FastAPI application, command line, settings, logging | 1,005 |
| `backend/migrations/` | Alembic environment and the initial migration | 324 |
| `backend/tests/` | 8 unit and 6 integration test modules, the fake Massive server | 4,203 |
| `backend/pyproject.toml`, `requirements.lock`, `alembic.ini` | packaging, pinned runtime dependencies | 146 |
| `frontend/src/` | dashboard: nine components and shared interface parts, live-data hook, API client, formatting, styles | 2,321 |
| `frontend/` (other) | `package.json`, lock file, Vite and TypeScript configuration, `index.html` | not counted |
| `config/instruments.yaml` | instrument registry | 117 |
| `Dockerfile`, `docker-compose.yml`, `.env.example`, `Makefile`, `.github/workflows/ci.yml`, `scripts/db-init/`, ignore files | deployment and automation | 280 |
| `README.md`, `docs/` | README, architecture, Massive API notes, runbook, roadmap, risks, this report | 1,225 |

## 5. Tests performed

### Automated

| Suite | Result |
|---|---|
| Backend on TimescaleDB 2.26.1 | 192 passed |
| Backend on plain PostgreSQL 16 | 190 passed, 2 skipped (TimescaleDB only) |
| Backend without a database | 159 passed, 33 skipped |
| Lint, format check, strict typing (ruff, mypy) | clean, 43 source files |
| Dashboard: type check, unit tests, production build | clean, 10 tests |

192 backend tests: 140 unit, 52 integration. Statement coverage 94%. The suite was
repeated to look for timing-dependent failures; the last three full runs on TimescaleDB
after the final change passed. The archive was also unpacked into a clean directory,
installed from scratch and tested there.

### Data quality tests

The specification asks for these by name. They assert, among other things:

* each validation rule accepts and rejects exactly what it should, with per-pair limits
  (a 10-pip USD/JPY quote passes where a 20-pip one is rejected);
* a price jump is rejected until three consecutive quotes confirm the new level;
* the grid has one row per second with the right status, never reopens a closed second,
  and assigns a quote to the second in which it was received;
* a second without quotes carries the last quote and turns stale at the threshold,
  with no change in price;
* bars equal the aggregation of their seconds, ignore stale and missing seconds, record
  completeness, and align to the New York rollover across both daylight-saving changes;
* the market calendar opens and closes at the right UTC instants in summer and winter;
* rebuilt seconds reproduce the high, low and last quote of the quotes the provider
  published in them, and never overwrite live capture;
* a quote is stored once: live seconds get no recovered ticks on top;
* mock data cannot enter a database that holds market data;
* an unsupported pair stays empty; a closed market produces no filler rows and no gaps.

### End-to-end runs

The application process itself, with real sockets and a real database:

1. **Mock mode.** The stack served the built dashboard, which was viewed at desktop and
   phone widths. SIGTERM stopped it cleanly with every buffer written.
2. **Outages and a restart, on TimescaleDB, against the stand-in.** The stand-in cut the
   connection for 8 seconds every 22 seconds. The application was stopped in the middle
   of the second outage and restarted 11 seconds later. Result:
   * the header said "Test endpoint" throughout, never "Live";
   * 4 gaps recorded and 4 recovered: three feed outages, and one gap of 15 seconds
     for the restart (the interrupted outage, the downtime and the reconnect);
   * the grid held 100 rows for 100 seconds, 63 captured live and 37 recovered, none
     stale or missing;
   * 19 of the 20 five-second bars were complete; the other is the bar in which the
     application was first started, of which it saw three seconds;
   * 387 live ticks, each with its receipt time; 235 recovered ticks, none with an
     invented receipt time; no quote stored twice;
   * hypertables, compression and retention policies in place, no policy errors;
   * no error-level log line; the API key appeared nowhere in the logs;
   * processing took 71 microseconds per quote at the median, 178 at the 99th percentile.
3. **A crash.** The process was killed with `kill -9` while streaming and restarted 8
   seconds later. One gap of 11 seconds was recorded and recovered. The grid held 50
   rows for 50 seconds with none stale or missing, and no quote was stored twice.
4. **No key.** "No data provider", nothing collected, no gaps recorded, and a notice
   that the market is closed with the next opening time.
5. **Container layout.** The pinned lock file was installed into a clean environment
   and the application was started from the paths and with the command the image uses.

### Defects that testing found

Worth listing, because each was invisible until the code met the real component:

* TimescaleDB installed but not loaded (`shared_preload_libraries`): `CREATE EXTENSION`
  fails with a fatal error that closes the connection, which no savepoint catches. The
  migration now checks first and falls back to ordinary tables with a warning.
* A changed `COMPRESS_AFTER_DAYS` was silently ignored after the first start. Policies
  are now re-created at every start.
* An outage still in progress when the process stopped was never recovered, and the
  seconds between a restart and the first new quote stayed marked stale. One gap now
  runs from the last real update to the first new quote.
* A detected outage that was waiting for its backfill was forgotten by a restart.
* Downtime that grew across two restarts without new data was treated as already known.
* Recovery could start before the last second of a gap was on disk and then store that
  second's quotes twice. It now waits until the gap's seconds are final.
* The second that was being captured when the process stopped had its ticks on disk but
  no observation, so recovery stored its quotes a second time. Recovered ticks now
  continue after the last one captured live.
* Reconnects shorter than five seconds were not recovered at all. Known interruptions
  are now recovered at any length.
* A measured latency below one millisecond was displayed as "0 ms". It now reads
  "<1 ms" with a note that this is the floor of the measurement.
* The fixed-date tests would have begun to fail on TimescaleDB in December, when the
  retention job starts dropping their rows. Those tests now run without policies; the
  policies have their own tests.

### Not tested

* Massive's production service, in any respect.
* The Docker image build and the Compose stack.
* The CI workflow.
* Behaviour over days: memory growth, disk growth, compression ratios, the policy jobs'
  own schedule. The longest continuous run was a few minutes.
* Load. Processing costs about 80 microseconds per quote here, which leaves a wide
  margin for one pair; it has not been measured at many pairs.
* The dashboard in browsers other than headless Chromium.

## 6. Acceptance criteria (specification section 18)

| # | Criterion | Status |
|---|---|---|
| 1 | The user can configure a valid Massive API key | Implemented (`MASSIVE_API_KEY`). Accepted and rejected keys were tested against the stand-in. **Not tested with a real key.** |
| 2 | The application connects to the actual EUR/USD feed | **Not verified.** Implemented from the documentation; needs a key. |
| 3 | Live price updates are received and persisted | Verified against the stand-in and with mock data. **Not with the real feed.** |
| 4 | Accurate 1-second observations without fabricated updates | Verified. This logic is independent of the provider. |
| 5 | Data age and connection health are visible | Verified (dashboard and `/api/status`). |
| 6 | Historical data can be retrieved and inspected | Verified against the stand-in. **Not with the real API.** |
| 7 | RSI, Bollinger Bands, EMA, MACD, ATR, ADX calculate correctly | Phase 2. |
| 8 | At least three strategies can be backtested | Phase 3. |
| 9 | Backtests include realistic costs | Phase 3. Cost placeholders exist per pair. |
| 10 | Live signals use the same logic as backtests | Phase 4. |
| 11 | Every signal is recorded with its supporting data | Phase 4. |
| 12 | Recovery from temporary API disconnections | Verified against the stand-in: disconnects, silence, restarts, crashes, a slow start. **Not against the real service.** |
| 13 | The dashboard works without hardcoded prices or fake signals | Verified. Every number comes from the API; mock data is labelled; without a key it shows nothing. Phase 1 produces no signals. |
| 14 | Automated tests pass | Yes, here. CI has not run yet. |
| 15 | A new pair can be added through configuration | Verified end to end: one changed line of YAML activated USD/JPY in a test. Which pairs your subscription offers is unknown until `verify-provider` runs. |

## 7. Known limitations

* No indicators, strategies, backtests, signals or paper trading yet. Phases 2 to 6.
* One provider. The adapter interface is in place; no second adapter exists.
* Validation thresholds are provisional. They were chosen before any real data was seen
  and are deliberately loose.
* Edge seconds keep their live capture even when it is partial: the second of the last
  quote before a disconnect, and the second of the first quote after it or after a
  restart. Their high and low can be understated. Only the seconds between are rebuilt.
* On a first start, the second in which the application starts is recorded as `missing`
  if the first quote arrives in the following second.
* Provider bars are bid-based and UTC-aligned; the system's own bars are mid-based and
  aligned to the New York rollover. They are stored separately and never mixed, but they
  are not interchangeable for backtests.
* The dashboard has no login. It listens on `127.0.0.1` only.
* No holiday calendar: on 25 December and 1 January the grid shows stale seconds.
* One process on one host. If the host is down, nothing is collected; the downtime is
  recovered afterwards, up to 72 hours automatically.
* Alerts exist only on the dashboard. Nothing notifies you when the feed is down.
* Raw ticks are kept for 30 days by default; observations and bars forever.

## 8. Remaining technical risks

The full register with mitigations is in [RISKS.md](RISKS.md). The ones that matter now:

1. **Massive behaves differently from its documentation** (message shapes, status texts,
   timestamp resolution, plan limits). Likely in details, unlikely in substance. Parsing
   is lenient and every unexpected message is counted and shown.
2. **First `docker compose up` fails on a detail.** The image has never been built.
3. **Thresholds reject real quotes** or let bad ones pass until they are calibrated.
4. **Whole-second provider timestamps** would make latency an upper bound only and
   shift some recovered seconds by one. The first live run will show which it is.
5. **Unattended operation is untested beyond minutes.**

## 9. Running the application

```bash
cp .env.example .env          # set POSTGRES_PASSWORD and MASSIVE_API_KEY
docker compose up -d --build
make verify                   # probes Massive with your key and prints a report
```

Dashboard: <http://127.0.0.1:8000>. API documentation: <http://127.0.0.1:8000/docs>.

Without a key the application starts and shows "No data provider". To look at the
dashboard with synthetic data, set `DATA_PROVIDER=mock` and `FXLAB_DB=fxlab_mock` in
`.env`.

The first-run checklist and what to do when something is wrong are in
[RUNBOOK.md](RUNBOOK.md). Development setup is in the [README](../README.md).

## 10. Recommended next step

Close Phase 1 before starting Phase 2:

1. Get a Currencies Starter key, start the stack, run `make verify` while the market is
   open, and send back the report. Whatever differs from the documentation gets fixed
   first.
2. Let it collect for a few days. Then calibrate the EUR/USD thresholds from the reject
   counters and look at the real latency and timestamp resolution.
3. Push the repository so CI runs once.

Then Phase 2, the indicator engine: RSI, Bollinger Bands, EMA, MACD, ATR and ADX as pure
functions with a streaming form, tested against independently computed values, drawn on
the existing chart. It needs a few days of collected data or downloaded provider bars.

## 11. Open questions

1. Do you have, or will you take, Massive Currencies Starter? Live quotes and gap
   recovery need it.
2. Where will this run? It should be always on, with a synchronised clock.
3. Do you need the dashboard from another machine or a phone? Then authentication moves
   forward from Phase 8.
4. How long should raw ticks be kept? Thirty days is the default.
5. Which spread, commission and slippage should backtests assume for your execution?
6. Will anything below one minute be traded? If not, the 5 s to 30 s bars can go.
7. USD/TRY, USD/ZAR, USD/PLN: availability and quote quality on Massive are unknown
   until `verify-provider` runs.

## 12. Addendum, 15 November 2026

What changed after this report was written:

* **CI has run and passes.** On GitHub's runners: the backend suite against the
  `timescale/timescaledb:2.26.1-pg16` image, the dashboard checks, and a new job that
  builds the Docker image and runs the Compose stack twice. Without an API key the
  stack reports "not configured", TimescaleDB is active with no policy errors, and
  nothing is collected. On the simulator, quotes flow into the separate mock database
  and the market-data database stays empty; a restart logs no error.
* **So items 2 and 3 of section 1 are no longer "never executed"**, and risk 2 of
  section 8 is reduced to "not yet run on the target server".
* **Item 1 is unchanged.** Nothing has run against Massive. A Currencies Starter key
  now exists, but the build environment has no network route to Massive, so the key
  can only be used on the server.
* **Decisions:** trading below one minute is intended, so all sub-minute bars stay;
  the dashboard is wanted on a phone, which [DEPLOY.md](DEPLOY.md) covers with a
  private network instead of a public address.
* **New:** `scripts/server-setup.sh` (first-time setup of a server) and
  [DEPLOY.md](DEPLOY.md).
* **Server size measured.** Memory, CPU and disk of the build and of the running stack
  were measured (figures in [DEPLOY.md](DEPLOY.md), "Server size"). A server with 2 GB
  of memory is enough for Phases 1 and 2; the setup script adds a swap file below 4 GB,
  and CI now runs the stack inside the memory limits of such a server.
* **Defect found by that measurement, fixed.** The Compose file mounted the folder
  `scripts/db-init/` over the database image's own first-start folder and so hid the
  image's two scripts. `TIMESCALEDB_TELEMETRY=off` had no effect (the level stayed
  `basic`), and the database ran on PostgreSQL's defaults instead of tuning itself to
  the machine: 128 MB of cache and 8 worker processes for 16 TimescaleDB background
  workers. Storage and policies worked regardless, which is why no test noticed. The
  file is now mounted on its own, and CI asserts the telemetry level and the tuned
  settings. No database volume existed outside CI, so nothing has to be migrated.
