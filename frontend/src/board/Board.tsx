import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Panel, Result } from "../components/ui";
import { dateTimeUTC } from "../format";
import { DownloadButtons, DownloadNote, useHistoryDownload } from "../lab/history";
import { type LabInfo, defaultBase, downloadRange, missingRanges, money, percent, signedMoney, stored, units } from "../lab/model";
import { WindowPanel } from "./ContextChart";
import {
  type BoardResult,
  type BoardState,
  type DayRow,
  KINDS,
  type Kind,
  type KindDay,
  type Line,
  PRESETS,
  type Preset,
  type SortKey,
  type Variant,
  boardBody,
  dayRows,
  defaultPicks,
  followTheWinner,
  headline,
  kindOf,
  methodLabel,
  presetPeriod,
  rank,
} from "./model";

const STORAGE_KEY = "fxlab.board.period.v1";
const MAX_PICKS = 6;

/** The period to run on: two days typed in, or one of the presets, which moves along with the stored history. */
type Period = { from: string; to: string; preset: Preset | null };

function savedPeriod(): Period | null {
  try {
    const value = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null") as Partial<Period> | null;
    const day = /^\d{4}-\d{2}-\d{2}$/;
    if (!value || !day.test(value.from ?? "") || !day.test(value.to ?? "")) return null;
    const preset = PRESETS.find((p) => p.key === value.preset)?.key ?? null;
    return { from: value.from ?? "", to: value.to ?? "", preset };
  } catch {
    return null; // private mode, or something else wrote there
  }
}

function savePeriod(period: Period) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(period));
  } catch {
    // a convenience only
  }
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** Switches for the bar lengths the methods read: all on unless switched off. */
function BarLengths({
  all,
  off,
  onToggle,
}: {
  all: string[];
  off: ReadonlySet<string>;
  onToggle: (timeframe: string) => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Bar lengths shown">
      <span className="note mr-1">signals from bars of</span>
      {all.map((tf) => (
        <button key={tf} type="button" className="btn num" aria-pressed={!off.has(tf)} onClick={() => onToggle(tf)}>
          {tf}
        </button>
      ))}
    </div>
  );
}

/** Which kind of method the scoreboard lists: all, those that trade with a move, or those that trade against one. */
function Kinds({ kind, onKind }: { kind: Kind | null; onKind: (kind: Kind | null) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Kind of method shown">
      <button type="button" className="btn" aria-pressed={kind == null} onClick={() => onKind(null)}>
        All kinds
      </button>
      {KINDS.filter((k) => k.key !== "other").map((k) => (
        <button key={k.key} type="button" className="btn" aria-pressed={kind === k.key} onClick={() => onKind(k.key)}>
          {k.label}
        </button>
      ))}
    </div>
  );
}

const KIND_SHORT = Object.fromEntries(KINDS.map((k) => [k.key, k.short])) as Record<Kind, string>;

function Scoreboard({
  result,
  lines,
  variant,
  onVariant,
  sort,
  onSort,
  picks,
  onPick,
  filters,
}: {
  result: BoardResult;
  lines: Line[];
  variant: Variant;
  onVariant: (variant: Variant) => void;
  sort: SortKey;
  onSort: (sort: SortKey) => void;
  picks: string[];
  onPick: (id: string) => void;
  filters: React.ReactNode;
}) {
  const head = headline(lines);
  const clips = result.request.execution.max_clips;
  const split = result.split_day != null;
  const pinned = { position: "sticky", left: 0, background: "var(--color-panel)" } as const;
  return (
    <Panel
      title="How each method did"
      aside={<span>money in {result.currency}, after the costs assumed; every strategy with its default parameters</span>}
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 px-3 pt-2.5">
        {result.variants.includes("all") ? (
          <div className="flex items-center gap-1" role="group" aria-label="How positions are built">
            <button type="button" className="btn" aria-pressed={variant === "all"} onClick={() => onVariant("all")}>
              Adding, up to {clips} clips
            </button>
            <button type="button" className="btn" aria-pressed={variant === "one"} onClick={() => onVariant("one")}>
              One clip only
            </button>
          </div>
        ) : null}
        {filters}
        <label className="inline-flex items-center gap-1.5 text-[13px]">
          <span className="note">best first by</span>
          <select className="field" value={sort} onChange={(e) => onSort(e.target.value as SortKey)}>
            <option value="net">net result</option>
            <option value="later">result on the later days</option>
            <option value="worked">share of trades that worked</option>
            <option value="trades">number of trades</option>
          </select>
        </label>
      </div>

      <p className="m-0 max-w-[110ch] px-3 pt-2.5 text-[13px]">
        Of {head.methods} methods, {head.positive} ended above zero
        {head.both != null ? `, ${head.both} of them on the earlier and on the later days alike` : ""}.{" "}
        {head.random != null ? (
          <>
            Entering at random on the same bars made up to <span className="num">{signedMoney(head.random)}</span>:{" "}
            {head.aboveRandom} {head.aboveRandom === 1 ? "method" : "methods"} did better than that.
          </>
        ) : null}
      </p>

      <div className="mt-2 max-h-[560px] overflow-auto border-t border-line">
        <table className="data sticky-head">
          <thead>
            <tr>
              <th style={{ ...pinned, zIndex: 2 }}>Method</th>
              <th title="Momentum: trades with a move. Mean reversion: trades against a stretched one.">Kind</th>
              <th className="r">Net result</th>
              <th className="r" title="Trades that closed with a profit">
                Worked
              </th>
              <th className="r" title="Trades that closed with a loss">
                Failed
              </th>
              <th className="r" title="What a trade that worked made on average">
                Avg win
              </th>
              <th className="r" title="What a trade that failed lost on average. Small wins next to large losses show here.">
                Avg loss
              </th>
              <th className="r">Trades</th>
              <th className="r" title="Share of the trades that made money. Never judge a method by this alone.">
                Share
              </th>
              {split ? (
                <>
                  <th className="r" title={`The trading days before ${result.split_day}`}>
                    Earlier days
                  </th>
                  <th className="r" title={`The trading days from ${result.split_day} on, kept back as a check`}>
                    Later days
                  </th>
                </>
              ) : null}
              <th className="r" title="Trading days that ended with a profit, and with a loss">
                Days up / down
              </th>
              <th className="r">Worst day</th>
              <th className="r">Largest loss</th>
              <th className="r" title="The largest position the method held">
                Largest position
              </th>
            </tr>
          </thead>
          <tbody>
            {lines.map(({ combo, tally, worked, earlier, later }) => {
              const picked = picks.includes(combo.id);
              const random = combo.family === "benchmark";
              return (
                <tr key={combo.id} className={random ? "text-ink-2" : ""}>
                  <td style={pinned}>
                    <label className="inline-flex cursor-pointer items-center gap-2" title={combo.title}>
                      <input
                        type="checkbox"
                        checked={picked}
                        disabled={!picked && picks.length >= MAX_PICKS}
                        onChange={() => onPick(combo.id)}
                        aria-label={`Show ${methodLabel(combo)} in the day table`}
                      />
                      <span className="font-medium">{combo.short}</span>
                      <span className="num text-ink-2">{combo.timeframe}</span>
                    </label>
                  </td>
                  <td className="text-ink-2">{KIND_SHORT[kindOf(combo)]}</td>
                  <td className="r num">{tally.trades ? <Result value={tally.net} /> : <span className="text-ink-3">no trade</span>}</td>
                  <td className="r num">{tally.trades ? tally.wins.toLocaleString("en-US") : ""}</td>
                  <td className="r num">{tally.trades ? tally.losses.toLocaleString("en-US") : ""}</td>
                  <td className="r num">{tally.average_win == null ? "" : signedMoney(tally.average_win)}</td>
                  <td className="r num">{tally.average_loss == null ? "" : signedMoney(tally.average_loss)}</td>
                  <td className="r num">{tally.trades.toLocaleString("en-US")}</td>
                  <td className="r num">{tally.trades ? percent(worked) : ""}</td>
                  {split ? (
                    <>
                      <td className="r num">{tally.in_sample?.trades ? <Result value={earlier} /> : ""}</td>
                      <td className="r num">{tally.out_of_sample?.trades ? <Result value={later} /> : ""}</td>
                    </>
                  ) : null}
                  <td className="r num">{tally.trades ? `${tally.days_won} / ${tally.days_lost}` : ""}</td>
                  <td className="r num">{tally.trades ? signedMoney(tally.worst_day) : ""}</td>
                  <td className="r num">{tally.largest_loss == null ? "" : money(tally.largest_loss)}</td>
                  <td className="r num">{tally.trades ? units(tally.max_units) : ""}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="note m-0 border-t border-line px-3 py-2">
        A trade “worked” when it closed with a profit after the spread. Momentum methods trade with a move (trend
        followers and breakouts); mean-reversion methods trade against a stretched one (bands, oscillators, levels). RAND
        enters at random: what the trading rules make on their own, and the line a method has to beat. Tick up to{" "}
        {MAX_PICKS} methods to follow them day by day below.
        {split
          ? " A method that only shines on the earlier days, or only on the later ones, has most likely been lucky."
          : ""}
      </p>
    </Panel>
  );
}

function Days({
  result,
  rows,
  picks,
  follow,
  selected,
  onSelect,
}: {
  result: BoardResult;
  rows: DayRow[];
  picks: string[];
  follow: ReturnType<typeof followTheWinner>;
  selected: string | null;
  onSelect: (day: string) => void;
}) {
  const combos = picks.map((id) => result.combos.find((c) => c.id === id)).filter((c) => c != null);
  const pinned = { position: "sticky", left: 0, background: "var(--color-panel)" } as const;
  const firstEarlier = result.split_day ? rows.findIndex((row) => !row.later) : -1;
  const columns = 4 + combos.length;
  return (
    <Panel title="Day by day" aside={<span>newest first; click a day to see it on the chart below</span>}>
      <div className="max-h-[460px] overflow-auto">
        <table className="data sticky-head">
          <thead>
            <tr>
              <th style={{ ...pinned, zIndex: 2 }}>Trading day</th>
              <th title="Among all methods on the board (random entries aside): the one that made the most that day. Known only afterwards.">
                Best that day
              </th>
              {combos.map((combo) => (
                <th key={combo.id} className="r" title={combo.title}>
                  <span className="text-ink">{methodLabel(combo)}</span>
                </th>
              ))}
              <th
                className="r"
                title="What all momentum methods made that day, added up, and how many of them made money and lost. A day on which they made money was a day that trended."
              >
                Momentum methods
              </th>
              <th
                className="r"
                title="What all mean-reversion methods made that day, added up, and how many of them made money and lost. A day on which they made money was a day that stayed in its range."
              >
                Mean-reversion methods
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <DayLine
                key={row.day}
                row={row}
                divider={index === firstEarlier && index > 0 ? columns : 0}
                selected={row.day === selected}
                onSelect={onSelect}
                pinned={pinned}
              />
            ))}
          </tbody>
        </table>
      </div>
      <p className="note m-0 border-t border-line px-3 py-2">
        Each cell: trades that worked ✓ and that failed ✗, then the result of the day. ★ marks the best of the ticked
        methods. The last two columns add up every method of a kind: how many of them made money that day (up), how many
        lost (down), and what they made together. Momentum methods earning while mean-reversion methods lose is what a
        trending day looks like.{" "}
        {follow ? (
          <>
            The best method of a day is known only when the day is over. Trading each day the method that was best the day
            before would have made <span className="num text-ink">{signedMoney(follow.net)}</span> {result.currency} over{" "}
            {follow.days} days: it made money on {follow.won}, lost on {follow.lost}
            {follow.idle ? `, did not trade on ${follow.idle}` : ""}.
            {follow.baseline != null ? ` Of all days on which any method traded, ${percent(follow.baseline)} made money.` : ""}
          </>
        ) : null}
      </p>
    </Panel>
  );
}

function DayLine({
  row,
  divider,
  selected,
  onSelect,
  pinned,
}: {
  row: DayRow;
  /** above this row, a line of this many columns that says where the earlier part of the period begins */
  divider: number;
  selected: boolean;
  onSelect: (day: string) => void;
  pinned: React.CSSProperties;
}) {
  return (
    <>
      {divider ? (
        <tr>
          <td colSpan={divider} className="note" style={{ background: "var(--color-plane)" }}>
            above: the later days, kept back as a check. Below: the earlier days.
          </td>
        </tr>
      ) : null}
      <tr
        onClick={() => onSelect(row.day)}
        className="cursor-pointer hover:bg-raise"
        style={selected ? { background: "var(--color-raise)" } : undefined}
        aria-selected={selected}
      >
        <td className="num" style={selected ? { ...pinned, background: "var(--color-raise)" } : pinned}>
          <button type="button" className="num cursor-pointer border-0 bg-transparent p-0 text-ink underline-offset-2 hover:underline">
            {row.day}
          </button>
        </td>
        <td>
          {row.top ? (
            <>
              {row.top.label} <span className="num text-ink-2">{signedMoney(row.top.pnl)}</span>
            </>
          ) : (
            <span className="text-ink-3">none made money</span>
          )}
        </td>
        {row.cells.map((cell, k) => (
          // the columns keep their order: the position is the method
          <td key={k} className="r num">
            {cell.trades === 0 ? (
              <span className="text-ink-3">no trade</span>
            ) : (
              <>
                {row.best === k ? (
                  <span aria-label="best of the ticked methods" title="Best of the ticked methods that day" style={{ color: "var(--color-amber)" }}>
                    ★{" "}
                  </span>
                ) : null}
                <span className="text-ink-2">
                  {cell.wins}✓ {cell.losses}✗
                </span>{" "}
                <Result value={cell.pnl} />
              </>
            )}
          </td>
        ))}
        <KindCell day={row.kinds.momentum} />
        <KindCell day={row.kinds.reversion} />
      </tr>
    </>
  );
}

/** What the methods of one kind made on a day, added up, with how many of them made money and how many lost. */
function KindCell({ day }: { day: KindDay }) {
  return (
    <td className="r num">
      {day.up + day.down === 0 ? (
        <span className="text-ink-3">no trade</span>
      ) : (
        <>
          <span className="text-ink-2">
            {day.up} up, {day.down} down
          </span>{" "}
          <Result value={day.net} />
        </>
      )}
    </td>
  );
}

export function Board({ symbol }: { symbol: string | null }) {
  const [info, setInfo] = useState<LabInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [period, setPeriod] = useState<Period | null>(savedPeriod);
  const [board, setBoard] = useState<BoardState | null>(null);
  const [result, setResult] = useState<{ id: number; finished_ms: number; data: BoardResult } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [variant, setVariant] = useState<Variant>("all");
  const [sort, setSort] = useState<SortKey>("net");
  const [off, setOff] = useState<ReadonlySet<string>>(new Set());
  const [kind, setKind] = useState<Kind | null>(null);
  const [picked, setPicked] = useState<string[] | null>(null);
  const [day, setDay] = useState<string | null>(null);
  const chart = useRef<HTMLDivElement>(null);

  const refresh = useCallback(() => {
    api
      .lab(symbol ?? undefined)
      .then((next) => {
        setInfo(next);
        setLoadError(null);
      })
      .catch((e: unknown) => setLoadError(message(e)));
  }, [symbol]);
  useEffect(refresh, [refresh]);
  const download = useHistoryDownload(info?.symbol ?? null, refresh);

  const base = info ? defaultBase(info) : "1m";
  const history = info ? stored(info, base) : null;
  const firstStored = history?.first ?? null;
  const lastStored = history?.last ?? null;
  // nothing chosen yet: the last month of what is stored.  A preset follows the history as it grows.
  useEffect(() => {
    if (!history) return;
    setPeriod((current) => {
      const preset = current ? current.preset : "month";
      return preset ? { ...presetPeriod(history, preset), preset } : current;
    });
    // the stored span as two strings: a fresh object for the same history must not run this again
  }, [firstStored, lastStored]);

  // --- the run in progress, and the last finished board ------------------------------------
  const look = useCallback(() => {
    api
      .board()
      .then(setBoard)
      .catch(() => undefined); // tried again by the next look
  }, []);
  useEffect(look, [look]);
  const running = board?.run?.status === "running";
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(look, 1000);
    return () => clearInterval(timer);
  }, [running, look]);

  const finished = board?.result ?? null;
  const loaded = result?.id ?? null;
  useEffect(() => {
    if (!finished || finished.id === loaded) return;
    let cancelled = false;
    api
      .boardResult()
      .then((data) => {
        if (cancelled) return;
        setResult({ id: finished.id, finished_ms: finished.finished_ms, data });
        setVariant(data.variants.includes("all") ? "all" : "one");
        setPicked(null);
        setDay(data.days[data.days.length - 1] ?? null);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(`The finished board could not be loaded: ${message(e)}`);
      });
    return () => {
      cancelled = true;
    };
  }, [finished, loaded]);

  const data = result?.data ?? null;
  const filter = useMemo(
    () => (data && off.size ? new Set(data.request.timeframes.filter((tf) => !off.has(tf))) : null),
    [data, off],
  );
  const lines = useMemo(() => (data ? rank(data.combos, variant, sort, filter, kind) : []), [data, variant, sort, filter, kind]);
  const picks = useMemo(() => {
    if (!data) return [];
    if (picked) return picked;
    return defaultPicks(filter ? data.combos.filter((c) => filter.has(c.timeframe)) : data.combos, variant);
  }, [data, picked, filter, variant]);
  const rows = useMemo(() => (data ? dayRows(data, variant, picks, filter) : []), [data, variant, picks, filter]);
  const follow = useMemo(() => (data ? followTheWinner(data.combos, variant, filter) : null), [data, variant, filter]);

  if (loadError && !info) {
    return (
      <main className="mx-auto max-w-[1680px] p-2">
        <div role="alert" className="panel px-4 py-3" style={{ borderLeft: "3px solid var(--color-bad)" }}>
          <h2 className="m-0 text-[15px] font-semibold">The signal board could not be loaded</h2>
          <p className="m-0 mt-1 text-[13px] text-ink-2">{loadError}</p>
          <button type="button" className="btn mt-2" onClick={refresh}>
            Try again
          </button>
        </div>
      </main>
    );
  }
  if (!info) {
    return (
      <main className="mx-auto max-w-[1680px] p-2">
        <p className="note m-0 px-1 py-2">Loading the signal board…</p>
      </main>
    );
  }

  const choose = (next: Period) => {
    savePeriod(next);
    setPeriod(next);
  };
  const minutes = stored(info, "1m");
  const missing = period && base === "1m" ? missingRanges(minutes, period.from, period.to, Date.now()) : [];
  const run = board?.run ?? null;

  const start = async () => {
    if (!period) return;
    const built = boardBody(info.symbol, base, period.from, period.to);
    if ("error" in built) {
      setError(built.error);
      return;
    }
    setError(null);
    try {
      setBoard(await api.startBoard(built.body));
    } catch (e) {
      setError(message(e));
      look();
    }
  };

  const toggle = (timeframe: string) =>
    setOff((current) => {
      const next = new Set(current);
      if (next.has(timeframe)) next.delete(timeframe);
      else if (data && next.size + 1 < data.request.timeframes.length) next.add(timeframe); // one stays on
      return next;
    });
  const pick = (id: string) => setPicked(picks.includes(id) ? picks.filter((p) => p !== id) : [...picks, id].slice(0, MAX_PICKS));
  const filters = data ? (
    <>
      <Kinds kind={kind} onKind={setKind} />
      <BarLengths all={data.request.timeframes} off={off} onToggle={toggle} />
    </>
  ) : null;
  const selectDay = (next: string) => {
    setDay(next);
    window.requestAnimationFrame(() => chart.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  const ex = data?.request.execution;
  return (
    <main className="mx-auto flex max-w-[1680px] flex-col gap-2 p-2">
      <Panel
        title="Signal board"
        aside={<span>every method on every bar length at once, on the stored provider history of {info.symbol}; reads only</span>}
      >
        <form
          className="flex flex-col gap-3 px-3 py-3"
          onSubmit={(event) => {
            event.preventDefault();
            void start();
          }}
        >
          <div className="flex flex-wrap items-end gap-x-3 gap-y-2">
            {history ? (
              <div className="flex flex-wrap items-center gap-1" role="group" aria-label="Period">
                {PRESETS.map((preset) => {
                  const span = presetPeriod(history, preset.key);
                  return (
                    <button
                      key={preset.key}
                      type="button"
                      className="btn"
                      aria-pressed={period?.from === span.from && period.to === span.to}
                      onClick={() => choose({ ...span, preset: preset.key })}
                    >
                      {preset.label}
                    </button>
                  );
                })}
              </div>
            ) : null}
            <label className="flex flex-col gap-0.5">
              <span className="note">From (first day)</span>
              <input
                className="field num"
                type="date"
                value={period?.from ?? ""}
                onChange={(e) => choose({ from: e.target.value, to: period?.to ?? e.target.value, preset: null })}
              />
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="note">To (last day)</span>
              <input
                className="field num"
                type="date"
                value={period?.to ?? ""}
                onChange={(e) => choose({ from: period?.from ?? e.target.value, to: e.target.value, preset: null })}
              />
            </label>
            <button type="submit" className="btn btn-primary px-4 py-1.5" disabled={running || !history || !period}>
              {running ? "Running…" : "Run all methods"}
            </button>
          </div>

          {running && run ? (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1" role="status">
              <progress className="h-2 w-56 max-w-full" max={run.total ?? 1} value={run.total ? run.done : undefined} />
              <span className="note num">
                {run.total ? `${run.done} of ${run.total} methods` : "reading the stored bars"}
                {run.label ? `, last: ${run.label.replace("@", " on ")}` : ""}
              </span>
              <span className="note">The run goes on if this page is closed.</span>
            </div>
          ) : null}
          {!running && run?.status === "failed" ? (
            <p role="alert" className="m-0 text-[13px]">
              <span aria-hidden style={{ color: "var(--color-bad)" }}>
                ✕
              </span>{" "}
              The last run did not finish: {run.error ?? "no reason given"}
            </p>
          ) : null}
          {error ? (
            <p role="alert" className="m-0 text-[13px]">
              <span aria-hidden style={{ color: "var(--color-bad)" }}>
                ✕
              </span>{" "}
              {error}
            </p>
          ) : null}

          {history ? (
            <p className="note m-0 flex flex-wrap items-center gap-x-3 gap-y-1.5">
              <span>
                Stored {history.timeframe} history: {history.first.slice(0, 10)} to {history.last.slice(0, 10)},{" "}
                {history.bars.toLocaleString("en-US")} bars.
                {missing.length ? " The period chosen reaches outside it; a run uses only what is stored." : ""}
              </span>
              <DownloadButtons history={minutes} missing={missing} download={download} />
              <DownloadNote download={download} />
            </p>
          ) : (
            <div role="alert" className="max-w-[110ch] text-[13px]" style={{ borderLeft: "3px solid var(--color-amber)", paddingLeft: 10 }}>
              <p className="m-0">
                No history of {info.symbol} is stored yet, so there is nothing to run the methods on. A year of one-minute
                bars is about 370,000 rows from the provider.
              </p>
              <p className="m-0 mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={download.downloading}
                  onClick={() => void download.start(downloadRange(null, Date.now()))}
                >
                  Download the last 12 months
                </button>
                <DownloadNote download={download} />
              </p>
            </div>
          )}
        </form>
      </Panel>

      {data && result && ex ? (
        <>
          <div className="panel px-3 py-2">
            <p className="m-0 text-[14px]">
              <strong className="font-semibold">{data.combos.length} methods</strong> on {data.request.symbol} from{" "}
              {data.data.first_day} to {data.data.last_day}: {data.data.trading_days} trading days,{" "}
              {data.data.base_bars.toLocaleString("en-US")} {data.request.base_timeframe} bars.
              {data.data.run_in_from ? ` The indicators had the bars from ${data.data.run_in_from.slice(0, 10)} on to settle.` : ""}{" "}
              Run finished {dateTimeUTC(result.finished_ms)} UTC.
            </p>
            <p className="note m-0 mt-1">
              Every method is traded by the same rules:{" "}
              {ex.max_clips > 1
                ? `clips of ${units(ex.clip_units)} ${data.unit_currency}, at most ${ex.max_clips}, one more every ${ex.add_step_pips} pips against`
                : `one order of ${units(ex.clip_units)} ${data.unit_currency} per position, nothing added to it`}
              ; target {ex.target_pips} and stop {ex.stop_pips} pips; spread {ex.spread_pips}; a signal needs{" "}
              {ex.min_potential_pips} pips of potential. Other rules and other parameters: the strategy lab.
            </p>
          </div>

          {data.warnings.length ? (
            <div role="alert" className="panel px-4 py-3" style={{ borderLeft: "3px solid var(--color-amber)" }}>
              <ul className="m-0 flex max-w-[120ch] list-none flex-col gap-1 p-0 text-[13px]">
                {data.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            </div>
          ) : null}

          <Scoreboard
            result={data}
            lines={lines}
            variant={variant}
            onVariant={setVariant}
            sort={sort}
            onSort={setSort}
            picks={picks}
            onPick={pick}
            filters={filters}
          />
          <Days result={data} rows={rows} picks={picks} follow={follow} selected={day} onSelect={selectDay} />
          <div ref={chart} className="scroll-mt-2">
            {day ? <WindowPanel result={data} day={day} precision={info.price_precision} pip={info.pip_size} /> : null}
          </div>
        </>
      ) : (
        <p className="note m-0 max-w-[100ch] px-1 py-2">
          {running
            ? "The first results appear here when the run has finished."
            : "Nothing has been run since the application started. Choose a period and run: every strategy is tried on bars of every length, a month takes a few seconds. What comes out is what these rules would have done on past prices. It is not a forecast, and no method here is known to be profitable in the future."}
        </p>
      )}
    </main>
  );
}
