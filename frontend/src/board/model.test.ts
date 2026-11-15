import { describe, expect, it } from "vitest";
import {
  type BoardResult,
  type Combo,
  type Method,
  type Signal,
  type Tally,
  barAt,
  boardBody,
  countSignals,
  dayRows,
  defaultPicks,
  followTheWinner,
  headline,
  laneMarks,
  lanesOf,
  methodLabel,
  outcomeOf,
  presetPeriod,
  rank,
  signalsByBar,
  tallyOf,
  visibleSignals,
  windowBody,
} from "./model";

// --- a small board: four trading days, the last two kept back as the later part ---------------

function tally(pnl: number[], trades: number[], wins: number[]): Tally {
  const losses = trades.map((count, day) => count - (wins[day] ?? 0));
  const sum = (values: number[]) => values.reduce((a, b) => a + b, 0);
  const part = (from: number, to: number) => ({
    trades: sum(trades.slice(from, to)),
    wins: sum(wins.slice(from, to)),
    losses: sum(losses.slice(from, to)),
    net: sum(pnl.slice(from, to)),
  });
  return {
    ...part(0, 4),
    profit_factor: null,
    largest_loss: null,
    max_drawdown: 0,
    max_units: 1_000_000,
    days_traded: trades.filter((count) => count > 0).length,
    days_won: pnl.filter((value) => value > 0).length,
    days_lost: pnl.filter((value) => value < 0).length,
    best_day: Math.max(...pnl),
    worst_day: Math.min(...pnl),
    daily: { trades, wins, losses, pnl },
    in_sample: part(0, 2),
    out_of_sample: part(2, 4),
  };
}

function combo(strategy: string, short: string, family: string, timeframe: string, all: Tally, one: Tally = all): Combo {
  return { id: `${strategy}@${timeframe}`, strategy, title: strategy, short, family, timeframe, signals: 10, all, one };
}

const idle = tally([0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]);
const combos: Combo[] = [
  //                                                     day 1  day 2  day 3  day 4
  combo("rsi_reversion", "RSI", "reversion", "5m", tally([500, -200, 300, 0], [2, 1, 1, 0], [2, 0, 1, 0]), tally([100, 0, 0, 0], [1, 0, 0, 0], [1, 0, 0, 0])),
  combo("ema_momentum", "EMA", "trend", "5m", tally([-400, 900, -100, -300], [1, 2, 1, 1], [0, 2, 0, 0])),
  combo("random_entry", "RAND", "benchmark", "5m", tally([200, 100, -50, 50], [3, 3, 3, 3], [2, 2, 1, 2])),
  combo("rsi_reversion", "RSI", "reversion", "15m", tally([0, 600, 0, 200], [0, 1, 0, 1], [0, 1, 0, 1])),
  combo("ema_momentum", "EMA", "trend", "15m", idle),
  combo("random_entry", "RAND", "benchmark", "15m", tally([-100, -100, -100, -100], [1, 1, 1, 1], [0, 0, 0, 0])),
];

const HOUR = 3_600_000;
const day = (d: number, hour: number) => Date.UTC(2026, 10, d, hour);
const result: BoardResult = {
  request: {
    symbol: "EURUSD",
    start: "2026-11-10T00:00:00Z",
    end: "2026-11-14T00:00:00Z",
    base_timeframe: "1m",
    timeframes: ["5m", "15m"],
    strategies: ["rsi_reversion", "ema_momentum", "random_entry"],
    execution: { max_clips: 5, clip_units: 1_000_000, spread_pips: 1, target_pips: 5, stop_pips: 10, add_step_pips: 5, min_potential_pips: 5 },
    split: "auto",
  },
  currency: "USD",
  unit_currency: "EUR",
  pip_value: 100,
  data: { base_bars: 5000, trading_days: 4, first_day: "2026-11-10", last_day: "2026-11-13" },
  days: ["2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"],
  // a trading day begins the evening before (17:00 in New York) and ends at 21:59 UTC
  day_spans: [
    [day(9, 22), day(10, 21) + 59 * 60_000],
    [day(10, 22), day(11, 21) + 59 * 60_000],
    [day(11, 22), day(12, 21) + 59 * 60_000],
    [day(12, 22), day(13, 21) + 59 * 60_000],
  ],
  split_day: "2026-11-12",
  variants: ["all", "one"],
  combos,
  warnings: [],
};

describe("the period", () => {
  const history = { timeframe: "1m", seconds: 60, bars: 370_000, first: "2025-11-03T00:00:00.000Z", last: "2026-11-13T21:59:00.000Z" };

  it("counts a preset back from the last stored day", () => {
    expect(presetPeriod(history, "week")).toEqual({ from: "2026-11-07", to: "2026-11-13" });
    expect(presetPeriod(history, "month")).toEqual({ from: "2026-10-15", to: "2026-11-13" });
    expect(presetPeriod(history, "quarter")).toEqual({ from: "2026-08-15", to: "2026-11-13" });
  });

  it("never begins before the first stored day", () => {
    expect(presetPeriod(history, "year")).toEqual({ from: "2025-11-14", to: "2026-11-13" });
    const short = { ...history, first: "2026-11-13T00:00:00.000Z" };
    expect(presetPeriod(short, "month")).toEqual({ from: "2026-11-13", to: "2026-11-13" });
  });

  it("becomes a request with the last day included", () => {
    expect(boardBody("EURUSD", "1m", "2026-10-15", "2026-11-13")).toEqual({
      body: { symbol: "EURUSD", start: "2026-10-15T00:00:00Z", end: "2026-11-14T00:00:00.000Z", base_timeframe: "1m" },
    });
    expect(boardBody("EURUSD", "1m", "2026-11-13", "2026-11-12")).toEqual({ error: "The last day is before the first day." });
    expect(boardBody("EURUSD", "1m", "", "2026-11-12")).toHaveProperty("error");
  });
});

describe("the scoreboard", () => {
  it("puts the best net result first and methods without a trade last", () => {
    const lines = rank(combos, "all", "net");
    expect(lines.map((line) => line.combo.id)).toEqual([
      "rsi_reversion@15m", // +800
      "rsi_reversion@5m", // +600
      "random_entry@5m", // +300
      "ema_momentum@5m", // +100
      "random_entry@15m", // -400
      "ema_momentum@15m", // no trade
    ]);
    expect(lines[0]).toMatchObject({ worked: 1, earlier: 600, later: 200 });
    expect(lines[3]?.worked).toBeCloseTo(2 / 5);
    expect(lines[5]).toMatchObject({ worked: null });
  });

  it("sorts by the later days, the share that worked, or the number of trades", () => {
    expect(rank(combos, "all", "later").map((l) => l.combo.id).slice(0, 2)).toEqual(["rsi_reversion@5m", "rsi_reversion@15m"]);
    expect(rank(combos, "all", "worked").map((l) => l.combo.id)[0]).toBe("rsi_reversion@15m");
    expect(rank(combos, "all", "trades").map((l) => l.combo.id)[0]).toBe("random_entry@5m");
  });

  it("shows the numbers of the variant asked for", () => {
    expect(tallyOf(combos[0]!, "one").net).toBe(100);
    expect(rank(combos, "one", "net").find((l) => l.combo.id === "rsi_reversion@5m")?.tally.net).toBe(100);
    // a board that was run without adding has one set of numbers
    const single = { ...combos[0]!, all: undefined };
    expect(tallyOf(single, "all").net).toBe(100);
  });

  it("can be narrowed to some bar lengths", () => {
    expect(rank(combos, "all", "net", new Set(["15m"])).map((l) => l.combo.id)).toEqual([
      "rsi_reversion@15m",
      "random_entry@15m",
      "ema_momentum@15m",
    ]);
  });

  it("says what the board amounts to against the random entries", () => {
    expect(headline(rank(combos, "all", "net"))).toEqual({
      methods: 4,
      traded: 3,
      positive: 3,
      both: 2, // RSI on both bar lengths made money before and after the split; EMA 5m only before
      random: 300,
      aboveRandom: 2,
    });
    // without random entries on the board there is nothing to measure against
    const real = combos.filter((c) => c.family !== "benchmark");
    expect(headline(rank(real, "all", "net"))).toMatchObject({ random: null, aboveRandom: null });
  });

  it("starts the day table with the best methods and the random entries on the same bars", () => {
    expect(defaultPicks(combos, "all")).toEqual(["rsi_reversion@15m", "rsi_reversion@5m", "ema_momentum@5m", "random_entry@15m"]);
    expect(defaultPicks(combos, "all", 1)).toEqual(["rsi_reversion@15m", "random_entry@15m"]);
    expect(defaultPicks([], "all")).toEqual([]);
  });

  it("writes a method as its short name and its bars", () => {
    expect(methodLabel(combos[0]!)).toBe("RSI 5m");
  });
});

describe("day by day", () => {
  const rows = dayRows(result, "all", ["rsi_reversion@5m", "ema_momentum@5m", "random_entry@5m"]);

  it("has one row per trading day, newest first, with the later part marked", () => {
    expect(rows.map((row) => row.day)).toEqual(["2026-11-13", "2026-11-12", "2026-11-11", "2026-11-10"]);
    expect(rows.map((row) => row.later)).toEqual([true, true, false, false]);
    expect(rows.map((row) => row.index)).toEqual([3, 2, 1, 0]);
  });

  it("counts for each chosen method the trades that worked and that failed", () => {
    const first = rows[3]!; // 2026-11-10
    expect(first.cells).toEqual([
      { trades: 2, wins: 2, losses: 0, pnl: 500 },
      { trades: 1, wins: 0, losses: 1, pnl: -400 },
      { trades: 3, wins: 2, losses: 1, pnl: 200 },
    ]);
  });

  it("marks the best of the chosen methods, and none on a day nobody made money", () => {
    expect(rows.map((row) => row.best)).toEqual([2, 0, 1, 0]);
    const losing = dayRows(result, "all", ["random_entry@15m"]);
    expect(losing.every((row) => row.best === null)).toBe(true);
  });

  it("names the best of all methods on the board that day, random entries aside", () => {
    expect(rows.map((row) => row.top)).toEqual([
      { id: "rsi_reversion@15m", label: "RSI 15m", pnl: 200 },
      { id: "rsi_reversion@5m", label: "RSI 5m", pnl: 300 },
      { id: "ema_momentum@5m", label: "EMA 5m", pnl: 900 },
      { id: "rsi_reversion@5m", label: "RSI 5m", pnl: 500 },
    ]);
    expect(rows.map((row) => [row.up, row.down])).toEqual([
      [1, 1],
      [1, 1],
      [2, 1],
      [1, 1],
    ]);
    // narrowed to the fifteen-minute methods
    const narrow = dayRows(result, "all", [], new Set(["15m"]));
    expect(narrow.map((row) => row.top?.label ?? null)).toEqual(["RSI 15m", null, "RSI 15m", null]);
  });

  it("tries out following yesterday's winner", () => {
    // day 1 -> RSI 5m (500): next day -200.  day 2 -> EMA 5m (900): next day -100.  day 3 -> RSI 5m (300): next day 0.
    expect(followTheWinner(combos, "all")).toEqual({ days: 3, won: 0, lost: 2, idle: 1, net: -300, baseline: 5 / 9 });
    expect(followTheWinner(combos, "all", new Set(["15m"]))).toMatchObject({ days: 1, lost: 0, idle: 1, net: 0 });
    expect(followTheWinner([], "all")).toBeNull();
  });
});

// --- the signals of a window ------------------------------------------------------------------

const methods: Method[] = [
  { id: "rsi_reversion@5m", strategy: "rsi_reversion", short: "RSI", title: "RSI reversion", timeframe: "5m", family: "reversion" },
  { id: "random_entry@5m", strategy: "random_entry", short: "RAND", title: "Random entry", timeframe: "5m", family: "benchmark" },
  { id: "ema_momentum@5m", strategy: "ema_momentum", short: "EMA", title: "EMA momentum", timeframe: "5m", family: "trend" },
  { id: "rsi_reversion@15m", strategy: "rsi_reversion", short: "RSI", title: "RSI reversion", timeframe: "15m", family: "reversion" },
];
const byId = new Map(methods.map((m) => [m.id, m]));
const T = Date.UTC(2026, 10, 12, 14, 0, 0);
const MIN = 60_000;
// chart bars of five minutes from 14:00, with a hole: no bar at 14:15
const times = [0, 5, 10, 20, 25].map((minute) => T / 1000 + minute * 60);
const signals: Signal[] = [
  { method: "rsi_reversion@5m", t: T + 5 * MIN, d: 1, potential: 8, status: "target", pnl: 500, clips: 1 },
  { method: "rsi_reversion@15m", t: T + 5 * MIN + 30_000, d: 1, potential: 9, status: "stop", pnl: -900, clips: 3 },
  { method: "ema_momentum@5m", t: T + 5 * MIN, d: -1, potential: 2, status: "potential" },
  { method: "random_entry@5m", t: T + 15 * MIN, d: -1, potential: 998, status: "busy" },
  { method: "ema_momentum@5m", t: T + 25 * MIN, d: 1, potential: 6, status: "time", pnl: 0, clips: 1 },
  { method: "rsi_reversion@5m", t: T + 40 * MIN, d: -1, potential: 7, status: "hours" },
];

describe("the window of signals", () => {
  it("asks for the trading days that end with the chosen one", () => {
    const week = windowBody(result, "2026-11-13", "week");
    expect(week?.from).toBe("2026-11-10"); // the board has four days: the window begins with the first
    expect(week?.body).toEqual({
      symbol: "EURUSD",
      start: new Date(day(9, 22)).toISOString(),
      end: new Date(day(13, 22)).toISOString(), // the last bar began at 21:59 and is a minute long
      base_timeframe: "1m",
      chart_timeframe: "5m",
    });
    const one = windowBody(result, "2026-11-12", "day");
    expect(one?.from).toBe("2026-11-12");
    expect(one?.body.start).toBe(new Date(day(11, 22)).toISOString());
    expect(one?.body.chart_timeframe).toBe("1m");
    expect(windowBody(result, "2026-12-01", "week")).toBeNull();
    expect(day(13, 22) - day(13, 21)).toBe(HOUR);
  });

  it("never asks for chart bars finer than the bars trades are simulated on", () => {
    const coarse = { ...result, request: { ...result.request, base_timeframe: "15m" } };
    expect(windowBody(coarse, "2026-11-13", "day")?.body.chart_timeframe).toBe("15m");
    expect(windowBody(coarse, "2026-11-13", "month")?.body.chart_timeframe).toBe("30m");
  });

  it("says what became of a signal", () => {
    expect(signals.map(outcomeOf)).toEqual(["won", "lost", "none", "none", "flat", "none"]);
  });

  it("shows the signals asked for", () => {
    expect(visibleSignals(signals, byId, null, true)).toHaveLength(6);
    expect(visibleSignals(signals, byId, null, false).map((s) => s.status)).toEqual(["target", "stop", "time"]);
    expect(visibleSignals(signals, byId, new Set(["15m"]), true).map((s) => s.method)).toEqual(["rsi_reversion@15m"]);
    // a signal of a method the window does not know is not shown
    expect(visibleSignals([{ ...signals[0]!, method: "gone@5m" }], byId, null, true)).toEqual([]);
  });

  it("gives every strategy one lane, kindred strategies together and the random entries last", () => {
    expect(lanesOf(methods).map((lane) => lane.short)).toEqual(["RSI", "EMA", "RAND"]);
  });

  it("finds the chart bar of a moment, and the next one where there is a hole", () => {
    expect(barAt(times, 300, T + 7 * MIN)).toBe(times[1]);
    expect(barAt(times, 300, T + 15 * MIN)).toBe(times[3]); // no bar at 14:15: the one at 14:20
    expect(barAt(times, 300, T + 40 * MIN)).toBeNull(); // after the last bar
    expect(barAt(times, 300, T - 60 * MIN)).toBe(times[0]);
  });

  it("puts the signals of one strategy and direction in one chart bar under one mark", () => {
    const lanes = lanesOf(methods);
    const marks = laneMarks(signals, byId, lanes, times, 300);
    expect(marks.map((m) => [m.time - T / 1000, lanes[m.lane]?.short, m.direction, m.outcome, m.signals.length])).toEqual([
      [300, "RSI", 1, "lost", 2], // +500 on five-minute bars and -900 on fifteen-minute bars: together a loss
      [300, "EMA", -1, "none", 1],
      [1200, "RAND", -1, "none", 1], // known at 14:15, where the chart has a hole: on the next bar
      [1500, "EMA", 1, "flat", 1],
      // the signal at 14:40 is after the last bar of the chart and has no mark
    ]);
  });

  it("lists the signals of a bar with the traded ones first", () => {
    const bars = signalsByBar(signals, times, 300);
    expect(bars.get(times[1]!)?.map((s) => s.method)).toEqual(["rsi_reversion@5m", "rsi_reversion@15m", "ema_momentum@5m"]);
    expect(bars.get(times[3]!)?.map((s) => s.method)).toEqual(["random_entry@5m"]);
    expect(bars.has(times[0]!)).toBe(false);
  });

  it("counts signals, trades and what they made", () => {
    expect(countSignals(signals)).toEqual({ signals: 6, traded: 3, won: 1, lost: 1, net: -400 });
    expect(countSignals([])).toEqual({ signals: 0, traded: 0, won: 0, lost: 0, net: 0 });
  });
});
