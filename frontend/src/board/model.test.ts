import { describe, expect, it } from "vitest";
import {
  type BoardResult,
  type Combo,
  type Tally,
  type WindowResult,
  boardBody,
  changeRows,
  countWindow,
  dayRows,
  defaultPicks,
  followTheWinner,
  headline,
  kindOf,
  lasted,
  methodLabel,
  momentumText,
  presetPeriod,
  rank,
  readingAt,
  straightAt,
  straightChannel,
  tallyOf,
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
    average_win: null,
    average_loss: null,
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

const STYLE: Record<string, string> = { reversion: "reversion", trend: "momentum", benchmark: "other" };

function combo(strategy: string, short: string, family: string, timeframe: string, all: Tally, one: Tally = all): Combo {
  const style = STYLE[family];
  return { id: `${strategy}@${timeframe}`, strategy, title: strategy, short, family, style, timeframe, signals: 10, all, one };
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

  it("knows the kind of each method and can be narrowed to one kind", () => {
    expect(combos.map(kindOf)).toEqual(["reversion", "momentum", "other", "reversion", "momentum", "other"]);
    expect(kindOf({})).toBe("other"); // a board from before methods were classed
    expect(rank(combos, "all", "net", null, "momentum").map((line) => line.combo.id)).toEqual(["ema_momentum@5m", "ema_momentum@15m"]);
    expect(rank(combos, "all", "net", new Set(["15m"]), "reversion").map((line) => line.combo.id)).toEqual(["rsi_reversion@15m"]);
    expect(headline(rank(combos, "all", "net", null, "reversion"))).toMatchObject({ methods: 2, positive: 2, random: null });
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

  it("adds up each day by kind of method: all momentum methods, all mean-reversion methods", () => {
    // newest first; the random entries are neither kind and are left out
    expect(rows.map((row) => row.kinds.momentum)).toEqual([
      { net: -300, up: 0, down: 1 },
      { net: -100, up: 0, down: 1 },
      { net: 900, up: 1, down: 0 },
      { net: -400, up: 0, down: 1 },
    ]);
    expect(rows.map((row) => row.kinds.reversion)).toEqual([
      { net: 200, up: 1, down: 0 },
      { net: 300, up: 1, down: 0 },
      { net: 400, up: 1, down: 1 },
      { net: 500, up: 1, down: 0 },
    ]);
    const narrow = dayRows(result, "all", [], new Set(["15m"]));
    expect(narrow.map((row) => row.kinds.reversion.net)).toEqual([200, 0, 600, 0]);
  });

  it("tries out following yesterday's winner", () => {
    // day 1 -> RSI 5m (500): next day -200.  day 2 -> EMA 5m (900): next day -100.  day 3 -> RSI 5m (300): next day 0.
    expect(followTheWinner(combos, "all")).toEqual({ days: 3, won: 0, lost: 2, idle: 1, net: -300, baseline: 5 / 9 });
    expect(followTheWinner(combos, "all", new Set(["15m"]))).toMatchObject({ days: 1, lost: 0, idle: 1, net: 0 });
    expect(followTheWinner([], "all")).toBeNull();
  });
});

// --- a window on the chart: what the methods said together ------------------------------------

const T = Date.UTC(2026, 10, 12, 14, 0, 0);
const MIN = 60_000;
// six five-minute bars from 14:00.  Momentum: 8 voters, three more one way than the other make the state.
const view: WindowResult = {
  request: { symbol: "EURUSD", start: "2026-11-12T14:00:00Z", end: "2026-11-12T14:30:00Z", chart_timeframe: "5m" },
  currency: "USD",
  run_in_from: "2026-10-22T00:00:00.000Z",
  methods: [
    { id: "rsi_reversion@5m", strategy: "rsi_reversion", short: "RSI", title: "RSI reversion", timeframe: "5m", style: "reversion" },
    { id: "ema_momentum@5m", strategy: "ema_momentum", short: "EMA", title: "EMA momentum", timeframe: "5m", style: "momentum" },
  ],
  bars: {
    timeframe: "5m",
    seconds: 300,
    t: [0, 5, 10, 15, 20, 25].map((minute) => T + minute * MIN),
    o: [1.16, 1.1602, 1.1606, 1.1611, 1.1604, 1.1596],
    h: [1.1603, 1.1607, 1.1612, 1.1612, 1.1605, 1.1597],
    l: [1.1599, 1.1601, 1.1605, 1.1603, 1.1595, 1.1589],
    c: [1.1602, 1.1606, 1.1611, 1.1604, 1.1596, 1.159],
  },
  context: {
    voters: 8,
    threshold: 3,
    votes: [null, 2, 4, 1, -3, -5],
    state: [0, 0, 1, 1, -1, -1],
    changes: [{ t: T + 25 * MIN, to: -1, bar: 4, votes: -3, price: 1.1596 }],
    reversion: { buys: [0, 0, 0, 0, 1, 2], sells: [0, 1, 2, 0, 0, 0] },
    channel: {
      hours: 24,
      width: 2,
      middle: [null, 1.1602, 1.1603, 1.1604, 1.1604, 1.1603],
      upper: [null, 1.1608, 1.1609, 1.161, 1.161, 1.1609],
      lower: [null, 1.1596, 1.1597, 1.1598, 1.1598, 1.1597],
    },
  },
  signal_count: 9,
};

describe("a window on the chart", () => {
  it("asks for the trading days that end with the chosen one, without the list of single signals", () => {
    const week = windowBody(result, "2026-11-13", "week");
    expect(week?.from).toBe("2026-11-10"); // the board has four days: the window begins with the first
    expect(week?.body).toEqual({
      symbol: "EURUSD",
      start: new Date(day(9, 22)).toISOString(),
      end: new Date(day(13, 22)).toISOString(), // the last bar began at 21:59 and is a minute long
      base_timeframe: "1m",
      chart_timeframe: "5m",
      list_signals: false,
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

  it("reads a bar: what was known when it closed", () => {
    expect(readingAt(view, 2)).toEqual({
      time: T + 15 * MIN, // the bar that began at 14:10 closed at 14:15
      close: 1.1611,
      votes: 4,
      state: 1,
      buys: 0,
      sells: 2,
      channel: { lower: 1.1597, middle: 1.1603, upper: 1.1609 },
      where: "above", // 1.1611 is over the upper line
    });
    expect(readingAt(view, 3)?.where).toBe("inside");
    expect(readingAt(view, 5)).toMatchObject({ votes: -5, state: -1, buys: 2, sells: 0, where: "below" });
    // before anything is known: no reading, no channel
    expect(readingAt(view, 0)).toMatchObject({ votes: null, state: 0, channel: null, where: null });
    expect(readingAt(view, 6)).toBeNull();
  });

  it("knows the straight channel of the window: one line from its first bar to its last", () => {
    expect(straightAt(view, 0)).toBeNull(); // this window was sent without one
    const channel = { ...view.context.channel, straight: { width: 2, bars: 6, start: 1.16, end: 1.161, half: 0.0004 } };
    const drawn = { ...view, context: { ...view.context, channel } };
    const at = (index: number) => {
      const edges = straightAt(drawn, index);
      return edges ? [edges.lower, edges.middle, edges.upper].map((value) => Number(value.toFixed(5))) : null;
    };
    expect(at(0)).toEqual([1.1596, 1.16, 1.1604]);
    expect(at(5)).toEqual([1.1606, 1.161, 1.1614]);
    expect(at(2)).toEqual([1.16, 1.1604, 1.1608]); // two fifths of the way
    expect(at(6)).toBeNull();
    // a bar read against it instead of against the channel known at the time
    expect(readingAt(drawn, 2, "window")?.where).toBe("above"); // close 1.1611 over 1.1608
    expect(readingAt(drawn, 2, "rolling")?.where).toBe("above");
    expect(readingAt(drawn, 3, "window")?.where).toBe("inside");
    expect(readingAt(drawn, 5, "window")).toMatchObject({ where: "below", votes: -5 }); // close 1.1590 under 1.1606
    expect(readingAt(view, 2, "window")).toMatchObject({ channel: null, where: null });
    // in a few numbers: it rises ten pips over the window, four pips to either side; the last close is under it
    expect(straightChannel(drawn, 0.0001)).toEqual({ rise: 10, half: 4, where: "below" });
    expect(straightChannel(view, 0.0001)).toBeNull();
    expect(straightChannel(drawn, 0)).toBeNull();
  });

  it("says the momentum reading in words", () => {
    expect(momentumText(readingAt(view, 2)!, 8)).toBe("+4 of 8: up");
    expect(momentumText(readingAt(view, 5)!, 8)).toBe("−5 of 8: down");
    expect(momentumText(readingAt(view, 1)!, 8)).toBe("+2 of 8: not decided");
    // the state stays what it was until enough methods point the other way
    expect(momentumText(readingAt(view, 3)!, 8)).toBe("+1 of 8: up");
    expect(momentumText(readingAt(view, 0)!, 8)).toBe("not known yet");
  });

  it("lists the momentum changes, newest first, with how long the state before had lasted", () => {
    expect(changeRows(view)).toEqual([{ t: T + 25 * MIN, to: -1, bar: 4, price: 1.1596, votes: -3, lasted: null }]);
    const earlier = { t: T + 15 * MIN, to: 1, bar: 2, votes: 4, price: 1.1611 };
    const two = { ...view, context: { ...view.context, changes: [earlier, ...view.context.changes] } };
    expect(changeRows(two).map((row) => [row.to, row.bar, row.lasted])).toEqual([
      [-1, 4, 10 * MIN],
      [1, 2, null], // what was before it began before the window
    ]);
  });

  it("counts what a window amounts to", () => {
    expect(countWindow(view)).toEqual({
      bars: 6,
      changes: 1,
      up: 2,
      down: 2,
      buys: 3,
      sells: 3,
      // two sells while momentum was up, three buys while it was down; the sell before it was decided is not counted
      against: 5,
      outside: 3, // 14:10 above; 14:20 and 14:25 below
    });
  });

  it("writes a length of time", () => {
    expect([40 * MIN, 60 * MIN, 200 * MIN, 24 * 60 * MIN, 52 * 60 * MIN].map(lasted)).toEqual(["40 min", "1 h", "3 h 20 min", "1 d", "2 d 4 h"]);
  });
});
