// The lanes under the price chart, drawn by hand: the chart library's own marks cannot be made
// smaller than a dozen pixels, and a week holds thousands of signals.

import type {
  IChartApiBase,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  Logical,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";

export interface Tick {
  /** position of the chart bar on the time scale */
  index: number;
  /** value on the price scale of the lane's line */
  level: number;
  direction: 1 | -1;
  /** colour of a signal that was traded; null for one that was not */
  color: string | null;
}

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];

/**
 * Draws the signals on the lanes of a series: a small triangle (up: buy, down: sell) for a signal
 * that was traded, a short tick under the line (buy) or over it (sell) for one that was not.
 */
export class LanesPrimitive implements ISeriesPrimitive<Time> {
  private chart: IChartApiBase<Time> | null = null;
  private series: ISeriesApi<SeriesType, Time> | null = null;
  private refresh: (() => void) | null = null;
  private ticks: Tick[] = [];
  private readonly views: readonly IPrimitivePaneView[];

  constructor(
    private readonly quiet: string,
    private readonly surface: string,
  ) {
    const renderer: IPrimitivePaneRenderer = { draw: (target) => this.draw(target) };
    this.views = [{ zOrder: () => "normal", renderer: () => renderer }];
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

  setTicks(ticks: Tick[]): void {
    this.ticks = ticks;
    this.refresh?.();
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  private draw(target: Target): void {
    const chart = this.chart;
    const series = this.series;
    if (!chart || !series || this.ticks.length === 0) return;
    const scale = chart.timeScale();
    target.useBitmapCoordinateSpace(({ context: ctx, horizontalPixelRatio: h, verticalPixelRatio: v, bitmapSize }) => {
      const levels = new Map<number, number | null>();
      const yOf = (level: number) => {
        let y = levels.get(level);
        if (y === undefined) {
          y = series.priceToCoordinate(level);
          levels.set(level, y);
        }
        return y;
      };
      const place = (tick: Tick): [number, number] | null => {
        const x = scale.logicalToCoordinate(tick.index as Logical);
        const y = yOf(tick.level);
        if (x == null || y == null || x * h < -12 || x * h > bitmapSize.width + 12) return null;
        return [Math.round(x * h), Math.round(y * v)];
      };
      // signals that were not traded first, so that a trade is never hidden under them
      const width = Math.max(1, Math.round(1.5 * h));
      ctx.fillStyle = this.quiet;
      for (const tick of this.ticks) {
        if (tick.color != null) continue;
        const at = place(tick);
        if (!at) continue;
        const top = tick.direction > 0 ? at[1] + Math.round(2 * v) : at[1] - Math.round(7 * v);
        ctx.fillRect(at[0] - Math.floor(width / 2), top, width, Math.round(5 * v));
      }
      ctx.lineWidth = Math.max(1, Math.round(h));
      ctx.strokeStyle = this.surface; // a rim in the colour of the surface keeps neighbours apart
      for (const tick of this.ticks) {
        if (tick.color == null) continue;
        const at = place(tick);
        if (!at) continue;
        const [x, y] = at;
        const half = 4.5 * h;
        const rise = 4.5 * v * tick.direction;
        ctx.beginPath();
        ctx.moveTo(x - half, y + rise);
        ctx.lineTo(x + half, y + rise);
        ctx.lineTo(x, y - rise);
        ctx.closePath();
        ctx.fillStyle = tick.color;
        ctx.fill();
        ctx.stroke();
      }
    });
  }
}
