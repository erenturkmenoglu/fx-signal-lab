// The triangles of the live chart: one where each order was signalled.  A buy points up from
// under the price, a sell points down from above it.  Drawn by hand, because the chart library's
// own markers are arrows and cannot be asked for by time alone.

import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";
import type { Mark } from "./model";

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];

/** Below this size the marks count as a crowd. */
const CROWDED = 3.5;

/** Half the width of a triangle in pixels: large while the marks have room, small once they crowd. */
export function markSize(barSpacing: number, visible: number, width: number): number {
  const roomy = Math.max(5, Math.min(8, barSpacing * 2.2));
  if (visible === 0 || width <= 0) return roomy;
  const each = width / visible; // pixels of chart for every mark in sight
  if (each >= roomy * 2.5) return roomy;
  return Math.max(2, Math.min(roomy, each / 2.5));
}

/** The first position in `marks` (ascending in time) whose time is not before `time`. */
export function firstAt(marks: readonly Mark[], time: number): number {
  let low = 0;
  let high = marks.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if ((marks[middle] as Mark).time < time) low = middle + 1;
    else high = middle;
  }
  return low;
}

export class SignalMarks implements ISeriesPrimitive<Time> {
  private chart: IChartApiBase<Time> | null = null;
  private series: ISeriesApi<SeriesType, Time> | null = null;
  private refresh: (() => void) | null = null;
  private marks: Mark[] = []; // ascending in time
  private chosen: number | null = null; // the time of the mark that is picked out
  private readonly views: readonly IPrimitivePaneView[];

  constructor(
    private readonly buy: string,
    private readonly sell: string,
    private readonly surface: string,
    private readonly ink: string,
  ) {
    // marks that have room stand over the price; a crowd of them goes under it, so that the price stays in sight
    const over: IPrimitivePaneRenderer = { draw: (target) => this.draw(target, false) };
    const under: IPrimitivePaneRenderer = { draw: (target) => this.draw(target, true) };
    this.views = [
      { zOrder: () => "bottom", renderer: () => under },
      { zOrder: () => "top", renderer: () => over },
    ];
  }

  attached(param: SeriesAttachedParameter<Time>): void {
    this.chart = param.chart;
    this.series = param.series;
    this.refresh = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
    this.refresh = null;
  }

  setMarks(marks: Mark[]): void {
    this.marks = marks;
    this.refresh?.();
  }

  /** Ring the mark at this time (the row of the table that was picked); null takes the ring away. */
  choose(time: number | null): void {
    this.chosen = time;
    this.refresh?.();
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  private draw(target: Target, crowd: boolean): void {
    const { chart, series, marks } = this;
    if (!chart || !series || marks.length === 0) return;
    const scale = chart.timeScale();
    const visible = scale.getVisibleRange();
    if (!visible) return;
    const from = firstAt(marks, Number(visible.from));
    const to = firstAt(marks, Number(visible.to) + 1); // one past the last mark in sight
    if (to <= from) return;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      const size = markSize(scale.options().barSpacing, to - from, mediaSize.width);
      const edged = size >= CROWDED; // a thin edge keeps neighbours apart; crowded marks go without
      if (edged === crowd) return; // drawn by the other layer
      const gap = edged ? 5 : 3; // the point looks at the price from a little away
      ctx.save();
      ctx.lineJoin = "round";
      ctx.lineWidth = 1;
      ctx.strokeStyle = this.surface;
      let ring: { x: number; y: number } | null = null;
      for (let i = from; i < to; i++) {
        const mark = marks[i] as Mark;
        const x = scale.timeToCoordinate(mark.time as Time);
        const y = series.priceToCoordinate(mark.price);
        if (x == null || y == null) continue;
        const up = mark.side === "buy";
        const tip = up ? y + gap : y - gap;
        const base = up ? tip + size * 1.6 : tip - size * 1.6;
        ctx.beginPath();
        ctx.moveTo(x, tip);
        ctx.lineTo(x - size, base);
        ctx.lineTo(x + size, base);
        ctx.closePath();
        ctx.fillStyle = up ? this.buy : this.sell;
        ctx.fill();
        if (edged) ctx.stroke();
        if (this.chosen === mark.time) ring = { x, y: (tip + base) / 2 };
      }
      if (ring) {
        ctx.beginPath();
        ctx.arc(ring.x, ring.y, Math.max(9, size * 2.1), 0, Math.PI * 2);
        ctx.lineWidth = 1.5;
        ctx.strokeStyle = this.ink;
        ctx.stroke();
      }
      ctx.restore();
    });
  }
}
