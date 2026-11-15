import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { dateTimeUTC, inputUTC, int, parseUTC } from "../format";
import type { Job } from "../types";
import { Panel, StatePill, type Tone } from "./ui";

const TIMEFRAMES = ["1s", "5s", "15s", "30s", "1m", "5m", "15m", "30m", "1h", "4h", "1d"];
const PRESETS: { label: string; hours: number }[] = [
  { label: "Last 24 hours", hours: 24 },
  { label: "Last 7 days", hours: 24 * 7 },
  { label: "Last 30 days", hours: 24 * 30 },
];
const JOB: Record<Job["status"], { tone: Tone; text: string }> = {
  queued: { tone: "idle", text: "Queued" },
  running: { tone: "idle", text: "Downloading" },
  done: { tone: "ok", text: "Downloaded" },
  failed: { tone: "bad", text: "Failed" },
};

export function HistoryPanel({
  symbol,
  available,
  unavailableReason,
  now,
  onShow,
}: {
  symbol: string;
  available: boolean;
  unavailableReason: string;
  now: number;
  onShow: (job: Job) => void;
}) {
  const [timeframe, setTimeframe] = useState("1m");
  const [from, setFrom] = useState(() => inputUTC(now - 24 * 3600_000));
  const [to, setTo] = useState(() => inputUTC(now));
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .jobs()
      .then(setJobs)
      .catch(() => undefined);
  }, []);

  const active = jobs.some((job) => job.status === "queued" || job.status === "running");
  useEffect(() => {
    load();
    const timer = setInterval(load, active ? 1000 : 15_000);
    return () => clearInterval(timer);
  }, [load, active]);

  const submit = async () => {
    const start = parseUTC(from);
    const end = parseUTC(to);
    if (start == null || end == null) {
      setError("Enter both times as YYYY-MM-DD HH:MM (UTC).");
      return;
    }
    setError(null);
    try {
      await api.createJob(symbol, timeframe, start, end);
      load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <Panel title="Historical data" aside={<span>provider aggregate bars, bid prices</span>}>
      {!available ? (
        <p className="note m-0 px-3 py-3">{unavailableReason}</p>
      ) : (
        <form
          className="flex flex-wrap items-end gap-x-3 gap-y-2 px-3 py-3"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <label className="flex flex-col gap-0.5">
            <span className="note">Bar size</span>
            <select className="field num" value={timeframe} onChange={(e) => setTimeframe(e.target.value)}>
              {TIMEFRAMES.map((tf) => (
                <option key={tf}>{tf}</option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="note">From (UTC)</span>
            <input className="field num w-[150px]" value={from} onChange={(e) => setFrom(e.target.value)} placeholder="2026-11-12 00:00" />
          </label>
          <label className="flex flex-col gap-0.5">
            <span className="note">To (UTC)</span>
            <input className="field num w-[150px]" value={to} onChange={(e) => setTo(e.target.value)} placeholder="2026-11-13 00:00" />
          </label>
          <div className="flex flex-wrap gap-1">
            {PRESETS.map((preset) => (
              <button
                key={preset.label}
                type="button"
                className="btn"
                onClick={() => {
                  setFrom(inputUTC(now - preset.hours * 3600_000));
                  setTo(inputUTC(now));
                }}
              >
                {preset.label}
              </button>
            ))}
          </div>
          <button type="submit" className="btn btn-primary">
            Download {symbol} bars
          </button>
          {error ? (
            <p role="alert" className="m-0 basis-full text-[13px]">
              <StatePill tone="bad">{error}</StatePill>
            </p>
          ) : null}
        </form>
      )}

      {jobs.length ? (
        <div className="max-h-[210px] overflow-auto border-t border-line">
          <table className="data">
            <thead>
              <tr>
                <th>Pair</th>
                <th>Bars</th>
                <th>Range (UTC)</th>
                <th>Result</th>
                <th className="r">Rows</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {jobs.map((job) => (
                <tr key={job.id}>
                  <td className="num">{job.symbol}</td>
                  <td className="num">{job.timeframe}</td>
                  <td className="num">
                    {dateTimeUTC(job.start_ms)} to {dateTimeUTC(job.end_ms)}
                  </td>
                  <td title={job.error ?? undefined}>
                    <StatePill tone={JOB[job.status].tone}>{JOB[job.status].text}</StatePill>
                    {job.status === "failed" && job.error ? <span className="note"> {job.error.slice(0, 60)}</span> : null}
                  </td>
                  <td className="r num">{int(job.rows_written)}</td>
                  <td className="r">
                    {job.status === "done" && job.rows_written > 0 ? (
                      <button type="button" className="btn" onClick={() => onShow(job)}>
                        Show on chart
                      </button>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Panel>
  );
}
