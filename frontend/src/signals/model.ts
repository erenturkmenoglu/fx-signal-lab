// Live signals: the shapes the backend sends (backend/fxlab/services/live_signals.py) and what
// the page works out from them.  Nothing here touches the chart or the network.

export type Side = "buy" | "sell";
export type Rule = "both" | "either";
export type ChannelName = "short" | "medium" | "long";
/** What the chart shows: the seconds of the trading day, or the minutes the channels are fitted to. */
export type Mode = "day" | "history";
/** How much of it: the last minutes of the day, the whole day, or the days the long channel covers. */
export type Range = "5m" | "15m" | "1h" | "day" | "history";

export interface LiveSettings {
  bar_seconds: number;
  rsi_period: number;
  rsi_buy_below: number;
  rsi_sell_above: number;
  bollinger_period: number;
  bollinger_width: number;
  rule: Rule;
  channel_short_hours: number;
  channel_medium_hours: number;
  channel_long_days: number;
  channel_width: number;
}

export interface LiveForm {
  settings: LiveSettings;
  defaults: LiveSettings;
  limits: Record<string, [number, number]>;
  bar_seconds: number[];
  rules: Rule[];
}

/** The rule's condition became true at the end of the second that starts at `t`. */
export interface LiveSignal {
  t: number;
  side: Side;
  bid: number;
  ask: number;
  rsi: number;
  middle: number;
  upper: number;
  lower: number;
  /** which of the two conditions held, judged by the backend on the exact values */
  by: "both" | "rsi" | "band";
}

/** A straight trend channel through one-minute bars: the line at its first and last bar, and half its width. */
export interface Channel {
  bars: number;
  asked: number;
  from: number;
  to: number;
  start: number;
  end: number;
  half: number;
  rise_per_bar: number;
  /** where the line stands at the first of its bars that belongs to the trading day (null: none does) */
  day: { from: number; start: number } | null;
}

export interface ChannelEntry {
  name: ChannelName;
  asked: number;
  channel: Channel | null;
}

export interface Channels {
  symbol: string;
  bar_ms: number;
  minutes: number;
  channels: ChannelEntry[];
}

export interface LiveView {
  symbol: string;
  price_precision: number;
  pip_size: number;
  day: { start: number; end: number };
  newest: number | null;
  warmup_s: number;
  settings: LiveSettings;
  /** one bid for every second from `start` on; null where no price fit to be drawn was stored */
  seconds: { start: number; bid: (number | null)[] };
  signals: LiveSignal[];
  bar_ms: number;
  minutes: number;
  channels: ChannelEntry[];
  history: { t: number[]; bid: number[] } | null;
  gate: { allowed: boolean; reason: string };
  synthetic: boolean;
  server_time_ms: number;
}

export interface SignalMessage {
  type: "signal";
  symbol: string;
  signal: LiveSignal;
}

// ------------------------------------------------------------------------------- the series
/** A point of a line: `value` is left out where the line has a hole. */
export interface Point {
  time: number; // epoch seconds
  value?: number;
}

/** The seconds of the day as points of a line; a second without a price is a hole, never a bridge. */
export function secondPoints(seconds: LiveView["seconds"]): Point[] {
  const first = Math.floor(seconds.start / 1000);
  return seconds.bid.map((value, i) => (value == null ? { time: first + i } : { time: first + i, value }));
}

export function minutePoints(history: NonNullable<LiveView["history"]>): Point[] {
  return history.t.map((t, i) => ({ time: Math.floor(t / 1000), value: history.bid[i] }));
}

// ------------------------------------------------------------------------------- the ranges
/** The ranges of the day's chart and how many seconds each shows (null: all of the day so far). */
export const DAY_RANGES: { key: Exclude<Range, "history">; label: string; seconds: number | null }[] = [
  { key: "5m", label: "5 min", seconds: 300 },
  { key: "15m", label: "15 min", seconds: 900 },
  { key: "1h", label: "1 h", seconds: 3600 },
  { key: "day", label: "Day", seconds: null },
];

export const modeOf = (range: Range): Mode => (range === "history" ? "history" : "day");

/** Points of the chart a range shows: seconds of the day, or all there is (null). */
export function rangePoints(range: Range): number | null {
  return DAY_RANGES.find((r) => r.key === range)?.seconds ?? null;
}

/**
 * The channels the price scale makes room for at a range: the one that matches it and the
 * shorter ones.  A longer channel is many times wider than the last minutes of prices; fitting
 * it in would flatten them to a line, so its edge shows only where the price comes near it.
 */
export function scaleChannels(range: Range): ChannelName[] {
  if (range === "history") return ["short", "medium", "long"];
  if (range === "day") return ["short", "medium"];
  return ["short"];
}

// ----------------------------------------------------------------------------- the channels
export const CHANNEL_NAMES: ChannelName[] = ["short", "medium", "long"];
export const CHANNEL_TITLE: Record<ChannelName, string> = { short: "Short", medium: "Medium", long: "Long" };

/** "1 h", "36 h", "10 d": the length a channel was asked for. */
export function channelSpan(name: ChannelName, settings: LiveSettings): string {
  const trim = (value: number) => String(Number(value.toFixed(2)));
  if (name === "long") return `${trim(settings.channel_long_days)} d`;
  return `${trim(name === "short" ? settings.channel_short_hours : settings.channel_medium_hours)} h`;
}

/** "of 14 400 asked": said when a channel had fewer bars than its length (history not stored that far back). */
export function channelShort(channel: Channel): string | null {
  if (channel.bars >= channel.asked) return null;
  const hours = channel.bars / 60;
  const have = hours >= 48 ? `${(hours / 24).toFixed(1)} d` : `${hours.toFixed(1)} h`;
  return `only ${have} of bars stored`;
}

export interface Segment {
  from: number; // epoch seconds
  to: number;
  middle: [number, number];
  half: number;
}

/**
 * Where to draw a channel.  Its bars are one-minute bars and the line is fitted to their closes,
 * so on the chart of seconds a bar stands at its last second; the line is taken on to the newest
 * second with its own slope.  On the chart of minutes a bar stands at its start, like the prices.
 */
export function segmentOf(channel: Channel | null, mode: Mode, barMs: number, newestMs: number | null): Segment | null {
  if (!channel) return null;
  if (mode === "history") {
    return { from: channel.from / 1000, to: channel.to / 1000, middle: [channel.start, channel.end], half: channel.half };
  }
  if (!channel.day) return null;
  const close = barMs - 1000;
  const from = (channel.day.from + close) / 1000;
  let to = (channel.to + close) / 1000;
  let end = channel.end;
  if (newestMs != null && newestMs / 1000 > to) {
    end += (channel.rise_per_bar * (newestMs / 1000 - to) * 1000) / barMs;
    to = newestMs / 1000;
  }
  if (to <= from) return null;
  return { from, to, middle: [channel.day.start, end], half: channel.half };
}

export interface Standing {
  where: "above" | "inside" | "below";
  /** inside: how far up between the edges, 0 at the lower and 1 at the upper */
  share: number;
  /** outside: how far beyond the edge, in pips */
  pips: number;
  lower: number;
  upper: number;
}

/** Where a price stands in a channel at the channel's newest bar. */
export function standing(channel: Channel, price: number, pipSize: number): Standing {
  const lower = channel.end - channel.half;
  const upper = channel.end + channel.half;
  const width = upper - lower;
  if (price > upper) return { where: "above", share: 1, pips: (price - upper) / pipSize, lower, upper };
  if (price < lower) return { where: "below", share: 0, pips: (lower - price) / pipSize, lower, upper };
  return { where: "inside", share: width > 0 ? (price - lower) / width : 0.5, pips: 0, lower, upper };
}

/** "64% up" (from the lower edge towards the upper one), or how far outside: "3.2 pips above". */
export function standingText(s: Standing): string {
  if (s.where === "inside") return `${Math.round(s.share * 100)}% up`;
  return `${s.pips.toFixed(1)} pips ${s.where}`;
}

/** Rise of the channel's line per hour, in pips (a bar is `barMs` long). */
export function slopePips(channel: Channel, barMs: number, pipSize: number): number {
  return (channel.rise_per_bar * 3_600_000) / barMs / pipSize;
}

// ------------------------------------------------------------------------------ the signals
/** Add a signal that arrived on the stream; one that is already there (a reconnect) is not added twice. */
export function withSignal(signals: LiveSignal[], signal: LiveSignal): LiveSignal[] {
  for (let i = signals.length - 1; i >= 0; i--) {
    const have = signals[i] as LiveSignal;
    if (have.t < signal.t) break;
    if (have.t === signal.t && have.side === signal.side) return signals;
  }
  const next = [...signals, signal];
  const before = signals[signals.length - 1];
  if (before && before.t > signal.t) next.sort((a, b) => a.t - b.t);
  return next;
}

export function countSides(signals: LiveSignal[]): Record<Side, number> {
  const out = { buy: 0, sell: 0 };
  for (const s of signals) out[s.side] += 1;
  return out;
}

/** "one every 55 s": how often the rule has signalled over the seconds of the day so far. */
export function paceText(count: number, startMs: number, newestMs: number | null): string | null {
  if (!count || newestMs == null || newestMs <= startMs) return null;
  const every = (newestMs - startMs) / 1000 / count;
  if (every < 90) return `one every ${Math.round(every)} s`;
  if (every < 5400) return `one every ${Math.round(every / 60)} min`;
  return `one every ${(every / 3600).toFixed(1)} h`;
}

export interface Mark {
  time: number; // epoch seconds, a time that exists on the chart
  price: number;
  side: Side;
}

/** The time on the chart at which a signal stands: its second, or its minute on the chart of minutes. */
export function markTime(signal: LiveSignal, mode: Mode, barMs: number): number {
  return mode === "day" ? signal.t / 1000 : (Math.floor(signal.t / barMs) * barMs) / 1000;
}

/** Where the triangles go: at the time of each signal, at the bid of that second. */
export function marksOf(signals: LiveSignal[], mode: Mode, barMs: number): Mark[] {
  return signals.map((s) => ({ time: markTime(s, mode, barMs), price: s.bid, side: s.side }));
}

/** What the rule read at a signal, in words: the indicator values next to the levels they met. */
export function reasonText(signal: LiveSignal, settings: LiveSettings, precision: number): string {
  const buy = signal.side === "buy";
  const level = buy ? settings.rsi_buy_below : settings.rsi_sell_above;
  const band = buy ? signal.lower : signal.upper;
  // which condition held is the backend's word: the rounded values shown here can sit on a level
  const rsiMet = signal.by !== "band";
  const bandMet = signal.by !== "rsi";
  const parts = [
    `RSI ${signal.rsi.toFixed(1)} ${rsiMet ? (buy ? "≤" : "≥") : buy ? ">" : "<"} ${level}`,
    `bid ${bandMet ? (buy ? "≤" : "≥") : buy ? ">" : "<"} ${buy ? "lower" : "upper"} band ${band.toFixed(precision)}`,
  ];
  return parts.join(", ");
}

// ----------------------------------------------------------------------------- the settings
export const RULE_TEXT: Record<Rule, string> = {
  both: "RSI and Bollinger together",
  either: "RSI or Bollinger, either one",
};

export type NumberField = Exclude<keyof LiveSettings, "rule" | "bar_seconds">;

export const FIELDS: { key: NumberField; label: string; unit?: string; whole?: boolean; group: "rsi" | "bollinger" | "channels" }[] = [
  { key: "rsi_period", label: "Period", unit: "bars", whole: true, group: "rsi" },
  { key: "rsi_buy_below", label: "Buy at or below", group: "rsi" },
  { key: "rsi_sell_above", label: "Sell at or above", group: "rsi" },
  { key: "bollinger_period", label: "Period", unit: "bars", whole: true, group: "bollinger" },
  { key: "bollinger_width", label: "Width", unit: "σ", group: "bollinger" },
  { key: "channel_short_hours", label: "Short", unit: "hours", group: "channels" },
  { key: "channel_medium_hours", label: "Medium", unit: "hours", group: "channels" },
  { key: "channel_long_days", label: "Long", unit: "days", group: "channels" },
  { key: "channel_width", label: "Width", unit: "σ", group: "channels" },
];

/** The form as typed: every number is a text until it is sent. */
export type Draft = Record<NumberField, string> & { rule: Rule; bar_seconds: number };

export function draftOf(settings: LiveSettings): Draft {
  const out = { rule: settings.rule, bar_seconds: settings.bar_seconds } as Draft;
  for (const { key } of FIELDS) out[key] = String(settings[key]);
  return out;
}

/** The furthest one of `period` prices can lie from their average, in their standard deviations. */
export const bandReach = (period: number) => Math.sqrt(period - 1);

/** The settings a draft stands for, or what is wrong with it, field by field. */
export function settingsOf(draft: Draft, limits: LiveForm["limits"]): { settings: LiveSettings | null; errors: Partial<Record<NumberField, string>> } {
  const errors: Partial<Record<NumberField, string>> = {};
  const values = { rule: draft.rule, bar_seconds: draft.bar_seconds } as LiveSettings;
  for (const { key, whole } of FIELDS) {
    const text = draft[key].trim().replace(",", ".");
    const value = Number(text);
    const [low, high] = limits[key] ?? [-Infinity, Infinity];
    if (text === "" || !Number.isFinite(value)) errors[key] = "must be a number";
    else if (whole && !Number.isInteger(value)) errors[key] = "must be a whole number";
    else if (value < low || value > high) errors[key] = `must be ${low} to ${high}`;
    else values[key] = value;
  }
  if (!errors.rsi_buy_below && !errors.rsi_sell_above && values.rsi_buy_below >= values.rsi_sell_above) {
    errors.rsi_sell_above = "must be above the buy level";
  }
  // among n prices none lies further from their average than the square root of (n - 1) of their
  // standard deviations: bands as wide as that could never be met
  if (!errors.bollinger_period && !errors.bollinger_width && values.bollinger_width >= bandReach(values.bollinger_period)) {
    errors.bollinger_width = `must be below ${bandReach(values.bollinger_period).toFixed(2)} for this period`;
  }
  const short = values.channel_short_hours;
  const medium = values.channel_medium_hours;
  const long = values.channel_long_days * 24;
  if (!errors.channel_short_hours && !errors.channel_medium_hours && short >= medium) errors.channel_medium_hours = "must be longer than short";
  if (!errors.channel_medium_hours && !errors.channel_long_days && medium >= long) errors.channel_long_days = "must be longer than medium";
  return { settings: Object.keys(errors).length ? null : values, errors };
}

export function sameSettings(a: LiveSettings, b: LiveSettings): boolean {
  return (Object.keys(a) as (keyof LiveSettings)[]).every((key) => a[key] === b[key]);
}

/** The rule in one line, for the head of the page. */
export function ruleLine(settings: LiveSettings): string {
  const bars = settings.bar_seconds === 1 ? "1 s bars" : `${settings.bar_seconds} s bars`;
  const joint = settings.rule === "both" ? "and" : "or";
  return `RSI ${settings.rsi_period} (${settings.rsi_buy_below}/${settings.rsi_sell_above}) ${joint} Bollinger ${settings.bollinger_period} (${settings.bollinger_width}σ), ${bars}`;
}
