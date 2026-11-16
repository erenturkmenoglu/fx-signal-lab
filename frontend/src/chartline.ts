// A price line that shows where there was no price.
//
// The chart library joins one valued point to the next with a straight stroke, also across points
// that carry no value: seconds without a price would be bridged by a line that reads as prices.
// The stroke between two valued points takes the colour of the first of them.  So a valued point
// that has a hole between it and the next valued point is given a colour that cannot be seen, and
// the stretch across the hole stays empty.
//
// One consequence: a price that stands alone between two holes has no stroke on either side and is
// not drawn.  The crosshair still reads it.

import type { ISeriesApi, LineData, UTCTimestamp, WhitespaceData } from "lightweight-charts";

/** The colour of a stroke that must not be seen. */
export const UNSEEN = "rgba(0, 0, 0, 0)";

export interface LinePoint {
  time: number; // epoch seconds
  /** left out: no price at this time */
  value?: number;
  color?: string;
}

type Row = LineData<UTCTimestamp> | WhitespaceData<UTCTimestamp>;

/**
 * The points of a line with its holes kept.  A hole is a point without a value, and, when `step`
 * (seconds between neighbouring points) is given, a longer step between two valued points.
 */
export function withHoles(points: readonly LinePoint[], step?: number): LinePoint[] {
  const out: LinePoint[] = points.map((p) => (p.value == null ? { time: p.time } : { time: p.time, value: p.value }));
  let last: LinePoint | null = null; // the newest valued point so far
  let hole = false; // a point without a value came after it
  for (const point of out) {
    if (point.value == null) {
      hole = true;
      continue;
    }
    if (last && (hole || (step != null && point.time - last.time > step))) last.color = UNSEEN;
    last = point;
    hole = false;
  }
  return out;
}

/** Feeds a line series, whole or one point at a time, and keeps its holes. */
export class HoleyLine {
  private last: { time: number; value: number } | null = null; // the newest valued point
  private hole = false; // a point without a value came after it

  constructor(
    private readonly series: Pick<ISeriesApi<"Line">, "setData" | "update">,
    private readonly step?: number,
  ) {}

  set(points: readonly LinePoint[]): void {
    this.series.setData(withHoles(points, this.step) as Row[]);
    this.last = null;
    this.hole = false;
    for (let i = points.length - 1; i >= 0; i--) {
      const point = points[i] as LinePoint;
      if (point.value != null) {
        this.last = { time: point.time, value: point.value };
        break;
      }
      this.hole = true;
    }
  }

  /** One more point, at the newest time or after it.  `value` null: no price at that time. */
  push(time: number, value: number | null): void {
    const last = this.last;
    if (value == null) {
      this.series.update({ time } as Row);
      if (last && last.time === time) this.last = null; // the newest price was taken back
      else this.hole = true;
      return;
    }
    if (last && last.time < time && (this.hole || (this.step != null && time - last.time > this.step))) {
      // the stroke from the last price to this one would cross a hole: it is not to be seen.  The
      // last price is no longer the newest point once empty ones came after it: an older point is changed.
      this.series.update({ time: last.time, value: last.value, color: UNSEEN } as Row, this.hole);
    }
    this.series.update({ time, value } as Row);
    this.last = { time, value };
    this.hole = false;
  }
}
