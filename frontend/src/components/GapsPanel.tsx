import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { dateTimeUTC, duration } from "../format";
import type { Gap } from "../types";
import { Panel, StatePill, type Tone } from "./ui";

const STATUS: Record<string, { tone: Tone; text: string; hint: string }> = {
  open: { tone: "warn", text: "Waiting", hint: "Recorded; recovery starts about a minute after the feed is back" },
  filled: { tone: "ok", text: "Recovered", hint: "Rebuilt from the provider's historical quotes" },
  no_provider_data: { tone: "idle", text: "No quotes existed", hint: "The provider has no quotes in this window either" },
  unfillable: { tone: "bad", text: "Not recoverable", hint: "Cannot be recovered automatically" },
  failed: { tone: "warn", text: "Retrying", hint: "The last recovery attempt failed and will be retried" },
};

const KIND: Record<string, string> = { hole: "No data (process was down)", feed_outage: "Feed outage" };

export function GapsPanel({ canFill, refreshKey }: { canFill: boolean; refreshKey: number }) {
  const [gaps, setGaps] = useState<Gap[] | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(() => {
    api
      .gaps()
      .then(setGaps)
      .catch((error: unknown) => setMessage(error instanceof Error ? error.message : String(error)));
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 30_000);
    return () => clearInterval(timer);
  }, [load, refreshKey]);

  const scan = async () => {
    setBusy(true);
    try {
      const { queued } = await api.scanGaps();
      setMessage(queued ? `Scan found ${queued} stretch${queued === 1 ? "" : "es"} to check.` : "Scan found no missing seconds.");
      setTimeout(load, 1500);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  };

  const fill = async (id: number) => {
    try {
      await api.fillGap(id);
      setMessage(`Recovery of gap ${id} queued.`);
      setTimeout(load, 2500);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <Panel
      title="Data gaps, last 7 days"
      aside={
        <>
          {message ? <span role="status">{message}</span> : null}
          <button type="button" className="btn" onClick={scan} disabled={busy}>
            Scan for missing seconds
          </button>
        </>
      }
    >
      {gaps === null ? (
        <p className="note m-0 px-3 py-3">Loading…</p>
      ) : gaps.length === 0 ? (
        <p className="note m-0 px-3 py-3">
          No gaps recorded. A gap is logged when the process was down or the feed dropped while the market was open.
        </p>
      ) : (
        <div className="max-h-[250px] overflow-auto">
          <table className="data">
            <thead>
              <tr>
                <th>From (UTC)</th>
                <th className="r">Length</th>
                <th>Pair</th>
                <th>Cause</th>
                <th>Outcome</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {gaps.map((gap) => {
                const s = STATUS[gap.status] ?? { tone: "idle" as Tone, text: gap.status, hint: "" };
                const reason = typeof gap.detail?.reason === "string" ? gap.detail.reason : typeof gap.detail?.error === "string" ? gap.detail.error : s.hint;
                return (
                  <tr key={gap.id}>
                    <td className="num">{dateTimeUTC(gap.start_ms, true)}</td>
                    <td className="r num">{duration(gap.seconds * 1000)}</td>
                    <td className="num">{gap.symbol}</td>
                    <td>{KIND[gap.kind] ?? gap.kind}</td>
                    <td title={reason}>
                      <StatePill tone={s.tone}>{s.text}</StatePill>
                    </td>
                    <td className="r">
                      {canFill && gap.status !== "filled" ? (
                        <button type="button" className="btn" onClick={() => fill(gap.id)}>
                          Recover
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}
