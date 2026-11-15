import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type AutoscaleInfo,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type SeriesMarker,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { dateTimeUTC, price } from "../format";
import type { Bar } from "../types";
import { type Day, type Marker, type Report, type Trade, dayRange, isoDay, signedMoney, tradeMarkers } from "./model";

// The dashboard's colours on its panel surface.  Direction is teal / red as everywhere else;
// the outcome of a trade is said by the shape of its mark and by its text, colour only supports it.
const PANEL = "#101826";
const INK = "#e9e6dc";
const INK_2 = "#a8b3c4";
const INK_3 = "#7d8aa0";
const UP = "#14a89c";
const DOWN = "#e66767";
const GOOD = "#0ca30c";
const BAD = "#d03b3b";
const AMBER = "#f0b429";

const SECONDS: Record<string, number> = { "1s": 1, "5s": 5, "15s": 15, "30s": 30, "1m": 60, "5m": 300, "15m": 900, "30m": 1800, "1h": 3600 };

const chartOptions = {
  autoSize: true,
  layout: {
    background: { type: ColorType.Solid, color: PANEL },
    textColor: INK_3,
    fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
    fontSize: 11,
    panes: { separatorColor: "#223048", separatorHoverColor: "#34455f", enableResize: false },
  },
  grid: { vertLines: { visible: false }, horzLines: { color: "#1a2537" } },
  rightPriceScale: { borderColor: "#223048" },
  crosshair: {
    mode: CrosshairMode.Normal,
    vertLine: { color: "#506080", style: LineStyle.Dashed, labelBackgroundColor: "#223048" },
    horzLine: { color: "#506080", style: LineStyle.Dashed, labelBackgroundColor: "#223048" },
  },
} as const;

const dayTime = (day: string) => (Date.parse(`${day}T00:00:00Z`) / 1000) as UTCTimestamp;

/** Running total by trading day (as tested, and with one clip only) above the result of each day. */
export function EquityChart({
  report,
  selected,
  onSelect,
}: {
  report: Report;
  selected: string | null;
  onSelect: (day: string) => void;
}) {
  const container = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<string | null>(null);
  const select = useRef(onSelect);
  select.current = onSelect;

  const single = report.without_adding?.daily ?? null;
  const byDay = useMemo(() => new Map(report.daily.map((d) => [d.day, d])), [report.daily]);
  const singleByDay = useMemo(() => new Map((single ?? []).map((d) => [d.day, d])), [single]);

  useEffect(() => {
    if (!container.current) return;
    const chart = createChart(container.current, {
      ...chartOptions,
      timeScale: { borderColor: "#223048", timeVisible: false, rightOffset: 2 },
    });
    const money = { type: "custom" as const, minMove: 1, formatter: (v: number) => Math.round(v).toLocaleString("en-US") };
    if (single) {
      chart
        .addSeries(LineSeries, {
          color: INK_2,
          lineWidth: 1,
          lineStyle: LineStyle.LargeDashed,
          priceFormat: money,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerRadius: 3,
        })
        .setData(single.map((d) => ({ time: dayTime(d.day), value: d.cumulative })));
    }
    const total = chart.addSeries(LineSeries, {
      color: INK,
      lineWidth: 2,
      priceFormat: money,
      priceLineVisible: false,
      crosshairMarkerRadius: 4,
    });
    total.setData(report.daily.map((d) => ({ time: dayTime(d.day), value: d.cumulative })));
    total.createPriceLine({ price: 0, color: "#34455f", lineWidth: 1, lineStyle: LineStyle.Solid, axisLabelVisible: false });
    if (report.split_day && byDay.has(report.split_day)) {
      createSeriesMarkers(total, [
        { time: dayTime(report.split_day), position: "aboveBar", shape: "arrowDown", color: AMBER, text: "out-of-sample from here" },
      ]);
    }
    const days = chart.addSeries(
      HistogramSeries,
      { priceFormat: money, priceLineVisible: false, lastValueVisible: false },
      1,
    );
    days.setData(report.daily.map((d) => ({ time: dayTime(d.day), value: d.pnl, color: d.pnl >= 0 ? UP : DOWN })));
    chart.panes()[0]?.setStretchFactor(0.68);
    chart.panes()[1]?.setStretchFactor(0.32);
    chart.timeScale().fitContent();
    chart.subscribeCrosshairMove((param) => setHover(typeof param.time === "number" ? isoDay(param.time * 1000) : null));
    chart.subscribeClick((param) => {
      if (typeof param.time === "number") select.current(isoDay(param.time * 1000));
    });
    return () => chart.remove();
  }, [report, single, byDay]);

  const shown = byDay.get(hover ?? "") ?? byDay.get(selected ?? "") ?? report.daily[report.daily.length - 1] ?? null;
  const shownSingle = shown ? singleByDay.get(shown.day) : undefined;
  return (
    <section className="panel" aria-label="Running total and daily results">
      <header className="panel-head">
        <h2 className="panel-title">Running total by trading day</h2>
        <div className="note flex flex-wrap items-center gap-x-4 gap-y-1">
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden className="inline-block w-5 border-t-2" style={{ borderColor: INK }} />
            {report.request.execution.max_clips > 1 ? `as tested (up to ${report.request.execution.max_clips} clips)` : "running total"}
          </span>
          {single ? (
            <span className="inline-flex items-center gap-1.5">
              <span aria-hidden className="inline-block w-5 border-t border-dashed" style={{ borderColor: INK_2 }} />
              one clip only
            </span>
          ) : null}
          <span className="inline-flex items-center gap-1.5">
            <span aria-hidden className="inline-block h-2.5 w-1.5" style={{ background: UP }} />
            <span aria-hidden className="inline-block h-2.5 w-1.5" style={{ background: DOWN }} />
            result of the day
          </span>
        </div>
      </header>
      <div className="flex min-h-[26px] flex-wrap items-center gap-x-4 gap-y-0.5 px-3 pt-2 text-[12px]">
        {shown ? (
          <>
            <span className="note num">{shown.day}</span>
            <span>
              <span className="text-ink-3">day</span> <span className="num">{signedMoney(shown.pnl)}</span>
            </span>
            <span>
              <span className="text-ink-3">trades</span> <span className="num">{shown.trades}</span>
            </span>
            <span>
              <span className="text-ink-3">total</span> <span className="num">{signedMoney(shown.cumulative)}</span>
            </span>
            {shownSingle ? (
              <span>
                <span className="text-ink-3">one clip only</span> <span className="num">{signedMoney(shownSingle.cumulative)}</span>
              </span>
            ) : null}
          </>
        ) : null}
        <span className="note ml-auto">{report.currency}; click a day to see its trades on the price chart</span>
      </div>
      <div ref={container} className="mt-1.5 h-[320px] sm:h-[400px]" />
    </section>
  );
}

function candle(bar: Bar) {
  const up = bar.close >= bar.open;
  const stroke = up ? UP : DOWN;
  return {
    time: Math.floor(bar.ts / 1000) as UTCTimestamp,
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    color: up ? PANEL : stroke,
    borderColor: stroke,
    wickColor: stroke,
  };
}

const MARK_COLOR: Record<Marker["kind"], (long: boolean) => string> = {
  entry: (long) => (long ? UP : DOWN),
  add: (long) => (long ? UP : DOWN),
  target: () => GOOD,
  stop: () => BAD,
  timed: () => INK_2,
};

/** The bars of one trading day with every entry, clip and exit of that day marked on them. */
export function DayChart({
  symbol,
  precision,
  report,
  day,
  trades,
  focus,
  storedTimeframes,
}: {
  symbol: string;
  precision: number;
  report: Report;
  day: Day;
  trades: Trade[];
  focus: Trade | null;
  storedTimeframes: string[];
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const series = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const lines = useRef<IPriceLine[]>([]);
  const lookup = useRef(new Map<number, Bar>());
  const [state, setState] = useState<{ loading: boolean; error: string | null; bars: number }>({
    loading: true,
    error: null,
    bars: 0,
  });
  const [hover, setHover] = useState<Bar | null>(null);

  // bars finer than a minute would be tens of thousands for a day: show the minute bars instead when they exist
  const base = report.request.base_timeframe;
  const timeframe = (SECONDS[base] ?? 60) < 60 && storedTimeframes.includes("1m") ? "1m" : base;
  const barSeconds = SECONDS[timeframe] ?? 60;

  useEffect(() => {
    if (!container.current) return;
    const c = createChart(container.current, {
      ...chartOptions,
      rightPriceScale: { borderColor: "#223048", scaleMargins: { top: 0.14, bottom: 0.12 } },
      timeScale: { borderColor: "#223048", timeVisible: true, secondsVisible: false, rightOffset: 3 },
    });
    chart.current = c;
    c.subscribeCrosshairMove((param) => setHover(typeof param.time === "number" ? (lookup.current.get(param.time) ?? null) : null));
    return () => {
      c.remove();
      chart.current = null;
      series.current = null;
      lines.current = [];
    };
  }, []);

  // --- the bars and the marks of the day --------------------------------------------------
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    let cancelled = false;
    const priceFormat = { type: "price" as const, precision, minMove: 10 ** -precision };
    const s = c.addSeries(CandlestickSeries, { priceFormat, borderVisible: true, priceLineVisible: false });
    series.current = s;
    lines.current = [];
    lookup.current = new Map();
    setHover(null);
    setState({ loading: true, error: null, bars: 0 });
    const range = dayRange(day, trades, barSeconds);
    const load = range
      ? api.bars(symbol, timeframe, "provider", 20_000, range.start, range.end).then((r) => r.bars)
      : Promise.resolve<Bar[]>([]);
    load
      .then((bars) => {
        if (cancelled) return;
        for (const bar of bars) lookup.current.set(Math.floor(bar.ts / 1000), bar);
        s.setData(bars.map(candle));
        // a mark needs a bar to sit on; one whose bar is missing from the chart is left out
        const marks: SeriesMarker<UTCTimestamp>[] = tradeMarkers(trades, barSeconds)
          .filter((m) => lookup.current.has(m.time))
          .map((m) => ({
            time: m.time as UTCTimestamp,
            position: m.position,
            shape: m.shape,
            color: MARK_COLOR[m.kind](m.shape === "arrowUp"),
            text: m.text,
            size: m.kind === "add" ? 0.8 : 1.2,
          }));
        createSeriesMarkers(s, marks);
        c.timeScale().fitContent();
        setState({ loading: false, error: null, bars: bars.length });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ loading: false, error: error instanceof Error ? error.message : String(error), bars: 0 });
      });
    return () => {
      cancelled = true;
      if (chart.current === c) c.removeSeries(s);
      if (series.current === s) series.current = null;
    };
  }, [symbol, precision, timeframe, barSeconds, day, trades]);

  // --- the levels of the trade in focus ---------------------------------------------------
  useEffect(() => {
    const s = series.current;
    if (!s || state.loading) return;
    for (const line of lines.current) s.removePriceLine(line);
    lines.current = [];
    // the price scale fits the bars; with a trade in focus it also has to reach its stop and its target
    s.applyOptions({
      autoscaleInfoProvider: (original: () => AutoscaleInfo | null) => {
        const fitted = original();
        if (!fitted?.priceRange || !focus) return fitted;
        return {
          ...fitted,
          priceRange: {
            minValue: Math.min(fitted.priceRange.minValue, focus.stop_price, focus.target_price),
            maxValue: Math.max(fitted.priceRange.maxValue, focus.stop_price, focus.target_price),
          },
        };
      },
    });
    if (!focus) return;
    const add = (value: number, color: string, title: string, style: LineStyle) =>
      lines.current.push(s.createPriceLine({ price: value, color, lineWidth: 1, lineStyle: style, title, axisLabelVisible: true }));
    add(focus.average_price, INK_2, `#${focus.n} average entry`, LineStyle.Solid);
    add(focus.target_price, GOOD, "target", LineStyle.Dashed);
    add(focus.stop_price, BAD, "stop", LineStyle.Dashed);
    const c = chart.current;
    if (c) {
      // from an hour before the signal to an hour after the exit
      c.timeScale().setVisibleRange({
        from: (Math.floor(focus.signal_ms / 1000) - 3600) as UTCTimestamp,
        to: (Math.floor(focus.exit_ms / 1000) + 3600) as UTCTimestamp,
      });
    }
    container.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [focus, state.loading]);

  const field = (label: string, value: number) => (
    <span>
      <span className="text-ink-3">{label}</span> <span className="num">{price(value, precision)}</span>
    </span>
  );
  return (
    <section className="panel" aria-label="Price chart of the selected day">
      <header className="panel-head">
        <h2 className="panel-title">
          {day.day}: {day.trades} {day.trades === 1 ? "trade" : "trades"}, {signedMoney(day.pnl)} {report.currency}
        </h2>
        <div className="note flex flex-wrap items-center gap-x-4 gap-y-1">
          <span>
            <span aria-hidden style={{ color: UP }}>
              ▲
            </span>{" "}
            buy,{" "}
            <span aria-hidden style={{ color: DOWN }}>
              ▼
            </span>{" "}
            sell, +n clip added
          </span>
          <span>
            <span aria-hidden style={{ color: GOOD }}>
              ●
            </span>{" "}
            target
          </span>
          <span>
            <span aria-hidden style={{ color: BAD }}>
              ■
            </span>{" "}
            stop
          </span>
          <span>
            <span aria-hidden style={{ color: INK_2 }}>
              ■
            </span>{" "}
            closed by the clock
          </span>
        </div>
      </header>
      <div className="flex min-h-[26px] flex-wrap items-center gap-x-4 gap-y-0.5 px-3 pt-2 text-[12px]">
        {hover ? (
          <span className="flex flex-wrap items-center gap-x-3">
            <span className="note num">{dateTimeUTC(hover.ts)}</span>
            {field("O", hover.open)}
            {field("H", hover.high)}
            {field("L", hover.low)}
            {field("C", hover.close)}
          </span>
        ) : focus ? (
          <span className="flex flex-wrap items-center gap-x-3">
            <span className="note">trade #{focus.n}</span>
            {field("average entry", focus.average_price)}
            {field("target", focus.target_price)}
            {field("stop", focus.stop_price)}
          </span>
        ) : null}
        <span className="note ml-auto">{timeframe} bid bars from the provider's history, UTC</span>
      </div>
      <div className="relative mt-1.5 h-[320px] sm:h-[420px]">
        <div ref={container} className="absolute inset-0" />
        {state.loading || state.error || state.bars === 0 ? (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center px-6 text-center">
            <p className="m-0 max-w-[46ch] bg-panel/90 px-3 py-2 text-[13px] text-ink-2">
              {state.error
                ? `Could not load the bars of this day: ${state.error}`
                : state.loading
                  ? "Loading…"
                  : "No bars are stored for this day."}
            </p>
          </div>
        ) : null}
      </div>
    </section>
  );
}
