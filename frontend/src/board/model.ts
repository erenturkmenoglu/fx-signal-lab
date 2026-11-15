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
  average_win: number | null;
  average_loss: number | null;
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

/** What kind of signal a method gives: with a move, against a stretched one, or neither. */
export type Kind = "momentum" | "reversion" | "other";

export const KINDS: { key: Kind; label: string; short: string }[] = [
  { key: "momentum", label: "Momentum", short: "Momentum" },
  { key: "reversion", label: "Mean reversion", short: "Mean rev." },
  { key: "other", label: "Other", short: "Other" },
];

/** The kind of a method as the server classed it; anything it did not class is "other". */
export const kindOf = (method: { style?: string }): Kind =>
  method.style === "momentum" || method.style === "reversion" ? method.style : "other";

/** A method: one strategy, with its default parameters, reading bars of one length. */
export interface Combo {
  id: string; // "rsi_reversion@5m"
  strategy: string;
  title: string;
  short: string;
  family: string;
  style?: string; // momentum, reversion or other
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
  /** false: the single signals are left out (a month of them is megabytes); what they add up to stays */
  list_signals: boolean;
}

export interface Method {
  id: string;
  strategy: string;
  short: string;
  title: string;
  timeframe: string;
  family?: string;
  style?: string;
}

/** What the methods said together, one value for each bar of the chart: what was known when that bar closed. */
export interface WindowContext {
  voters: number; // how many momentum methods there are
  threshold: number; // this many more pointing one way than the other make the state that way
  votes: (number | null)[]; // momentum methods pointing up minus those pointing down
  state: number[]; // +1 up, -1 down, 0 not decided
  /** where the state went from up to down or back, with the reading and the price of that moment */
  changes: { t: number; to: number; bar: number; votes: number; price: number }[];
  reversion: { buys: number[]; sells: number[] }; // mean-reversion signals that came in each bar
  channel: {
    hours: number;
    width: number;
    // the channel as it was known at each bar: fitted to the hours before it
    middle: (number | null)[];
    upper: (number | null)[];
    lower: (number | null)[];
    /**
     * The channel of the whole window as it stands at its end: one straight line through the closes
     * (``start`` at the first bar, ``end`` at the last) and ``half`` to either edge.  Missing for a
     * window too short for a line.
     */
    straight?: { width: number; bars: number; start: number; end: number; half: number } | null;
  };
}

/** Which channel a chart shows: the straight one of the window, or the one known at each bar. */
export type ChannelView = "window" | "rolling";

export interface WindowResult {
  request: { symbol: string; start: string; end: string; chart_timeframe: string };
  currency: string;
  run_in_from: string;
  methods: Method[];
  bars: { timeframe: string; seconds: number; t: number[]; o: number[]; h: number[]; l: number[]; c: number[] };
  context: WindowContext;
  signal_count: number;
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
export function rank(
  combos: Combo[],
  variant: Variant,
  sort: SortKey,
  timeframes: ReadonlySet<string> | null = null,
  kind: Kind | null = null,
): Line[] {
  const lines = combos
    .filter((c) => (timeframes == null || timeframes.has(c.timeframe)) && (kind == null || kindOf(c) === kind))
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
  /** the same day by kind of method: what all momentum methods made together, and all mean-reversion methods */
  kinds: Record<"momentum" | "reversion", KindDay>;
}

export interface KindDay {
  net: number;
  up: number; // methods of the kind that made money that day
  down: number; // ... that lost
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
    const kinds: DayRow["kinds"] = { momentum: { net: 0, up: 0, down: 0 }, reversion: { net: 0, up: 0, down: 0 } };
    for (const { combo, pnl } of field) {
      const made = pnl[index] ?? 0;
      if (made > 0) up += 1;
      else if (made < 0) down += 1;
      if (made > 0 && (top == null || made > top.pnl)) top = { id: combo.id, label: methodLabel(combo), pnl: made };
      const kind = kindOf(combo);
      if (kind !== "other") {
        const sum = kinds[kind];
        sum.net += made;
        if (made > 0) sum.up += 1;
        else if (made < 0) sum.down += 1;
      }
    }
    kinds.momentum.net = Math.round(kinds.momentum.net * 100) / 100;
    kinds.reversion.net = Math.round(kinds.reversion.net * 100) / 100;
    return { day, index, later: result.split_day != null && day >= result.split_day, cells, best, top, up, down, kinds };
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

// --- a window on the chart: what the methods said together ---------------------------------------

export const WINDOWS = [
  { key: "day", label: "1 day", days: 1, chart: "1m" },
  { key: "week", label: "1 week", days: 5, chart: "5m" },
  { key: "fortnight", label: "2 weeks", days: 10, chart: "15m" },
  { key: "month", label: "1 month", days: 22, chart: "30m" },
] as const;
export type WindowKey = (typeof WINDOWS)[number]["key"];

/**
 * The request for the window of the ``length`` trading days that end with ``day``.  The chart gets
 * bars that keep a window readable, never finer than the bars trades are simulated on.
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
      list_signals: false,
    },
  };
}

export type Where = "inside" | "above" | "below";

/** One bar of the window in words and numbers: what was known when it closed. */
export interface Reading {
  time: number; // when the bar closed, ms
  close: number;
  votes: number | null; // momentum methods pointing up minus those pointing down; null: not known yet
  state: -1 | 0 | 1;
  buys: number; // mean-reversion signals that came in this bar
  sells: number;
  channel: { lower: number; middle: number; upper: number } | null;
  /** the close against the trend channel; null while the channel is not drawn yet */
  where: Where | null;
}

const sign = (value: number): -1 | 0 | 1 => (value > 0 ? 1 : value < 0 ? -1 : 0);

/** The straight channel of the window at one of its bars; null when the window has none. */
export function straightAt(result: WindowResult, index: number): { lower: number; middle: number; upper: number } | null {
  const line = result.context.channel.straight;
  const last = result.bars.t.length - 1;
  if (!line || index < 0 || index > last) return null;
  const middle = last > 0 ? line.start + ((line.end - line.start) * index) / last : line.end;
  return { lower: middle - line.half, middle, upper: middle + line.half };
}

const against = (close: number, channel: { lower: number; upper: number }): Where =>
  close > channel.upper ? "above" : close < channel.lower ? "below" : "inside";

export function readingAt(result: WindowResult, index: number, view: ChannelView = "rolling"): Reading | null {
  const { bars, context } = result;
  const start = bars.t[index];
  const close = bars.c[index];
  if (start == null || close == null) return null;
  const lower = context.channel.lower[index];
  const middle = context.channel.middle[index];
  const upper = context.channel.upper[index];
  const rolling = lower != null && middle != null && upper != null ? { lower, middle, upper } : null;
  const channel = view === "window" ? straightAt(result, index) : rolling;
  return {
    time: start + bars.seconds * 1000,
    close,
    votes: context.votes[index] ?? null,
    state: sign(context.state[index] ?? 0),
    buys: context.reversion.buys[index] ?? 0,
    sells: context.reversion.sells[index] ?? 0,
    channel,
    where: channel == null ? null : against(close, channel),
  };
}

export interface StraightChannel {
  rise: number; // what the middle line gains from the first bar of the window to the last, in pips
  half: number; // from the middle line to either edge, in pips
  /** the last close of the window against it */
  where: Where;
}

/** The straight channel of a window in a few numbers; null when the window has none. */
export function straightChannel(result: WindowResult, pip: number): StraightChannel | null {
  const line = result.context.channel.straight;
  const last = result.bars.t.length - 1;
  const close = result.bars.c[last];
  const edges = straightAt(result, last);
  if (!line || close == null || !edges || !(pip > 0)) return null;
  const pips = (value: number) => Math.round((value / pip) * 10) / 10;
  return { rise: pips(line.end - line.start), half: pips(line.half), where: against(close, edges) };
}

export const STATE_TEXT: Record<-1 | 0 | 1, string> = { 1: "up", [-1]: "down", 0: "not decided" };

/** The momentum reading of a bar as a sentence: "+12 of 40: up". */
export function momentumText(reading: Reading, voters: number): string {
  if (reading.votes == null) return "not known yet";
  const votes = reading.votes > 0 ? `+${reading.votes}` : reading.votes < 0 ? `−${Math.abs(reading.votes)}` : "0";
  return `${votes} of ${voters}: ${STATE_TEXT[reading.state]}`;
}

export interface ChangeRow {
  t: number; // when momentum changed, ms
  to: 1 | -1;
  bar: number; // the bar of the chart that closed with it
  price: number; // the price at that moment
  votes: number; // the reading that made the change
  /** how long momentum had pointed the other way, ms; null when that began before the window */
  lasted: number | null;
}

/** The momentum changes of a window, newest first. */
export function changeRows(result: WindowResult): ChangeRow[] {
  const rows: ChangeRow[] = result.context.changes.map((change, k, all) => {
    const before = all[k - 1];
    return {
      t: change.t,
      to: change.to > 0 ? 1 : -1,
      bar: change.bar,
      price: change.price,
      votes: change.votes,
      lasted: before ? change.t - before.t : null,
    };
  });
  return rows.reverse();
}

export interface WindowCount {
  bars: number;
  changes: number;
  up: number; // bars on which momentum was up
  down: number;
  buys: number; // mean-reversion signals
  sells: number;
  /** ... of which pointed against the momentum of their bar: a buy while it was down, a sell while it was up */
  against: number;
  /** bars that closed outside the trend channel */
  outside: number;
}

/** What a window amounts to before any single bar is looked at. */
export function countWindow(result: WindowResult): WindowCount {
  const { context, bars } = result;
  const out: WindowCount = { bars: bars.t.length, changes: context.changes.length, up: 0, down: 0, buys: 0, sells: 0, against: 0, outside: 0 };
  for (let i = 0; i < bars.t.length; i += 1) {
    const state = context.state[i] ?? 0;
    const buys = context.reversion.buys[i] ?? 0;
    const sells = context.reversion.sells[i] ?? 0;
    if (state > 0) out.up += 1;
    else if (state < 0) out.down += 1;
    out.buys += buys;
    out.sells += sells;
    if (state < 0) out.against += buys;
    else if (state > 0) out.against += sells;
    const where = readingAt(result, i)?.where;
    if (where === "above" || where === "below") out.outside += 1;
  }
  return out;
}

/** A length of time in words: "40 min", "3 h 20 min", "2 d 4 h". */
export function lasted(ms: number): string {
  const minutes = Math.round(ms / 60_000);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  const days = Math.floor(hours / 24);
  return hours % 24 ? `${days} d ${hours % 24} h` : `${days} d`;
}
