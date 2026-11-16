import {
  ColorType,
  CrosshairMode,
  LineSeries,
  LineStyle,
  createChart,
  createTextWatermark,
  type AutoscaleInfo,
  type IChartApi,
  type ISeriesApi,
  type LineData,
  type UTCTimestamp,
  type WhitespaceData,
} from "lightweight-charts";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { HoleyLine } from "../chartline";
import { Panel, StatePill } from "../components/ui";
import { dateTimeUTC, price as priceText, timeUTC } from "../format";
import type { Live } from "../live";
import type { InstrumentStatus, ObsMessage } from "../types";
import { SignalMarks } from "./marks";
import {
  CHANNEL_NAMES,
  CHANNEL_TITLE,
  type Channel,
  type ChannelEntry,
  type ChannelName,
  DAY_RANGES,
  type Draft,
  FIELDS,
  type LiveForm,
  type LiveSettings,
  type LiveSignal,
  type LiveView,
  type Mode,
  type NumberField,
  RULE_TEXT,
  type Range,
  type SignalMessage,
  type Standing,
  channelShort,
  channelSpan,
  countSides,
  draftOf,
  markTime,
  marksOf,
  minutePoints,
  modeOf,
  paceText,
  rangePoints,
  reasonText,
  ruleLine,
  sameSettings,
  scaleChannels,
  secondPoints,
  segmentOf,
  settingsOf,
  slopePips,
  standing,
  standingText,
  withSignal,
} from "./model";

// The dashboard's colours on its panel.  Colour says direction and nothing else: teal is a buy,
// red is a sell, and each comes with its own shape.  The three channels are one blue from light
// to dark (short to long), told apart by their line as well (solid, dashed, dotted) and named in
// the legend; the ramp is checked as an ordered one on this surface.
const PANEL = "#101826";
const INK = "#e9e6dc";
const INK_3 = "#7d8aa0";
const BUY = "#14a89c";
const SELL = "#e66767";
const CHANNEL: Record<ChannelName, { color: string; style: LineStyle; dash: string | undefined }> = {
  short: { color: "#b7d3f6", style: LineStyle.Solid, dash: undefined },
  medium: { color: "#6da7ec", style: LineStyle.Dashed, dash: "6 4" },
  long: { color: "#2a78d6", style: LineStyle.Dotted, dash: "2 3" },
};
const MONO = '"IBM Plex Mono", ui-monospace, monospace';

const ROWS = 60; // orders listed before "show all"
const RIGHT_GAP = 8; // empty bars to the right of the newest price
const MAX_BRIDGE = 900; // seconds the stream may skip before the picture is asked for again

type Row = LineData<UTCTimestamp> | WhitespaceData<UTCTimestamp>;
const text = (error: unknown) => (error instanceof ApiError || error instanceof Error ? error.message : String(error));

/** What the chart holds right now: stream messages are read against this, not against what was asked for. */
interface Applied {
  symbol: string;
  mode: Mode;
  dayStart: number;
  dayEnd: number;
  barMs: number;
  /** the newest second the picture already holds, epoch ms */
  newest: number;
}

export function Signals({ inst, live, synthetic }: { inst: InstrumentStatus; live: Live; synthetic: boolean }) {
  const symbol = inst.symbol;
  const [range, setRange] = useState<Range>("15m");
  const [custom, setCustom] = useState(false); // the owner moved or zoomed the chart by hand
  const mode = modeOf(range); // what is asked for
  const [pictured, setPictured] = useState<Mode>("day"); // what the chart holds: it follows once the answer is in
  const [view, setView] = useState<LiveView | null>(null);
  const [signals, setSignals] = useState<LiveSignal[]>([]);
  const [channels, setChannels] = useState<{ entries: ChannelEntry[]; barMs: number }>({ entries: [], barMs: 60_000 });
  const [state, setState] = useState<{ loading: boolean; error: string | null }>({ loading: true, error: null });
  const [form, setForm] = useState<LiveForm | null>(null);
  const [shown, setShown] = useState<Record<ChannelName, boolean>>({ short: true, medium: true, long: true });
  const [hover, setHover] = useState<{ time: number; value: number | null } | null>(null);
  const [chosen, setChosen] = useState<number | null>(null);
  const [reload, setReload] = useState(0);
  const [newest, setNewest] = useState<number | null>(null); // the newest second on the chart, epoch ms
  const [count, setCount] = useState(0); // points of the price line (0: nothing to show)

  const container = useRef<HTMLDivElement>(null);
  const chart = useRef<IChartApi | null>(null);
  const line = useRef<ISeriesApi<"Line"> | null>(null);
  const price = useRef<HoleyLine | null>(null); // feeds the price line and keeps its holes
  const marks = useRef<SignalMarks | null>(null);
  const edges = useRef<Record<ChannelName, [ISeriesApi<"Line">, ISeriesApi<"Line">] | null>>({ short: null, medium: null, long: null });
  const edgeTo = useRef<Record<ChannelName, number | null>>({ short: null, medium: null, long: null });
  const points = useRef(0); // how many points the price line holds
  const lastTime = useRef(0); // the time of its newest point, epoch seconds
  const applied = useRef<Applied | null>(null);
  const shownKey = useRef("");
  const waiting = useRef<{ obs: ObsMessage[]; signals: SignalMessage[] } | null>(null); // the stream while a picture loads
  const drawn = useRef<{ entries: ChannelEntry[]; barMs: number; shown: Record<ChannelName, boolean> }>({
    entries: [],
    barMs: 60_000,
    shown,
  });
  const preset = useRef<{ spacing: number; offset: number } | null>(null); // the zoom a range button left behind
  const unsettled = useRef(false); // a range was set and what it left behind is still to be read
  const frame = useRef(0);
  const rangeNow = useRef(range);
  rangeNow.current = range;
  const customNow = useRef(custom);
  customNow.current = custom;
  const marketOpen = useRef(false);
  marketOpen.current = inst.market_open;
  const rolled = useRef(0); // the end of the trading day a new picture was already asked for

  const { generation, subscribe, subscribeSignals } = live;

  // --- how much of it is in sight ------------------------------------------------------------
  /**
   * Remember the zoom a range button left behind.  The chart takes a new range in with its next
   * frame, so what it left is read after that; a later zoom that differs from it is the owner's.
   */
  const settle = useCallback(() => {
    preset.current = null;
    unsettled.current = true;
    cancelAnimationFrame(frame.current);
    frame.current = requestAnimationFrame(() => {
      const scale = chart.current?.timeScale();
      if (!scale || scale.width() === 0) return; // out of sight: read when it is seen again
      preset.current = { spacing: scale.options().barSpacing, offset: scale.scrollPosition() };
      unsettled.current = false;
    });
  }, []);

  /** Show what a range button stands for; the chart follows the newest price from there. */
  const zoomTo = useCallback(
    (wanted: Range) => {
      const scale = chart.current?.timeScale();
      if (!scale) return;
      const seconds = rangePoints(wanted);
      if (seconds == null) scale.fitContent();
      else scale.setVisibleLogicalRange({ from: points.current - seconds, to: points.current + RIGHT_GAP });
      settle();
    },
    [settle],
  );

  // --- the chart, once ---------------------------------------------------------------------
  useEffect(() => {
    if (!container.current) return;
    const c = createChart(container.current, {
      autoSize: true,
      layout: { background: { type: ColorType.Solid, color: PANEL }, textColor: INK_3, fontFamily: MONO, fontSize: 11 },
      grid: { vertLines: { visible: false }, horzLines: { color: "#1a2537" } },
      rightPriceScale: { borderColor: "#223048", scaleMargins: { top: 0.14, bottom: 0.14 } },
      timeScale: {
        borderColor: "#223048",
        timeVisible: true,
        secondsVisible: true,
        rightOffset: RIGHT_GAP,
        // a day is 86 400 seconds: the bars have to be allowed to be far thinner than a pixel
        minBarSpacing: 0.001,
        // a second without a price still moves the chart on: the channels reach it, the price line has a hole
        allowShiftVisibleRangeOnWhitespaceReplacement: true,
      },
      // a finger moving up or down scrolls the page, not the price scale
      handleScroll: { vertTouchDrag: false },
      crosshair: {
        mode: CrosshairMode.Normal,
        vertLine: { color: "#506080", style: LineStyle.Dashed, labelBackgroundColor: "#223048" },
        horzLine: { color: "#506080", style: LineStyle.Dashed, labelBackgroundColor: "#223048" },
      },
    });
    // the channels first: the price is drawn over them
    for (const name of CHANNEL_NAMES) {
      const make = () =>
        c.addSeries(LineSeries, {
          color: CHANNEL[name].color,
          lineWidth: 1,
          lineStyle: CHANNEL[name].style,
          lastValueVisible: false,
          priceLineVisible: false,
          crosshairMarkerVisible: false,
          autoscaleInfoProvider: () => null,
        });
      edges.current[name] = [make(), make()];
    }
    const series = c.addSeries(LineSeries, { color: INK, lineWidth: 2, crosshairMarkerRadius: 4, priceLineColor: "#506080" });
    const triangles = new SignalMarks(BUY, SELL, PANEL, INK);
    series.attachPrimitive(triangles);
    c.subscribeCrosshairMove((param) => {
      if (typeof param.time !== "number") {
        setHover(null);
        return;
      }
      const at = param.seriesData.get(series) as { value?: number } | undefined;
      setHover({ time: param.time, value: at?.value ?? null });
    });
    // a zoom or a move that no range button made is the owner's own: the button lets go
    const scale = c.timeScale();
    scale.subscribeVisibleLogicalRangeChange(() => {
      if (scale.width() === 0) return;
      const left = preset.current;
      if (!left) {
        if (unsettled.current) settle(); // the page was out of sight when the range was set: read it now
        return;
      }
      const spacing = scale.options().barSpacing;
      if (Math.abs(spacing - left.spacing) > left.spacing * 0.01 || Math.abs(scale.scrollPosition() - left.offset) > 0.75) {
        preset.current = null;
        setCustom(true);
      }
    });
    // a chart that changes its width (a phone turned on its side) shows its range again
    scale.subscribeSizeChange(() => {
      if (scale.width() > 0 && !customNow.current && applied.current) zoomTo(rangeNow.current);
    });
    chart.current = c;
    line.current = series;
    marks.current = triangles;
    return () => {
      chart.current = null;
      line.current = null;
      price.current = null;
      marks.current = null;
      edges.current = { short: null, medium: null, long: null };
      cancelAnimationFrame(frame.current);
      c.remove();
    };
  }, [settle, zoomTo]);

  // synthetic prices are labelled on the chart itself
  useEffect(() => {
    const c = chart.current;
    const pane = c?.panes()[0];
    if (!pane || !synthetic) return;
    const mark = createTextWatermark(pane, {
      horzAlign: "center",
      vertAlign: "center",
      lines: [{ text: "MOCK DATA", color: "rgba(240, 180, 41, 0.13)", fontSize: 54 }],
    });
    return () => {
      if (chart.current === c) mark.detach();
    };
  }, [synthetic]);

  const choose = (wanted: Range) => {
    setCustom(false);
    setRange(wanted);
    if (modeOf(wanted) === applied.current?.mode) zoomTo(wanted); // else the other picture is loaded and zoomed then
  };

  // the price scale makes room for the channel that matches the range, and for the shorter ones
  useEffect(() => {
    const roomFor = scaleChannels(range);
    for (const name of CHANNEL_NAMES) {
      const included = roomFor.includes(name);
      for (const series of edges.current[name] ?? []) {
        series.applyOptions({ autoscaleInfoProvider: included ? (original: () => AutoscaleInfo | null) => original() : () => null });
      }
    }
  }, [range]);

  // --- the channels on the chart -----------------------------------------------------------
  const drawChannels = useCallback(() => {
    const at = applied.current;
    const { entries, barMs, shown: visible } = drawn.current;
    for (const name of CHANNEL_NAMES) {
      const pair = edges.current[name];
      if (!pair) continue;
      const channel = entries.find((entry) => entry.name === name)?.channel ?? null;
      const segment = at && visible[name] ? segmentOf(channel, at.mode, barMs, lastTime.current ? lastTime.current * 1000 : null) : null;
      edgeTo.current[name] = segment && at?.mode === "day" ? segment.to : null;
      for (const [series, sign] of [
        [pair[0], 1],
        [pair[1], -1],
      ] as const) {
        series.setData(
          segment
            ? ([
                { time: segment.from, value: segment.middle[0] + sign * segment.half },
                { time: segment.to, value: segment.middle[1] + sign * segment.half },
              ] as Row[])
            : [],
        );
      }
    }
  }, []);

  /** One more second: each channel is taken on to it along its own line (one more point, no redraw). */
  const extendChannels = useCallback((newestMs: number) => {
    const { entries, barMs } = drawn.current;
    for (const entry of entries) {
      const pair = edges.current[entry.name];
      const reached = edgeTo.current[entry.name];
      if (!pair || reached == null) continue;
      const segment = segmentOf(entry.channel, "day", barMs, newestMs);
      if (!segment || segment.to <= reached) continue;
      pair[0].update({ time: segment.to, value: segment.middle[1] + segment.half } as Row);
      pair[1].update({ time: segment.to, value: segment.middle[1] - segment.half } as Row);
      edgeTo.current[entry.name] = segment.to;
    }
  }, []);

  useEffect(() => {
    drawn.current = { entries: channels.entries, barMs: channels.barMs, shown };
    drawChannels();
  }, [channels, shown, drawChannels]);

  // --- the stream: one more second ----------------------------------------------------------
  const applyObs = useCallback(
    (message: ObsMessage) => {
      const at = applied.current;
      const feed = price.current;
      if (!at || !feed || message.symbol !== at.symbol) return;
      const { obs } = message;
      if (obs.ts >= at.dayEnd) {
        // a second of the next trading day: its picture is asked for, once
        if (marketOpen.current && rolled.current !== at.dayEnd) {
          rolled.current = at.dayEnd;
          setReload((n) => n + 1);
        }
        return;
      }
      if (obs.ts <= at.newest || obs.ts < at.dayStart) return; // the picture holds it already
      const usable = (obs.status === "fresh" || obs.status === "carried") && obs.bid != null;
      const scale = chart.current?.timeScale();
      if (at.mode === "day") {
        const time = obs.ts / 1000;
        if (time <= lastTime.current) return;
        if (lastTime.current > 0 && time - lastTime.current > MAX_BRIDGE) {
          setReload((n) => n + 1); // the stream skipped a long stretch: read the day again
          return;
        }
        // seconds the stream skipped stay holes in the line
        for (let t = lastTime.current + 1; lastTime.current > 0 && t < time; t++) {
          feed.push(t, null);
          points.current += 1;
        }
        feed.push(time, usable ? obs.bid : null);
        lastTime.current = time;
        points.current += 1;
        extendChannels(obs.ts);
        if (rangeNow.current === "day" && !customNow.current && scale) {
          scale.fitContent(); // the whole day stays in sight as it grows
          settle();
        }
      } else {
        // the chart of minutes: the minute in progress stands at its newest bid
        if (!usable) return;
        const time = (Math.floor(obs.ts / at.barMs) * at.barMs) / 1000;
        if (time < lastTime.current) return;
        const added = time > lastTime.current;
        feed.push(time, obs.bid);
        lastTime.current = time;
        if (added) {
          points.current += 1;
          if (!customNow.current && scale) {
            scale.fitContent();
            settle();
          }
        }
      }
      setCount(points.current);
      setNewest(obs.ts);
    },
    [extendChannels, settle],
  );

  useEffect(
    () =>
      subscribe((message: ObsMessage) => {
        if (waiting.current) waiting.current.obs.push(message);
        else applyObs(message);
      }),
    [subscribe, applyObs],
  );

  useEffect(
    () =>
      subscribeSignals((message: SignalMessage) => {
        if (waiting.current) waiting.current.signals.push(message);
        else if (message.symbol === applied.current?.symbol) setSignals((before) => withSignal(before, message.signal));
      }),
    [subscribeSignals],
  );

  // --- the picture: asked for at the start, on a new connection, after new settings ----------
  useEffect(() => {
    let cancelled = false;
    waiting.current = { obs: [], signals: [] }; // what the stream brings meanwhile is taken in after the picture
    setState((before) => ({ loading: true, error: before.error }));
    const catchUp = (after: number) => {
      const held = waiting.current;
      waiting.current = null;
      if (!held) return;
      for (const message of held.obs) applyObs(message);
      const fresh = held.signals.filter((message) => message.symbol === symbol && message.signal.t > after);
      if (fresh.length) setSignals((before) => fresh.reduce((list, message) => withSignal(list, message.signal), before));
    };
    api
      .liveView(symbol, mode === "history")
      .then((next) => {
        if (cancelled) return;
        const series = line.current;
        const scale = chart.current?.timeScale();
        if (!series || !scale) return;
        const data = mode === "history" ? minutePoints(next.history ?? { t: [], bid: [] }) : secondPoints(next.seconds);
        const key = `${next.symbol}|${mode}|${next.day.start}`;
        // a chart the owner had moved by hand stays where it was when the same day is read again
        const kept = customNow.current && shownKey.current === key && points.current > 0 ? scale.getVisibleLogicalRange() : null;
        const had = points.current;
        const precision = next.price_precision;
        // every series is given the instrument's price format: the price scale takes its labels from the first it holds
        const priceFormat = { type: "price" as const, precision, minMove: 10 ** -precision };
        series.applyOptions({ priceFormat });
        for (const name of CHANNEL_NAMES) for (const edge of edges.current[name] ?? []) edge.applyOptions({ priceFormat });
        scale.applyOptions({ secondsVisible: mode === "day" });
        // a second without a price, and minutes that are missing, stay holes in the line
        price.current = new HoleyLine(series, mode === "day" ? 1 : next.bar_ms / 1000);
        price.current.set(data);
        points.current = data.length;
        lastTime.current = data[data.length - 1]?.time ?? 0;
        applied.current = {
          symbol: next.symbol,
          mode,
          dayStart: next.day.start,
          dayEnd: next.day.end,
          barMs: next.bar_ms,
          newest: next.newest ?? Number.NEGATIVE_INFINITY,
        };
        shownKey.current = key;
        drawn.current = { ...drawn.current, entries: next.channels, barMs: next.bar_ms };
        drawChannels();
        if (kept) {
          const shift = kept.to >= had - 1 ? data.length - had : 0; // at the newest price: stay at it
          scale.setVisibleLogicalRange({ from: kept.from + shift, to: kept.to + shift });
        } else {
          zoomTo(rangeNow.current);
        }
        setView(next);
        setPictured(mode);
        setSignals(next.signals);
        setChannels({ entries: next.channels, barMs: next.bar_ms });
        setNewest(next.newest);
        setCount(data.length);
        setState({ loading: false, error: null });
        catchUp(next.newest ?? Number.NEGATIVE_INFINITY);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setState({ loading: false, error: text(error) });
        catchUp(applied.current?.newest ?? Number.NEGATIVE_INFINITY); // the picture that is there goes on
      });
    return () => {
      cancelled = true;
    };
  }, [symbol, mode, generation, reload, applyObs, drawChannels, zoomTo]);

  // the form of the settings, and again whenever they were changed anywhere
  useEffect(() => {
    let cancelled = false;
    api
      .liveSettings()
      .then((next) => {
        if (!cancelled) setForm(next);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [generation]);

  // the channels are fitted again as minutes complete: asked for once a minute
  useEffect(() => {
    const timer = setInterval(() => {
      api
        .liveChannels(symbol)
        .then((next) => {
          if (applied.current?.symbol === next.symbol) setChannels({ entries: next.channels, barMs: next.bar_ms });
        })
        .catch(() => undefined);
    }, 60_000);
    return () => clearInterval(timer);
  }, [symbol, generation]);

  const barMs = channels.barMs;
  useEffect(() => {
    marks.current?.setMarks(marksOf(signals, pictured, barMs));
  }, [signals, pictured, barMs]);
  useEffect(() => {
    marks.current?.choose(chosen);
  }, [chosen]);

  // --- what the buttons do ------------------------------------------------------------------
  const look = useCallback(
    (signal: LiveSignal) => {
      const time = markTime(signal, pictured, barMs);
      setChosen(time);
      const around = pictured === "day" ? 240 : 240 * 60;
      try {
        chart.current?.timeScale().setVisibleRange({ from: (time - around) as UTCTimestamp, to: (time + around) as UTCTimestamp });
      } catch {
        // the times around it are not on the chart: the view stays as it is
      }
      container.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    },
    [pictured, barMs],
  );

  const [saveError, setSaveError] = useState<string | null>(null);
  const save = useCallback(async (changes: Partial<LiveSettings>) => {
    setSaveError(null);
    try {
      setForm(await api.saveLiveSettings(changes));
      setReload((n) => n + 1);
    } catch (error) {
      setSaveError(text(error));
    }
  }, []);

  const toggle = useCallback((name: ChannelName) => setShown((before) => ({ ...before, [name]: !before[name] })), []);

  // --- what is said around the chart --------------------------------------------------------
  const settings = view?.settings ?? form?.settings ?? null;
  const precision = inst.price_precision;
  const pip = inst.pip_size;
  const counts = useMemo(() => countSides(signals), [signals]);
  const pace = view ? paceText(signals.length, view.day.start, newest) : null;
  const last = signals.length ? signals[signals.length - 1] : null;
  const atTime = useMemo(() => {
    const map = new Map<number, LiveSignal[]>();
    for (const signal of signals) {
      const key = markTime(signal, pictured, barMs);
      const list = map.get(key);
      if (list) list.push(signal);
      else map.set(key, [signal]);
    }
    return map;
  }, [signals, pictured, barMs]);
  const bid = inst.bid;
  const gate = inst.signal_gate;
  const empty = !state.loading && !state.error && count === 0;
  const longSpan = settings ? channelSpan("long", settings) : "10 d";

  return (
    <main className="mx-auto flex max-w-[1680px] flex-col gap-2 p-2">
      <section className="panel flex flex-col" aria-label="Live signals">
        <header className="panel-head">
          <h2 className="panel-title">Live signals: {inst.symbol}</h2>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
            <StatePill
              tone={gate.allowed ? "ok" : "warn"}
              title="Orders are signalled only from prices that are fresh, inside market hours and from a feed that is connected"
            >
              {gate.allowed ? "Signalling" : `Held: ${gate.reason.toLowerCase()}`}
            </StatePill>
            <span className="num text-[15px]" title="Bid and ask right now">
              {priceText(bid, precision)} <span className="note">/</span> {priceText(inst.ask, precision)}
            </span>
          </div>
        </header>

        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 px-3 pt-2 text-[13px]">
          <span>
            <Triangle side="buy" /> <span className="num">{counts.buy.toLocaleString("en-US")}</span> buy
          </span>
          <span>
            <Triangle side="sell" /> <span className="num">{counts.sell.toLocaleString("en-US")}</span> sell
          </span>
          {last ? (
            <span className="note">
              {pace ? `${pace}, ` : ""}last: {last.side} at <span className="num">{timeUTC(last.t)}</span>
            </span>
          ) : (
            <span className="note">no order signalled this trading day</span>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-1 px-3 pt-2" role="group" aria-label="How much the chart shows">
          {DAY_RANGES.map((r) => (
            <button
              key={r.key}
              type="button"
              className="btn num"
              aria-pressed={!custom && range === r.key}
              title={r.seconds ? `The last ${r.label} of today, one point a second` : "All of today so far, one point a second"}
              onClick={() => choose(r.key)}
            >
              {r.label}
            </button>
          ))}
          <button
            type="button"
            className="btn num"
            aria-pressed={range === "history"}
            title={`The ${longSpan} the long channel is fitted to, one point a minute`}
            onClick={() => choose("history")}
          >
            {longSpan}
          </button>
        </div>

        <ChannelLegend
          entries={channels.entries}
          settings={settings}
          shown={shown}
          onToggle={toggle}
          barMs={barMs}
          pip={pip}
          precision={precision}
          price={bid}
        />

        <div className="flex min-h-[22px] flex-wrap items-center gap-x-4 px-3 pt-1.5 text-[12px]">
          <Readout hover={hover} signals={hover ? atTime.get(hover.time) : undefined} settings={settings} precision={precision} mode={pictured} />
        </div>

        <div className="relative mt-1 h-[340px] sm:h-[440px] xl:h-[520px]">
          <div ref={container} className="absolute inset-0" />
          {state.loading || state.error || empty ? (
            <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center px-6 text-center">
              <p className="m-0 max-w-[52ch] bg-panel/90 px-3 py-2 text-[13px] text-ink-2">
                {state.error
                  ? `The live picture could not be loaded: ${state.error}`
                  : state.loading
                    ? "Loading…"
                    : mode === "history"
                      ? "No one-minute bars are stored yet. They build up while the feed runs; older ones can be downloaded on the Live page."
                      : inst.market_open
                        ? "No second of this trading day has a price yet."
                        : "The market is closed and no second of the last trading day is stored."}
              </p>
            </div>
          ) : null}
        </div>
        <p className="note m-0 max-w-[110ch] px-3 py-2 max-lg:order-3">
          Bid, {pictured === "day" ? "one point a second" : "one point a minute"}. A triangle marks the second at which the rule turned
          true: a signal, not an order. Each channel is a straight line fitted to one-minute bars with a band on either side; it is
          fitted again every minute and drawn as it stands now, so earlier in the day it stood elsewhere. Times are UTC.
        </p>
      </section>

      {form ? <SettingsPanel form={form} onSave={save} error={saveError} /> : null}

      <Orders signals={signals} settings={settings} precision={precision} chosen={chosen} mode={pictured} barMs={barMs} onLook={look} />

      <footer className="note px-1 py-2">
        Decision support only. A signal says that the rule's condition became true at that second. It is not a recommendation, no
        order is placed anywhere, and no profit or loss is worked out here. What a rule would have earned or lost is a question
        for “My setups”.
      </footer>
    </main>
  );
}

/** Direction as a shape and a colour together: up and teal is a buy, down and red is a sell. */
function Triangle({ side }: { side: "buy" | "sell" }) {
  return (
    <span aria-hidden style={{ color: side === "buy" ? BUY : SELL }}>
      {side === "buy" ? "▲" : "▼"}
    </span>
  );
}

function Readout({
  hover,
  signals,
  settings,
  precision,
  mode,
}: {
  hover: { time: number; value: number | null } | null;
  signals: LiveSignal[] | undefined;
  settings: LiveSettings | null;
  precision: number;
  mode: Mode;
}) {
  if (!hover) return <span className="note">Point at the chart to read a second.</span>;
  const first = signals?.slice(0, 2) ?? [];
  const more = (signals?.length ?? 0) - first.length;
  return (
    <>
      <span className="note num">{dateTimeUTC(hover.time * 1000, mode === "day")}</span>
      <span>
        <span className="text-ink-3">bid</span>{" "}
        <span className="num">{hover.value == null ? "no price" : priceText(hover.value, precision)}</span>
      </span>
      {first.map((signal) => (
        <span key={`${signal.t}-${signal.side}`}>
          <Triangle side={signal.side} /> {signal.side}
          {mode === "history" ? <span className="num"> {timeUTC(signal.t)}</span> : null}
          {settings ? <span className="note"> ({reasonText(signal, settings, precision)})</span> : null}
        </span>
      ))}
      {more > 0 ? <span className="note">and {more} more in this minute</span> : null}
    </>
  );
}

/** The three channels by name: how each is drawn, which way it leans, and where the price stands in it. */
function ChannelLegend({
  entries,
  settings,
  shown,
  onToggle,
  barMs,
  pip,
  precision,
  price,
}: {
  entries: ChannelEntry[];
  settings: LiveSettings | null;
  shown: Record<ChannelName, boolean>;
  onToggle: (name: ChannelName) => void;
  barMs: number;
  pip: number;
  precision: number;
  price: number | null;
}) {
  if (!settings) return null;
  return (
    <div className="grid grid-cols-1 gap-1.5 px-3 pt-2 max-lg:order-2 xl:grid-cols-3" role="group" aria-label="Trend channels">
      {CHANNEL_NAMES.map((name) => {
        const channel = entries.find((entry) => entry.name === name)?.channel ?? null;
        const where = channel && price != null ? standing(channel, price, pip) : null;
        const slope = channel ? slopePips(channel, barMs, pip) : null;
        const few = channel ? channelShort(channel) : null;
        const span = channelSpan(name, settings);
        return (
          <button
            key={name}
            type="button"
            role="switch"
            aria-checked={shown[name]}
            className="btn flex flex-wrap items-center justify-between gap-x-2 gap-y-0.5 px-2 py-1 text-left"
            onClick={() => onToggle(name)}
            style={{ opacity: shown[name] ? 1 : 0.5 }}
            title={channelTitle(name, span, settings, channel, precision, shown[name])}
          >
            <span className="flex items-center gap-2">
              <svg width="24" height="8" aria-hidden>
                <line x1="0" y1="4" x2="24" y2="4" stroke={CHANNEL[name].color} strokeWidth="2" strokeDasharray={CHANNEL[name].dash} />
              </svg>
              <span>
                <span className="font-medium">{CHANNEL_TITLE[name]}</span> <span className="note num">{span}</span>
              </span>
              {slope != null ? (
                <span className="note num" title="How much the channel's line rises or falls in an hour">
                  {slope >= 0 ? "+" : "−"}
                  {Math.abs(slope).toFixed(1)} pips/h
                </span>
              ) : null}
            </span>
            <span className="flex items-center gap-2" title="Where the bid stands between the channel's lower edge (left) and its upper edge (right)">
              {channel ? (
                <>
                  <Gauge where={where} color={CHANNEL[name].color} />
                  <span className="note num">{where ? standingText(where) : "no price"}</span>
                </>
              ) : (
                <span className="note">no bars stored for it yet</span>
              )}
            </span>
            {few ? <span className="note basis-full">{few}: the channel is fitted to what there is</span> : null}
          </button>
        );
      })}
    </div>
  );
}

function channelTitle(name: ChannelName, span: string, settings: LiveSettings, channel: Channel | null, precision: number, on: boolean) {
  const what = `${CHANNEL_TITLE[name]} channel: a straight line through the last ${span} of one-minute bars, with a band of ${settings.channel_width} standard deviations on either side.`;
  const edgesNow = channel
    ? ` Its edges now: ${priceText(channel.end - channel.half, precision)} and ${priceText(channel.end + channel.half, precision)}.`
    : "";
  return `${what}${edgesNow} Press to ${on ? "hide" : "show"} it.`;
}

/** A channel from its lower edge (left) to its upper edge (right), and a mark where the price stands. */
function Gauge({ where, color }: { where: Standing | null; color: string }) {
  // the edges stand at 8 and 52; a price outside the channel stands beyond its edge
  const x = !where ? null : where.where === "inside" ? 8 + where.share * 44 : where.where === "above" ? 57 : 3;
  return (
    <svg width="60" height="12" className="shrink-0" aria-hidden>
      <line x1="8" y1="6" x2="52" y2="6" stroke={color} strokeWidth="2" />
      <line x1="8" y1="2" x2="8" y2="10" stroke={color} strokeWidth="1.5" />
      <line x1="52" y1="2" x2="52" y2="10" stroke={color} strokeWidth="1.5" />
      {x != null ? <circle cx={x} cy="6" r="3.25" fill={INK} stroke={PANEL} strokeWidth="1.5" /> : null}
    </svg>
  );
}

/** The orders the rule signalled, newest first.  Kept apart so that a new second does not redraw the table. */
const Orders = memo(function Orders({
  signals,
  settings,
  precision,
  chosen,
  mode,
  barMs,
  onLook,
}: {
  signals: LiveSignal[];
  settings: LiveSettings | null;
  precision: number;
  chosen: number | null;
  mode: Mode;
  barMs: number;
  onLook: (signal: LiveSignal) => void;
}) {
  const [all, setAll] = useState(false);
  const rows = useMemo(() => {
    const newestFirst = signals.slice().reverse();
    return all ? newestFirst : newestFirst.slice(0, ROWS);
  }, [signals, all]);
  const total = signals.length.toLocaleString("en-US");
  return (
    <Panel
      title="Orders signalled this trading day"
      aside={signals.length ? <span>newest first{signals.length > ROWS && !all ? `, the last ${ROWS} of ${total}` : `, ${total}`}</span> : null}
    >
      {signals.length === 0 ? (
        <p className="m-0 px-3 py-3 text-[13px] text-ink-2">
          Nothing yet. An order is listed at the second the rule turns true, with the bid and the ask of that second.
        </p>
      ) : (
        <div className="max-h-[460px] max-w-[860px] overflow-auto">
          <table className="data sticky-head">
            <thead>
              <tr>
                <th>Time</th>
                <th>Order</th>
                <th className="r">Bid</th>
                <th className="r">Ask</th>
                <th className="r">RSI</th>
                <th className="r" title="The Bollinger band on that side: the lower one for a buy, the upper one for a sell">
                  Band
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((signal) => (
                <tr
                  key={`${signal.t}-${signal.side}`}
                  className="hover:bg-raise"
                  style={chosen === markTime(signal, mode, barMs) ? { background: "var(--color-raise)" } : undefined}
                >
                  <td className="num">
                    <button
                      type="button"
                      className="cursor-pointer border-0 bg-transparent p-0 font-[inherit] text-inherit underline decoration-dotted underline-offset-4"
                      title={settings ? `${reasonText(signal, settings, precision)}. Press to see it on the chart.` : "See it on the chart"}
                      onClick={() => onLook(signal)}
                    >
                      {timeUTC(signal.t)}
                    </button>
                  </td>
                  <td>
                    <Triangle side={signal.side} /> {signal.side === "buy" ? "Buy" : "Sell"}
                  </td>
                  <td className="num r">{priceText(signal.bid, precision)}</td>
                  <td className="num r">{priceText(signal.ask, precision)}</td>
                  <td className="num r">{signal.rsi.toFixed(1)}</td>
                  <td className="num r">{priceText(signal.side === "buy" ? signal.lower : signal.upper, precision)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {signals.length > ROWS ? (
        <div className="border-t border-line px-3 py-2">
          <button type="button" className="btn" onClick={() => setAll((before) => !before)}>
            {all ? `Show the last ${ROWS} only` : `Show all ${total}`}
          </button>
        </div>
      ) : null}
    </Panel>
  );
});

const GROUPS: { title: string; group: (typeof FIELDS)[number]["group"] }[] = [
  { title: "RSI", group: "rsi" },
  { title: "Bollinger bands", group: "bollinger" },
  { title: "Trend channels", group: "channels" },
];

/** The settings of the rule and of the channels.  Closed until asked for; the defaults are one press away. */
const SettingsPanel = memo(function SettingsPanel({
  form,
  onSave,
  error,
}: {
  form: LiveForm;
  onSave: (changes: Partial<LiveSettings>) => Promise<void>;
  error: string | null;
}) {
  const [draft, setDraft] = useState<Draft>(() => draftOf(form.settings));
  const [busy, setBusy] = useState(false);
  // the settings changed on the server (saved here or on another screen): the form follows
  useEffect(() => setDraft(draftOf(form.settings)), [form.settings]);
  const { settings, errors } = settingsOf(draft, form.limits);
  const changed = settings ? !sameSettings(settings, form.settings) : true;
  const atDefaults = sameSettings(form.settings, form.defaults);
  const apply = async (next: LiveSettings) => {
    setBusy(true);
    await onSave(next);
    setBusy(false);
  };
  const field = (key: NumberField, label: string, unit: string | undefined) => {
    const typed = draft[key].trim();
    const hint = errors[key] ?? (typed === String(form.defaults[key]) ? "" : `default ${form.defaults[key]}`);
    return (
      <label key={key} className="flex flex-col gap-0.5 text-[12px] text-ink-2">
        <span>
          {label}
          {unit ? <span className="note"> {unit}</span> : null}
        </span>
        <input
          className="field num w-[96px]"
          inputMode="decimal"
          value={draft[key]}
          aria-invalid={errors[key] != null}
          onChange={(event) => setDraft((before) => ({ ...before, [key]: event.target.value }))}
        />
        <span className="min-h-[16px] text-[11px]" style={errors[key] ? { color: "var(--color-warn)" } : undefined}>
          {hint}
        </span>
      </label>
    );
  };
  return (
    <details className="panel">
      <summary className="cursor-pointer px-3 py-2 text-[13px]">
        <span className="font-semibold text-amber">Settings</span>{" "}
        <span className="note">
          {ruleLine(form.settings)}
          {atDefaults ? " (the defaults)" : ""}
        </span>
      </summary>
      <form
        className="flex flex-col gap-3 border-t border-line px-3 py-3"
        onSubmit={(event) => {
          event.preventDefault();
          if (settings) void apply(settings);
        }}
      >
        <div className="flex flex-wrap gap-x-10 gap-y-3">
          {GROUPS.map(({ title, group }) => (
            <fieldset key={group} className="m-0 min-w-0 border-0 p-0">
              <legend className="mb-1 p-0 text-[13px] font-medium">{title}</legend>
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                {FIELDS.filter((f) => f.group === group).map((f) => field(f.key, f.label, f.unit))}
              </div>
            </fieldset>
          ))}
        </div>
        <fieldset className="m-0 flex flex-wrap items-center gap-x-5 gap-y-1 border-0 p-0 text-[13px]">
          <legend className="mb-1 p-0 font-medium">An order is signalled by</legend>
          {form.rules.map((rule) => (
            <label key={rule} className="flex items-center gap-1.5">
              <input type="radio" name="live-rule" checked={draft.rule === rule} onChange={() => setDraft((before) => ({ ...before, rule }))} />
              {RULE_TEXT[rule]}
            </label>
          ))}
        </fieldset>
        <fieldset className="m-0 flex flex-wrap items-center gap-1 border-0 p-0 text-[13px]">
          <legend className="mb-1 p-0 font-medium">Bars the two indicators read</legend>
          {form.bar_seconds.map((seconds) => (
            <button
              key={seconds}
              type="button"
              className="btn num"
              aria-pressed={draft.bar_seconds === seconds}
              onClick={() => setDraft((before) => ({ ...before, bar_seconds: seconds }))}
            >
              {seconds} s
            </button>
          ))}
          <span className="note ml-2 basis-full sm:basis-auto">
            The chart stays on seconds. Longer bars are read at their close and signal less often.
          </span>
        </fieldset>
        <div className="flex flex-wrap items-center gap-2">
          <button type="submit" className={`btn ${settings && changed && !busy ? "btn-primary" : ""}`} disabled={!settings || !changed || busy}>
            {busy ? "Applying…" : "Apply"}
          </button>
          <button type="button" className="btn" disabled={busy || atDefaults} onClick={() => void apply(form.defaults)}>
            Back to the defaults
          </button>
          {error ? (
            <span role="alert" className="text-[13px]" style={{ color: "var(--color-warn)" }}>
              {error}
            </span>
          ) : null}
        </div>
        <p className="note m-0 max-w-[100ch]">
          New settings apply to the whole trading day: its orders are worked out again from the stored seconds, so the table and
          the triangles change with them. An order is signalled once, on the bar where the condition turns true, and never from
          a price that is stale or outside market hours.
        </p>
      </form>
    </details>
  );
});
