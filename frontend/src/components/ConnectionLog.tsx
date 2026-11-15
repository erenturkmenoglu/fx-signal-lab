import { dateTimeUTC, detailText } from "../format";
import type { ConnEvent } from "../types";
import { Panel } from "./ui";

const LABEL: Record<string, string> = {
  connect_attempt: "Connecting",
  connected: "Connected",
  auth_success: "Authenticated",
  auth_failed: "Authentication failed",
  subscribed: "Subscribed",
  subscription_ack: "Subscription confirmed",
  first_data: "First quote received",
  disconnected: "Disconnected",
  reconnect_scheduled: "Reconnect scheduled",
  watchdog_timeout: "Feed silent, forcing reconnect",
  server_status: "Server notice",
  error: "Error",
  stopped: "Stopped",
};

const PROBLEM = new Set(["auth_failed", "disconnected", "watchdog_timeout", "error", "server_status"]);

export function ConnectionLog({ events }: { events: ConnEvent[] }) {
  return (
    <Panel title="Connection log" aside={<span>newest first, UTC</span>}>
      {events.length === 0 ? (
        <p className="note m-0 px-3 py-3">No connection events yet.</p>
      ) : (
        <ul className="m-0 max-h-[250px] list-none overflow-auto p-0 text-[12px]">
          {events.map((event, i) => (
            <li key={`${event.ts_ms}-${event.kind}-${i}`} className="grid grid-cols-[max-content_1fr] gap-x-3 border-b border-line px-3 py-1 last:border-b-0">
              <span className="num note">{dateTimeUTC(event.ts_ms, true).slice(5)}</span>
              <span className="min-w-0">
                <span className={PROBLEM.has(event.kind) ? "font-medium text-ink" : "text-ink-2"}>
                  {PROBLEM.has(event.kind) ? "▲ " : ""}
                  {LABEL[event.kind] ?? event.kind}
                </span>{" "}
                <span className="num note break-words">{detailText(event.detail)}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
