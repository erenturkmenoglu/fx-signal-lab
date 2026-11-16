import { describe, expect, it } from "vitest";
import { firstAt, markSize } from "./marks";
import {
  type Channel,
  DAY_RANGES,
  type LiveForm,
  type LiveSettings,
  type LiveSignal,
  channelShort,
  channelSpan,
  countSides,
  draftOf,
  markTime,
  marksOf,
  minutePoints,
  modeOf,
  paceText,
  rangePoints,
  reasonText,
  ruleLine,
  sameSettings,
  scaleChannels,
  secondPoints,
  segmentOf,
  settingsOf,
  slopePips,
  standing,
  standingText,
  withSignal,
} from "./model";

const DEFAULTS: LiveSettings = {
  bar_seconds: 1,
  rsi_period: 14,
  rsi_buy_below: 30,
  rsi_sell_above: 70,
  bollinger_period: 20,
  bollinger_width: 2,
  rule: "both",
  channel_short_hours: 1,
  channel_medium_hours: 24,
  channel_long_days: 10,
  channel_width: 2,
};

const LIMITS: LiveForm["limits"] = {
  rsi_period: [2, 1000],
  rsi_buy_below: [1, 99],
  rsi_sell_above: [1, 99],
  bollinger_period: [2, 2000],
  bollinger_width: [0.1, 10],
  channel_short_hours: [0.25, 48],
  channel_medium_hours: [1, 240],
  channel_long_days: [1, 30],
  channel_width: [0.5, 5],
};

const T0 = Date.UTC(2026, 10, 16, 22, 0, 0); // the start of a trading day (17:00 New York)
const MINUTE = 60_000;

const signal = (t: number, side: "buy" | "sell", bid = 1.1): LiveSignal => ({
  t,
  side,
  bid,
  ask: bid + 0.0001,
  rsi: side === "buy" ? 24.5 : 77.25,
  middle: bid,
  upper: bid + 0.0002,
  lower: bid - 0.0002,
  by: "both",
});

/** A channel through sixty one-minute bars that rises one pip a bar and is ten pips wide. */
const channel = (over: Partial<Channel> = {}): Channel => ({
  bars: 60,
  asked: 60,
  from: T0,
  to: T0 + 59 * MINUTE,
  start: 1.1,
  end: 1.1059,
  half: 0.0005,
  rise_per_bar: 0.0001,
  day: { from: T0, start: 1.1 },
  ...over,
});

describe("the series of the chart", () => {
  it("gives every second a point and leaves a hole where there is no price", () => {
    const points = secondPoints({ start: T0, bid: [1.1, null, 1.1002] });
    expect(points).toEqual([{ time: T0 / 1000, value: 1.1 }, { time: T0 / 1000 + 1 }, { time: T0 / 1000 + 2, value: 1.1002 }]);
  });

  it("puts a minute at its start", () => {
    expect(minutePoints({ t: [T0, T0 + MINUTE], bid: [1.1, 1.2] })).toEqual([
      { time: T0 / 1000, value: 1.1 },
      { time: T0 / 1000 + 60, value: 1.2 },
    ]);
  });
});

describe("ranges", () => {
  it("shows seconds of the day for every range but the one of the long channel", () => {
    expect(DAY_RANGES.map((r) => r.key)).toEqual(["5m", "15m", "1h", "day"]);
    expect(DAY_RANGES.every((r) => modeOf(r.key) === "day")).toBe(true);
    expect(modeOf("history")).toBe("history");
    expect(rangePoints("15m")).toBe(900);
    expect(rangePoints("day")).toBeNull();
    expect(rangePoints("history")).toBeNull();
  });

  it("makes room on the price scale for the channel that matches the range and the shorter ones", () => {
    expect(scaleChannels("5m")).toEqual(["short"]);
    expect(scaleChannels("1h")).toEqual(["short"]);
    expect(scaleChannels("day")).toEqual(["short", "medium"]);
    expect(scaleChannels("history")).toEqual(["short", "medium", "long"]);
  });
});

describe("channels", () => {
  it("names the length a channel was asked for", () => {
    expect(channelSpan("short", DEFAULTS)).toBe("1 h");
    expect(channelSpan("medium", { ...DEFAULTS, channel_medium_hours: 36.5 })).toBe("36.5 h");
    expect(channelSpan("long", DEFAULTS)).toBe("10 d");
  });

  it("says so when fewer bars were stored than the channel asks for", () => {
    expect(channelShort(channel())).toBeNull();
    expect(channelShort(channel({ bars: 200, asked: 1440 }))).toBe("only 3.3 h of bars stored");
    expect(channelShort(channel({ bars: 5760, asked: 14_400 }))).toBe("only 4.0 d of bars stored");
  });

  it("draws a channel of the day from the close of its first bar to the newest second, along its own line", () => {
    const newest = T0 + 60 * MINUTE + 29_000; // half a minute into the bar after the channel's last
    const segment = segmentOf(channel(), "day", MINUTE, newest);
    expect(segment).not.toBeNull();
    // a one-minute bar stands at its last second
    expect(segment?.from).toBe((T0 + 59_000) / 1000);
    expect(segment?.to).toBe(newest / 1000);
    expect(segment?.middle[0]).toBe(1.1);
    // thirty seconds past the close of the last bar: half a bar further along the line
    expect(segment?.middle[1]).toBeCloseTo(1.1059 + 0.0001 * 0.5, 10);
    expect(segment?.half).toBe(0.0005);
  });

  it("does not take a channel back past its last bar when the chart ends before it", () => {
    const segment = segmentOf(channel(), "day", MINUTE, T0 + 30 * MINUTE);
    expect(segment?.to).toBe((T0 + 59 * MINUTE + 59_000) / 1000);
    expect(segment?.middle[1]).toBe(1.1059);
  });

  it("draws nothing of a channel that has no bar in the trading day yet, and nothing where there is none", () => {
    expect(segmentOf(channel({ day: null }), "day", MINUTE, T0 + MINUTE)).toBeNull();
    expect(segmentOf(null, "day", MINUTE, T0)).toBeNull();
    expect(segmentOf(null, "history", MINUTE, T0)).toBeNull();
  });

  it("draws the whole channel on the chart of minutes, bar start to bar start", () => {
    const whole = channel({ from: T0 - 600 * MINUTE, start: 1.04, day: { from: T0, start: 1.1 } });
    expect(segmentOf(whole, "history", MINUTE, null)).toEqual({
      from: (T0 - 600 * MINUTE) / 1000,
      to: (T0 + 59 * MINUTE) / 1000,
      middle: [1.04, 1.1059],
      half: 0.0005,
    });
  });

  it("says where a price stands in a channel", () => {
    const c = channel();
    const inside = standing(c, 1.1059, 0.0001);
    expect(inside.where).toBe("inside");
    expect(inside.share).toBeCloseTo(0.5, 6);
    expect(standingText(inside)).toBe("50% up");
    expect(standingText(standing(c, 1.1055, 0.0001))).toBe("10% up");
    const above = standing(c, 1.1067, 0.0001);
    expect(above.where).toBe("above");
    expect(above.pips).toBeCloseTo(3, 6);
    expect(standingText(above)).toBe("3.0 pips above");
    expect(standingText(standing(c, 1.1049, 0.0001))).toBe("5.0 pips below");
  });

  it("gives the slope of a channel in pips an hour", () => {
    expect(slopePips(channel(), MINUTE, 0.0001)).toBeCloseTo(60, 6);
    expect(slopePips(channel({ rise_per_bar: -0.00001 }), MINUTE, 0.0001)).toBeCloseTo(-6, 6);
  });
});

describe("signals", () => {
  it("adds a signal from the stream once, and keeps the list in time order", () => {
    const first = signal(T0 + 5000, "buy");
    const second = signal(T0 + 9000, "sell");
    let list = withSignal([], first);
    list = withSignal(list, second);
    expect(list).toEqual([first, second]);
    // the same signal again (a reconnect): nothing changes, not even the list itself
    expect(withSignal(list, { ...second })).toBe(list);
    // an older one that arrives late goes to its place
    const early = signal(T0 + 1000, "sell");
    expect(withSignal(list, early)).toEqual([early, first, second]);
    // a buy and a sell can share a second only under different rules, but the list tells them apart
    expect(withSignal(list, signal(T0 + 9000, "buy"))).toHaveLength(3);
  });

  it("counts the sides", () => {
    expect(countSides([signal(T0, "buy"), signal(T0 + 1000, "sell"), signal(T0 + 2000, "buy")])).toEqual({ buy: 2, sell: 1 });
    expect(countSides([])).toEqual({ buy: 0, sell: 0 });
  });

  it("says how often the rule has signalled", () => {
    expect(paceText(0, T0, T0 + 3_600_000)).toBeNull();
    expect(paceText(5, T0, null)).toBeNull();
    expect(paceText(80, T0, T0 + 3_600_000)).toBe("one every 45 s");
    expect(paceText(6, T0, T0 + 3_600_000)).toBe("one every 10 min");
    expect(paceText(2, T0, T0 + 8 * 3_600_000)).toBe("one every 4.0 h");
  });

  it("puts a triangle at the second of its signal, or at its minute on the chart of minutes", () => {
    const s = signal(T0 + 125_000, "sell", 1.2345);
    expect(markTime(s, "day", MINUTE)).toBe((T0 + 125_000) / 1000);
    expect(markTime(s, "history", MINUTE)).toBe((T0 + 120_000) / 1000);
    expect(marksOf([s], "day", MINUTE)).toEqual([{ time: (T0 + 125_000) / 1000, price: 1.2345, side: "sell" }]);
  });

  it("puts into words what the rule read, taking the backend's word for which condition held", () => {
    expect(reasonText(signal(T0, "buy", 1.1), DEFAULTS, 5)).toBe("RSI 24.5 ≤ 30, bid ≤ lower band 1.09980");
    expect(reasonText({ ...signal(T0, "buy", 1.1), by: "rsi" }, DEFAULTS, 5)).toBe("RSI 24.5 ≤ 30, bid > lower band 1.09980");
    expect(reasonText({ ...signal(T0, "sell", 1.1), rsi: 55.5, by: "band" }, DEFAULTS, 5)).toBe("RSI 55.5 < 70, bid ≥ upper band 1.10020");
    // a value that rounds onto its level is still on the side the backend says
    expect(reasonText({ ...signal(T0, "buy", 1.1), rsi: 30, by: "band" }, DEFAULTS, 5)).toBe("RSI 30.0 > 30, bid ≤ lower band 1.09980");
  });
});

describe("the triangles", () => {
  it("finds the first mark at or after a time", () => {
    const marks = [10, 20, 20, 30].map((time) => ({ time, price: 1, side: "buy" as const }));
    expect(firstAt(marks, 5)).toBe(0);
    expect(firstAt(marks, 20)).toBe(1);
    expect(firstAt(marks, 21)).toBe(3);
    expect(firstAt(marks, 31)).toBe(4);
    expect(firstAt([], 1)).toBe(0);
  });

  it("draws them large while they have room and small once they crowd", () => {
    expect(markSize(6, 5, 900)).toBe(8); // wide bars, few marks: as large as they get
    expect(markSize(0.3, 5, 900)).toBe(5); // thin bars, few marks: the smallest roomy size
    expect(markSize(0.01, 200, 900)).toBe(2); // a day of marks: as small as they get
    expect(markSize(0.3, 120, 900)).toBeCloseTo(3, 6); // in between: sized to what there is
    expect(markSize(1, 0, 900)).toBe(5);
  });
});

describe("the settings form", () => {
  it("round-trips the settings through the form", () => {
    const { settings, errors } = settingsOf(draftOf(DEFAULTS), LIMITS);
    expect(errors).toEqual({});
    expect(settings).toEqual(DEFAULTS);
    expect(settings && sameSettings(settings, DEFAULTS)).toBe(true);
  });

  it("reads a decimal comma and says what is wrong field by field", () => {
    const draft = { ...draftOf(DEFAULTS), bollinger_width: "2,5", rsi_period: "14.5", rsi_buy_below: "", channel_width: "9" };
    const { settings, errors } = settingsOf(draft, LIMITS);
    expect(settings).toBeNull();
    expect(errors).toEqual({ rsi_period: "must be a whole number", rsi_buy_below: "must be a number", channel_width: "must be 0.5 to 5" });
    const fixed = settingsOf({ ...draft, rsi_period: "9", rsi_buy_below: "25", channel_width: "1.5" }, LIMITS);
    expect(fixed.errors).toEqual({});
    expect(fixed.settings).toMatchObject({ bollinger_width: 2.5, rsi_period: 9, rsi_buy_below: 25, channel_width: 1.5 });
  });

  it("keeps the levels and the channel lengths in order", () => {
    expect(settingsOf({ ...draftOf(DEFAULTS), rsi_buy_below: "70" }, LIMITS).errors).toEqual({ rsi_sell_above: "must be above the buy level" });
    expect(settingsOf({ ...draftOf(DEFAULTS), channel_short_hours: "24" }, LIMITS).errors).toEqual({
      channel_medium_hours: "must be longer than short",
    });
    expect(settingsOf({ ...draftOf(DEFAULTS), channel_medium_hours: "240" }, LIMITS).errors).toEqual({
      channel_long_days: "must be longer than medium",
    });
  });

  it("refuses bands that no price could reach", () => {
    // four prices: none can be two standard deviations from their average (the most is 1.73)
    expect(settingsOf({ ...draftOf(DEFAULTS), bollinger_period: "4" }, LIMITS).errors).toEqual({
      bollinger_width: "must be below 1.73 for this period",
    });
    expect(settingsOf({ ...draftOf(DEFAULTS), bollinger_period: "4", bollinger_width: "1.5" }, LIMITS).errors).toEqual({});
    expect(settingsOf({ ...draftOf(DEFAULTS), bollinger_width: "4.4" }, LIMITS).errors).toEqual({
      bollinger_width: "must be below 4.36 for this period",
    });
  });

  it("carries the rule and the bar length, which are not typed", () => {
    const draft = { ...draftOf(DEFAULTS), rule: "either" as const, bar_seconds: 15 };
    expect(settingsOf(draft, LIMITS).settings).toMatchObject({ rule: "either", bar_seconds: 15 });
    expect(sameSettings({ ...DEFAULTS, bar_seconds: 15 }, DEFAULTS)).toBe(false);
  });

  it("says the rule in one line", () => {
    expect(ruleLine(DEFAULTS)).toBe("RSI 14 (30/70) and Bollinger 20 (2σ), 1 s bars");
    expect(ruleLine({ ...DEFAULTS, rule: "either", bar_seconds: 15 })).toBe("RSI 14 (30/70) or Bollinger 20 (2σ), 15 s bars");
  });
});
