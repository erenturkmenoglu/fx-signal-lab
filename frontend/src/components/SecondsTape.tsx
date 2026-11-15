import { useEffect, useMemo, useRef, useState } from "react";
import { duration, price, timeUTC } from "../format";
import type { InstrumentStatus, Obs, ObsStatus } from "../types";
import { ObsPill, Panel } from "./ui";

/*
 * The seconds tape: one cell per second of the 1-second observation grid.
 *
 * State is encoded by shape first and colour second, so it survives colour-vision
 * deficiencies and greyscale:
 *   fresh    solid bar rising from the floor; its height is the number of quotes in that second
 *   carried  low hollow box on the floor (the previous quote is still in force)
 *   stale    hatched block (the quote is too old to be used)
 *   missing  solid block hanging from the ceiling (no quote known)
 *   (blank)  no row was recorded for that second
 */

const LEGEND: { status: ObsStatus; label: string }[] = [
  { status: "fresh", label: "Fresh, height = quotes in that second" },
  { status: "carried", label: "Carried forward" },
  { status: "stale", label: "Stale" },
  { status: "missing", label: "Missing" },
];

function Mark({ status, share, backfilled = false }: { status: ObsStatus; share: number; backfilled?: boolean }) {
  switch (status) {
    case "fresh":
      // seconds recovered after an outage are real market data but were not seen live: drawn as an outline
      return (
        <div
          className="w-full"
          style={{
            height: `${Math.round(16 + 84 * share)}%`,
            background: backfilled ? "transparent" : "var(--color-ink-2)",
            border: backfilled ? "1px solid var(--color-ink-2)" : undefined,
          }}
        />
      );
    case "carried":
      return <div className="w-full" style={{ height: 4, border: "1px solid var(--color-ink-3)" }} />;
    case "stale":
      return (
        <div
          className="w-full"
          style={{
            height: "40%",
            background: "repeating-linear-gradient(45deg, var(--color-warn) 0 2px, transparent 2px 4px)",
            outline: "1px solid var(--color-warn)",
            outlineOffset: -1,
          }}
        />
      );
    case "missing":
      return <div className="absolute inset-x-0 top-0" style={{ height: "22%", background: "var(--color-bad)" }} />;
  }
}

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => setWidth(entries[0]?.contentRect.width ?? 0));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

export function SecondsTape({ observations, now, inst }: { observations: Obs[]; now: number; inst: InstrumentStatus }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<number | null>(null);
  const [asTable, setAsTable] = useState(false);
  const count = width && width < 520 ? 60 : 120;

  const { cells, counts, maxQuotes, backfilled } = useMemo(() => {
    const byTs = new Map(observations.map((o) => [o.ts, o]));
    const end = Math.floor(now / 1000) * 1000; // the second in progress; the last closed one is end - 1000
    const list = Array.from({ length: count }, (_, i) => {
      const ts = end - (count - i) * 1000;
      return { ts, obs: byTs.get(ts) ?? null };
    });
    const tally: Record<ObsStatus | "none", number> = { fresh: 0, carried: 0, stale: 0, missing: 0, none: 0 };
    let max = 8;
    for (const cell of list) {
      tally[cell.obs?.status ?? "none"] += 1;
      if (cell.obs?.status === "fresh") max = Math.max(max, cell.obs.n_quotes);
    }
    return { cells: list, counts: tally, maxQuotes: max, backfilled: list.some((c) => c.obs?.source === "backfill") };
  }, [observations, now, count]);

  const hovered = hover != null ? cells[hover] : null;
  const first = cells[0]?.ts;
  const middle = cells[Math.floor(count / 2)]?.ts;
  const last = cells[count - 1]?.ts;

  return (
    <Panel
      title={`Last ${count} seconds`}
      aside={
        <>
          <span>
            fresh <span className="num text-ink">{counts.fresh}</span>
          </span>
          <span>
            carried <span className="num text-ink">{counts.carried}</span>
          </span>
          <span>
            stale <span className="num text-ink">{counts.stale}</span>
          </span>
          <span>
            missing <span className="num text-ink">{counts.missing}</span>
          </span>
          {counts.none ? (
            <span title="Seconds without a stored row">
              no row <span className="num text-ink">{counts.none}</span>
            </span>
          ) : null}
          <button type="button" className="btn" aria-pressed={asTable} onClick={() => setAsTable((v) => !v)}>
            Table
          </button>
        </>
      }
    >
      <div ref={ref} className="px-3 pt-3 pb-2">
        <div
          className="grid h-[68px] border-b border-line-2"
          style={{ gridTemplateColumns: `repeat(${count}, minmax(0, 1fr))`, columnGap: 1 }}
          onMouseLeave={() => setHover(null)}
          role="img"
          aria-label={`Observation status of the last ${count} seconds: ${counts.fresh} fresh, ${counts.carried} carried, ${counts.stale} stale, ${counts.missing} missing`}
        >
          {cells.map((cell, i) => (
            <div
              key={cell.ts}
              className="relative flex h-full items-end"
              style={hover === i ? { background: "rgb(255 255 255 / 0.08)" } : undefined}
              onMouseEnter={() => setHover(i)}
            >
              {cell.obs ? (
                <Mark
                  status={cell.obs.status}
                  share={Math.sqrt(Math.min(1, cell.obs.n_quotes / maxQuotes))}
                  backfilled={cell.obs.source === "backfill"}
                />
              ) : null}
            </div>
          ))}
        </div>
        <div className="note num flex justify-between pt-1">
          <span>{timeUTC(first)}</span>
          <span>{timeUTC(middle)}</span>
          <span>{timeUTC(last)}</span>
        </div>

        <div className="mt-1 flex min-h-[22px] flex-wrap items-center gap-x-5 gap-y-1 text-[12px]">
          {hovered ? (
            hovered.obs ? (
              <>
                <span className="num">{timeUTC(hovered.ts)}</span>
                <ObsPill status={hovered.obs.status} />
                <span>
                  <span className="text-ink-3">quotes</span> <span className="num">{hovered.obs.n_quotes}</span>
                </span>
                <span>
                  <span className="text-ink-3">bid</span>{" "}
                  <span className="num">{price(hovered.obs.bid, inst.price_precision)}</span>
                </span>
                <span>
                  <span className="text-ink-3">ask</span>{" "}
                  <span className="num">{price(hovered.obs.ask, inst.price_precision)}</span>
                </span>
                <span>
                  <span className="text-ink-3">quote age</span> <span className="num">{duration(hovered.obs.age_ms)}</span>
                </span>
                {hovered.obs.source !== "live" ? <span className="text-ink-3">source: {hovered.obs.source}</span> : null}
              </>
            ) : (
              <>
                <span className="num">{timeUTC(hovered.ts)}</span>
                <span className="text-ink-3">no row recorded for this second</span>
              </>
            )
          ) : (
            <>
              {LEGEND.map((item) => (
                <span key={item.status} className="inline-flex items-center gap-1.5 text-ink-2">
                  <span className="relative inline-flex h-[14px] w-[7px] items-end" aria-hidden>
                    <Mark status={item.status} share={0.6} />
                  </span>
                  {item.label}
                </span>
              ))}
              {backfilled ? (
                <span className="inline-flex items-center gap-1.5 text-ink-2">
                  <span className="relative inline-flex h-[14px] w-[7px] items-end" aria-hidden>
                    <Mark status="fresh" share={0.6} backfilled />
                  </span>
                  Recovered after an outage
                </span>
              ) : null}
            </>
          )}
        </div>
      </div>

      {asTable ? (
        <div className="max-h-[260px] overflow-auto border-t border-line">
          <table className="data">
            <thead>
              <tr>
                <th>Second (UTC)</th>
                <th>Status</th>
                <th className="r">Quotes</th>
                <th className="r">Bid</th>
                <th className="r">Ask</th>
                <th className="r">Quote age</th>
                <th>Source</th>
              </tr>
            </thead>
            <tbody>
              {[...cells].reverse().map((cell) => (
                <tr key={cell.ts}>
                  <td className="num">{timeUTC(cell.ts)}</td>
                  <td>{cell.obs ? <ObsPill status={cell.obs.status} /> : <span className="text-ink-3">no row</span>}</td>
                  <td className="r num">{cell.obs?.n_quotes ?? "—"}</td>
                  <td className="r num">{price(cell.obs?.bid, inst.price_precision)}</td>
                  <td className="r num">{price(cell.obs?.ask, inst.price_precision)}</td>
                  <td className="r num">{duration(cell.obs?.age_ms)}</td>
                  <td className="text-ink-3">{cell.obs?.source ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Panel>
  );
}
