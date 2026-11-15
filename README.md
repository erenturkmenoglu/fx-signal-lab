# FX Signal Lab

A self-hosted FX research and decision-support platform: it collects live FX quotes,
keeps an honest 1-second record of what the market did, and (in later phases) computes
indicators, backtests strategies and raises signals for a discretionary trader.

**It is not a trading bot.** It places no orders and has no broker connection. The human
makes every trading decision.

**Status: Phase 1 of 8 (data infrastructure).** Built, tested, and passing CI including
a run of the Docker stack. Not yet run against Massive itself. Read
[docs/PHASE1_REPORT.md](docs/PHASE1_REPORT.md) first: it says exactly what was built,
what was tested, and what still has to be verified with a real API key.

## What Phase 1 does

* Connects to Massive (formerly Polygon.io) over WebSocket, authenticates, subscribes
  to EUR/USD quotes, reconnects with back-off and recovers what it missed.
* Validates every quote (bad prices, crossed and absurdly wide quotes, duplicates,
  out-of-order quotes, unconfirmed price jumps) and counts every outcome.
* Maintains a 1-second observation grid in which each second is `fresh`, `carried`,
  `stale` or `missing`. It never invents a market update.
* Builds 5s to 1d bars from that grid, with completeness recorded per bar.
* Stores ticks, observations, bars, connection events and gaps in PostgreSQL with
  TimescaleDB.
* Detects downtime and feed outages and rebuilds the missing seconds from the provider's
  historical quotes, tagged as backfilled.
* Downloads historical provider bars on request.
* Measures feed latency (p50, p95, p99), data age and throughput, and says when the
  figures cannot be trusted.
* Serves a dashboard: market monitor, dealing-style quote, chart, the seconds tape,
  system health, connection log, gaps, history.

## Quick start

Requirements: Docker with Compose. A Massive **Currencies Starter** plan (or higher) for
live data; see [docs/MASSIVE_API_NOTES.md](docs/MASSIVE_API_NOTES.md).

```bash
cp .env.example .env          # set POSTGRES_PASSWORD and MASSIVE_API_KEY
docker compose up -d --build
```

Open <http://127.0.0.1:8000>. API documentation is at <http://127.0.0.1:8000/docs>.

On a fresh server, `./scripts/server-setup.sh` does the same and installs Docker first;
[docs/DEPLOY.md](docs/DEPLOY.md) walks through the server and phone access.

Then verify what your key can do. The probe needs the account's single WebSocket
connection, so the app is stopped for a moment:

```bash
make verify
# or: docker compose stop app
#     docker compose run --rm app python -m fxlab.cli verify-provider
#     docker compose start app
```

The FX market is closed from Friday 17:00 to Sunday 17:00 New York time. During the
closure the WebSocket check reports "no data", which is expected.

### Without a subscription

Without an API key the application starts, shows "No data provider" and collects
nothing. It never falls back to invented prices on its own.

For development you can ask for synthetic data explicitly:

```bash
# in .env
DATA_PROVIDER=mock
FXLAB_DB=fxlab_mock
```

Mock mode is labelled everywhere (banner, chart watermark, API payloads, stored rows)
and uses its own database. The application refuses to mix mock and market data.

## The dashboard

| Panel | Shows |
|---|---|
| Market monitor | every active pair: bid, ask, spread, change on the day, quote age, state of the last second, quote rate, whether signals would be allowed |
| Quote | the selected pair in dealing-screen form (big figure, pips, fractional pip) |
| Chart | 1s line (gaps where data was stale or missing) or 5s to 1d candles; faded candles have seconds missing; "Provider history" shows downloaded bid-based bars |
| Last 120 seconds | one cell per second: fresh (height = quotes that second), carried, stale, missing, or recovered after an outage |
| System health | feed state, latency, throughput, validation counters, database and write buffer, gaps |
| Connection log | every connect, disconnect, reconnect and server notice |
| Data gaps | detected gaps and whether they were recovered |
| Historical data | download provider bars for a range and view them on the chart |

All times are UTC.

## Configuration

* `.env`: secrets and process settings. `.env.example` documents the common ones;
  `backend/fxlab/config.py` lists all of them.
* `config/instruments.yaml`: everything that differs between currency pairs (pip size,
  precision, session, stale threshold, spread and jump limits, cost assumptions).

### Adding a currency pair

1. Add or edit the entry in `config/instruments.yaml` and set `active: true`.
2. `docker compose restart app`.

At start-up the pair is checked against Massive's reference API. A pair your
subscription does not offer is marked unsupported and stays empty.

## Commands

Inside the container (`docker compose exec app python -m fxlab.cli ...`) or in a
development environment (`fxlab ...`):

| Command | Purpose |
|---|---|
| `serve` | run the API and the ingestion service (one process) |
| `migrate` | apply database migrations (also run automatically at container start) |
| `verify-provider` | probe the Massive API and print a capability report |
| `instruments` | show the instrument registry |
| `history --symbol EURUSD --timeframe 1m --start 2026-11-01 --end 2026-11-08` | download provider bars |
| `gaps list`, `gaps scan`, `gaps fill --id N` | inspect and repair data gaps |
| `reconcile --minutes 10` | compare live ticks with the provider's quote history (read-only) |

`make help` lists shortcuts for the usual Docker operations, including `make backup`.

## Development

```bash
# database for tests (the application's tables in it are dropped and recreated)
docker run -d --name fxlab-test-db -p 5432:5432 \
  -e POSTGRES_USER=fxlab -e POSTGRES_PASSWORD=fxlab -e POSTGRES_DB=fxlab_test \
  timescale/timescaledb:2.26.1-pg16

# backend
cd backend
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
export TEST_DATABASE_URL=postgresql+asyncpg://fxlab:fxlab@127.0.0.1:5432/fxlab_test
python -m pytest              # without TEST_DATABASE_URL the database tests are skipped
ruff check fxlab tests && ruff format --check fxlab tests migrations && mypy

# dashboard
cd ../frontend
npm ci
npm run dev                   # http://localhost:5173, proxies /api and /ws to :8000
npm run typecheck && npm test && npm run build
```

Run the backend locally against the mock provider:

```bash
cd backend
DATA_PROVIDER=mock DATABASE_URL=postgresql+asyncpg://fxlab:fxlab@127.0.0.1:5432/fxlab_mock \
  INSTRUMENTS_FILE=../config/instruments.yaml LOG_FORMAT=console \
  sh -c "python -m fxlab.cli migrate && python -m fxlab.cli serve"
```

To exercise the real Massive code path without a subscription there is a local stand-in
that implements the documented protocol (`python -m tests.support.fake_massive`); point
`MASSIVE_WS_URL` and `MASSIVE_REST_URL` at it. The dashboard then says "Test endpoint",
never "Live".

## Layout

```
backend/fxlab/domain        records, instrument registry, calendars, timeframes
backend/fxlab/marketdata    adapter interfaces, Massive WebSocket + REST, mock adapter
backend/fxlab/pipeline      normaliser, 1-second sampler, bars, latency, ingestion service
backend/fxlab/storage       schema, repository, batch writer, data-mode guard
backend/fxlab/services      gap recovery, history download, provider verification
backend/fxlab/api           FastAPI app (REST, live stream, dashboard files)
backend/migrations          Alembic migrations
backend/tests               unit and integration tests, fake Massive server
frontend/                   React + TypeScript dashboard
config/instruments.yaml     instrument registry
docs/                       architecture, API notes, deployment, roadmap, risks, runbook, phase report
```

## Documentation

* [PHASE1_REPORT.md](docs/PHASE1_REPORT.md): what was delivered and what is unverified
* [ARCHITECTURE.md](docs/ARCHITECTURE.md): design, conventions, data model
* [MASSIVE_API_NOTES.md](docs/MASSIVE_API_NOTES.md): verified API facts with sources
* [DEPLOY.md](docs/DEPLOY.md): an always-on server and the dashboard on your phone
* [RUNBOOK.md](docs/RUNBOOK.md): operating and recovering the system
* [RISKS.md](docs/RISKS.md): technical risks and open questions
* [ROADMAP.md](docs/ROADMAP.md): Phases 2 to 8

## Third-party notices

The chart uses [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts)
(Apache 2.0); its attribution logo is shown on the chart as the licence requires. Fonts:
IBM Plex Sans Condensed and IBM Plex Mono (SIL Open Font License), bundled with the
dashboard.
