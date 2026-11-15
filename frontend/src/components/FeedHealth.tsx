import { duration, fixed, int, latencyMs, timeUTC } from "../format";
import type { InstrumentStatus, SystemStatus } from "../types";
import { Panel, StatePill, type Tone } from "./ui";

const STATE: Record<string, { tone: Tone; text: string }> = {
  streaming: { tone: "ok", text: "Streaming" },
  connecting: { tone: "idle", text: "Connecting" },
  authenticating: { tone: "idle", text: "Authenticating" },
  subscribing: { tone: "idle", text: "Subscribing" },
  reconnect_wait: { tone: "warn", text: "Disconnected, waiting to reconnect" },
  auth_failed: { tone: "bad", text: "API key rejected" },
  not_configured: { tone: "bad", text: "No API key configured" },
  idle: { tone: "idle", text: "Idle, nothing to subscribe to" },
  stopped: { tone: "idle", text: "Stopped" },
};


export function FeedHealth({ status, inst }: { status: SystemStatus; inst: InstrumentStatus | null }) {
  const now = status.server_time_ms;
  const conn = status.connection;
  const state = STATE[conn.state] ?? { tone: "idle" as Tone, text: conn.state };
  const lat = inst?.latency ?? null;
  const counters = inst?.counters ?? {};
  const rejects = Object.entries(counters).filter(([key]) => key.startsWith("rejected:"));
  const storage = status.storage;
  const gaps = status.gaps.last_24h;
  const buffered = Object.values(storage.buffered).reduce((a, b) => a + b, 0);
  const dropped = Object.values(storage.dropped).reduce((a, b) => a + b, 0);

  return (
    <Panel title="System health" aside={<span>up {duration(status.uptime_s * 1000)}</span>}>
      <dl className="rows">
        <dt>Feed</dt>
        <dd>
          <StatePill tone={state.tone}>{state.text}</StatePill>{" "}
          <span className="note">for {duration(now - conn.state_since_ms)}</span>
        </dd>
        <dt>Last message</dt>
        <dd className="num">{conn.last_message_ms ? `${duration(now - conn.last_message_ms)} ago` : "—"}</dd>
        <dt>Connections</dt>
        <dd>
          <span className="num">{conn.sessions_established ?? 0}</span> established,{" "}
          <span className="num">{Math.max(0, (conn.sessions_established ?? 0) - 1)}</span> reconnect
          {(conn.sessions_established ?? 0) - 1 === 1 ? "" : "s"}
        </dd>
        {conn.next_retry_ms ? (
          <>
            <dt>Next attempt</dt>
            <dd className="num">in {duration(Math.max(0, conn.next_retry_ms - now))}</dd>
          </>
        ) : null}
        {conn.last_error ? (
          <>
            <dt>Last feed error</dt>
            <dd>
              <span className="num note">{timeUTC(conn.last_error_ms)}</span> {conn.last_error}
            </dd>
          </>
        ) : null}
      </dl>

      <dl className="rows">
        <dt title="Local receipt time minus the provider's quote timestamp, last 5 minutes">Feed latency</dt>
        <dd>
          {lat && lat.n > 0 ? (
            <>
              <span className="num inline-flex flex-wrap gap-x-3">
                <span className="whitespace-nowrap">p50 {latencyMs(lat.p50_ms)}</span>
                <span className="whitespace-nowrap">p95 {latencyMs(lat.p95_ms)}</span>
                <span className="whitespace-nowrap">p99 {latencyMs(lat.p99_ms)}</span>
                <span className="note whitespace-nowrap">n={int(lat.n)}</span>
              </span>
              {lat.note ? (
                <div className="mt-0.5">
                  <StatePill tone={lat.meaningful ? "idle" : "warn"}>{lat.note}</StatePill>
                </div>
              ) : null}
            </>
          ) : (
            <span className="note">no quotes in the last 5 minutes, so there is nothing to measure</span>
          )}
        </dd>
        <dt title="Time spent validating and sampling one quote">Processing</dt>
        <dd className="num">
          {status.processing_us.n ? (
            <span className="inline-flex flex-wrap gap-x-3">
              <span className="whitespace-nowrap">p50 {fixed(status.processing_us.p50, 0)} µs</span>
              <span className="whitespace-nowrap">p99 {fixed(status.processing_us.p99, 0)} µs</span>
            </span>
          ) : (
            "—"
          )}
        </dd>
        <dt>Throughput</dt>
        <dd className="num">
          {fixed(status.throughput.quotes_per_s_1s, 0)}/s now, {fixed(status.throughput.quotes_per_s_60s, 1)}/s over 60 s,{" "}
          {int(status.throughput.total_quotes)} total
        </dd>
      </dl>

      <dl className="rows">
        <dt title="Outcome of validating every received quote since start-up">Quotes</dt>
        <dd className="num">
          {int(counters.accepted ?? 0)} accepted, {int(counters.duplicate ?? 0)} duplicate, {int(counters.late ?? 0)} late,{" "}
          {int(counters.rejected ?? 0)} rejected
        </dd>
        {rejects.length ? (
          <>
            <dt>Rejected for</dt>
            <dd className="num">
              {rejects.map(([key, value]) => `${key.slice("rejected:".length).replace("_", " ")} ${int(value)}`).join(", ")}
            </dd>
          </>
        ) : null}
      </dl>

      <dl className="rows">
        <dt>Database</dt>
        <dd>
          {storage.db_ok === false ? (
            <StatePill tone="bad">Writes failing: {storage.last_error}</StatePill>
          ) : storage.db_ok ? (
            <StatePill tone="ok">Writing</StatePill>
          ) : (
            <StatePill tone="idle">Nothing written yet</StatePill>
          )}{" "}
          <span className="note">
            last flush {storage.last_flush_ms ? `${duration(now - storage.last_flush_ms)} ago` : "—"}
            {storage.last_flush_duration_ms != null ? `, took ${fixed(storage.last_flush_duration_ms, 0)} ms` : ""}
          </span>
        </dd>
        <dt>Rows written</dt>
        <dd className="num">
          {int(storage.written.obs)} seconds, {int(storage.written.ticks)} ticks, {int(storage.written.bars)} bars
        </dd>
        <dt>Write buffer</dt>
        <dd>
          <span className="num">{int(buffered)}</span> rows waiting
          {dropped ? (
            <>
              {" "}
              <StatePill tone="bad">{int(dropped)} rows dropped</StatePill>
            </>
          ) : null}
        </dd>
        {!storage.timescaledb ? (
          <>
            <dt>Storage engine</dt>
            <dd>
              <StatePill tone="warn">Plain PostgreSQL: no compression or retention policies</StatePill>
            </dd>
          </>
        ) : null}
        {storage.timescaledb && !storage.policies_managed ? (
          <>
            <dt>Storage policies</dt>
            <dd>
              <StatePill tone="warn">Not managed by the application</StatePill>
              <div className="note">Compression and retention are left to you (STORAGE_POLICIES_ENABLED=false).</div>
            </dd>
          </>
        ) : null}
        {storage.policy_errors.length ? (
          <>
            <dt>Storage policies</dt>
            <dd>
              <StatePill tone="warn">
                {storage.policy_errors.length} problem{storage.policy_errors.length === 1 ? "" : "s"}:{" "}
                {storage.policy_errors
                  .slice(0, 3)
                  .map((e) => e.policy)
                  .join(", ")}
              </StatePill>
              <div className="note">{storage.policy_errors[0]?.error}</div>
            </dd>
          </>
        ) : null}
        <dt>Gaps, 24 h</dt>
        <dd>
          {Object.keys(gaps).length ? (
            <span className="num">
              {Object.entries(gaps)
                .map(([key, value]) => `${value} ${key.replaceAll("_", " ")}`)
                .join(", ")}
            </span>
          ) : (
            <span className="note">none recorded</span>
          )}
          {status.gaps.queued ? <span className="note"> ({status.gaps.queued} queued)</span> : null}
        </dd>
      </dl>

      {status.errors.length ? (
        <div className="border-t border-line px-3 py-2">
          <div className="note mb-1">Recent problems</div>
          <ul className="m-0 max-h-[120px] list-none overflow-auto p-0 text-[12px]">
            {status.errors.slice(0, 8).map((error) => (
              <li key={`${error.ts_ms}-${error.where}-${error.message}`} className="py-0.5">
                <span className="num note">{timeUTC(error.ts_ms)}</span> <span className="text-ink-2">{error.where}</span>{" "}
                {error.message}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </Panel>
  );
}
