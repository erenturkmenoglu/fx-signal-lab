// What a pane of the chart carries besides its series: a caption in its corner, and upright lines
// where momentum changed.  Drawn by hand: the chart library has neither.

import type {
  IChartApiBase,
  IPanePrimitive,
  IPanePrimitivePaneView,
  IPrimitivePaneRenderer,
  Logical,
  PaneAttachedParameter,
  Time,
} from "lightweight-charts";

export interface Rule {
  /** position of the chart bar on the time scale: the line stands on its left edge */
  index: number;
  color: string;
}

type Target = Parameters<IPrimitivePaneRenderer["draw"]>[0];

export class PaneNotes implements IPanePrimitive<Time> {
  private chart: IChartApiBase<Time> | null = null;
  private refresh: (() => void) | null = null;
  private rules: Rule[] = [];
  private readonly views: readonly IPanePrimitivePaneView[];

  constructor(
    private readonly caption: string,
    private readonly ink: string,
    private readonly surface: string,
    private readonly font: string,
  ) {
    const lines: IPrimitivePaneRenderer = { draw: (target) => this.drawRules(target) };
    const text: IPrimitivePaneRenderer = { draw: (target) => this.drawCaption(target) };
    this.views = [
      { zOrder: () => "bottom", renderer: () => lines }, // under the bars
      { zOrder: () => "top", renderer: () => text },
    ];
  }

  attached(param: PaneAttachedParameter<Time>): void {
    this.chart = param.chart;
    this.refresh = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.refresh = null;
  }

  setRules(rules: Rule[]): void {
    this.rules = rules;
    this.refresh?.();
  }

  paneViews(): readonly IPanePrimitivePaneView[] {
    return this.views;
  }

  private drawRules(target: Target): void {
    const chart = this.chart;
    if (!chart || this.rules.length === 0) return;
    const scale = chart.timeScale();
    const half = scale.options().barSpacing / 2;
    target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
      ctx.save();
      ctx.lineWidth = 1;
      ctx.globalAlpha = 0.7;
      ctx.setLineDash([3, 3]);
      for (const rule of this.rules) {
        const centre = scale.logicalToCoordinate(rule.index as Logical);
        if (centre == null) continue;
        const x = Math.round(centre - half) + 0.5;
        if (x < -1 || x > mediaSize.width + 1) continue;
        ctx.strokeStyle = rule.color;
        ctx.beginPath();
        ctx.moveTo(x, 0);
        ctx.lineTo(x, mediaSize.height);
        ctx.stroke();
      }
      ctx.restore();
    });
  }

  private drawCaption(target: Target): void {
    if (!this.caption) return;
    target.useMediaCoordinateSpace(({ context: ctx }) => {
      ctx.save();
      ctx.font = this.font;
      ctx.textBaseline = "top";
      const width = ctx.measureText(this.caption).width;
      ctx.globalAlpha = 0.85;
      ctx.fillStyle = this.surface; // a plate under the words keeps them readable over bars
      ctx.fillRect(4, 3, width + 8, 16);
      ctx.globalAlpha = 1;
      ctx.fillStyle = this.ink;
      ctx.fillText(this.caption, 8, 6);
      ctx.restore();
    });
  }
}
