import { describe, expect, it } from "vitest";
import {
  buildRequest,
  dayRange,
  defaultBase,
  downloadRange,
  defaultForm,
  equitySeries,
  money,
  paramValue,
  percent,
  ratio,
  restoreForm,
  signedMoney,
  signedPips,
  storedPeriod,
  tradeMarkers,
  units,
  type Day,
  type LabInfo,
  type Trade,
} from "./model";

const info: LabInfo = {
  symbol: "EURUSD",
  currency: "USD",
  unit_currency: "EUR",
  pip_size: 0.0001,
  price_precision: 5,
  session_tz: "America/New_York",
  timeframes: ["1m", "5m", "15m", "30m", "1h"],
  strategies: [
    {
      key: "rsi_reversion",
      title: "RSI reversion",
      summary: "…",
      timeframe: "5m",
      params: [
        { name: "rsi_period", label: "RSI period", default: 14, kind: "int", minimum: 2, maximum: 200, choices: [], help: "" },
        {
          name: "trigger",
          label: "Signal when",
          default: "enter",
          kind: "choice",
          minimum: null,
          maximum: null,
          choices: ["enter", "leave"],
          help: "",
        },
      ],
    },
    { key: "session_breakout", title: "Session breakout", summary: "…", timeframe: "15m", params: [] },
  ],
  execution: {
    pip_size: 0.0001,
    spread_pips: 1,
    slippage_pips: 0,
    commission_per_million: 0,
    clip_units: 1_000_000,
    max_clips: 5,
    add_step_pips: 5,
    target_pips: 5,
    stop_pips: 10,
    min_potential_pips: 5,
    reaction_delay_s: 5,
    max_wait_s: 120,
    max_hold_minutes: 240,
    cooldown_minutes: 5,
    entry_start_utc: "06:00",
    entry_end_utc: "20:00",
    flat_time: "16:45",
    day_end: "17:00",
    session_tz: "America/New_York",
    long: true,
    short: true,
  },
  history: [
    { timeframe: "5s", seconds: 5, bars: 100, first: "2026-11-09T00:00:00.000Z", last: "2026-11-13T21:59:55.000Z" },
    { timeframe: "1m", seconds: 60, bars: 370_000, first: "2025-11-03T00:00:00.000Z", last: "2026-11-13T21:59:00.000Z" },
  ],
};

const T0 = Date.UTC(2026, 10, 12, 14, 0, 0);
const MINUTE = 60_000;

function trade(changes: Partial<Trade>): Trade {
  return {
    n: 1,
    day: "2026-11-12",
    direction: "long",
    signal_ms: T0,
    entry_ms: T0 + MINUTE,
    entry_price: 1.1001,
    clips: 1,
    units: 1_000_000,
    average_price: 1.1001,
    exit_ms: T0 + 9 * MINUTE,
    exit_price: 1.1006,
    reason: "target",
    stop_price: 1.0971,
    target_price: 1.1006,
    pnl: 500,
    pips: 5,
    potential_pips: 9,
    adverse_pips: 2,
    minutes: 8,
    entry_hour_utc: 14,
    fills: [[T0 + MINUTE, 1.1001, 1_000_000]],
    ...changes,
  };
}

describe("the form", () => {
  it("starts on the first strategy, one-minute bars and the whole stored period", () => {
    const form = defaultForm(info);
    expect(form).toMatchObject({
      strategy: "rsi_reversion",
      timeframe: "5m",
      base: "1m",
      from: "2025-11-03",
      to: "2026-11-13",
      split: "auto",
      long: true,
      short: true,
    });
    expect(form.execution.spread_pips).toBe("1");
    expect(form.execution.clip_units).toBe("1000000");
    expect(form.execution.flat_time).toBe("16:45");
    expect(paramValue(form, info.strategies[0]!, info.strategies[0]!.params[0]!)).toBe("14");
  });

  it("copes with a server that has no history yet", () => {
    const empty = { ...info, history: [] };
    expect(defaultBase(empty)).toBe("1m");
    expect(storedPeriod(empty, "1m")).toBeNull();
    const form = defaultForm(empty);
    expect([form.from, form.to]).toEqual(["", ""]);
    expect(buildRequest(empty, form)).toEqual({ error: "Enter the first and the last day of the period." });
  });

  it("prefers one-minute bars and otherwise the finest bars of a minute or more", () => {
    const five = { timeframe: "5m", seconds: 300, bars: 1, first: "2026-01-01T00:00:00.000Z", last: "2026-01-02T00:00:00.000Z" };
    const hour = { ...five, timeframe: "1h", seconds: 3600 };
    const day = { ...five, timeframe: "1d", seconds: 86_400 };
    expect(defaultBase({ ...info, history: [info.history[0]!, five, hour] })).toBe("5m");
    expect(defaultBase({ ...info, history: [info.history[0]!] })).toBe("5s");
    expect(defaultBase({ ...info, history: [day] })).toBe("1m"); // daily bars cannot carry a test
  });

  it("becomes a request with the last day included", () => {
    const form = defaultForm(info);
    form.from = "2026-01-05";
    form.to = "2026-01-09";
    form.params.rsi_reversion = { rsi_period: " 9 " };
    form.execution.max_clips = "1";
    form.short = false;
    const built = buildRequest(info, form);
    expect(built).toEqual({
      body: {
        symbol: "EURUSD",
        strategy: "rsi_reversion",
        start: "2026-01-05T00:00:00Z",
        end: "2026-01-10T00:00:00.000Z",
        timeframe: "5m",
        base_timeframe: "1m",
        params: { rsi_period: "9", trigger: "enter" },
        execution: {
          long: true,
          short: false,
          spread_pips: "1",
          min_potential_pips: "5",
          clip_units: "1000000",
          max_clips: "1",
          add_step_pips: "5",
          target_pips: "5",
          stop_pips: "10",
          max_hold_minutes: "240",
          cooldown_minutes: "5",
          reaction_delay_s: "5",
          slippage_pips: "0",
          commission_per_million: "0",
          entry_start_utc: "06:00",
          entry_end_utc: "20:00",
          flat_time: "16:45",
        },
        split: "auto",
      },
    });
    // what belongs to the instrument is never sent
    const sent = "body" in built ? Object.keys(built.body.execution) : [];
    expect(sent).not.toContain("pip_size");
    expect(sent).not.toContain("session_tz");
    expect(sent).not.toContain("day_end");
  });

  it("says what is missing instead of sending a broken request", () => {
    const base = defaultForm(info);
    expect(buildRequest(info, { ...base, to: "2025-01-01" })).toEqual({ error: "The last day is before the first day." });
    expect(buildRequest(info, { ...base, split: "date", splitDate: "" })).toHaveProperty("error");
    expect(buildRequest(info, { ...base, split: "date", splitDate: "2026-09-01" })).toMatchObject({
      body: { split: "2026-09-01" },
    });
    expect(buildRequest(info, { ...base, long: false, short: false })).toHaveProperty("error");
    expect(buildRequest(info, { ...base, strategy: "gone" })).toEqual({ error: "Choose a strategy." });
    expect(buildRequest(info, { ...base, execution: { ...base.execution, target_pips: " " } })).toEqual({
      error: 'Fill in "Target".',
    });
  });

  it("restores what was saved and drops what no longer exists", () => {
    const saved = {
      strategy: "session_breakout",
      timeframe: "15m",
      base: "5s",
      from: "2026-11-09",
      to: "2026-11-13",
      split: "date",
      splitDate: "2026-11-12",
      params: { rsi_reversion: { rsi_period: "9", gone: "1" }, removed_strategy: { x: "1" } },
      execution: { spread_pips: "0.5", leverage: "10" },
      long: false,
      short: true,
    };
    const form = restoreForm(info, JSON.parse(JSON.stringify(saved)));
    expect(form).toMatchObject({
      strategy: "session_breakout",
      timeframe: "15m",
      base: "5s",
      from: "2026-11-09",
      split: "date",
      splitDate: "2026-11-12",
      long: false,
    });
    expect(form.params).toEqual({ rsi_reversion: { rsi_period: "9" } }); // unknown names and strategies are gone
    expect(form.execution.spread_pips).toBe("0.5");
    expect(form.execution).not.toHaveProperty("leverage");
    expect(form.execution.target_pips).toBe("5"); // not saved: the server's default

    const stale = restoreForm(info, { strategy: "deleted", timeframe: "2m", base: "1h", from: "soon", split: "maybe" });
    expect(stale).toEqual(defaultForm(info));
    expect(restoreForm(info, null)).toEqual(defaultForm(info));
    expect(restoreForm(info, "text")).toEqual(defaultForm(info));
  });
});

describe("numbers as text", () => {
  it("writes money without decimals, with separators and a real minus sign", () => {
    expect(money(-92570.4)).toBe("−92,570");
    expect(money(1500)).toBe("1,500");
    expect(money(-0.2)).toBe("0");
    expect(money(null)).toBe("—");
    expect(signedMoney(500)).toBe("+500");
    expect(signedMoney(-1000)).toBe("−1,000");
    expect(signedMoney(0)).toBe("0");
    expect(signedMoney(undefined)).toBe("—");
  });

  it("writes pips with a sign, one decimal and separators", () => {
    expect(signedPips(15)).toBe("+15.0");
    expect(signedPips(-1151.46)).toBe("−1,151.5");
    expect(signedPips(-0.04)).toBe("0.0");
    expect(signedPips(0)).toBe("0.0");
    expect(signedPips(null)).toBe("—");
  });

  it("writes shares, ratios and position sizes", () => {
    expect(percent(0.6731)).toBe("67.3%");
    expect(percent(null)).toBe("—");
    expect(ratio(0.634)).toBe("0.63");
    expect(ratio(-1.98)).toBe("−1.98");
    expect(ratio(null)).toBe("—");
    expect(units(5_000_000)).toBe("5 mn");
    expect(units(2_500_000)).toBe("2.5 mn");
    expect(units(500_000)).toBe("500,000");
    expect(units(null)).toBe("—");
  });
});

describe("marks on the price chart", () => {
  it("draws a buy below its bar and its profit above the bar it was taken in", () => {
    expect(tradeMarkers([trade({})], 60)).toEqual([
      { time: (T0 + MINUTE) / 1000, position: "belowBar", shape: "arrowUp", kind: "entry", text: "Buy", trade: 1 },
      { time: (T0 + 9 * MINUTE) / 1000, position: "aboveBar", shape: "circle", kind: "target", text: "Target +500", trade: 1 },
    ]);
  });

  it("draws a sell above its bar, every clip added, and a stop as a square", () => {
    const short = trade({
      n: 7,
      direction: "short",
      reason: "stop",
      pnl: -3000,
      fills: [
        [T0 + MINUTE, 1.1, 1e6],
        [T0 + 3 * MINUTE, 1.1005, 1e6],
        [T0 + 3 * MINUTE, 1.101, 1e6],
      ],
      exit_ms: T0 + 5 * MINUTE,
    });
    expect(tradeMarkers([short], 60).map((m) => [m.time - T0 / 1000, m.position, m.shape, m.kind, m.text])).toEqual([
      [60, "aboveBar", "arrowDown", "entry", "Sell"],
      [180, "aboveBar", "arrowDown", "add", "+1"],
      [180, "aboveBar", "arrowDown", "add", "+2"],
      [300, "belowBar", "square", "stop", "Stop −3,000"],
    ]);
  });

  it("keeps the marks in time order across trades and snaps them to the bars of the chart", () => {
    const first = trade({ n: 1, exit_ms: T0 + 9 * MINUTE + 25_000, reason: "time", pnl: 0 });
    const second = trade({
      n: 2,
      direction: "short",
      entry_ms: T0 + 9 * MINUTE + 30_000,
      fills: [[T0 + 9 * MINUTE + 30_000, 1.1, 1e6]],
      exit_ms: T0 + 20 * MINUTE,
      reason: "end_of_day",
      pnl: -120,
    });
    const marks = tradeMarkers([second, first], 60); // given in the wrong order on purpose
    expect(marks.map((m) => [(m.time * 1000 - T0) / MINUTE, m.trade, m.kind])).toEqual([
      [1, 1, "entry"],
      [9, 2, "entry"], // five-second fills fall on the one-minute bar they happened in
      [9, 1, "timed"],
      [20, 2, "timed"],
    ]);
    expect(marks.at(-1)?.text).toBe("End of day −120");
    expect(marks[2]?.text).toBe("Time 0");
    const sorted = [...marks].sort((a, b) => a.time - b.time);
    expect(marks.map((m) => m.time)).toEqual(sorted.map((m) => m.time));
  });
});

describe("downloading history", () => {
  const now = Date.UTC(2026, 10, 15, 12, 0, 0);
  const day = 86_400_000;

  it("asks for the last twelve months when nothing is stored", () => {
    expect(downloadRange(null, now)).toEqual({ start: now - 365 * day, end: now });
  });

  it("continues from the last stored bar, one day earlier", () => {
    const history = info.history[1]!; // ends 2026-11-13 21:59
    expect(downloadRange(history, now)).toEqual({ start: Date.UTC(2026, 10, 12, 21, 59, 0), end: now });
  });

  it("never asks for more than a year at once", () => {
    const old = { ...info.history[1]!, last: "2024-01-01T00:00:00.000Z" };
    expect(downloadRange(old, now)).toEqual({ start: now - 365 * day, end: now });
  });
});

describe("what a chart is given", () => {
  const day: Day = {
    day: "2026-11-12",
    trades: 1,
    wins: 1,
    pnl: 500,
    pips: 5,
    cumulative: 1500,
    max_units: 1e6,
    first_bar_ms: T0 - 16 * 3_600_000,
    last_bar_ms: T0 + 7 * 3_600_000,
  };

  it("loads the whole trading day", () => {
    expect(dayRange(day, [], 60)).toEqual({ start: day.first_bar_ms, end: day.last_bar_ms! + 60_000 });
  });

  it("widens the range when a trade reaches beyond the day's bars", () => {
    const late = trade({ signal_ms: T0 - 17 * 3_600_000, exit_ms: T0 + 8 * 3_600_000 });
    expect(dayRange(day, [late], 60)).toEqual({ start: T0 - 18 * 3_600_000, end: T0 + 9 * 3_600_000 });
  });

  it("has nothing to load for a day without bar times", () => {
    expect(dayRange({ ...day, first_bar_ms: undefined }, [], 60)).toBeNull();
  });

  it("turns days into a running total by date", () => {
    expect(equitySeries([day, { ...day, day: "2026-11-13", cumulative: 900 }])).toEqual([
      { time: "2026-11-12", value: 1500 },
      { time: "2026-11-13", value: 900 },
    ]);
  });
});
