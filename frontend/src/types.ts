// Shapes of the backend payloads (see backend/fxlab/pipeline/payloads.py and ingestion.status()).

export type ObsStatus = "fresh" | "carried" | "stale" | "missing";

export interface Obs {
  ts: number; // window start, epoch ms (UTC)
  status: ObsStatus;
  bid: number | null;
  ask: number | null;
  mid: number | null;
  mid_open: number | null;
  mid_high: number | null;
  mid_low: number | null;
  spread_pips: number | null;
  n_quotes: number;
  quote_ts: number | null;
  age_ms: number | null;
  source: string;
}

export interface Bar {
  ts: number; // bar start, epoch ms (UTC)
  timeframe: string;
  open: number;
  high: number;
  low: number;
  close: number;
  bid_close: number | null;
  ask_close: number | null;
  n_quotes: number | null;
  n_fresh: number | null;
  n_valid: number | null;
  n_expected: number | null;
  n_backfilled: number | null;
  complete: boolean | null;
  source: string;
  price_basis: string;
  partial?: boolean;
}

export interface Latency {
  n: number;
  p50_ms: number | null;
  p95_ms: number | null;
  p99_ms: number | null;
  min_ms: number | null;
  max_ms: number | null;
  mean_ms: number | null;
  resolution_ms: number;
  negative_share: number;
  clock_skew_suspected: boolean;
  meaningful: boolean;
  note: string | null;
}

export interface InstrumentStatus {
  symbol: string;
  description: string;
  pip_size: number;
  price_precision: number;
  provider_status: string;
  supported: boolean;
  market_open: boolean;
  next_open_ms: number | null;
  next_close_ms: number | null;
  bid: number | null;
  ask: number | null;
  mid: number | null;
  spread_pips: number | null;
  quote_ts_ms: number | null;
  quote_age_ms: number | null;
  status: ObsStatus | null;
  last_obs_ts_ms: number | null;
  quotes_last_second: number | null;
  quotes_per_s_10s: number;
  quotes_per_s_60s: number;
  stale_after_ms: number;
  signal_gate: { allowed: boolean; reason: string };
  change: { ref_mid: number; ref_ts_ms: number | null; abs: number; pips: number; pct: number } | null;
  counters: Record<string, number>;
  latency: Latency;
}

export interface Connection {
  provider: string;
  state: string;
  state_since_ms: number;
  is_synthetic?: boolean;
  endpoint?: string | null;
  subscriptions?: string[];
  connect_attempts?: number;
  sessions_established?: number;
  last_message_ms?: number | null;
  last_data_ms?: number | null;
  last_error?: string | null;
  last_error_ms?: number | null;
  next_retry_ms?: number | null;
  frames_received?: number;
  quotes_received?: number;
  parse_errors?: number;
  malformed_events?: number;
  unknown_events?: number;
}

export interface SystemStatus {
  startup_error?: string;
  server_time_ms: number;
  started_ms: number;
  uptime_s: number;
  version: string;
  provider: {
    name: string;
    label: "LIVE" | "MOCK" | "NOT CONFIGURED" | "TEST ENDPOINT";
    configured: boolean;
    synthetic: boolean;
    official_endpoint: boolean;
    feed_host: string | null;
    rest_available: boolean;
  };
  connection: Connection;
  sessions: string[];
  instruments: InstrumentStatus[];
  throughput: { quotes_per_s_1s: number; quotes_per_s_10s: number; quotes_per_s_60s: number; total_quotes: number };
  processing_us: { n: number; p50: number | null; p95: number | null; p99: number | null; max: number | null };
  storage: {
    db_ok: boolean | null;
    buffered: Record<string, number>;
    written: Record<string, number>;
    dropped: Record<string, number>;
    last_flush_ms: number | null;
    last_flush_duration_ms: number | null;
    consecutive_failures: number;
    last_error: string | null;
    last_error_ms: number | null;
    timescaledb: boolean;
    policy_errors: { policy: string; error: string }[];
    policies_managed: boolean;
    persist_raw_ticks: boolean;
  };
  gaps: { queued: number; processed: number; last_24h: Record<string, number> };
  live_clients: number;
  errors: { ts_ms: number; where: string; message: string }[];
}

export interface ConnEvent {
  ts_ms: number;
  provider: string;
  kind: string;
  detail: Record<string, unknown>;
}

export interface Gap {
  id: number;
  symbol: string;
  start_ms: number;
  end_ms: number;
  seconds: number;
  kind: string;
  status: string;
  detected_ms: number;
  resolved_ms: number | null;
  detail: Record<string, unknown> | null;
}

export interface Job {
  id: number;
  kind: string;
  symbol: string;
  timeframe: string;
  start_ms: number;
  end_ms: number;
  status: "queued" | "running" | "done" | "failed";
  rows_written: number;
  pages: number;
  error: string | null;
  created_ms: number | null;
  started_ms: number | null;
  finished_ms: number | null;
}

export interface ObsMessage {
  type: "obs";
  symbol: string;
  obs: Obs;
  partials: Record<string, Bar>;
  closed: Bar[];
}
