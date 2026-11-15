// The signal board: shapes of the backend payloads (backend/fxlab/research/board.py) and the pure
// functions between them and the screen.  No React, no network.

import type { HistoryInfo } from "../lab/model";

export interface Part {
  trades: number;
  wins: number;
  losses: number;
  net: number;
}

export interface Tally extends Part {
  profit_factor: number | null;
  largest_loss: number | null;
  max_drawdown: number;
  max_units: number;
  days_traded: number;
  days_won: number;
  days_lost: number;
  best_day: number | null;
  worst_day: number | null;
  daily: { trades: number[]; wins: number[]; losses: number[]; pnl: number[] };
  in_sample?: Part; // the earlier part of the period
  out_of_sample?: Part; // the later part
}

export type Variant = "all" | "one";

/** A method: one strategy, with its default parameters, reading bars of one length. */
export interface Combo {
  id: string; // "rsi_reversion@5m"
  strategy: string;
  title: string;
  short: string;
  family: string;
  timeframe: string;
  signals: number;
  all?: Tally; // adding to positions as the rules allow
  one: Tally; // one clip only
}

export interface BoardResult {
  request: {
    symbol: string;
    start: string;
    end: string;
    base_timeframe: string;
    timeframes: string[];
    strategies: string[];
    execution: {
      max_clips: number;
      clip_units: number;
      spread_pips: number;
      target_pips: number;
      stop_pips: number;
      add_step_pips: number;
      min_potential_pips: number;
    };
    split: string;
  };
  currency: string;
  unit_currency: string;
  pip_value: number;
  data: { base_bars: number; trading_days: number; first_day: string; last_day: string; run_in_from?: string | null };
  days: string[];
  day_spans: [number, number][]; // when the first and the last bar of each trading day began, ms
  split_day: string | null;
  variants: Variant[];
  combos: Combo[];
  warnings: string[];
}

export interface Run {
  id: number;
  status: "running" | "done" | "failed";
  started_ms: number;
  finished_ms: number | null;
  done: number;
  total: number | null;
  label: string;
  error: string | null;
}

export interface BoardState {
  run: Run | null;
  result: { id: number; finished_ms: number; size: number } | null;
}

export interface BoardBody {
  symbol: string;
  start: string;
  end: string;
  base_timeframe: string;
}

export interface WindowBody extends BoardBody {
  chart_timeframe: string;
}

export interface Signal {
  method: string;
  t: number; // when the signal was known: the close of its bar, ms
  d: 1 | -1;
  potential: number | null; // pips the signal saw, after the spread
  /** how the trade ended, or why there was none */
  status: string;
  pnl?: number;
  clips?: number;
  entry_ms?: number;
  entry_price?: number;
  exit_ms?: number;
  exit_price?: number;
}

export interface Method {
  id: string;
  strategy: string;
  short: string;
  title: string;
  timeframe: string;
  family?: string;
}

export interface WindowResult {
  request: { symbol: string; start: string; end: string; chart_timeframe: string };
  currency: string;
  run_in_from: string;
  methods: Method[];
  bars: { timeframe: string; seconds: number; t: number[]; o: number[]; h: number[]; l: number[]; c: number[] };
  signals: Signal[];
}

const DAY_MS = 86_400_000;
const isoDay = (ms: number) => new Date(ms).toISOString().slice(0, 10);
const SECONDS: Record<string, number> = { "1s": 1, "5s": 5, "15s": 15, "30s": 30, "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600 };

/** How a method is called in a table or on a chart: "RSI 5m". */
export const methodLabel = (method: { short: string; timeframe: string }) => `${method.short} ${method.timeframe}`;

// --- the period ------------------------------------------------------------------------------

export const PRESETS = [
  { key: "week", label: "Last week", days: 7 },
  { key: "month", label: "Last month", days: 30 },
  { key: "quarter", label: "Last 3 months", days: 91 },
  { key: "year", label: "Last 12 months", days: 365 },
] as const;
export type Preset = (typeof PRESETS)[number]["key"];

/** First and last day of a preset, counted back from the last stored day and never before the first. */
export function presetPeriod(history: HistoryInfo, preset: Preset): { from: string; to: string } {
  const first = history.first.slice(0, 10);
  const to = history.last.slice(0, 10);
  const days = PRESETS.find((p) => p.key === preset)?.days ?? 30;
  const from = isoDay(Date.parse(`${to}T00:00:00Z`) - (days - 1) * DAY_MS);
  return { from: from < first ? first : from, to };
}

/** The request for a board over a period (both days included), or the reason it cannot be sent. */
export function boardBody(symbol: string, base: string, from: string, to: string): { body: BoardBody } | { error: string } {
  const start = Date.parse(`${from}T00:00:00Z`);
  const end = Date.parse(`${to}T00:00:00Z`);
  if (Number.isNaN(start) || Number.isNaN(end)) return { error: "Enter the first and the last day of the period." };
  if (end < start) return { error: "The last day is before the first day." };
  return {
    body: { symbol, start: `${from}T00:00:00Z`, end: new Date(end + DAY_MS).toISOString(), base_timeframe: base },
  };
}

// --- the scoreboard ----------------------------------------------------------------------------

export interface Line {
  combo: Combo;
  tally: Tally;
  /** share of the trades that made money; null without trades */
  worked: number | null;
  earlier: number | null; // net result on the earlier part of the period
  later: number | null; // ... and on the later part
}

export type SortKey = "net" | "later" | "worked" | "trades";

/** The tally a combo shows for a variant: a board run without adding has only "one". */
export function tallyOf(combo: Combo, variant: Variant): Tally {
  return (variant === "all" ? combo.all : undefined) ?? combo.one;
}

const isBenchmark = (combo: Combo) => combo.family === "benchmark";

/** The methods as lines of the scoreboard, best first.  Methods without a trade go to the end. */
export function rank(combos: Combo[], variant: Variant, sort: SortKey, timeframes: ReadonlySet<string> | null = null): Line[] {
  const lines = combos
    .filter((c) => timeframes == null || timeframes.has(c.timeframe))
    .map((combo) => {
      const tally = tallyOf(combo, variant);
      return {
        combo,
        tally,
        worked: tally.trades ? tally.wins / tally.trades : null,
        earlier: tally.in_sample?.net ?? null,
        later: tally.out_of_sample?.net ?? null,
      };
    });
  const value = (line: Line): number => {
    if (line.tally.trades === 0) return Number.NEGATIVE_INFINITY;
    if (sort === "later") return line.later ?? Number.NEGATIVE_INFINITY;
    if (sort === "worked") return line.worked ?? Number.NEGATIVE_INFINITY;
    if (sort === "trades") return line.tally.trades;
    return line.tally.net;
  };
  return lines.sort((a, b) => value(b) - value(a) || a.combo.id.localeCompare(b.combo.id));
}

export interface Headline {
  methods: number; // methods that are not the benchmark
  traded: number; // ... that made at least one trade
  positive: number; // ... that ended above zero
  /** ... that ended above zero on the earlier and on the later part alike; null without a split */
  both: number | null;
  /** the best result among the random-entry lines; null when there is none */
  random: number | null;
  /** methods that ended above the best random-entry line */
  aboveRandom: number | null;
}

/** What the board amounts to before any single line is looked at. */
export function headline(lines: Line[]): Headline {
  const real = lines.filter((line) => !isBenchmark(line.combo));
  const randoms = lines.filter((line) => isBenchmark(line.combo) && line.tally.trades > 0).map((line) => line.tally.net);
  const random = randoms.length ? Math.max(...randoms) : null;
  const split = real.some((line) => line.earlier != null);
  return {
    methods: real.length,
    traded: real.filter((line) => line.tally.trades > 0).length,
    positive: real.filter((line) => line.tally.net > 0).length,
    both: split ? real.filter((line) => (line.earlier ?? 0) > 0 && (line.later ?? 0) > 0).length : null,
    random,
    aboveRandom: random == null ? null : real.filter((line) => line.tally.trades > 0 && line.tally.net > random).length,
  };
}

/**
 * The methods shown in the day table when nobody has chosen yet: the best few by net result,
 * and the random entries on the same bars as the best one, to measure them against.
 */
export function defaultPicks(combos: Combo[], variant: Variant, count = 3): string[] {
  const real = rank(
    combos.filter((c) => !isBenchmark(c)),
    variant,
    "net",
  ).filter((line) => line.tally.trades > 0);
  const picks = real.slice(0, count).map((line) => line.combo.id);
  const top = real[0]?.combo.timeframe;
  const benchmark = combos.find((c) => isBenchmark(c) && c.timeframe === top) ?? combos.find(isBenchmark);
  if (benchmark && !picks.includes(benchmark.id)) picks.push(benchmark.id);
  return picks;
}

// --- the days ----------------------------------------------------------------------------------

export interface DayCell {
  trades: number;
  wins: number;
  losses: number;
  pnl: number;
}

export interface DayRow {
  day: string;
  index: number; // position in the board's list of days
  later: boolean; // in the later (out-of-sample) part of the period
  cells: DayCell[]; // one per chosen method, in the order given
  /** index into ``cells`` of the chosen method that made the most that day; null when none made money */
  best: number | null;
  /** the method that made the most that day among all on the board; null when none made money */
  top: { id: string; label: string; pnl: number } | null;
  /** how many of the board's methods made money that day, and how many lost */
  up: number;
  down: number;
}

/**
 * One row per trading day, newest first: a cell for each chosen method, and what the best of all
 * methods on the board (``timeframes`` narrows them; the benchmark is left out) made that day.
 */
export function dayRows(
  result: BoardResult,
  variant: Variant,
  ids: string[],
  timeframes: ReadonlySet<string> | null = null,
): DayRow[] {
  const chosen = ids
    .map((id) => result.combos.find((c) => c.id === id))
    .filter((c): c is Combo => c != null)
    .map((c) => tallyOf(c, variant));
  const field = result.combos
    .filter((c) => !isBenchmark(c) && (timeframes == null || timeframes.has(c.timeframe)))
    .map((c) => ({ combo: c, pnl: tallyOf(c, variant).daily.pnl }));
  const rows: DayRow[] = result.days.map((day, index) => {
    const cells = chosen.map((t) => ({
      trades: t.daily.trades[index] ?? 0,
      wins: t.daily.wins[index] ?? 0,
      losses: t.daily.losses[index] ?? 0,
      pnl: t.daily.pnl[index] ?? 0,
    }));
    let best: number | null = null;
    cells.forEach((cell, k) => {
      if (cell.pnl > 0 && (best == null || cell.pnl > (cells[best]?.pnl ?? 0))) best = k;
    });
    let top: DayRow["top"] = null;
    let up = 0;
    let down = 0;
    for (const { combo, pnl } of field) {
      const made = pnl[index] ?? 0;
      if (made > 0) up += 1;
      else if (made < 0) down += 1;
      if (made > 0 && (top == null || made > top.pnl)) top = { id: combo.id, label: methodLabel(combo), pnl: made };
    }
    return { day, index, later: result.split_day != null && day >= result.split_day, cells, best, top, up, down };
  });
  return rows.reverse();
}

export interface Follow {
  days: number; // days on which the method that was best the day before could be followed
  won: number; // ... and it made money
  lost: number; // ... and it lost
  idle: number; // ... and it did not trade, or came out at zero
  net: number; // what following it made in all
  /** of all days on which any method traded, the share that made money: what "won" is measured against */
  baseline: number | null;
}

/**
 * The simplest way to use a table of daily winners, tried out: each day trade the method that made
 * the most the day before.  If yesterday's winner is no better than any method on any day, the
 * table shows luck moving around, not skill.
 */
export function followTheWinner(combos: Combo[], variant: Variant, timeframes: ReadonlySet<string> | null = null): Follow | null {
  const field = combos
    .filter((c) => !isBenchmark(c) && (timeframes == null || timeframes.has(c.timeframe)))
    .map((c) => tallyOf(c, variant).daily);
  const days = field[0]?.pnl.length ?? 0;
  if (days < 3) return null;
  const out: Follow = { days: 0, won: 0, lost: 0, idle: 0, net: 0, baseline: null };
  for (let day = 0; day + 1 < days; day += 1) {
    let best = -1;
    let most = 0;
    field.forEach((daily, k) => {
      const made = daily.pnl[day] ?? 0;
      if (made > most) {
        most = made;
        best = k;
      }
    });
    const winner = field[best];
    if (!winner) continue;
    const next = winner.pnl[day + 1] ?? 0;
    out.days += 1;
    out.net += next;
    if (next > 0) out.won += 1;
    else if (next < 0) out.lost += 1;
    else out.idle += 1;
  }
  let traded = 0;
  let made = 0;
  for (const daily of field) {
    daily.trades.forEach((count, day) => {
      if (count > 0) {
        traded += 1;
        if ((daily.pnl[day] ?? 0) > 0) made += 1;
      }
    });
  }
  out.net = Math.round(out.net * 100) / 100;
  out.baseline = traded ? made / traded : null;
  return out.days ? out : null;
}

// --- the signals of a window -------------------------------------------------------------------

export const WINDOWS = [
  { key: "day", label: "1 day", days: 1, chart: "1m" },
  { key: "week", label: "1 week", days: 5, chart: "5m" },
  { key: "fortnight", label: "2 weeks", days: 10, chart: "15m" },
  { key: "month", label: "1 month", days: 22, chart: "30m" },
] as const;
export type WindowKey = (typeof WINDOWS)[number]["key"];

/**
 * The request for the signals of the ``length`` trading days that end with ``day``.  The chart
 * under them gets bars that keep a window readable, never finer than the bars trades are simulated on.
 */
export function windowBody(result: BoardResult, day: string, length: WindowKey): { body: WindowBody; from: string } | null {
  const last = result.days.indexOf(day);
  const choice = WINDOWS.find((w) => w.key === length);
  if (last < 0 || !choice) return null;
  const first = Math.max(0, last - choice.days + 1);
  const startSpan = result.day_spans[first];
  const endSpan = result.day_spans[last];
  if (!startSpan || !endSpan) return null;
  const base = result.request.base_timeframe;
  const baseSeconds = SECONDS[base] ?? 60;
  return {
    from: result.days[first] ?? day,
    body: {
      symbol: result.request.symbol,
      start: new Date(startSpan[0]).toISOString(),
      end: new Date(endSpan[1] + baseSeconds * 1000).toISOString(),
      base_timeframe: base,
      chart_timeframe: (SECONDS[choice.chart] ?? 60) >= baseSeconds ? choice.chart : base,
    },
  };
}

export const TRADED: ReadonlySet<string> = new Set(["target", "stop", "time", "end_of_day", "end_of_data"]);

export const STATUS_TEXT: Record<string, string> = {
  target: "took its profit",
  stop: "was stopped out",
  time: "closed after the longest holding time",
  end_of_day: "closed at the end of the day",
  end_of_data: "still open where the data ends",
  potential: "not traded: too little potential",
  busy: "not traded: this method had a position open",
  hours: "not traded: outside the entry hours",
  day_end: "not traded: too close to the end of the day",
  no_price: "not traded: no price after the signal",
  off: "not traded: this direction is switched off",
};

export type Outcome = "won" | "lost" | "flat" | "none";

/** What became of a signal: its trade made money, lost, came out at zero, or there was no trade. */
export function outcomeOf(signal: Signal): Outcome {
  if (signal.pnl == null) return "none";
  return signal.pnl > 0 ? "won" : signal.pnl < 0 ? "lost" : "flat";
}

export const OUTCOME_SIGN: Record<Outcome, string> = { won: "✓", lost: "✗", flat: "=", none: "" };

/** Which signals a reader has asked to see: of which bar lengths, and whether those not traded too. */
export function visibleSignals(
  signals: Signal[],
  methods: Map<string, Method>,
  timeframes: ReadonlySet<string> | null,
  untraded: boolean,
): Signal[] {
  return signals.filter((s) => {
    if (!untraded && !TRADED.has(s.status)) return false;
    const method = methods.get(s.method);
    return method != null && (timeframes == null || timeframes.has(method.timeframe));
  });
}

export interface Lane {
  strategy: string;
  short: string;
  title: string;
}

const FAMILIES = ["reversion", "trend", "breakout", "level", "time", "benchmark"];

/** One lane per strategy, kindred strategies next to each other, the benchmark last. */
export function lanesOf(methods: Method[]): Lane[] {
  const seen = new Map<string, Lane & { family: number; order: number }>();
  methods.forEach((m, order) => {
    if (seen.has(m.strategy)) return;
    const family = FAMILIES.indexOf(m.family ?? "");
    seen.set(m.strategy, { strategy: m.strategy, short: m.short, title: m.title, family: family < 0 ? FAMILIES.length : family, order });
  });
  return [...seen.values()]
    .sort((a, b) => a.family - b.family || a.order - b.order)
    .map(({ strategy, short, title }) => ({ strategy, short, title }));
}

export interface LaneMark {
  time: number; // seconds: the chart bar in which the signals became known
  lane: number; // index into the lanes
  direction: 1 | -1;
  /** of the trades these signals led to, taken together; "none" when none was traded */
  outcome: Outcome;
  signals: Signal[];
}

/** The chart bar a moment falls in, or the first one after it when there is a hole; null beyond the last bar. */
export function barAt(times: number[], seconds: number, ms: number): number | null {
  const wanted = Math.floor(ms / 1000 / seconds) * seconds;
  let low = 0;
  let high = times.length;
  while (low < high) {
    const mid = (low + high) >> 1;
    if ((times[mid] ?? 0) < wanted) low = mid + 1;
    else high = mid;
  }
  return times[low] ?? null;
}

/**
 * Marks for the lanes under a chart: the signals of one strategy and one direction that became known
 * in the same chart bar share a mark.  A signal known when its bar closed sits on the chart bar that
 * starts at that moment: the first one in which it could be acted on.  Sorted by time.
 */
export function laneMarks(
  signals: Signal[],
  methods: Map<string, Method>,
  lanes: Lane[],
  times: number[], // start of each chart bar, seconds, ascending
  seconds: number,
): LaneMark[] {
  const laneOf = new Map(lanes.map((lane, index) => [lane.strategy, index]));
  const groups = new Map<string, LaneMark>();
  for (const signal of signals) {
    const lane = laneOf.get(methods.get(signal.method)?.strategy ?? "");
    const time = barAt(times, seconds, signal.t);
    if (lane == null || time == null) continue;
    const key = `${lane}:${time}:${signal.d}`;
    let mark = groups.get(key);
    if (!mark) {
      mark = { time, lane, direction: signal.d, outcome: "none", signals: [] };
      groups.set(key, mark);
    }
    mark.signals.push(signal);
  }
  const marks = [...groups.values()];
  for (const mark of marks) {
    const traded = mark.signals.filter((s) => s.pnl != null);
    if (traded.length) {
      const net = traded.reduce((sum, s) => sum + (s.pnl ?? 0), 0);
      mark.outcome = net > 0 ? "won" : net < 0 ? "lost" : "flat";
    }
  }
  return marks.sort((a, b) => a.time - b.time || a.lane - b.lane || b.direction - a.direction);
}

/** The signals by the chart bar they became known in, traded ones first, for the read-out of a bar. */
export function signalsByBar(signals: Signal[], times: number[], seconds: number): Map<number, Signal[]> {
  const out = new Map<number, Signal[]>();
  for (const signal of signals) {
    const time = barAt(times, seconds, signal.t);
    if (time == null) continue;
    const list = out.get(time);
    if (list) list.push(signal);
    else out.set(time, [signal]);
  }
  for (const list of out.values()) {
    list.sort((a, b) => Number(TRADED.has(b.status)) - Number(TRADED.has(a.status)) || a.t - b.t || a.method.localeCompare(b.method));
  }
  return out;
}

export interface Count {
  signals: number;
  traded: number;
  won: number;
  lost: number;
  net: number;
}

/** How many signals there are, how many were traded, and what the trades made. */
export function countSignals(signals: Signal[]): Count {
  const out: Count = { signals: signals.length, traded: 0, won: 0, lost: 0, net: 0 };
  for (const signal of signals) {
    if (signal.pnl == null) continue;
    out.traded += 1;
    out.net += signal.pnl;
    if (signal.pnl > 0) out.won += 1;
    else if (signal.pnl < 0) out.lost += 1;
  }
  out.net = Math.round(out.net * 100) / 100;
  return out;
}
