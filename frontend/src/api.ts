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
  return (await response.json()) as T;
}

const query = (params: Record<string, string | number | undefined>) =>
  Object.entries(params)
    .filter((entry): entry is [string, string | number] => entry[1] !== undefined)
    .map(([key, value]) => `${key}=${encodeURIComponent(value)}`)
    .join("&");

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
  createJob: (symbol: string, timeframe: string, start: number, end: number) =>
    request<Job>("/api/history/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ symbol, timeframe, start, end }),
    }),
};
