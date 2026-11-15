// The strategy lab: shapes of the backend payloads (backend/fxlab/research/report.py) and the
// pure functions between the form, the request and the charts.  No React, no network.

export interface ParamInfo {
  name: string;
  label: string;
  default: number | string;
  kind: "int" | "float" | "choice";
  minimum: number | null;
  maximum: number | null;
  choices: string[];
  help: string;
}

export interface StrategyInfo {
  key: string;
  title: string;
  summary: string;
  timeframe: string;
  params: ParamInfo[];
  short?: string; // a few letters for charts and tables: "RSI"
  family?: string; // reversion, trend, breakout, level, time, benchmark
  style?: string; // the kind of signal it gives: momentum, reversion or other
}

export interface HistoryInfo {
  timeframe: string;
  seconds: number;
  bars: number;
  first: string; // ISO-8601, UTC
  last: string;
}

export interface Execution {
  pip_size: number;
  spread_pips: number;
  slippage_pips: number;
  commission_per_million: number;
  clip_units: number;
  max_clips: number;
  add_step_pips: number;
  target_pips: number;
  stop_pips: number;
  min_potential_pips: number;
  reaction_delay_s: number;
  max_wait_s: number;
  max_hold_minutes: number;
  cooldown_minutes: number;
  entry_start_utc: string;
  entry_end_utc: string;
  flat_time: string;
  day_end: string;
  session_tz: string;
  long: boolean;
  short: boolean;
}

export interface LabInfo {
  symbol: string;
  currency: string; // money is in this currency (the quote currency of the pair)
  unit_currency: string; // position sizes are in this one (the base currency)
  pip_size: number;
  price_precision: number;
  session_tz: string;
  timeframes: string[];
  strategies: StrategyInfo[];
  execution: Execution;
  /** what can be said about a trade from what the market was doing around it */
  conditions?: { key: string; label: string; when: "entry" | "after" }[];
  history: HistoryInfo[];
}

export interface Group {
  trades: number;
  wins: number;
  pnl: number;
  win_rate: number;
}

export interface Summary {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  net_pnl: number;
  net_pips: number;
  gross_profit: number;
  gross_loss: number;
  profit_factor: number | null;
  expectancy: number | null;
  average_win: number | null;
  average_loss: number | null;
  largest_win: number | null;
  largest_loss: number | null;
  max_drawdown: number;
  longest_losing_run: number;
  max_units: number;
  average_clips: number | null;
  trades_with_adds: number;
  average_minutes: number | null;
  worst_adverse_pips: number | null;
  by_reason: Record<string, Group>;
  by_direction: Record<string, Group>;
  by_entry_hour_utc: Record<string, Group>;
  days: number;
  days_traded: number;
  days_positive: number;
  days_negative: number;
  best_day: number | null;
  worst_day: number | null;
  average_day: number | null;
  daily_sharpe: number | null;
}

export type ExitReason = "target" | "stop" | "time" | "end_of_day" | "end_of_data";

export interface Trade {
  n: number;
  day: string;
  direction: "long" | "short";
  signal_ms: number;
  entry_ms: number;
  entry_price: number;
  clips: number;
  units: number;
  average_price: number;
  exit_ms: number;
  exit_price: number;
  reason: ExitReason;
  stop_price: number;
  target_price: number;
  pnl: number;
  pips: number;
  potential_pips: number;
  adverse_pips: number;
  minutes: number;
  entry_hour_utc: number;
  fills: [number, number, number][]; // time ms, price, units
}

export interface Day {
  day: string; // trading day, YYYY-MM-DD
  trades: number;
  wins: number;
  pnl: number;
  pips: number;
  cumulative: number;
  max_units: number;
  first_bar_ms?: number;
  last_bar_ms?: number;
}

export interface Part {
  first_day: string | null;
  last_day: string | null;
  summary: Summary;
}

export interface Report {
  request: {
    symbol: string;
    strategy: string;
    strategy_title: string;
    start: string;
    end: string;
    timeframe: string;
    base_timeframe: string;
    params: Record<string, number | string>;
    execution: Execution;
    split: string;
  };
  currency: string;
  unit_currency: string;
  pip_value: number;
  data: {
    source: string;
    price_basis: string;
    base_timeframe: string;
    signal_timeframe: string;
    base_bars: number;
    signal_bars: number;
    first_bar: string;
    last_bar: string;
    trading_days: number;
    first_day: string;
    last_day: string;
    holes_over_30_minutes: number;
    longest_hole_minutes: number;
    /** where the bars before the period begin, on which the indicators settle; null when there are none */
    run_in_from?: string | null;
  };
  signals: Record<string, number>;
  summary: Summary;
  daily: Day[];
  equity: [number, number][];
  trades: Trade[];
  split_day: string | null;
  in_sample?: Part;
  out_of_sample?: Part;
  without_adding?: { summary: Summary; daily: Day[] };
  warnings: string[];
}

// --- the form ------------------------------------------------------------------------------

/** Trading assumptions a person may change for a run, in the order the form shows them. */
export const EXECUTION_FIELDS = [
  "spread_pips",
  "min_potential_pips",
  "clip_units",
  "max_clips",
  "add_step_pips",
  "target_pips",
  "stop_pips",
  "max_hold_minutes",
  "cooldown_minutes",
  "reaction_delay_s",
  "slippage_pips",
  "commission_per_million",
  "entry_start_utc",
  "entry_end_utc",
  "flat_time",
] as const;
export type ExecutionField = (typeof EXECUTION_FIELDS)[number];

export interface Form {
  strategy: string;
  timeframe: string;
  base: string;
  from: string; // YYYY-MM-DD, first day (UTC)
  to: string; // YYYY-MM-DD, last day (UTC), inclusive
  split: "auto" | "none" | "date";
  splitDate: string;
  /** what was typed, per strategy and parameter (kept when switching strategies) */
  params: Record<string, Record<string, string>>;
  execution: Record<ExecutionField, string>;
  long: boolean;
  short: boolean;
}

const DAY_MS = 86_400_000;

export const isoDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);

/** Bars the trades are simulated on: one minute if stored, otherwise the finest stored of a minute or more. */
export function defaultBase(info: LabInfo): string {
  const usable = info.history.filter((h) => h.seconds <= 3600);
  if (usable.some((h) => h.timeframe === "1m")) return "1m";
  const coarse = usable.filter((h) => h.seconds >= 60);
  return (coarse[0] ?? usable[usable.length - 1])?.timeframe ?? "1m";
}

export function stored(info: LabInfo, timeframe: string): HistoryInfo | null {
  return info.history.find((h) => h.timeframe === timeframe) ?? null;
}

/** The whole stored period of a timeframe as first and last day, or null when nothing is stored. */
export function storedPeriod(info: LabInfo, timeframe: string): { from: string; to: string } | null {
  const h = stored(info, timeframe);
  return h ? { from: h.first.slice(0, 10), to: h.last.slice(0, 10) } : null;
}

export function defaultForm(info: LabInfo): Form {
  const base = defaultBase(info);
  const period = storedPeriod(info, base);
  const strategy = info.strategies[0];
  const execution = {} as Record<ExecutionField, string>;
  for (const field of EXECUTION_FIELDS) execution[field] = String(info.execution[field]);
  return {
    strategy: strategy?.key ?? "",
    timeframe: strategy?.timeframe ?? "5m",
    base,
    from: period?.from ?? "",
    to: period?.to ?? "",
    split: "auto",
    splitDate: "",
    params: {},
    execution,
    long: info.execution.long,
    short: info.execution.short,
  };
}

/** What the form shows for a parameter: what was typed, or the strategy's default. */
export function paramValue(form: Form, strategy: StrategyInfo, param: ParamInfo): string {
  return form.params[strategy.key]?.[param.name] ?? String(param.default);
}

/**
 * A stored form laid over the defaults of today's server: strategies, parameters and fields that
 * no longer exist are dropped, new ones get their default.
 */
export function restoreForm(info: LabInfo, saved: unknown): Form {
  const form = defaultForm(info);
  if (!saved || typeof saved !== "object") return form;
  const old = saved as Partial<Form>;
  const text = (value: unknown): value is string => typeof value === "string";
  if (text(old.strategy) && info.strategies.some((s) => s.key === old.strategy)) form.strategy = old.strategy;
  if (text(old.timeframe) && info.timeframes.includes(old.timeframe)) form.timeframe = old.timeframe;
  if (text(old.base) && stored(info, old.base)) form.base = old.base;
  const day = /^\d{4}-\d{2}-\d{2}$/;
  if (text(old.from) && day.test(old.from)) form.from = old.from;
  if (text(old.to) && day.test(old.to)) form.to = old.to;
  if (old.split === "auto" || old.split === "none" || old.split === "date") form.split = old.split;
  if (text(old.splitDate) && day.test(old.splitDate)) form.splitDate = old.splitDate;
  if (old.params && typeof old.params === "object") {
    for (const strategy of info.strategies) {
      const typed = (old.params as Record<string, unknown>)[strategy.key];
      if (!typed || typeof typed !== "object") continue;
      const kept: Record<string, string> = {};
      for (const param of strategy.params) {
        const value = (typed as Record<string, unknown>)[param.name];
        if (text(value)) kept[param.name] = value;
      }
      form.params[strategy.key] = kept;
    }
  }
  if (old.execution && typeof old.execution === "object") {
    for (const field of EXECUTION_FIELDS) {
      const value = (old.execution as Record<string, unknown>)[field];
      if (text(value)) form.execution[field] = value;
    }
  }
  if (typeof old.long === "boolean") form.long = old.long;
  if (typeof old.short === "boolean") form.short = old.short;
  return form;
}

export interface BacktestBody {
  symbol: string;
  strategy: string;
  start: string;
  end: string;
  timeframe: string;
  base_timeframe: string;
  params: Record<string, string>;
  execution: Record<string, string | boolean>;
  split: string;
}

/** The request for a form, or the reason it cannot be sent.  The server checks every value again. */
export function buildRequest(info: LabInfo, form: Form): { body: BacktestBody } | { error: string } {
  const strategy = info.strategies.find((s) => s.key === form.strategy);
  if (!strategy) return { error: "Choose a strategy." };
  const from = Date.parse(`${form.from}T00:00:00Z`);
  const to = Date.parse(`${form.to}T00:00:00Z`);
  if (Number.isNaN(from) || Number.isNaN(to)) return { error: "Enter the first and the last day of the period." };
  if (to < from) return { error: "The last day is before the first day." };
  if (form.split === "date" && !/^\d{4}-\d{2}-\d{2}$/.test(form.splitDate)) {
    return { error: "Enter the first out-of-sample day, or choose another way to split the period." };
  }
  if (!form.long && !form.short) return { error: "Buying and selling are both switched off: nothing would be traded." };
  const params: Record<string, string> = {};
  for (const param of strategy.params) params[param.name] = paramValue(form, strategy, param).trim();
  const execution: Record<string, string | boolean> = { long: form.long, short: form.short };
  for (const field of EXECUTION_FIELDS) {
    const value = form.execution[field].trim();
    if (value === "") return { error: `Fill in "${EXECUTION_LABELS[field].label}".` };
    execution[field] = value;
  }
  return {
    body: {
      symbol: info.symbol,
      strategy: strategy.key,
      start: `${form.from}T00:00:00Z`,
      end: new Date(to + DAY_MS).toISOString(), // the last day is included
      timeframe: form.timeframe,
      base_timeframe: form.base,
      params,
      execution,
      split: form.split === "date" ? form.splitDate : form.split,
    },
  };
}

export const EXECUTION_LABELS: Record<ExecutionField, { label: string; unit: string; help: string; time?: boolean }> = {
  spread_pips: { label: "Spread", unit: "pips", help: "Charged on every round trip: a buy pays the ask, a sell gets the bid." },
  min_potential_pips: {
    label: "Minimum potential",
    unit: "pips",
    help: "A signal is taken only when it sees at least this much after the spread.",
  },
  clip_units: { label: "Clip size", unit: "", help: "Size of one order. The profit and loss figures scale with it." },
  max_clips: { label: "Clips at most", unit: "", help: "1 = never add to a position." },
  add_step_pips: {
    label: "Add every",
    unit: "pips",
    help: "Another clip each time the price has gone this far against the last one.",
  },
  target_pips: { label: "Target", unit: "pips", help: "Profit target from the average entry, after the spread." },
  stop_pips: { label: "Stop", unit: "pips", help: "Beyond the last clip that could be added. Closes the whole position." },
  max_hold_minutes: { label: "Hold at most", unit: "min", help: "A position still open after this is closed." },
  cooldown_minutes: { label: "Pause after a trade", unit: "min", help: "No new position for this long after one is closed." },
  reaction_delay_s: {
    label: "Reaction delay",
    unit: "s",
    help: "Time between the signal and the order. The order is filled at the first bar that opens after it.",
  },
  slippage_pips: { label: "Slippage", unit: "pips", help: "Extra loss on stops and timed exits. Targets are filled at their level." },
  commission_per_million: { label: "Commission", unit: "per mn, per side", help: "In the money currency." },
  entry_start_utc: { label: "Entries from", unit: "UTC", help: "Positions are opened only inside these hours.", time: true },
  entry_end_utc: { label: "Entries until", unit: "UTC", help: "Positions are opened only inside these hours.", time: true },
  flat_time: {
    label: "Flat at",
    unit: "session time",
    help: "Every position is closed at this time, and none is opened between it and the end of the trading day.",
    time: true,
  },
};

// --- numbers as text -----------------------------------------------------------------------

const MINUS = "−";

/** Money without decimals and with thousands separators: "−92,570", "1,500". */
export function money(value: number | null | undefined): string {
  if (value == null) return "—";
  const rounded = Math.round(value);
  const text = Math.abs(rounded).toLocaleString("en-US");
  return rounded < 0 ? `${MINUS}${text}` : text;
}

/** Money with a sign also when positive: "+500", "−1,000", "0". */
export function signedMoney(value: number | null | undefined): string {
  if (value == null) return "—";
  const rounded = Math.round(value);
  return rounded > 0 ? `+${money(rounded)}` : money(rounded);
}

/** Pips with a sign and one decimal: "+15.0", "−1,151.5". */
export function signedPips(value: number | null | undefined): string {
  if (value == null) return "—";
  const text = Math.abs(value).toLocaleString("en-US", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  if (Number(text.replace(/,/g, "")) === 0) return text;
  return value > 0 ? `+${text}` : `${MINUS}${text}`;
}

export function percent(value: number | null | undefined): string {
  return value == null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export function ratio(value: number | null | undefined, digits = 2): string {
  if (value == null) return "—";
  return value < 0 ? `${MINUS}${Math.abs(value).toFixed(digits)}` : value.toFixed(digits);
}

/** Position size: "1 mn", "2.5 mn", "500,000". */
export function units(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value >= 1_000_000) return `${Number((value / 1_000_000).toFixed(2))} mn`;
  return Math.round(value).toLocaleString("en-US");
}

export const REASONS: Record<ExitReason, string> = {
  target: "Target",
  stop: "Stop",
  time: "Time",
  end_of_day: "End of day",
  end_of_data: "End of data",
};

// --- charts --------------------------------------------------------------------------------

export interface Marker {
  time: number; // seconds, the start of the bar the marker sits on
  position: "aboveBar" | "belowBar";
  shape: "arrowUp" | "arrowDown" | "circle" | "square";
  kind: "entry" | "add" | "target" | "stop" | "timed";
  text: string;
  trade: number;
}

/**
 * Marks for a price chart of bars ``barSeconds`` long: an arrow where a position is opened and at
 * each clip added (up below the bar for a buy, down above it for a sell), a circle where it took
 * its profit, a square where it was stopped or closed by the clock.  Sorted by time, as the chart
 * library requires.
 */
export function tradeMarkers(trades: Trade[], barSeconds: number): Marker[] {
  const snap = (ms: number) => Math.floor(ms / 1000 / barSeconds) * barSeconds;
  const out: Marker[] = [];
  for (const trade of trades) {
    const long = trade.direction === "long";
    trade.fills.forEach(([ms], index) => {
      out.push({
        time: snap(ms),
        position: long ? "belowBar" : "aboveBar",
        shape: long ? "arrowUp" : "arrowDown",
        kind: index === 0 ? "entry" : "add",
        text: index === 0 ? (long ? "Buy" : "Sell") : `+${index}`,
        trade: trade.n,
      });
    });
    const won = trade.reason === "target";
    out.push({
      time: snap(trade.exit_ms),
      // an exit sits on the side of the bar the position leaves on: above for a sale, below for a buy-back
      position: long ? "aboveBar" : "belowBar",
      shape: won ? "circle" : "square",
      kind: won ? "target" : trade.reason === "stop" ? "stop" : "timed",
      text: `${REASONS[trade.reason]} ${signedMoney(trade.pnl)}`,
      trade: trade.n,
    });
  }
  // stable: marks of one bar keep the order entry, clips, exit
  return out
    .map((marker, index) => ({ marker, index }))
    .sort((a, b) => a.marker.time - b.marker.time || a.index - b.index)
    .map(({ marker }) => marker);
}

/**
 * The period of history to ask the provider for: everything since the last stored bar (with a day of
 * overlap, which is simply written again), or the last twelve months when nothing is stored.
 */
export function downloadRange(history: HistoryInfo | null, now: number): { start: number; end: number } {
  const start = history ? Date.parse(history.last) - DAY_MS : now - 365 * DAY_MS;
  return { start: Math.max(start, now - 365 * DAY_MS), end: now };
}

/** The twelve months before the first stored bar (with a day of overlap). */
export function olderRange(history: HistoryInfo): { start: number; end: number } {
  const first = Date.parse(history.first);
  return { start: first - 365 * DAY_MS, end: first + DAY_MS };
}

/** The longest stretch one download may cover: the server takes a year of minute bars per job. */
export const DOWNLOAD_LIMIT_MS = 365 * DAY_MS;

export interface Missing {
  start: number;
  end: number;
  /** first and last day of the stretch, for the button that downloads it */
  from: string;
  to: string;
}

/**
 * The parts of a chosen period (first and last day, both included) that lie outside the stored
 * history: before its first bar, after its last one, or all of it when nothing is stored.  Nothing
 * after ``now`` is asked for, a part longer than one download may cover is cut to the stretch next
 * to what is stored, and Saturdays and Sundays at the edges of a part are not counted as missing:
 * the market is closed for nearly all of them, and "Update to now" fetches the Sunday evening.
 */
export function missingRanges(history: HistoryInfo | null, from: string, to: string, now: number): Missing[] {
  const start = Date.parse(`${from}T00:00:00Z`);
  const end = Math.min(Date.parse(`${to}T00:00:00Z`) + DAY_MS, now);
  if (Number.isNaN(start) || Number.isNaN(end) || end <= start) return [];
  const weekend = (ms: number) => [0, 6].includes(new Date(ms).getUTCDay());
  const part = (a: number, b: number): Missing | null => {
    while (a < b && weekend(a)) a += DAY_MS;
    while (b - DAY_MS >= a && weekend(b - 1)) b -= b % DAY_MS || DAY_MS; // back to the start of that day
    return a < b ? { start: a, end: b, from: isoDay(a), to: isoDay(b - 1) } : null;
  };
  const parts: (Missing | null)[] = [];
  if (!history) {
    parts.push(part(Math.max(start, end - DOWNLOAD_LIMIT_MS), end));
  } else {
    // whole days: a period that begins on the first stored day is not missing its first hours
    const first = Date.parse(`${history.first.slice(0, 10)}T00:00:00Z`);
    const last = Date.parse(`${history.last.slice(0, 10)}T00:00:00Z`) + DAY_MS;
    if (start < first) {
      const until = Math.min(end, first);
      parts.push(part(Math.max(start, until - DOWNLOAD_LIMIT_MS), until));
    }
    if (end > last) {
      const since = Math.max(start, last);
      parts.push(part(since, Math.min(end, since + DOWNLOAD_LIMIT_MS)));
    }
  }
  return parts.filter((p): p is Missing => p != null);
}

/** The time range of bars to load for a trading day, with a margin so the first and last trade are not at the edge. */
export function dayRange(day: Day, trades: Trade[], barSeconds: number): { start: number; end: number } | null {
  if (day.first_bar_ms == null || day.last_bar_ms == null) return null;
  let start = day.first_bar_ms;
  let end = day.last_bar_ms + barSeconds * 1000;
  for (const trade of trades) {
    start = Math.min(start, trade.signal_ms - 3_600_000);
    end = Math.max(end, trade.exit_ms + 3_600_000);
  }
  return { start, end };
}

/** Days as the chart library wants them for a daily series. */
export function equitySeries(days: Day[]): { time: string; value: number }[] {
  return days.map((d) => ({ time: d.day, value: d.cumulative }));
}
