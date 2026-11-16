import { notifyUnauthorized } from "./auth";
import type { BoardBody, BoardResult, BoardState, WindowBody, WindowResult } from "./board/model";
import type { BacktestBody, LabInfo, Report } from "./lab/model";
import type { Definition, Saved, SetupBody, SetupReport } from "./setups/model";
import type { Channels, LiveForm, LiveSettings, LiveView } from "./signals/model";
import type { Bar, ConnEvent, Gap, Job, Obs, SystemStatus } from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (response.status === 401) notifyUnauthorized(); // the session ended: back to the login form
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      // keep the status text
    }
    throw new ApiError(detail, response.status);
  }
  if (response.status === 204) return undefined as T; // done, and nothing to say
  return (await response.json()) as T;
}

const query = (params: Record<string, string | number | undefined>) =>
  Object.entries(params)
    .filter((entry): entry is [string, string | number] => entry[1] !== undefined)
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
    .join("&");

const post = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export const api = {
  status: () => request<SystemStatus>("/api/status"),
  observations: (symbol: string, limit: number) =>
    request<{ observations: Obs[] }>(`/api/observations?${query({ symbol, limit })}`),
  bars: (symbol: string, timeframe: string, source: "live" | "provider", limit: number, start?: number, end?: number) =>
    request<{ bars: Bar[]; source: string; price_basis: string }>(
      `/api/bars?${query({ symbol, timeframe, source, limit, start, end })}`,
    ),
  events: (limit = 60) => request<ConnEvent[]>(`/api/events?limit=${limit}`),
  gaps: () => request<Gap[]>("/api/gaps?hours=168"),
  scanGaps: () => request<{ queued: number }>("/api/gaps/scan", { method: "POST" }),
  fillGap: (id: number) => request<{ queued: boolean }>(`/api/gaps/${id}/fill`, { method: "POST" }),
  jobs: () => request<Job[]>("/api/history/jobs?limit=8"),
  job: (id: number) => request<Job>(`/api/history/jobs/${id}`),
  createJob: (symbol: string, timeframe: string, start: number, end: number) =>
    request<Job>("/api/history/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, timeframe, start, end }),
    }),
  lab: (symbol?: string) => request<LabInfo>(`/api/lab${symbol ? `?${query({ symbol })}` : ""}`),
  backtest: (body: BacktestBody) => request<Report>("/api/backtests", post(body)),
  /** The board run in progress or the last one, and whether a finished board is there to fetch. */
  board: () => request<BoardState>("/api/board"),
  startBoard: (body: BoardBody) => request<BoardState>("/api/board/runs", post(body)),
  boardResult: () => request<BoardResult>("/api/board/result"),
  boardSignals: (body: WindowBody) => request<WindowResult>("/api/board/signals", post(body)),
  /** The saved setups (the owner's own rules), oldest first. */
  setups: () => request<Saved[]>("/api/setups"),
  /** Keep a setup under its name; a name that exists is replaced. */
  saveSetup: (symbol: string, definition: Definition) =>
    request<Saved>(`/api/setups?${query({ symbol })}`, { ...post(definition), method: "PUT" }),
  deleteSetup: (id: number) => request<void>(`/api/setups/${id}`, { method: "DELETE" }),
  runSetup: (body: SetupBody) => request<SetupReport>("/api/setups/run", post(body)),
  /**
   * The live page's picture: the signals of the trading day and the channels, with either the day's seconds
   * or (`minutes`) the one-minute closes the channels were fitted to.
   */
  liveView: (symbol: string, minutes: boolean) =>
    request<LiveView>(`/api/live/view?${query(minutes ? { symbol, history: "true", seconds: "false" } : { symbol })}`),
  liveChannels: (symbol: string) => request<Channels>(`/api/live/channels?${query({ symbol })}`),
  liveSettings: () => request<LiveForm>("/api/live/settings"),
  /** Keep new settings of the live rule; what is left out stays as it is. */
  saveLiveSettings: (changes: Partial<LiveSettings>) =>
    request<LiveForm>("/api/live/settings", { ...post(changes), method: "PUT" }),
};
