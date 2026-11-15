import { describe, expect, it } from "vitest";
import { dateTimeUTC, detailText, duration, latencyMs, parseUTC, signed, splitPrice, timeUTC } from "./format";

describe("splitPrice", () => {
  it("splits a 5-decimal major into big figure, pips and fractional pip", () => {
    expect(splitPrice(1.16019, 5, 0.0001)).toEqual({ big: "1.16", pips: "01", frac: "9" });
    expect(splitPrice(1.1, 5, 0.0001)).toEqual({ big: "1.10", pips: "00", frac: "0" });
  });
  it("follows the pip size for JPY pairs", () => {
    expect(splitPrice(154.305, 3, 0.01)).toEqual({ big: "154.", pips: "30", frac: "5" });
  });
  it("handles emerging-market levels", () => {
    expect(splitPrice(42.01234, 5, 0.0001)).toEqual({ big: "42.01", pips: "23", frac: "4" });
  });
  it("falls back to the plain price when the convention does not apply", () => {
    expect(splitPrice(1234.5, 1, 1)).toEqual({ big: "1234.5", pips: "", frac: "" });
  });
});

describe("time", () => {
  const t = Date.UTC(2026, 10, 12, 14, 5, 9);
  it("formats in UTC regardless of the browser zone", () => {
    expect(timeUTC(t)).toBe("14:05:09");
    expect(dateTimeUTC(t)).toBe("2026-11-12 14:05");
    expect(dateTimeUTC(t, true)).toBe("2026-11-12 14:05:09");
    expect(timeUTC(null)).toBe("—");
  });
  it("parses user input as UTC and rejects nonsense", () => {
    expect(parseUTC("2026-11-12 14:05")).toBe(Date.UTC(2026, 10, 12, 14, 5));
    expect(parseUTC("2026-11-12")).toBe(Date.UTC(2026, 10, 12));
    expect(parseUTC("2026-11-12T14:05:09Z")).toBe(t);
    expect(parseUTC("2026-02-30 10:00")).toBeNull();
    expect(parseUTC("2026-11-12 25:00")).toBeNull();
    expect(parseUTC("yesterday")).toBeNull();
  });
});

describe("duration and numbers", () => {
  it("scales the unit", () => {
    expect(duration(0)).toBe("<1 ms");
    expect(duration(85)).toBe("85 ms");
    expect(duration(3200)).toBe("3.2 s");
    expect(duration(245_000)).toBe("4 m 05 s");
    expect(duration(7_800_000)).toBe("2 h 10 m");
    expect(duration(273_600_000)).toBe("3 d 4 h");
    expect(duration(null)).toBe("—");
  });
  it("shows the sign explicitly", () => {
    expect(signed(3.21)).toBe("+3.2");
    expect(signed(-0.04, 2)).toBe("−0.04");
    expect(signed(0)).toBe("0.0");
  });
  it("never shows a measured latency as zero", () => {
    expect(latencyMs(0)).toBe("<1 ms");
    expect(latencyMs(0.4)).toBe("<1 ms");
    expect(latencyMs(1)).toBe("1 ms");
    expect(latencyMs(182.6)).toBe("183 ms");
    expect(latencyMs(-12)).toBe("-12 ms"); // clock skew: shown as measured, flagged by the note
    expect(latencyMs(null)).toBe("—");
  });
  it("renders event details on one line", () => {
    expect(detailText({ code: 1006, reason: "", streamed_s: 12.5, channels: ["C.EUR-USD"], synthetic: true })).toBe(
      'code=1006  streamed_s=12.5  channels=["C.EUR-USD"]',
    );
  });
});
