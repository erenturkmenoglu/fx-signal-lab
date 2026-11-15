# Runbook

How to operate FX Signal Lab and what to do when something is wrong. Commands assume
the Docker Compose deployment and are run from the repository root.

The system is built to run unattended, but not forever without attention. A weekly
look at the System health panel and the Data gaps table is enough in normal operation.

## Daily commands

```bash
docker compose ps                 # state and health of both containers
docker compose logs -f app        # structured (JSON) application log
docker compose restart app        # restart the application (the downtime is recorded and recovered)
docker compose down               # stop everything; data is kept in the pgdata volume
docker compose up -d --build      # start, rebuilding after a code or dependency change
make backup                       # dump the database to backups/
make reconcile                    # compare live ticks with Massive's quote history (read-only)
```

Health endpoints: `GET /api/health/live` (process is up), `GET /api/health/ready`
(started and the database accepted the last write), `GET /api/status` (everything the
dashboard shows).

## First run checklist

1. `.env` has `POSTGRES_PASSWORD` and `MASSIVE_API_KEY`.
2. `docker compose up -d --build`, then `docker compose ps` shows both services healthy.
3. `make verify` and read the report. Expected on a Starter plan during market hours:
   every check `OK`. On a weekend the WebSocket check says `NODATA`.
4. Open the dashboard. The header says "Live market data". System health shows
   "Streaming" and latency figures, and shows neither a "Plain PostgreSQL" warning nor
   storage policy problems.
5. After a few minutes, check the reject counters under System health. If many quotes
   are rejected as `wide spread` or `outlier`, the thresholds in
   `config/instruments.yaml` are too tight for the real feed; adjust and restart.
6. Make sure the host clock is synchronised (NTP). Latency and data-age figures depend
   on it.
7. After about fifteen minutes of live data, `make reconcile`. It compares the last ten
   settled minutes of live ticks with `/v3/quotes`, second by second, and prints what is
   equal and what differs. Outage recovery rests on the two being the same quotes. It
   only reads, so unlike `make verify` it does not interrupt the feed. A longer window:
   `make reconcile ARGS="--minutes 60"`.

## What the dashboard is telling you

| You see | Meaning | Action |
|---|---|---|
| Header "No data provider" | no API key configured | set `MASSIVE_API_KEY`, restart |
| Header "Mock data" + striped banner | synthetic prices | switch `DATA_PROVIDER` and `FXLAB_DB` back for real data |
| Header "Test endpoint" | the feed URL is not Massive's production host | check `MASSIVE_WS_URL` / `MASSIVE_REST_URL` |
| Feed "API key rejected" | authentication failed; retried every 5 minutes | fix the key, restart |
| Feed "Disconnected, waiting to reconnect" | connection lost; back-off in progress | none if it recovers; see below if it does not |
| Last feed error "connection limit" | another client holds the account's single connection | stop the other client (a second instance, a `verify-provider` run, a notebook) |
| Last second `STALE` during market hours | no quote for longer than the pair's stale threshold | check the connection log; for illiquid pairs raise `stale_after_ms` |
| Latency note about 1 s resolution | the provider stamps whole seconds; figures are upper bounds | none |
| Latency note about negative latency | the local clock is behind the provider's | fix NTP |
| Database "Writes failing" | the database is unreachable; rows are buffered in memory | see "Database down" |
| "rows dropped" | the write buffer overflowed during a long database outage | fix the database, then `gaps scan` |
| "Plain PostgreSQL" | TimescaleDB is not installed or not loaded in this database | use the `timescale/timescaledb` image; see "TimescaleDB policies" |
| "Storage policies: Not managed by the application" | `STORAGE_POLICIES_ENABLED=false` | none, if intended |
| "Storage policies: N problems" | a compression or retention statement failed | read the message; see "TimescaleDB policies" |
| "The application could not start" | start-up failed; the message says why | most often the data-mode guard, see below |
| Download "failed: interrupted by an application restart" | the application stopped while the download was running | request the download again; rows already written are kept |

## Incidents

### The feed does not reconnect

1. `docker compose logs --tail 100 app | grep feed_event` shows each attempt and its error.
2. Check Massive's status page and your network (DNS, firewall, proxy).
3. `make verify` isolates the problem: REST works but WebSocket does not, or neither.
4. Nothing is lost permanently: when the feed is back, the outage is recorded and
   rebuilt from historical quotes (up to `GAP_BACKFILL_MAX_HOURS`, default 72).

### Frequent reconnects

* "watchdog" in the log: the feed was silent for 30 s while the market was open. If you
  subscribe only to illiquid pairs, raise `WS_SILENCE_TIMEOUT_S`.
* Server notice `slow_consumer`: the process is not reading fast enough. Check host CPU
  and whether the database is slow (`last flush took ...` on the health panel).

### Gaps that are not recovered

`docker compose exec app python -m fxlab.cli gaps list` shows each gap with the reason.

| Outcome | Why | What to do |
|---|---|---|
| `Waiting` | recorded; recovery starts about a minute after the feed is back (also after a restart) | nothing |
| `Recovered` | rebuilt from historical quotes | nothing |
| `No quotes existed` | the provider has no quotes in that window either (holiday, illiquid pair) | nothing |
| `Retrying` | a transient error; retried up to 5 times by the periodic scan | wait, or press Recover |
| `Not recoverable`: no REST access | no API key, or mock mode | configure the key |
| `Not recoverable`: plan without historical quotes | `/v3/quotes` needs Currencies Starter | upgrade, or accept the gap |
| `Not recoverable`: longer than the limit | downtime above `GAP_BACKFILL_MAX_HOURS` | `fxlab gaps fill --id N` or the Recover button |

Manual scan for holes: `fxlab gaps scan` or the button on the dashboard.

### Database down

The application keeps collecting into memory (up to `WRITER_MAX_BUFFER_ROWS`, default
500 000 rows) and retries every second. When the database returns, the backlog is
written. If the buffer overflowed, ticks are dropped first, then observations; the
resulting holes are found by the next gap scan and backfilled.

```bash
docker compose ps db
docker compose logs --tail 100 db
docker compose restart db
df -h                               # a full disk is the usual cause
```

### Disk filling up

* `RETENTION_TICKS_DAYS` controls how long raw ticks are kept (default 30).
  `PERSIST_RAW_TICKS=false` stops storing them; the 1-second grid and bars are unaffected.
* Compression applies after `COMPRESS_AFTER_DAYS` (default 7).
* Size per table: `docker compose exec db psql -U fxlab fxlab -c "SELECT hypertable_name, pg_size_pretty(hypertable_size(format('%I', hypertable_name)::regclass)) FROM timescaledb_information.hypertables;"`
* What to expect per pair and day is in [DEPLOY.md](DEPLOY.md), "Server size".

### Memory running short

The database sizes itself once, when its volume is first created: a quarter of the
server's memory for its cache (`SHOW shared_buffers;`), which it fills over the first
days and then keeps. The application stays below 100 MB. Measured figures are in
[DEPLOY.md](DEPLOY.md), "Server size".

* `free -m`: the "available" column is what counts. Swap in use during normal operation
  means the server is too small.
* `docker stats --no-stream`: memory per container.
* A larger server: Lightsail cannot resize in place. Take a snapshot, create a larger
  instance from it, check it, then stop the old one. The database keeps the settings of
  the old size; `timescaledb-tune` inside the `db` container re-computes them (not tried
  here).

### "The application could not start: DataModeMismatchError"

The database was initialised for a different data mode (for example it holds market
data and the application was started with `DATA_PROVIDER=mock`). This guard protects
your data. Point the application at the right database (`FXLAB_DB=fxlab` for Massive,
`fxlab_mock` for mock) and restart. Only if the data is disposable, drop the database.

### TimescaleDB policies

At every start the application sets compression (ticks and 1-second observations after
`COMPRESS_AFTER_DAYS`, bars after 30 days at the earliest) and retention
(`RETENTION_TICKS_DAYS`, `RETENTION_OBS_DAYS`; 0 keeps everything). Policies are removed
and re-created each time, so a changed value takes effect at the next restart. Chunks
that are already compressed stay compressed; gap recovery and repeated history downloads
can still write into them.

This was tested against TimescaleDB 2.26.1, the version of the pinned image. It uses the
classic compression API, which is deprecated in favour of the columnstore API but still
supported there. If a future image removes it, the health panel shows the failing
statement and the system keeps running uncompressed. The statements are in
`backend/fxlab/storage/db.py` (`apply_storage_policies`).

* "Plain PostgreSQL" on the health panel although TimescaleDB is installed: the library
  is not loaded. `SHOW shared_preload_libraries;` must list `timescaledb` (the
  `timescale/timescaledb` image does this). The migration then creates ordinary tables
  and says so in its output; converting them later is a manual step.
* To manage policies yourself, set `STORAGE_POLICIES_ENABLED=false`. The application
  then leaves them alone and says so on the health panel.

### Clock skew

Symptoms: negative latency, "CLOCK SKEW" as the reason signals are suppressed.
Fix the host's time synchronisation (`timedatectl status`, chrony or systemd-timesyncd).
Containers use the host clock.

## Backup and restore

```bash
make backup                                    # backups/fxlab-<UTC timestamp>.dump

# restore into a fresh stack
docker compose down
docker volume rm fx-signal-lab_pgdata          # deletes the current database
docker compose up -d --wait db                 # an empty database; the application stays stopped
make restore FILE=backups/<file>.dump
docker compose up -d
```

`pg_dump` prints warnings about circular foreign-key constraints on `hypertable`,
`chunk` and `continuous_agg`. They concern TimescaleDB's own catalogue and are expected.

`make restore` refuses a database that already has tables and a running application,
and puts TimescaleDB into its restore mode around `pg_restore`. A plain `pg_restore`
ends with errors and without the foreign keys of the three hypertables. Checked on
TimescaleDB 2.26.1 with compressed and uncompressed chunks: the restored database has
the same rows, constraints, indexes, compression settings and policies as the source,
and the application runs on it. CI repeats the round trip on every push.

Scheduled backups are not automated yet (Phase 8). Until then, a host cron entry such as
`0 3 * * * cd /path/to/fx-signal-lab && make backup` is enough; copy the dumps off the
machine.

## Upgrading

```bash
git pull
docker compose up -d --build        # migrations run automatically before the server starts
docker compose logs --tail 50 app
```

Take a backup first. Database migrations are versioned (Alembic) and forward-only in
normal operation.

## Remote access and the login

The application is published on `127.0.0.1` only. How to reach it from a phone or any
browser is in [DEPLOY.md](DEPLOY.md), step 5.

* `make password` sets or changes the dashboard password (asked twice, hidden). Only a
  hash goes into `.env` (`DASHBOARD_PASSWORD_HASH`); the application restarts and every
  browser is signed out. `make password ARGS=--remove` turns the login off.
* With a password set, everything under `/api` except `/api/health/live`,
  `/api/health/ready` and `/api/auth/*` answers 401 without a session, the live stream
  refuses the connection, and `/docs` is off. `fxlab gaps scan` and `fill` keep working:
  they run next to the application and use the same secret.
* Forgotten password: run `make password` again. There is nothing to recover.
* "Too many failed attempts": five wrong passwords within a minute lock the login for
  everybody until the oldest is a minute old. If it keeps happening, somebody is
  guessing; the lock is what stops them.
* A broken `DASHBOARD_PASSWORD_HASH` (edited by hand) does not turn the login off: nobody
  can log in, and `/api/health/ready` names the problem. Run `make password`.
* The session cookie is sent over HTTPS only. For a plain `http://` tunnel to a browser
  other than Chrome or Firefox on `localhost`, set `SESSION_COOKIE_SECURE=false`; never
  on a public address.
* Never publish port 8000 on a public interface without a password.

## Changing behaviour safely

* Thresholds (stale time, spread and jump limits): `config/instruments.yaml`, then
  `docker compose restart app`.
* Never run two application containers against the same Massive account: the second
  connection is refused and the two would compete.
* System maintenance (this runbook) and trading logic are separate on purpose. Nothing
  here changes a strategy; later phases keep it that way.
