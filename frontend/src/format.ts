// Pure formatting helpers.  Everything is displayed in UTC: the data, the bars and the
// provider all work in UTC, and a trader should never have to wonder which zone a time is in.

const pad = (n: number, width = 2) => String(n).padStart(width, "0");

export function timeUTC(ms: number | null | undefined): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

export function dateTimeUTC(ms: number | null | undefined, withSeconds = false): string {
  if (ms == null) return "—";
  const d = new Date(ms);
  const date = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
  const time = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}${withSeconds ? `:${pad(d.getUTCSeconds())}` : ""}`;
  return `${date} ${time}`;
}

/** "YYYY-MM-DD HH:MM" (UTC) for text inputs. */
export function inputUTC(ms: number): string {
  return dateTimeUTC(ms);
}

/** Parse "YYYY-MM-DD", "YYYY-MM-DD HH:MM[:SS]" or ISO text as UTC.  Returns null when invalid. */
export function parseUTC(text: string): number | null {
  const m = /^\s*(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?\s*Z?\s*$/.exec(text);
  if (!m) return null;
  const [, y, mo, d, h = "0", mi = "0", s = "0"] = m;
  const ms = Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s));
  const check = new Date(ms);
  if (check.getUTCMonth() !== Number(mo) - 1 || check.getUTCDate() !== Number(d) || Number(h) > 23 || Number(mi) > 59) {
    return null;
  }
  return ms;
}

/** Age / latency: "85 ms", "3.2 s", "4 m 05 s", "2 h 10 m", "3 d 4 h". */
export function duration(ms: number | null | undefined): string {
  if (ms == null || Number.isNaN(ms)) return "—";
  const a = Math.abs(ms);
  const sign = ms < 0 ? "−" : "";
  if (a < 1) return "<1 ms"; // below what the timestamps resolve; "0 ms" would overstate it
  if (a < 1000) return `${sign}${Math.round(a)} ms`;
  if (a < 60_000) return `${sign}${(a / 1000).toFixed(1)} s`;
  const s = Math.floor(a / 1000);
  if (s < 3600) return `${sign}${Math.floor(s / 60)} m ${pad(s % 60)} s`;
  if (s < 86_400) return `${sign}${Math.floor(s / 3600)} h ${pad(Math.floor((s % 3600) / 60))} m`;
  return `${sign}${Math.floor(s / 86_400)} d ${Math.floor((s % 86_400) / 3600)} h`;
}

export function price(value: number | null | undefined, precision: number): string {
  return value == null ? "—" : value.toFixed(precision);
}

export function int(value: number | null | undefined): string {
  return value == null ? "—" : Math.round(value).toLocaleString("en-US");
}

export function fixed(value: number | null | undefined, digits = 1): string {
  return value == null ? "—" : value.toFixed(digits);
}

/**
 * A measured latency.  A value below one millisecond is shown as "<1 ms": the timestamps
 * cannot resolve it, and "0 ms" would read as a claim that there is no latency.
 */
export function latencyMs(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value >= 0 && value < 1) return "<1 ms";
  return `${value.toFixed(0)} ms`;
}

export function signed(value: number | null | undefined, digits = 1): string {
  if (value == null) return "—";
  const text = Math.abs(value).toFixed(digits);
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

/**
 * Split a price the way FX dealing screens show it: big figure, the two pip digits, and
 * the fractional pip.  The split follows the instrument's pip size, so it is right for
 * 1.16019 (pip 0.0001 -> "1.16" "01" "9") as well as 154.305 (pip 0.01 -> "154." "30" "5").
 */
export function splitPrice(value: number, precision: number, pipSize: number): { big: string; pips: string; frac: string } {
  const text = value.toFixed(precision);
  const pipDecimals = Math.max(0, Math.round(-Math.log10(pipSize)));
  const dot = text.indexOf(".");
  if (dot < 0 || pipDecimals < 2 || pipDecimals > precision) {
    return { big: text, pips: "", frac: "" };
  }
  const pipsEnd = dot + 1 + pipDecimals;
  return { big: text.slice(0, pipsEnd - 2), pips: text.slice(pipsEnd - 2, pipsEnd), frac: text.slice(pipsEnd) };
}

/** One-line rendering of an event detail object for the connection log. */
export function detailText(detail: Record<string, unknown> | null | undefined): string {
  if (!detail) return "";
  return Object.entries(detail)
    .filter(([key, value]) => value !== null && value !== "" && key !== "synthetic")
    .map(([key, value]) => `${key}=${typeof value === "object" ? JSON.stringify(value) : String(value)}`)
    .join("  ");
}
