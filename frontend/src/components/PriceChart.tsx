import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  createTextWatermark,
  type CandlestickData,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { dateTimeUTC, int, price } from "../format";
import type { Live } from "../live";
import type { Bar, InstrumentStatus, Obs, ObsMessage } from "../types";

const TIMEFRAMES = ["1s", "5s", "15s", "30s", "1m", "5m", "15m", "30m", "1h", "4h", "1d"] as const;
type Timeframe = (typeof TIMEFRAMES)[number];
export type ChartSource = "live" | "provider";

// Chart colours (validated on this surface with the palette checker: teal/red keep a
// colour-vision-deficiency distance of 9.6; up candles are also hollow, down candles filled).
const PANEL = "#101826";
const UP = "#14a89c";
const DOWN = "#e66767";
const DIM = 0.4;

const toTime = (ms: number) => Math.floor(ms / 1000) as UTCTimestamp;
const alpha = (hex: string, a: number) => {
  const n = Number.parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
};

function candle(bar: Bar): CandlestickData<UTCTimestamp> {
  const up = bar.close >= bar.open;
  // bars with seconds missing from the grid are drawn faded: visible, but not to be trusted blindly
  const stroke = bar.complete === false && !bar.partial ? alpha(up ? UP : DOWN, DIM) : up ? UP : DOWN;
  return {
    time: toTime(bar.ts),
    open: bar.open,
    high: bar.high,
    low: bar.low,
    close: bar.close,
    color: up ? PANEL : stroke,
    borderColor: stroke,
    wickColor: stroke,
  };
}

function point(obs: Obs): LineData<UTCTimestamp> | WhitespaceData<UTCTimestamp> {
  // stale and missing seconds leave a hole in the line; they are never bridged
  const usable = (obs.status === "fresh" || obs.status === "carried") && obs.mid != null;
  return usable ? { time: toTime(obs.ts), value: obs.mid as number } : { time: toTime(obs.ts) };
}

type Readout = { kind: "bar"; bar: Bar } | { kind: "obs"; obs: Obs } | null;

export function PriceChart({
  inst,
  live,
  synthetic,
  source,
  onSourceChange,
  providerRange,
}: {
  inst: InstrumentStatus;
  live: Live;
  synthetic: boolean;
  source: ChartSource;
  onSourceChange: (source: ChartSource) => void;
  providerRange: { timeframe: string; start: number; end: number } | null;
}) {
  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const [timeframe, setTimeframe] = useState<Timeframe>("1m");
  const [state, setState] = useState<{ loading: boolean; error: string | null; empty: boolean }>({
    loading: true,
    error: null,
    empty: false,
  });
  const [hover, setHover] = useState<Readout>(null);
  const [latest, setLatest] = useState<Readout>(null);
  const lookup = useRef(new Map<number, Readout>());

  // a history job asks to be shown: follow its timeframe
  useEffect(() => {
    if (providerRange && TIMEFRAMES.includes(providerRange.timeframe as Timeframe)) {
      setTimeframe(providerRange.timeframe as Timeframe);
    }
  }, [providerRange]);

  // --- create the chart once ------------------------------------------------------------
  useEffect(() => {
    if (!container.current) return;
    const c = createChart(container.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: PANEL },
        textColor: "#7d8aa0",
        fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
        fontSize: 11,
      },
      grid: { vertLines: { visible: false }, horzLines: { color: "#1a2537" } },
      rightPriceScale: { borderColor: "#223048", scaleMargins: { top: 0.12, bottom: 0.08 } },
      timeScale: { borderColor: "#223048", timeVisible: true, secondsVisible: true, rightOffset: 4 },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#506080", style: LineStyle.Dashed, labelBackgroundColor: "#223048" },
        horzLine: { color: "#506080", style: LineStyle.Dashed, labelBackgroundColor: "#223048" },
      },
    });
    chart.current = c;
    c.subscribeCrosshairMove((param) => {
      setHover(typeof param.time === "number" ? (lookup.current.get(param.time) ?? null) : null);
    });
    return () => {
      c.remove();
      chart.current = null;
    };
  }, []);

  // --- watermark: synthetic data is labelled on the chart itself --------------------------
  useEffect(() => {
    const c = chart.current;
    const pane = c?.panes()[0];
    if (!pane || !synthetic) return;
    const mark = createTextWatermark(pane, {
      horzAlign: "center",
      vertAlign: "center",
      lines: [{ text: "MOCK DATA", color: "rgba(240, 180, 41, 0.13)", fontSize: 54 }],
    });
    return () => mark.detach();
  }, [synthetic]);

  // --- (re)load data whenever the question changes ----------------------------------------
  const symbol = inst.symbol;
  const precision = inst.price_precision;
  const { generation, observations, subscribe } = live;
  useEffect(() => {
    const c = chart.current;
    if (!c) return;
    let cancelled = false;
    const isLine = source === "live" && timeframe === "1s";
    const priceFormat = { type: "price" as const, precision, minMove: 10 ** -precision };
    const series: ISeriesApi<"Line"> | ISeriesApi<"Candlestick"> = isLine
      ? c.addSeries(LineSeries, { color: "#e9e6dc", lineWidth: 2, priceFormat, crosshairMarkerRadius: 4 })
      : c.addSeries(CandlestickSeries, { priceFormat, borderVisible: true });
    c.timeScale().applyOptions({ secondsVisible: timeframe.endsWith("s") });
    lookup.current = new Map();
    setLatest(null);
    setHover(null);
    setState({ loading: true, error: null, empty: false });

    let lastTime = 0;
    let ready = false;
    const pending: ObsMessage[] = [];

    const applyBar = (bar: Bar) => {
      const time = toTime(bar.ts);
      if (time < lastTime) return;
      lastTime = time;
      lookup.current.set(time, { kind: "bar", bar });
      (series as ISeriesApi<"Candlestick">).update(candle(bar));
      setLatest({ kind: "bar", bar });
    };
    const applyObs = (obs: Obs) => {
      const time = toTime(obs.ts);
      if (time < lastTime) return;
      lastTime = time;
      lookup.current.set(time, { kind: "obs", obs });
      (series as ISeriesApi<"Line">).update(point(obs));
      setLatest({ kind: "obs", obs });
    };
    const onMessage = (message: ObsMessage) => {
      if (message.symbol !== symbol) return;
      if (!ready) {
        pending.push(message);
        return;
      }
      if (isLine) {
        applyObs(message.obs);
        return;
      }
      for (const bar of message.closed) if (bar.timeframe === timeframe) applyBar(bar);
      const partial = message.partials[timeframe];
      if (partial) applyBar({ ...partial, partial: true });
    };
    const unsubscribe = source === "live" ? subscribe(onMessage) : () => undefined;

    const load = async () => {
      if (isLine) {
        const list = observations(symbol);
        for (const obs of list) lookup.current.set(toTime(obs.ts), { kind: "obs", obs });
        (series as ISeriesApi<"Line">).setData(list.map(point));
        const tail = list[list.length - 1];
        if (tail) {
          lastTime = toTime(tail.ts);
          setLatest({ kind: "obs", obs: tail });
        }
        return list.length;
      }
      const range = source === "provider" && providerRange?.timeframe === timeframe ? providerRange : null;
      const { bars } = await api.bars(symbol, timeframe, source, source === "provider" ? 5000 : 800, range?.start, range?.end);
      if (cancelled) return 0;
      for (const bar of bars) lookup.current.set(toTime(bar.ts), { kind: "bar", bar });
      (series as ISeriesApi<"Candlestick">).setData(bars.map(candle));
      const tail = bars[bars.length - 1];
      if (tail) {
        lastTime = toTime(tail.ts);
        setLatest({ kind: "bar", bar: tail });
      }
      return bars.length;
    };

    load()
      .then((n) => {
        if (cancelled) return;
        ready = true;
        for (const message of pending) onMessage(message);
        // always show the most recent ~160 bars at a constant bar width (a handful of bars
        // stretched across the whole pane would exaggerate every move)
        if (n > 0) c.timeScale().setVisibleLogicalRange({ from: n - 160, to: n + 4 });
        setState({ loading: false, error: null, empty: n === 0 && pending.length === 0 });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ loading: false, error: error instanceof Error ? error.message : String(error), empty: false });
      });

    return () => {
      cancelled = true;
      unsubscribe();
      c.removeSeries(series);
    };
  }, [symbol, precision, timeframe, source, generation, observations, subscribe, providerRange]);

  const shown = hover ?? latest;
  return (
    <section className="panel" aria-label="Price chart">
      <header className="panel-head">
        <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Timeframe">
          {TIMEFRAMES.map((tf) => (
            <button key={tf} type="button" className="btn num" aria-pressed={tf === timeframe} onClick={() => setTimeframe(tf)}>
              {tf}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1" role="group" aria-label="Data source">
          <button type="button" className="btn" aria-pressed={source === "live"} onClick={() => onSourceChange("live")}>
            Live grid
          </button>
          <button type="button" className="btn" aria-pressed={source === "provider"} onClick={() => onSourceChange("provider")}>
            Provider history
          </button>
        </div>
      </header>

      <div className="flex min-h-[26px] flex-wrap items-center gap-x-4 gap-y-0.5 px-3 pt-2 text-[12px]">
        <Legend readout={shown} precision={precision} hovering={hover != null} />
        <span className="note ml-auto">
          {source === "live"
            ? timeframe === "1s"
              ? "mid price, one point per second; gaps are stale or missing seconds"
              : "mid price, built from the 1-second grid; faded bars have seconds missing"
            : "bid price, provider aggregates aligned to UTC"}
        </span>
      </div>

      <div className="relative mt-1.5 h-[300px] sm:h-[380px]">
        <div ref={container} className="absolute inset-0" />
        {state.loading || state.error || state.empty ? (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center px-6 text-center">
            <p className="m-0 max-w-[46ch] bg-panel/90 px-3 py-2 text-[13px] text-ink-2">
              {state.error
                ? `Could not load the chart data: ${state.error}`
                : state.loading
                  ? "Loading…"
                  : source === "provider"
                    ? "No provider bars are stored for this timeframe. Download a range under “Historical data” below."
                    : inst.signal_gate.reason === "PROVIDER NOT CONFIGURED"
                      ? "Nothing to chart: no data provider is configured."
                      : !inst.market_open
                        ? `No ${timeframe} bars stored yet. The market is closed; bars resume when it opens.`
                        : `No ${timeframe} bars yet. The first one appears when its window closes.`}
            </p>
          </div>
        ) : null}
      </div>
    </section>
  );
}

function Legend({ readout, precision, hovering }: { readout: Readout; precision: number; hovering: boolean }) {
  if (!readout) return <span className="note">No data</span>;
  if (readout.kind === "obs") {
    const { obs } = readout;
    return (
      <span className="flex flex-wrap items-center gap-x-3">
        <span className="note num">{dateTimeUTC(obs.ts, true)}</span>
        <span>
          <span className="text-ink-3">mid</span> <span className="num">{price(obs.mid, precision)}</span>
        </span>
        <span className="text-ink-3">{obs.status}</span>
        <span>
          <span className="text-ink-3">quotes</span> <span className="num">{obs.n_quotes}</span>
        </span>
      </span>
    );
  }
  const { bar } = readout;
  const field = (label: string, value: number) => (
    <span>
      <span className="text-ink-3">{label}</span> <span className="num">{price(value, precision)}</span>
    </span>
  );
  return (
    <span className="flex flex-wrap items-center gap-x-3">
      <span className="note num">{dateTimeUTC(bar.ts, true)}</span>
      {field("O", bar.open)}
      {field("H", bar.high)}
      {field("L", bar.low)}
      {field("C", bar.close)}
      {bar.n_quotes != null ? (
        <span>
          <span className="text-ink-3">quotes</span> <span className="num">{int(bar.n_quotes)}</span>
        </span>
      ) : null}
      {bar.n_expected != null ? (
        <span title="Seconds of this bar that had a usable price / seconds the bar spans">
          <span className="text-ink-3">seconds</span>{" "}
          <span className="num">
            {bar.n_valid}/{bar.n_expected}
          </span>
        </span>
      ) : null}
      {bar.partial ? <span className="text-amber">in progress</span> : null}
      {bar.complete === false && !bar.partial ? <span className="text-ink-2">incomplete</span> : null}
      {bar.n_backfilled ? <span className="text-ink-2">{bar.n_backfilled} s backfilled</span> : null}
      {!hovering && !bar.partial ? <span className="note">latest</span> : null}
    </span>
  );
}
