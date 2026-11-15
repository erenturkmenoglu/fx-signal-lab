# Roadmap

Phases follow the project specification. Each phase ends with the same report as
Phase 1 (features, files, tests, limitations, risks, run instructions, next step) and
is built on the previous one without restructuring it.

## Phase 1: foundation and data infrastructure (done, pending live verification)

Delivered; see [PHASE1_REPORT.md](PHASE1_REPORT.md). Since 15 November 2026 the
repository is on GitHub and CI passes: the backend suite on the TimescaleDB image, the
dashboard, the Docker image build and a run of the Compose stack without a key and on
the simulator.

Close-out work that needs you:

1. Set up the server ([DEPLOY.md](DEPLOY.md)).
2. Run `make verify` there with the Massive key, once at any time and once during market
   hours, and keep both reports.
3. Let the system collect for a few days; calibrate `max_spread_pips`, `max_jump_pct`
   and `stale_after_ms` for EUR/USD from the reject counters.

## Phase 2: indicator engine

* Indicator interface: pure functions over bar arrays plus an incremental (streaming)
  form, both tested against each other.
* RSI, Bollinger Bands, EMA, MACD, ATR, ADX first; then the rest of the specification's
  trend, momentum and volatility families.
* Reference tests against independently computed values (hand-checked vectors and a
  second implementation).
* Multi-timeframe evaluation on the existing bar stream (only completed bars; the
  start-labelled convention makes look-ahead impossible by construction).
* Indicator parameters stored in the database and editable from the dashboard;
  overlays on the chart.
* Depends on: a few days of collected data, or downloaded provider bars for development.

## Phase 3: backtesting laboratory

* Event-driven engine that replays stored observations and bars through strategy
  objects, with configurable spread, slippage, execution delay, stop and target handling
  (conservative when a bar touches both).
* Strategy interface shared with the live engine of Phase 4.
* Metrics from the specification; equity and drawdown curves; trade table; export.
* Chronological train, validation and test splits; parameter sensitivity.
* Research dataset: Parquet export of the grid and bars; decision on mid bars built
  here versus provider bid bars for long histories (see RISKS R7).
* Strategies A (RSI mean reversion), B (Bollinger reversion), C (momentum continuation)
  as the first three.

## Phase 4: live signal engine

* Strategies evaluated on bar close, gated by `evaluate_signal_gate`.
* Signal records with supporting data, reasons, reference levels and expiry; signal
  history; dashboard panel; in-app and browser notifications with cool-downs and
  duplicate suppression.

## Phase 5: strategy intelligence

* Market regime classification with its evidence shown; walk-forward analysis;
  strategy comparison with multiple-comparison awareness; ensemble view; performance
  monitoring and deterioration alerts.

## Phase 6: paper trading and journal

* Simulated positions filled against stored bid and ask; virtual portfolio; manual
  trade journal; comparison of signals, paper fills and manual trades. A broker
  interface stub, inactive.

## Phase 7: multi-currency expansion

* Activate further pairs through `config/instruments.yaml` after `verify-provider`
  confirms availability; per-pair thresholds calibrated from data; cross-pair monitor.

## Phase 8: production hardening

* Dashboard authentication and a documented reverse-proxy setup.
* Scheduled backups with retention and a restore test.
* Alerting outside the browser (Telegram or email) for feed, database and disk problems.
* Holiday calendar from the provider's market-status API.
* Move compression to the columnstore API if the classic API is removed.
* Optional split of ingestion and API into two processes.
