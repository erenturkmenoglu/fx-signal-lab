# Technical risks and open questions

Ordered by how much they matter for the next step.

## Risks

| # | Risk | Impact | Mitigation in place | Next step |
|---|---|---|---|---|
| R1 | The Massive integration has met the production service only with the market closed (`make verify` on the server, 15 November 2026). All six REST checks and the WebSocket handshake (connect, key accepted, subscription confirmed) passed. No live quote has been received yet. | The quote message, the resolution of its timestamp and the real latency could still differ from the documentation. | Lenient parsing; every unexpected status and reject is counted and shown; whole-second timestamps are covered by tests since they turned up in the quote history. | After the open (Sunday 17:00 New York): read latency, its note and the accept/late/reject counters on the dashboard, then compare a few minutes of live ticks with `/v3/quotes`. |
| R2 | The Compose stack runs on the server since 15 November 2026 (set up with `scripts/server-setup.sh`). Its memory use there has not been reported yet. | The measured budget (DEPLOY.md, "Server size") contains an estimate for the operating system. | CI runs the stack inside the limits of a 2 GB server on every push. | `free -m` and `docker stats --no-stream` after a day of live data. |
| R3 | Resolved on 15 November 2026: CI ran and passes (backend suite on the TimescaleDB image, dashboard, image build, stack smoke test). | | | |
| R4 | Validation thresholds are provisional (chosen before seeing real data). | Too tight: real quotes rejected. Too loose: bad quotes pass. | Per-pair configuration; reject counters by reason on the dashboard and per minute in `ingest_stats_1m`. | Calibrate after a few days of EUR/USD data. |
| R5 | Classic TimescaleDB compression API is deprecated since 2.18 (it works in the pinned 2.26.1, tested). | A future image could remove it. | Image tag is pinned; failures are visible, not fatal. | Move to the columnstore API in Phase 8. |
| R6 | The dashboard and API have no authentication. | Anyone who can reach port 8000 can read data and trigger downloads. | Published on 127.0.0.1 only; documented. | Authentication in Phase 8, or a reverse proxy now if remote access is needed. |
| R7 | Provider bars are bid-based and UTC-aligned; the system's bars are mid-based and aligned to the New York rollover. | Long backtests on provider bars are not directly comparable with live-collected data. | Stored under different `source` and `price_basis`; never mixed. | Decide in Phase 3: rebuild history from `/v3/quotes` (accurate, heavy) or use bid bars with a spread model. |
| R8 | Single process, single host. | A host outage stops collection. | Automatic restart; downtime is recorded and backfilled up to 72 h. | Run on an always-on machine, not a laptop. |
| R9 | Massive stamps historical quotes (`/v3/quotes`) and the last quote in whole seconds: confirmed on 15 November 2026. Whether the live stream does the same is not known yet. | Recovered quotes cannot be ordered against live ones inside a second. If the stream is also whole-second, latency can only be bounded (to one second), not measured, and a 50 ms difference between hosting regions cannot be seen in the data. | Detected from the data and reported next to the latency figures; backfilled rows are tagged; equal quotes sharing a whole second are kept as separate quotes, as the provider counts them. | Read the latency note on the dashboard after the open. |
| R10 | No holiday calendar: on days when FX does not trade (25 December, 1 January) the grid shows stale seconds and gaps resolve as "no quotes existed". | Cosmetic; some pointless REST calls. | Outcomes are explicit. | Provider holiday API in Phase 8. |
| R11 | Edge seconds keep their partial live capture: the second of the last quote before a disconnect and the second of the first quote after it or after a restart. | Those seconds can miss quotes, so their high and low can be understated. With whole-second history stamps the same holds for the ticks of the second that was being captured when the process stopped: its row is rebuilt from all of the provider's quotes, its ticks stay as captured. | Documented; two seconds per outage, one per restart; no quote is ever stored twice. | Revisit once live ticks have been compared with Massive's quote history. |
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
5. **Server size.** 2 GB of memory and 60 GB of disk, after measuring (DEPLOY.md,
   "Server size"). Larger only when backtests on the server need it; the way up is a
   snapshot, and there is no way down.

## Open questions for you

1. **Raw ticks.** 30 days of raw quotes are kept by default. Longer retention costs disk
   but allows exact re-simulation of fills later. Measured on synthetic quotes: about
   28 MB per pair and trading day at 20 quotes a second once compressed, so about 7 GB a
   year per pair. To be confirmed on real data.
2. **Costs for backtests.** Which spread, commission and slippage should Phase 3 assume
   for your execution venue? The registry holds placeholders.
3. **Reaction time.** Below one minute, the delay between a signal and your order
   decides whether an edge survives. Phase 3 needs a realistic figure for it (seconds,
   from a phone); backtests will be run across a range of delays.
4. **Emerging-market pairs.** Massive lists all eight configured pairs, including
   USD/TRY, USD/ZAR and USD/PLN (reference check, 15 November 2026). How often they are
   quoted and how wide is not known; that decides their `stale_after_ms` and whether
   trading them below one minute makes sense.
