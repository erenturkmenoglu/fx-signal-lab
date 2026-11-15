import { describe, expect, it } from "vitest";
import type { LabInfo, Report, StrategyInfo } from "../lab/model";
import {
  type Condition,
  type Definition,
  MAX_METHODS,
  type SetupTrade,
  type WhyCondition,
  aroundText,
  defaultDraft,
  definitionOf,
  draftOf,
  groups,
  headlineOf,
  labeller,
  lineLabel,
  points,
  restoreDraft,
  ruleText,
  runBody,
  sameRule,
  shortLabel,
  toggleMethod,
  verdictOf,
} from "./model";

const strategy = (key: string, short: string, style: string): StrategyInfo => ({
  key,
  title: key.replace(/_/g, " "),
  summary: "",
  timeframe: "5m",
  params: [],
  short,
  style,
});

const conditions: Condition[] = [
  { key: "against_momentum", label: "Against the momentum when it was opened", when: "entry" },
  { key: "against_channel", label: "Against the slope of the trend channel", when: "entry" },
  { key: "outside_channel", label: "Opened outside the trend channel, on the losing side", when: "entry" },
  { key: "momentum_turned", label: "Momentum turned against it while it was open", when: "after" },
  { key: "left_channel", label: "The price left the trend channel against it while it was open", when: "after" },
];

const info = {
  symbol: "EURUSD",
  currency: "USD",
  unit_currency: "EUR",
  pip_size: 0.0001,
  price_precision: 5,
  session_tz: "America/New_York",
  timeframes: ["1m", "5m", "15m", "30m", "1h"],
  strategies: [
    strategy("rsi_reversion", "RSI", "reversion"),
    strategy("bollinger_reversion", "BB", "reversion"),
    strategy("ema_momentum", "EMA", "momentum"),
    strategy("donchian_breakout", "DON", "momentum"),
    strategy("random_entry", "RAND", "other"),
  ],
  execution: { stop_pips: 5, target_pips: 5, min_potential_pips: 5 },
  conditions,
  history: [],
} as unknown as LabInfo;

describe("the methods a setup can name", () => {
  it("lists the strategies by the kind of signal they give, mean reversion first", () => {
    expect(groups(info.strategies).map((g) => [g.kind, g.strategies.map((s) => s.short)])).toEqual([
      ["reversion", ["RSI", "BB"]],
      ["momentum", ["EMA", "DON"]],
      ["other", ["RAND"]],
    ]);
    // a kind without a strategy is not a heading over nothing
    expect(groups(info.strategies.slice(0, 2)).map((g) => g.kind)).toEqual(["reversion"]);
  });

  it("calls a method by its short name and its bars", () => {
    const label = labeller(info.strategies);
    expect(label("rsi_reversion@5m")).toBe("RSI 5m");
    expect(label("gone_strategy@15m")).toBe("gone_strategy 15m");
  });

  it("ticks and unticks, and takes no more than a setup may name", () => {
    expect(toggleMethod(["a@5m"], "b@5m")).toEqual(["a@5m", "b@5m"]);
    expect(toggleMethod(["a@5m", "b@5m"], "a@5m")).toEqual(["b@5m"]);
    const full = Array.from({ length: MAX_METHODS }, (_, k) => `m${k}@5m`);
    expect(toggleMethod(full, "one_more@5m")).toBe(full);
    expect(toggleMethod(full, "m3@5m")).toHaveLength(MAX_METHODS - 1);
  });
});

describe("the form", () => {
  const draft = { ...defaultDraft(info), from: "2026-10-15", to: "2026-11-13" };

  it("starts with the owner's first rule: every signal of RSI or Bollinger, the instrument's stop and target", () => {
    expect(defaultDraft(info)).toEqual({
      name: "RSI or Bollinger",
      methods: ["rsi_reversion@5m", "bollinger_reversion@5m"],
      mode: "any",
      within: "30",
      stop: "5",
      target: "5",
      room: "0", // every signal is taken
      skip: [],
      from: "",
      to: "",
      preset: "month",
    });
    // a server without those strategies: an empty rule to fill in
    const bare = { ...info, strategies: info.strategies.slice(2) } as LabInfo;
    expect(defaultDraft(bare)).toMatchObject({ name: "My setup", methods: [] });
  });

  it("becomes a rule", () => {
    expect(definitionOf(draft)).toEqual({
      definition: {
        name: "RSI or Bollinger",
        methods: ["rsi_reversion@5m", "bollinger_reversion@5m"],
        mode: "any",
        within_minutes: 30,
        skip: [],
        execution: { stop_pips: 5, target_pips: 5, min_potential_pips: 0 },
      },
    });
    const both = definitionOf({ ...draft, name: "  Both agree ", mode: "all", within: "12.5", room: "5", skip: ["against_momentum"] });
    expect(both).toMatchObject({
      definition: { name: "Both agree", mode: "all", within_minutes: 12.5, skip: ["against_momentum"], execution: { min_potential_pips: 5 } },
    });
  });

  it("says what is missing before anything is sent", () => {
    const error = (change: object) => (definitionOf({ ...draft, ...change }) as { error: string }).error;
    expect(error({ name: "  " })).toBe("Give the setup a name.");
    expect(error({ methods: [] })).toBe("Tick at least one method.");
    expect(error({ stop: "" })).toBe("Fill in “Stop”.");
    expect(error({ stop: "0" })).toBe("“Stop” must be more than zero.");
    expect(error({ target: "five" })).toBe("Fill in “Target”.");
    expect(error({ room: "-1" })).toBe("“Room a signal must see” must be zero or more.");
    expect(error({ mode: "all", within: "0" })).toBe("“within … minutes” must be more than zero.");
    // how old the other signals may be matters only when all have to agree
    expect(definitionOf({ ...draft, within: "" })).toHaveProperty("definition");
  });

  it("becomes the request of a run with the last day included", () => {
    expect(runBody("EURUSD", "1m", draft)).toEqual({
      body: {
        symbol: "EURUSD",
        start: "2026-10-15T00:00:00Z",
        end: "2026-11-14T00:00:00.000Z",
        base_timeframe: "1m",
        name: "RSI or Bollinger",
        methods: ["rsi_reversion@5m", "bollinger_reversion@5m"],
        mode: "any",
        within_minutes: 30,
        skip: [],
        execution: { stop_pips: 5, target_pips: 5, min_potential_pips: 0 },
      },
    });
    expect(runBody("EURUSD", "1m", { ...draft, to: "2026-10-01" })).toEqual({ error: "The last day is before the first day." });
    expect(runBody("EURUSD", "1m", { ...draft, from: "" })).toEqual({ error: "Enter the first and the last day of the period." });
    expect(runBody("EURUSD", "1m", { ...draft, methods: [] })).toEqual({ error: "Tick at least one method." });
  });

  it("is restored over today's defaults, without what the server no longer knows", () => {
    const stored = {
      ...draft,
      name: "Mine",
      methods: ["ema_momentum@15m", "martingale@5m", "rsi_reversion@4h", "ema_momentum@15m", 7],
      mode: "all",
      stop: "7.5",
      skip: ["against_momentum", "left_channel", "nonsense"],
      preset: null,
      extra: "ignored",
    };
    expect(restoreDraft(info, stored)).toEqual({
      ...draft,
      name: "Mine",
      methods: ["ema_momentum@15m"],
      mode: "all",
      stop: "7.5",
      skip: ["against_momentum"], // only what is known at entry can be left out
      preset: null,
    });
    expect(restoreDraft(info, null)).toEqual(defaultDraft(info));
    expect(restoreDraft(info, "nonsense")).toEqual(defaultDraft(info));
    expect(restoreDraft(info, { from: "13/11/2026", preset: "decade" })).toEqual(defaultDraft(info));
  });

  const saved: Definition = {
    name: "Breakouts together",
    methods: ["donchian_breakout@15m", "ema_momentum@5m", "martingale@5m"],
    mode: "all",
    within_minutes: 20,
    skip: ["against_channel"],
    execution: { stop_pips: 8, target_pips: 12 },
  };

  it("takes a saved setup into the form and keeps the period", () => {
    expect(draftOf(info, saved, draft)).toEqual({
      name: "Breakouts together",
      methods: ["donchian_breakout@15m", "ema_momentum@5m"],
      mode: "all",
      within: "20",
      stop: "8",
      target: "12",
      room: "5", // not part of the saved rule: the instrument's own
      skip: ["against_channel"],
      from: "2026-10-15",
      to: "2026-11-13",
      preset: "month",
    });
  });

  it("knows whether the form still says what a saved setup says", () => {
    const known = { ...saved, methods: saved.methods.slice(0, 2), execution: { stop_pips: 8, target_pips: 12, min_potential_pips: 5 } };
    const loaded = draftOf(info, known, draft);
    expect(sameRule(loaded, known)).toBe(true);
    expect(sameRule({ ...loaded, from: "2026-01-01", preset: null }, known)).toBe(true); // the period is not part of the rule
    expect(sameRule({ ...loaded, methods: [...loaded.methods].reverse() }, known)).toBe(true);
    expect(sameRule({ ...loaded, stop: "9" }, known)).toBe(false);
    expect(sameRule({ ...loaded, skip: [] }, known)).toBe(false);
    expect(sameRule({ ...loaded, name: "" }, known)).toBe(false);
  });
});

describe("the result in words", () => {
  const label = labeller(info.strategies);

  it("says when a setup opens a trade", () => {
    const methods = ["rsi_reversion@5m", "bollinger_reversion@5m", "ema_momentum@15m"];
    expect(ruleText({ methods, mode: "any", within_minutes: 30 }, label)).toBe("any of RSI 5m, BB 5m, EMA 15m signals");
    expect(ruleText({ methods, mode: "all", within_minutes: 30 }, label)).toBe(
      "RSI 5m, BB 5m and EMA 15m all point the same way within 30 minutes",
    );
    expect(ruleText({ methods: methods.slice(0, 1), mode: "all", within_minutes: 30 }, label)).toBe("RSI 5m signals");
  });

  it("answers first what was asked first: how many trades, how many worked, what they made", () => {
    const report = {
      summary: { trades: 101, wins: 42, losses: 58, net_pnl: -8500, average_win: 494, average_loss: -494, days: 22, days_positive: 9, days_negative: 13 },
    } as unknown as Report;
    expect(headlineOf(report)).toEqual({
      trades: 101,
      worked: 42,
      failed: 58,
      flat: 1,
      net: -8500,
      averageWin: 494,
      averageLoss: -494,
      days: 22,
      daysUp: 9,
      daysDown: 13,
    });
  });

  const around = (change: Partial<SetupTrade>): SetupTrade =>
    ({
      votes: 0,
      momentum: 0,
      against_momentum: false,
      against_channel: false,
      outside_channel: false,
      momentum_turned: false,
      left_channel: false,
      ...change,
    }) as SetupTrade;

  it("says what was going on around a trade", () => {
    expect(aroundText(around({ votes: -12, momentum: -1, against_momentum: true, left_channel: true }), conditions)).toBe(
      "against the momentum (reading −12), price left the channel",
    );
    expect(aroundText(around({ votes: 9, momentum: 1, outside_channel: true, momentum_turned: true }), conditions)).toBe(
      "with the momentum (reading +9), outside the channel, momentum turned",
    );
    expect(aroundText(around({}), conditions)).toBe("momentum not decided (reading 0)");
    // a condition this page has no short words for is said in the server's own
    expect(shortLabel({ key: "new_one", label: "Something New" })).toBe("something new");
    expect(conditions.map(lineLabel)).toEqual([
      "Against the momentum",
      "Against the channel's slope",
      "Outside the channel",
      "Momentum turned",
      "Price left the channel",
    ]);
  });

  const line = (held: [number, number], free: [number, number], difference: number | null, noise: number | null): WhyCondition => ({
    ...conditions[0]!,
    with: { trades: held[0], wins: held[1], losses: held[0] - held[1], win_rate: held[0] ? held[1] / held[0] : null, net: 0 },
    without: { trades: free[0], wins: free[1], losses: free[0] - free[1], win_rate: free[0] ? free[1] / free[0] : null, net: 0 },
    failed_with: held[0] - held[1],
    difference,
    noise,
  });

  it("reads a line of the why table against what chance gives", () => {
    expect(verdictOf(line([61, 22], [40, 20], -0.1393, 0.2006))).toBe("chance");
    expect(verdictOf(line([621, 223], [677, 290], -0.0693, 0.0543))).toBe("worse");
    expect(verdictOf(line([100, 60], [300, 120], 0.2, 0.11))).toBe("better");
    expect(verdictOf(line([50, 20], [50, 25], -0.1, 0.1))).toBe("chance"); // on the line is not beyond it
    expect(verdictOf(line([0, 0], [101, 42], null, null))).toBe("none");
    expect(verdictOf(line([101, 42], [0, 0], null, null))).toBe("all");
  });

  it("writes a difference in percentage points", () => {
    expect([-0.1393, 0.0423, 0, 0.0004, null].map(points)).toEqual(["−13.9", "+4.2", "0.0", "0.0", "—"]);
  });
});
