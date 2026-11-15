import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Job } from "../types";
import { type HistoryInfo, type Missing, downloadRange, olderRange } from "./model";

export interface Download {
  job: Job | null;
  error: string | null;
  downloading: boolean;
  /** Ask the provider for the one-minute bars of a period.  One download at a time. */
  start: (range: { start: number; end: number }) => Promise<void>;
}

/**
 * A download of one-minute history from the provider, followed until it ends.  ``onEnd`` is called
 * when it has ended, well or badly, so that the page can read again what is stored.
 */
export function useHistoryDownload(symbol: string | null, onEnd: () => void): Download {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ended = useRef(onEnd);
  ended.current = onEnd;

  const downloading = job?.status === "queued" || job?.status === "running";
  const jobId = job?.id;
  useEffect(() => {
    if (!downloading || jobId == null) return;
    const timer = setInterval(() => {
      api
        .job(jobId)
        .then((next) => {
          setJob(next);
          if (next.status === "done" || next.status === "failed") ended.current();
        })
        .catch(() => undefined); // a failed poll is tried again
    }, 1500);
    return () => clearInterval(timer);
  }, [downloading, jobId]);

  const start = useCallback(
    async (range: { start: number; end: number }) => {
      if (!symbol) return;
      setError(null);
      try {
        setJob(await api.createJob(symbol, "1m", range.start, range.end));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [symbol],
  );
  return { job, error, downloading, start };
}

const day = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/** What a download is doing or how it ended, in words. */
export function DownloadNote({ download }: { download: Download }) {
  const { job, error, downloading } = download;
  const failure = (text: string) => (
    <span role="alert" className="text-[13px]">
      <span aria-hidden style={{ color: "var(--color-bad)" }}>
        ✕
      </span>{" "}
      {text}
    </span>
  );
  if (downloading && job) {
    return (
      <span className="note" role="status">
        Downloading one-minute bars from {day(job.start_ms)} to {day(job.end_ms - 1)}:{" "}
        {job.rows_written.toLocaleString("en-US")} so far…
      </span>
    );
  }
  if (job?.status === "failed") return failure(`The download failed: ${job.error ?? "no reason given"}`);
  if (job?.status === "done") {
    return (
      <span className="note" role="status">
        {job.rows_written > 0
          ? `Downloaded ${job.rows_written.toLocaleString("en-US")} bars from ${day(job.start_ms)} to ${day(job.end_ms - 1)}.`
          : `The provider has no bars from ${day(job.start_ms)} to ${day(job.end_ms - 1)}.`}
      </span>
    );
  }
  return error ? failure(error) : null;
}

/**
 * The buttons that bring history in: the parts of the chosen period that are not stored, everything
 * since the last stored bar, and the twelve months before the first one.
 */
export function DownloadButtons({
  history,
  missing,
  download,
}: {
  history: HistoryInfo | null;
  missing: Missing[];
  download: Download;
}) {
  const { downloading, start } = download;
  return (
    <>
      {missing.map((part) => (
        <button
          key={part.start}
          type="button"
          className="btn btn-primary"
          disabled={downloading}
          onClick={() => void start(part)}
          title="Asks the provider for the one-minute bars of the part of the chosen period that is not stored"
        >
          Download {part.from === part.to ? part.from : `${part.from} to ${part.to}`}
        </button>
      ))}
      {history ? (
        <>
          <button
            type="button"
            className="btn"
            disabled={downloading}
            onClick={() => void start(downloadRange(history, Date.now()))}
            title="Asks the provider for the one-minute bars since the last stored one"
          >
            Update to now
          </button>
          <button
            type="button"
            className="btn"
            disabled={downloading}
            onClick={() => void start(olderRange(history))}
            title={`Asks the provider for the one-minute bars of the twelve months before ${history.first.slice(0, 10)}`}
          >
            Get 12 months more
          </button>
        </>
      ) : null}
    </>
  );
}
