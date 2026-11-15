# Technical risks and open questions

Ordered by how much they matter for the next step.

## Risks

| # | Risk | Impact | Mitigation in place | Next step |
|---|---|---|---|---|
| R1 | The Massive integration has never run against the production service. The build environment has no network route to it, so the key that exists since 15 November can only be used on the server. | Message shapes, status texts, timestamp resolution or plan behaviour could differ from the documentation. | Built only from documented endpoints; lenient parsing; every unexpected status and reject is counted and shown; local protocol-level tests. | Run `make verify` with a real key; fix what it reports. |
| R2 | The Docker image and the Compose stack have run in CI only, not yet on the server that will host them. | A difference between the CI runner and the server (memory, disk, clock) could still surface at the first start. | CI builds the image and runs the stack on every push, once without a key and once on the simulator, with the pinned TimescaleDB image. | `./scripts/server-setup.sh` on the server. |
| R3 | Resolved on 15 November 2026: CI ran and passes (backend suite on the TimescaleDB image, dashboard, image build, stack smoke test). | | | |
| R4 | Validation thresholds are provisional (chosen before seeing real data). | Too tight: real quotes rejected. Too loose: bad quotes pass. | Per-pair configuration; reject counters by reason on the dashboard and per minute in `ingest_stats_1m`. | Calibrate after a few days of EUR/USD data. |
| R5 | Classic TimescaleDB compression API is deprecated since 2.18 (it works in the pinned 2.26.1, tested). | A future image could remove it. | Image tag is pinned; failures are visible, not fatal. | Move to the columnstore API in Phase 8. |
| R6 | The dashboard and API have no authentication. | Anyone who can reach port 8000 can read data and trigger downloads. | Published on 127.0.0.1 only; documented. | Authentication in Phase 8, or a reverse proxy now if remote access is needed. |
| R7 | Provider bars are bid-based and UTC-aligned; the system's bars are mid-based and aligned to the New York rollover. | Long backtests on provider bars are not directly comparable with live-collected data. | Stored under different `source` and `price_basis`; never mixed. | Decide in Phase 3: rebuild history from `/v3/quotes` (accurate, heavy) or use bid bars with a spread model. |
| R8 | Single process, single host. | A host outage stops collection. | Automatic restart; downtime is recorded and backfilled up to 72 h. | Run on an always-on machine, not a laptop. |
| R9 | Source timestamps may have 1-second resolution. | Latency can only be bounded, not measured exactly; seconds rebuilt by backfill can differ by one second from what live capture would have recorded. | Detected from the data and reported; backfilled rows are tagged. | Check the `verify-provider` report. |
| R10 | No holiday calendar: on days when FX does not trade (25 December, 1 January) the grid shows stale seconds and gaps resolve as "no quotes existed". | Cosmetic; some pointless REST calls. | Outcomes are explicit. | Provider holiday API in Phase 8. |
| R11 | Edge seconds keep their partial live capture: the second of the last quote before a disconnect and the second of the first quote after it or after a restart. | Those seconds can miss quotes, so their high and low can be understated. | Documented; two seconds per outage, one per restart. | Revisit once it is known how quickly Massive's quote history is complete. |
| R12 | Rebuilding bars after a backfill loads the affected trading days into memory. | A multi-day backfill makes one slow rebuild (seconds of CPU, off the event loop). | Auto-backfill limited to 72 h. | Chunked rebuild if it becomes a problem. |
| R13 | Dependency versions are recent major releases (FastAPI 0.163, SQLAlchemy 2.1, Vite 8, TypeScript 7, React 19). | Future upgrades may need attention. | Python dependencies pinned in `requirements.lock`, JavaScript in `package-lock.json`. | Upgrade deliberately, with the test-suite. |
| R14 | The watchdog is feed-level. | If only illiquid pairs are subscribed, 30 s of silence can be normal and causes needless reconnects. | Configurable (`WS_SILENCE_TIMEOUT_S`). | Revisit in Phase 7. |

## Decisions taken (15 November 2026)

1. **Subscription.** Massive Currencies Starter is in place.
2. **Timeframes.** Trading below one minute is intended. The 1-second grid and the 5, 15
   and 30 second bars all stay.
3. **Remote access.** The dashboard is wanted on a phone, away from the machine that
   runs it. For now: a private network (Tailscale), see [DEPLOY.md](DEPLOY.md). A login
   inside the application remains Phase 8.
4. **Host.** A small cloud server on the US east coast is recommended over a home PC;
   reasons and steps are in [DEPLOY.md](DEPLOY.md). Not yet set up.

## Open questions for you

1. **Raw ticks.** 30 days of raw quotes are kept by default. Longer retention costs disk
   (to be measured on real data) but allows exact re-simulation of fills later.
2. **Costs for backtests.** Which spread, commission and slippage should Phase 3 assume
   for your execution venue? The registry holds placeholders.
3. **Reaction time.** Below one minute, the delay between a signal and your order
   decides whether an edge survives. Phase 3 needs a realistic figure for it (seconds,
   from a phone); backtests will be run across a range of delays.
4. **Emerging-market pairs.** USD/TRY, USD/ZAR and USD/PLN availability and quote
   quality on Massive are unknown until `verify-provider` runs.
