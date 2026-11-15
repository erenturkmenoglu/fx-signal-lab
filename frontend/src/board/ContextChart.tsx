import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Panel } from "../components/ui";
import { dateTimeUTC, price } from "../format";
import { percent } from "../lab/model";
import { PaneNotes } from "./notes";
import {
  type BoardResult,
  type ChannelView,
  WINDOWS,
  type WindowKey,
  type WindowResult,
  changeRows,
  countWindow,
  lasted,
  momentumText,
  readingAt,
  straightAt,
  straightChannel,
  windowBody,
} from "./model";

// The dashboard's colours on its panel surface.  On this chart colour says one thing only:
// direction.  Teal is up (or buy), red is down (or sell), grey is "not decided".  What the bars
// mean is said by where they stand (above or below their zero line) and, in words, by the
// read-out and the table: the teal and red pair is checked for colour-vision deficiencies.
const PANEL = "#101826";
const INK_2 = "#a8b3c4";
const INK_3 = "#7d8aa0";
const UP = "#14a89c";
const DOWN = "#e66767";
const QUIET = "#5f6f8a";
const FONT = '11px "IBM Plex Mono", ui-monospace, monospace';

const STATE_COLOR = (state: number) => (state > 0 ? UP : state < 0 ? DOWN : QUIET);
const count = (value: number) => value.toLocaleString("en-US");

/**
 * The bars of a window with what all methods said together under them: the momentum reading,
 * the mean-reversion signals, and the trend channel on the prices.  No single signal is drawn.
 */
export function WindowPanel({
  result,
  day,
  precision,
  pip,
}: {
  result: BoardResult;
  day: string;
  precision: number;
  /** size of one pip of the instrument, to say the channel in pips */
  pip: number;
}) {
  const [length, setLength] = useState<WindowKey>("week");
  const [wanted, setWanted] = useState<ChannelView>("window");
  const [state, setState] = useState<{ loading: boolean; error: string | null; data: WindowResult | null }>({
    loading: true,
    error: null,
    data: null,
  });
  const [hover, setHover] = useState<number | null>(null);
  const [pinned, setPinned] = useState<number | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  const asked = useMemo(() => windowBody(result, day, length), [result, day, length]);
  useEffect(() => {
    if (!asked) {
      setState({ loading: false, error: "This day is not part of the board.", data: null });
      return;
    }
    let cancelled = false;
    setState((s) => ({ ...s, loading: true, error: null }));
    setPinned(null);
    setHover(null);
    api
      .boardSignals(asked.body)
      .then((data) => {
        if (!cancelled) setState({ loading: false, error: null, data });
      })
      .catch((e: unknown) => {
        if (!cancelled) setState({ loading: false, error: e instanceof Error ? e.message : String(e), data: null });
      });
    return () => {
      cancelled = true;
    };
  }, [asked]);

  const data = state.data;
  const times = useMemo(() => (data?.bars.t ?? []).map((ms) => Math.floor(ms / 1000)), [data]);
  const changes = useMemo(() => (data ? changeRows(data) : []), [data]);
  const total = useMemo(() => (data ? countWindow(data) : null), [data]);
  // the straight channel of the window where there is one; otherwise the one known at each bar
  const view: ChannelView = wanted === "window" && data?.context.channel.straight ? "window" : "rolling";
  const straight = useMemo(() => (data ? straightChannel(data, pip) : null), [data, pip]);

  // --- the chart: prices and the channel, the momentum reading, the mean-reversion signals ----
  const tall = typeof window !== "undefined" && window.innerWidth >= 640;
  const height = tall ? 640 : 500;
  // how to read the chart: in sight on a wide screen, one tap away on a phone
  const [help, setHelp] = useState(tall);
  useEffect(() => {
    if (!container.current || !data || times.length === 0) return;
    const context = data.context;
    const chart = createChart(container.current, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: PANEL },
        textColor: INK_3,
        fontFamily: '"IBM Plex Mono", ui-monospace, monospace',
        fontSize: 11,
        panes: { separatorColor: "#223048", separatorHoverColor: "#34455f", enableResize: false },
      },
      grid: { vertLines: { visible: false }, horzLines: { color: "#1a2537" } },
      rightPriceScale: { borderColor: "#223048", minimumWidth: 68 },
      // a week of five-minute bars has to fit the width of a phone
      timeScale: { borderColor: "#223048", timeVisible: true, secondsVisible: false, rightOffset: 2, minBarSpacing: 0.05 },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#506080", style: LineStyle.Dashed, labelBackgroundColor: "#223048" },
        horzLine: { color: "#506080", style: LineStyle.Dashed, labelBackgroundColor: "#223048" },
      },
    });
    chartRef.current = chart;
    const time = (i: number) => (times[i] ?? 0) as UTCTimestamp;

    const candles = chart.addSeries(CandlestickSeries, {
      priceFormat: { type: "price", precision, minMove: 10 ** -precision },
      priceLineVisible: false,
    });
    candles.setData(
      times.map((_, i) => {
        const open = data.bars.o[i] ?? 0;
        const close = data.bars.c[i] ?? 0;
        const stroke = close >= open ? UP : DOWN;
        return {
          time: time(i),
          open,
          high: data.bars.h[i] ?? open,
          low: data.bars.l[i] ?? open,
          close,
          color: close >= open ? PANEL : stroke,
          borderColor: stroke,
          wickColor: stroke,
        };
      }),
    );
    // the trend channel: its two edges, and its middle as a dashed line
    const edge = (values: (number | null)[], dashed: boolean) => {
      const line = chart.addSeries(LineSeries, {
        color: INK_2,
        lineWidth: 1,
        lineStyle: dashed ? LineStyle.Dashed : LineStyle.Solid,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
        priceFormat: { type: "price", precision, minMove: 10 ** -precision },
      });
      line.setData(times.map((_, i) => (values[i] == null ? { time: time(i) } : { time: time(i), value: values[i] })));
    };
    if (view === "window") {
      // one straight line from the first bar to the last: a value at both ends is enough to draw it
      const last = times.length - 1;
      const ends = (pick: (edges: { lower: number; middle: number; upper: number }) => number) =>
        times.map((_, i) => {
          const edges = i === 0 || i === last ? straightAt(data, i) : null;
          return edges ? pick(edges) : null;
        });
      edge(ends((e) => e.upper), false);
      edge(ends((e) => e.middle), true);
      edge(ends((e) => e.lower), false);
    } else {
      edge(context.channel.upper, false);
      edge(context.channel.middle, true);
      edge(context.channel.lower, false);
    }

    // momentum: methods pointing up minus methods pointing down, in the colour of the state
    const whole = { type: "custom" as const, minMove: 1, formatter: (v: number) => String(Math.round(v)).replace("-", "−") };
    const reach = context.threshold + 2;
    const momentum = chart.addSeries(
      HistogramSeries,
      {
        priceFormat: whole,
        priceLineVisible: false,
        lastValueVisible: false,
        autoscaleInfoProvider: (original: () => { priceRange: { minValue: number; maxValue: number } | null } | null) => {
          const fitted = original();
          const range = fitted?.priceRange;
          // the lines at which the state changes are always in sight
          return {
            priceRange: {
              minValue: Math.min(range?.minValue ?? 0, -reach),
              maxValue: Math.max(range?.maxValue ?? 0, reach),
            },
          };
        },
      },
      1,
    );
    momentum.setData(
      times.map((_, i) => ({ time: time(i), value: context.votes[i] ?? 0, color: STATE_COLOR(context.state[i] ?? 0) })),
    );
    for (const level of [context.threshold, -context.threshold]) {
      momentum.createPriceLine({
        price: level,
        color: INK_3,
        lineWidth: 1,
        lineStyle: LineStyle.Dashed,
        axisLabelVisible: true,
        title: "",
      });
    }

    // mean reversion: buy signals upwards, sell signals downwards
    const counted = { type: "custom" as const, minMove: 1, formatter: (v: number) => String(Math.abs(Math.round(v))) };
    const signals = { priceFormat: counted, priceLineVisible: false, lastValueVisible: false };
    const buys = chart.addSeries(HistogramSeries, { ...signals, color: UP }, 2);
    const sells = chart.addSeries(HistogramSeries, { ...signals, color: DOWN }, 2);
    buys.setData(times.map((_, i) => ({ time: time(i), value: context.reversion.buys[i] ?? 0 })));
    sells.setData(times.map((_, i) => ({ time: time(i), value: -(context.reversion.sells[i] ?? 0) })));

    // a caption in the corner of each pane, and a line through all three where momentum changed
    const rules = context.changes.map((change) => ({ index: change.bar, color: STATE_COLOR(change.to) }));
    const captions = [
      `Price, ${data.bars.timeframe} bars, with the trend channel`,
      `Momentum: methods up minus down (of ${context.voters})`,
      "Mean reversion: buys up, sells down",
    ];
    chart.panes().forEach((pane, k) => {
      const notes = new PaneNotes(captions[k] ?? "", INK_2, PANEL, FONT);
      pane.attachPrimitive(notes);
      notes.setRules(rules);
    });
    // the prices get what the two panes under them leave (the time axis takes its own strip)
    const under = tall ? [150, 110] : [120, 90];
    const shares = [height - 28 - (under[0] ?? 0) - (under[1] ?? 0), ...under];
    chart.panes().forEach((pane, k) => pane.setStretchFactor(shares[k] ?? 1));
    chart.timeScale().fitContent();

    const index = new Map(times.map((t, i) => [t, i]));
    chart.subscribeCrosshairMove((param) => setHover(typeof param.time === "number" ? (index.get(param.time) ?? null) : null));
    chart.subscribeClick((param) => {
      if (typeof param.time !== "number") return;
      const at = index.get(param.time) ?? null;
      setPinned((current) => (current === at ? null : at));
    });
    return () => {
      chartRef.current = null;
      chart.remove();
    };
  }, [data, times, precision, tall, height, view]);

  const at = hover ?? pinned;
  const reading = data && at != null ? readingAt(data, at, view) : null;
  const voters = data?.context.voters ?? 0;
  const threshold = data?.context.threshold ?? 0;
  const signals = total ? total.buys + total.sells : 0;
  const decided = total ? total.up + total.down : 0;

  /** Bring a moment of the window to the middle of the chart, a few hours to each side. */
  const look = (bar: number) => {
    const span = Math.max(40, Math.round(times.length / 12));
    chartRef.current?.timeScale().setVisibleLogicalRange({ from: bar - span, to: bar + span });
    setPinned(bar);
    container.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  };

  return (
    <Panel
      title={asked ? `What the methods said together, ${asked.from === day ? day : `${asked.from} to ${day}`}` : "What the methods said together"}
      aside={<span>{data ? `${data.bars.timeframe} bid bars from the provider's history; times in UTC` : "times in UTC"}</span>}
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3 pt-2.5">
        <div className="flex items-center gap-1" role="group" aria-label="Length of the window">
          {WINDOWS.map((w) => (
            <button key={w.key} type="button" className="btn" aria-pressed={length === w.key} onClick={() => setLength(w.key)}>
              {w.label}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Which trend channel is drawn">
          <span className="note mr-1">trend channel</span>
          <button
            type="button"
            className="btn"
            aria-pressed={view === "window"}
            disabled={!data?.context.channel.straight}
            onClick={() => setWanted("window")}
          >
            of this window
          </button>
          <button type="button" className="btn" aria-pressed={view === "rolling"} onClick={() => setWanted("rolling")}>
            as known at each bar
          </button>
        </div>
        {pinned != null || hover != null ? null : <span className="note">Point at a bar, or tap it, to read it.</span>}
      </div>

      <details className="px-3 pt-2" open={help} onToggle={(event) => setHelp(event.currentTarget.open)}>
        <summary className="note cursor-pointer">How to read this chart</summary>
        <ul className="note m-0 flex max-w-[150ch] list-none flex-col gap-0.5 p-0 pt-1">
          <li>
            <span aria-hidden className="mr-1.5 inline-block w-4 border-t align-middle" style={{ borderColor: INK_2 }} />
            {view === "window"
              ? `Trend channel of this window: one straight line through all its closes with a band of ${data?.context.channel.width ?? 2} standard deviations: the channel as it stands at the end of the window. Its earlier part is drawn with hindsight.`
              : `Trend channel as known at each bar: a straight line through the ${data?.context.channel.hours ?? 24} hours of five-minute closes before it with a band of ${data?.context.channel.width ?? 2} standard deviations. This is the one the rules and the “why” table use.`}
          </li>
          <li>
            <span aria-hidden className="mr-0.5 inline-block h-2.5 w-1.5 align-middle" style={{ background: UP }} />
            <span aria-hidden className="mr-1.5 inline-block h-2.5 w-1.5 align-middle" style={{ background: DOWN }} />
            Momentum: of the {voters} momentum methods, those pointing up minus those pointing down. Teal: momentum is up;
            red: it is down. It changes when {threshold} more point the other way (the dashed lines); an upright line marks
            each change.
          </li>
          <li>
            Mean reversion: the signals of the mean-reversion methods in each bar, buys above the line and sells below it.
            They trade against the move, so they pile up where momentum is strongest.
          </li>
        </ul>
      </details>

      <div className="min-h-[44px] px-3 pt-2 text-[12px]" aria-live="off">
        {reading ? (
          <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1">
            <span className="note num">
              {dateTimeUTC(reading.time)} close {price(reading.close, precision)}
            </span>
            <span>
              <span className="text-ink-3">Momentum</span> <span className="num">{momentumText(reading, voters)}</span>
            </span>
            <span>
              <span className="text-ink-3">Mean reversion</span>{" "}
              <span className="num">
                {reading.buys} buy, {reading.sells} sell
              </span>
            </span>
            <span>
              <span className="text-ink-3">Channel</span>{" "}
              {reading.channel ? (
                <span className="num">
                  {price(reading.channel.lower, precision)} to {price(reading.channel.upper, precision)}: price {reading.where}
                </span>
              ) : (
                <span className="text-ink-3">not drawn yet</span>
              )}
            </span>
          </div>
        ) : total ? (
          <p className="m-0 max-w-[150ch] text-[13px]">
            Momentum changed {count(total.changes)} {total.changes === 1 ? "time" : "times"} in this window
            {decided ? `: it was up on ${percent(total.up / decided)} of the bars and down on ${percent(total.down / decided)}` : ""}.
            The mean-reversion methods gave {count(total.buys)} buy and {count(total.sells)} sell signals
            {signals ? `; ${percent(total.against / signals)} of them pointed against the momentum of their moment` : ""}.{" "}
            {view === "window" && straight
              ? `The channel of this window ${straight.rise > 0 ? "rises" : straight.rise < 0 ? "falls" : "is flat:"} ${Math.abs(straight.rise).toFixed(1)} pips from its first bar to its last and reaches ${straight.half.toFixed(1)} pips to either side of its middle; the last close is ${straight.where === "inside" ? "inside it" : `${straight.where} it`}.`
              : `The price closed outside the channel known at the time on ${percent(total.bars ? total.outside / total.bars : null)} of the bars.`}
          </p>
        ) : null}
      </div>

      <div className="relative mt-1" style={{ height }}>
        <div ref={container} className="absolute inset-0" />
        {state.loading || state.error || (data && times.length === 0) ? (
          <div className="absolute inset-0 z-10 flex items-center justify-center px-6 text-center">
            <p className="m-0 max-w-[52ch] bg-panel/90 px-3 py-2 text-[13px] text-ink-2" role={state.error ? "alert" : "status"}>
              {state.error
                ? `This window could not be worked out: ${state.error}`
                : state.loading
                  ? "Working out what every method said…"
                  : "No bars are stored for this window."}
            </p>
          </div>
        ) : null}
      </div>

      <div className="border-t border-line">
        <p className="note m-0 flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
          <span>
            {changes.length
              ? `Momentum changes in this window, newest first. Click one to see it closer.`
              : state.loading
                ? "…"
                : "Momentum did not change in this window."}
          </span>
          {pinned != null ? (
            <button
              type="button"
              className="btn"
              onClick={() => {
                setPinned(null);
                chartRef.current?.timeScale().fitContent();
              }}
            >
              Show the whole window
            </button>
          ) : null}
        </p>
        {changes.length ? (
          <div className="max-h-[300px] overflow-auto">
            <table className="data sticky-head">
              <thead>
                <tr>
                  <th>When</th>
                  <th>Momentum turned</th>
                  <th className="r">Price then</th>
                  <th className="r" title="Momentum methods pointing up minus those pointing down when the state changed">
                    Reading
                  </th>
                  <th className="r" title="How long momentum had pointed the other way before this change">
                    The state before lasted
                  </th>
                </tr>
              </thead>
              <tbody>
                {changes.map((change) => (
                  <tr
                    key={change.t}
                    onClick={() => look(change.bar)}
                    className="cursor-pointer hover:bg-raise"
                    style={change.bar === pinned ? { background: "var(--color-raise)" } : undefined}
                  >
                    <td className="num">
                      <button type="button" className="num cursor-pointer border-0 bg-transparent p-0 text-ink underline-offset-2 hover:underline">
                        {dateTimeUTC(change.t)}
                      </button>
                    </td>
                    <td>
                      <span aria-hidden className="mr-1.5 inline-block h-2.5 w-1.5 align-middle" style={{ background: STATE_COLOR(change.to) }} />
                      {change.to > 0 ? "Up" : "Down"}
                    </td>
                    <td className="r num">{price(change.price, precision)}</td>
                    <td className="r num">
                      {change.votes > 0 ? "+" : change.votes < 0 ? "−" : ""}
                      {Math.abs(change.votes)}
                    </td>
                    <td className="r num">
                      {change.lasted == null ? <span className="text-ink-3">longer than the window shows</span> : lasted(change.lasted)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        <div className="border-t border-line px-3 py-2">
          <p className="note m-0 max-w-[150ch]">
            Momentum and the signals on a bar are what was known when that bar closed. A change of momentum is a count of
            methods crossing a line: it says the move has lasted long enough for trend rules to notice, not that it will
            go on.
          </p>
        </div>
      </div>
    </Panel>
  );
}
