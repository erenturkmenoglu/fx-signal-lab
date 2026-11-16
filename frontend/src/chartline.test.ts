import { describe, expect, it } from "vitest";
import { HoleyLine, type LinePoint, UNSEEN, withHoles } from "./chartline";

/** Stands in for a line series: keeps what it was given, in the order of the calls. */
class Recorder {
  calls: unknown[] = [];
  setData(rows: unknown[]) {
    this.calls.push(["set", rows]);
  }
  update(row: unknown, historical?: boolean) {
    this.calls.push(historical ? ["update", row, "older"] : ["update", row]);
  }
}

describe("a line with holes", () => {
  it("hides the stroke from a price to the next one when points without a price lie between", () => {
    const points: LinePoint[] = [{ time: 1, value: 10 }, { time: 2, value: 11 }, { time: 3 }, { time: 4 }, { time: 5, value: 12 }, { time: 6, value: 13 }];
    expect(withHoles(points)).toEqual([
      { time: 1, value: 10 },
      { time: 2, value: 11, color: UNSEEN }, // the stroke from here would cross the hole
      { time: 3 },
      { time: 4 },
      { time: 5, value: 12 },
      { time: 6, value: 13 },
    ]);
    expect(points[1]).toEqual({ time: 2, value: 11 }); // what was passed in is left alone
  });

  it("leaves the newest price as it is while only empty points follow it", () => {
    expect(withHoles([{ time: 1, value: 10 }, { time: 2 }, { time: 3 }])).toEqual([{ time: 1, value: 10 }, { time: 2 }, { time: 3 }]);
    expect(withHoles([])).toEqual([]);
    expect(withHoles([{ time: 1 }, { time: 2, value: 5 }])).toEqual([{ time: 1 }, { time: 2, value: 5 }]);
  });

  it("treats a longer step between two prices as a hole when the step is known", () => {
    const minutes: LinePoint[] = [{ time: 0, value: 1 }, { time: 60, value: 2 }, { time: 180, value: 3 }, { time: 240, value: 4 }];
    expect(withHoles(minutes, 60).map((p) => p.color)).toEqual([undefined, UNSEEN, undefined, undefined]);
    expect(withHoles(minutes).map((p) => p.color)).toEqual([undefined, undefined, undefined, undefined]);
  });

  it("does not draw a price that stands alone between two holes", () => {
    const rows = withHoles([{ time: 1, value: 1 }, { time: 2 }, { time: 3, value: 2 }, { time: 4 }, { time: 5, value: 3 }]);
    expect(rows.map((p) => p.color)).toEqual([UNSEEN, undefined, UNSEEN, undefined, undefined]);
  });
});

describe("a line fed point by point", () => {
  it("goes on like the whole line would have been drawn", () => {
    const series = new Recorder();
    const line = new HoleyLine(series);
    line.set([{ time: 1, value: 10 }, { time: 2, value: 11 }]);
    line.push(3, 12); // next to the last price: an ordinary point
    line.push(4, null); // no price
    line.push(5, null);
    line.push(6, 13); // a price again: the stroke from 3 to 6 is hidden, by changing the older point
    line.push(7, 14);
    expect(series.calls).toEqual([
      ["set", [{ time: 1, value: 10 }, { time: 2, value: 11 }]],
      ["update", { time: 3, value: 12 }],
      ["update", { time: 4 }],
      ["update", { time: 5 }],
      ["update", { time: 3, value: 12, color: UNSEEN }, "older"],
      ["update", { time: 6, value: 13 }],
      ["update", { time: 7, value: 14 }],
    ]);
  });

  it("knows a hole at the end of what it was given", () => {
    const series = new Recorder();
    const line = new HoleyLine(series);
    line.set([{ time: 1, value: 10 }, { time: 2 }]);
    line.push(3, 11);
    expect(series.calls.slice(1)).toEqual([
      ["update", { time: 1, value: 10, color: UNSEEN }, "older"],
      ["update", { time: 3, value: 11 }],
    ]);
  });

  it("starts from nothing, and from a line that has no price yet", () => {
    const series = new Recorder();
    const line = new HoleyLine(series);
    line.push(5, null);
    line.push(6, 1);
    line.push(7, 2);
    expect(series.calls).toEqual([
      ["update", { time: 5 }],
      ["update", { time: 6, value: 1 }],
      ["update", { time: 7, value: 2 }],
    ]);
  });

  it("moves the point of the minute in progress without breaking the line", () => {
    const series = new Recorder();
    const line = new HoleyLine(series, 60);
    line.set([{ time: 0, value: 1 }, { time: 60, value: 2 }]);
    line.push(120, 3);
    line.push(120, 3.5); // the same minute, a newer price
    line.push(180, 4); // the next minute
    line.push(360, 5); // minutes are missing before this one: the stroke to it is hidden
    expect(series.calls.slice(1)).toEqual([
      ["update", { time: 120, value: 3 }],
      ["update", { time: 120, value: 3.5 }],
      ["update", { time: 180, value: 4 }],
      ["update", { time: 180, value: 4, color: UNSEEN }], // still the newest point: changed in place
      ["update", { time: 360, value: 5 }],
    ]);
  });

  it("forgets a newest price that is taken back", () => {
    const series = new Recorder();
    const line = new HoleyLine(series);
    line.push(1, 10);
    line.push(1, null); // the same second, now without a price
    line.push(2, 11);
    expect(series.calls).toEqual([
      ["update", { time: 1, value: 10 }],
      ["update", { time: 1 }],
      ["update", { time: 2, value: 11 }],
    ]);
  });
});
