import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Panel, Result } from "../components/ui";
import { dateTimeUTC, price } from "../format";
import { LanesPrimitive } from "./lanes";
import {
  type BoardResult,
  type Method,
  OUTCOME_SIGN,
  type Outcome,
  STATUS_TEXT,
  type Signal,
  TRADED,
  WINDOWS,
  type WindowKey,
  type WindowResult,
  countSignals,
  laneMarks,
  lanesOf,
  methodLabel,
  outcomeOf,
  signalsByBar,
  visibleSignals,
  windowBody,
} from "./model";

// The dashboard's colours on its panel surface.  What became of a signal is said by colour here
// (teal: its trade made money, red: it lost, grey and small: not traded) and in words in the
// read-out and the table below; the pair is checked for colour-vision deficiencies.
const PANEL = "#101826";
const INK_2 = "#a8b3c4";
const INK_3 = "#7d8aa0";
const UP = "#14a89c";
const DOWN = "#e66767";
const QUIET = "#5f6f8a";
const OUTCOME_COLOR: Record<Outcome, string> = { won: UP, lost: DOWN, flat: INK_2, none: QUIET };

const LANE_PX = 20;
// room under the last lane, in lanes: the chart library's mark sits there instead of on a lane
const FOOT = 1.9;
const TABLE_LIMIT = 200;
const READOUT_LIMIT = 6;

function SignalText({ signal, methods }: { signal: Signal; methods: Map<string, Method> }) {
  const method = methods.get(signal.method);
  const outcome = outcomeOf(signal);
  return (
    <span className="whitespace-nowrap">
      <span aria-hidden style={{ color: OUTCOME_COLOR[outcome] }}>
        {signal.d > 0 ? "▲" : "▼"}
      </span>{" "}
      <span className="font-medium">{method ? methodLabel(method) : signal.method}</span>{" "}
      <span className="text-ink-2">{signal.d > 0 ? "buy" : "sell"}</span>
      {outcome === "none" ? (
        <span className="text-ink-3"> (not traded)</span>
      ) : (
        <span className="num">
          {" "}
          {OUTCOME_SIGN[outcome]} {signal.pnl != null && signal.pnl > 0 ? "+" : ""}
          {Math.round(signal.pnl ?? 0).toLocaleString("en-US").replace("-", "−")}
        </span>
      )}
    </span>
  );
}

/**
 * The bars of a window with every signal of every method under them: one lane per strategy, an
 * arrow up for a buy signal and down for a sell signal at the moment it became known.
 */
export function WindowPanel({
  result,
  day,
  precision,
  timeframes,
  untraded,
  onUntraded,
  filters,
}: {
  result: BoardResult;
  day: string;
  precision: number;
  /** bar lengths whose signals are shown; null = all */
  timeframes: ReadonlySet<string> | null;
  untraded: boolean;
  onUntraded: (value: boolean) => void;
  /** the bar-length switches, shared with the tables above */
  filters: React.ReactNode;
}) {
  const [length, setLength] = useState<WindowKey>("week");
  const [state, setState] = useState<{ loading: boolean; error: string | null; data: WindowResult | null }>({
    loading: true,
    error: null,
    data: null,
  });
  const [hover, setHover] = useState<number | null>(null);
  const [pinned, setPinned] = useState<number | null>(null);
  const container = useRef<HTMLDivElement>(null);
  const drawing = useRef<LanesPrimitive | null>(null);

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
  const methods = useMemo(() => new Map((data?.methods ?? []).map((m) => [m.id, m])), [data]);
  const lanes = useMemo(() => lanesOf(data?.methods ?? []), [data]);
  const times = useMemo(() => (data?.bars.t ?? []).map((ms) => Math.floor(ms / 1000)), [data]);
  const seconds = data?.bars.seconds ?? 60;
  const shown = useMemo(
    () => visibleSignals(data?.signals ?? [], methods, timeframes, untraded),
    [data, methods, timeframes, untraded],
  );
  const byBar = useMemo(() => signalsByBar(shown, times, seconds), [shown, times, seconds]);
  const marks = useMemo(() => laneMarks(shown, methods, lanes, times, seconds), [shown, methods, lanes, times, seconds]);
  const count = useMemo(() => countSignals(shown), [shown]);
  const closes = useMemo(() => new Map(times.map((t, i) => [t, data?.bars.c[i] ?? null])), [times, data]);

  // --- the chart: bars above, the lanes of the strategies below ----------------------------
  const tall = typeof window !== "undefined" && window.innerWidth >= 640;
  const lanesHeight = Math.round((lanes.length + FOOT + 0.6) * LANE_PX);
  const height = (tall ? 380 : 290) + lanesHeight + 28;
  useEffect(() => {
    if (!container.current || !data || times.length === 0) return;
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
    const candles = chart.addSeries(CandlestickSeries, {
      priceFormat: { type: "price", precision, minMove: 10 ** -precision },
      priceLineVisible: false,
    });
    candles.setData(
      times.map((time, i) => {
        const open = data.bars.o[i] ?? 0;
        const close = data.bars.c[i] ?? 0;
        const stroke = close >= open ? UP : DOWN;
        return {
          time: time as UTCTimestamp,
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

    // the lanes: a line nobody sees carries the marks; its scale reads names instead of numbers
    const top = lanes.length - 1;
    const name = (value: number) => lanes[top - Math.round(value)]?.short ?? "";
    const rail = chart.addSeries(
      LineSeries,
      {
        lineVisible: false,
        pointMarkersVisible: false,
        crosshairMarkerVisible: false,
        lastValueVisible: false,
        priceLineVisible: false,
        priceFormat: { type: "custom", minMove: 1, formatter: name },
        autoscaleInfoProvider: () => ({ priceRange: { minValue: -FOOT, maxValue: top + 0.6 } }),
      },
      1,
    );
    rail.setData(times.map((time) => ({ time: time as UTCTimestamp, value: 0 })));
    rail.priceScale().applyOptions({ scaleMargins: { top: 0, bottom: 0 } });
    lanes.forEach((_, index) =>
      rail.createPriceLine({
        price: top - index,
        color: "#1a2537",
        lineWidth: 1,
        lineStyle: LineStyle.Solid,
        axisLabelVisible: true,
        axisLabelColor: "#172235",
        axisLabelTextColor: INK_2,
        title: "",
      }),
    );
    const primitive = new LanesPrimitive(QUIET, PANEL);
    rail.attachPrimitive(primitive);
    drawing.current = primitive;
    chart.panes()[1]?.setHeight(Math.round((lanes.length + FOOT + 0.6) * LANE_PX));
    chart.timeScale().fitContent();
    chart.subscribeCrosshairMove((param) => setHover(typeof param.time === "number" ? param.time : null));
    chart.subscribeClick((param) => {
      if (typeof param.time === "number") {
        const time = param.time;
        setPinned((current) => (current === time ? null : time));
      }
    });
    return () => {
      drawing.current = null;
      chart.remove();
    };
  }, [data, times, lanes, precision]);

  // --- the marks follow what the reader has asked to see -----------------------------------
  useEffect(() => {
    if (!drawing.current) return;
    const top = lanes.length - 1;
    const index = new Map(times.map((time, i) => [time, i]));
    drawing.current.setTicks(
      marks.map((mark) => ({
        index: index.get(mark.time) ?? -1,
        level: top - mark.lane,
        direction: mark.direction,
        color: mark.outcome === "none" ? null : OUTCOME_COLOR[mark.outcome],
      })),
    );
  }, [marks, lanes, data, times, precision]);

  const at = hover ?? pinned;
  const here = at == null ? [] : (byBar.get(at) ?? []);
  // under the chart: the trades of the window, or everything that was known in the bar clicked
  const listed = useMemo(
    () => (pinned == null ? shown.filter((signal) => TRADED.has(signal.status)) : (byBar.get(pinned) ?? [])),
    [pinned, shown, byBar],
  );
  const rows = useMemo(() => [...listed].reverse().slice(0, TABLE_LIMIT), [listed]);

  return (
    <Panel
      title={asked ? `Every signal, ${asked.from === day ? day : `${asked.from} to ${day}`}` : "Every signal"}
      aside={
        <span>
          {data ? `${data.bars.timeframe} bid bars from the provider's history; times in UTC` : "times in UTC"}
        </span>
      }
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3 pt-2.5">
        <div className="flex items-center gap-1" role="group" aria-label="Length of the window">
          {WINDOWS.map((w) => (
            <button key={w.key} type="button" className="btn" aria-pressed={length === w.key} onClick={() => setLength(w.key)}>
              {w.label}
            </button>
          ))}
        </div>
        {filters}
        <label className="inline-flex items-center gap-1.5 text-[13px]">
          <input type="checkbox" checked={untraded} onChange={(e) => onUntraded(e.target.checked)} />
          signals that were not traded too
        </label>
      </div>

      <p className="note m-0 flex flex-wrap items-center gap-x-4 gap-y-1 px-3 pt-2">
        <span>One lane for each strategy, a mark where a signal became known:</span>
        <span>
          <span aria-hidden style={{ color: UP }}>
            ▲ ▼
          </span>{" "}
          buy or sell signal whose trade made money
        </span>
        <span>
          <span aria-hidden style={{ color: DOWN }}>
            ▲ ▼
          </span>{" "}
          whose trade lost
        </span>
        <span>
          <span aria-hidden style={{ color: INK_3 }}>
            ╵ ╷
          </span>{" "}
          not traded (a tick under the line: buy, over it: sell)
        </span>
      </p>

      <div className="min-h-[44px] px-3 pt-2 text-[12px]" aria-live="off">
        {at != null ? (
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <span className="note num">
              {dateTimeUTC(at * 1000)}
              {closes.get(at) != null ? ` close ${price(closes.get(at) ?? null, precision)}` : ""}
            </span>
            {here.length === 0 ? <span className="text-ink-3">no signal in this bar</span> : null}
            {here.slice(0, READOUT_LIMIT).map((signal) => (
              <SignalText key={`${signal.method}:${signal.t}`} signal={signal} methods={methods} />
            ))}
            {here.length > READOUT_LIMIT ? (
              <span className="text-ink-3">and {here.length - READOUT_LIMIT} more: click the bar to list them</span>
            ) : null}
          </div>
        ) : (
          <span className="note">
            {count.signals.toLocaleString("en-US")} signals in this window, {count.traded.toLocaleString("en-US")} traded:{" "}
            {count.won.toLocaleString("en-US")} made money, {count.lost.toLocaleString("en-US")} lost. Point at a bar to read
            its signals; click it to list them below.
          </span>
        )}
      </div>

      <div className="relative mt-1" style={{ height }}>
        <div ref={container} className="absolute inset-0" />
        {state.loading || state.error || (data && times.length === 0) ? (
          <div className="absolute inset-0 z-10 flex items-center justify-center px-6 text-center">
            <p className="m-0 max-w-[52ch] bg-panel/90 px-3 py-2 text-[13px] text-ink-2" role={state.error ? "alert" : "status"}>
              {state.error
                ? `The signals of this window could not be worked out: ${state.error}`
                : state.loading
                  ? "Working out the signals of every method…"
                  : "No bars are stored for this window."}
            </p>
          </div>
        ) : null}
      </div>

      <div className="border-t border-line">
        <p className="note m-0 flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
          {pinned != null ? (
            <>
              <span>
                Signals known in the bar of {dateTimeUTC(pinned * 1000)}: {listed.length}
              </span>
              <button type="button" className="btn" onClick={() => setPinned(null)}>
                Show the whole window
              </button>
            </>
          ) : (
            <span>
              {listed.length > rows.length
                ? `The last ${rows.length} of the ${listed.length.toLocaleString("en-US")} signals of this window that were traded, newest first.`
                : `The ${listed.length.toLocaleString("en-US")} signals of this window that were traded, newest first.`}{" "}
              All methods together made <Result value={count.net} /> {data?.currency ?? result.currency} on them. Click a bar
              to list everything that was known at that moment.
            </span>
          )}
        </p>
        <div className="max-h-[340px] overflow-auto">
          <table className="data sticky-head">
            <thead>
              <tr>
                <th>Known at</th>
                <th>Method</th>
                <th>Signal</th>
                <th>What became of it</th>
                <th className="r">Result</th>
                <th className="r" title="What the signal saw, in pips after the spread">
                  Saw
                </th>
                <th className="r">Entry</th>
                <th className="r">Exit</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={8} className="text-ink-3">
                    {state.loading ? "…" : "No signal of this window was traded. Switch on more bar lengths, or click a bar."}
                  </td>
                </tr>
              ) : null}
              {rows.map((signal) => {
                const method = methods.get(signal.method);
                const traded = TRADED.has(signal.status);
                return (
                  <tr key={`${signal.method}:${signal.t}`} className={traded ? "" : "text-ink-2"}>
                    <td className="num">{dateTimeUTC(signal.t)}</td>
                    <td title={method?.title}>{method ? methodLabel(method) : signal.method}</td>
                    <td>
                      <span aria-hidden style={{ color: OUTCOME_COLOR[outcomeOf(signal)] }}>
                        {signal.d > 0 ? "▲" : "▼"}
                      </span>{" "}
                      {signal.d > 0 ? "Buy" : "Sell"}
                    </td>
                    <td>
                      {traded ? `${OUTCOME_SIGN[outcomeOf(signal)]} ` : ""}
                      {STATUS_TEXT[signal.status] ?? signal.status}
                      {traded && (signal.clips ?? 1) > 1 ? `, ${signal.clips} clips` : ""}
                    </td>
                    <td className="r num">{traded ? <Result value={signal.pnl} /> : ""}</td>
                    <td className="r num">
                      {/* random entries see nothing: their "potential" is a placeholder that always passes */}
                      {signal.potential == null || method?.family === "benchmark" ? "" : signal.potential.toFixed(1)}
                    </td>
                    <td className="r num">{signal.entry_price == null ? "" : price(signal.entry_price, precision)}</td>
                    <td className="r num">{signal.exit_price == null ? "" : price(signal.exit_price, precision)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>
    </Panel>
  );
}
