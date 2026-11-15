import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { PRESETS, presetPeriod } from "../board/model";
import { Panel } from "../components/ui";
import { DownloadButtons, DownloadNote, useHistoryDownload } from "../lab/history";
import { type LabInfo, defaultBase, downloadRange, missingRanges, stored, units } from "../lab/model";
import { SetupResults } from "./Results";
import {
  type Draft,
  MAX_METHODS,
  type Saved,
  type SetupReport,
  definitionOf,
  draftOf,
  groups,
  methodId,
  restoreDraft,
  runBody,
  sameRule,
  shortLabel,
  toggleMethod,
} from "./model";

const STORAGE_KEY = "fxlab.setups.draft.v1";

function loadDraft(): unknown {
  try {
    const text = window.localStorage.getItem(STORAGE_KEY);
    return text ? JSON.parse(text) : null;
  } catch {
    return null; // private mode, or something else wrote there: start from the defaults
  }
}

function keepDraft(draft: Draft) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(draft));
  } catch {
    // a convenience only
  }
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** The methods a setup can name: every strategy on every bar length, by the kind of signal it gives. */
function Methods({ info, chosen, onToggle }: { info: LabInfo; chosen: string[]; onToggle: (id: string) => void }) {
  const [open, setOpen] = useState<Record<string, boolean>>({ reversion: true, momentum: true });
  const full = chosen.length >= MAX_METHODS;
  return (
    <div className="grid grid-cols-1 items-start gap-2 lg:max-w-[1180px] lg:grid-cols-2">
      {groups(info.strategies).map((group) => {
        const ticked = group.strategies.reduce(
          (sum, s) => sum + info.timeframes.filter((tf) => chosen.includes(methodId(s.key, tf))).length,
          0,
        );
        return (
          <details
            key={group.kind}
            className="border border-line"
            open={open[group.kind] ?? ticked > 0}
            onToggle={(event) => {
              const now = event.currentTarget.open;
              setOpen((before) => (before[group.kind] === now ? before : { ...before, [group.kind]: now }));
            }}
          >
            <summary className="cursor-pointer px-3 py-1.5 text-[13px]">
              <span className="font-semibold">{group.title}</span> <span className="note">{group.note}</span>
              {ticked ? <span className="note num"> · {ticked} ticked</span> : null}
            </summary>
            <div className="overflow-x-auto border-t border-line">
              <table className="data matrix">
                <thead>
                  <tr>
                    <th>Signals from bars of</th>
                    {info.timeframes.map((tf) => (
                      <th key={tf} className="c num">
                        {tf}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {group.strategies.map((s) => (
                    <tr key={s.key}>
                      <td title={s.summary} style={{ whiteSpace: "normal" }}>
                        <span className="font-medium">{s.short ?? s.key}</span>{" "}
                        <span className="block text-[11px] leading-tight text-ink-3 sm:inline sm:text-[13px] sm:text-ink-2">{s.title}</span>
                      </td>
                      {info.timeframes.map((tf) => {
                        const id = methodId(s.key, tf);
                        const checked = chosen.includes(id);
                        return (
                          <td key={tf} className="c">
                            <input
                              type="checkbox"
                              checked={checked}
                              disabled={!checked && full}
                              onChange={() => onToggle(id)}
                              aria-label={`${s.title} on ${tf} bars`}
                            />
                          </td>
                        );
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        );
      })}
    </div>
  );
}

export function Setups({ symbol }: { symbol: string | null }) {
  const [info, setInfo] = useState<LabInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [saved, setSaved] = useState<Saved[]>([]);
  const [running, setRunning] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [asking, setAsking] = useState<number | null>(null); // the saved setup about to be deleted
  const [leaveOut, setLeaveOut] = useState(false); // whether the "Leave out" part of the form is open
  const [report, setReport] = useState<SetupReport | null>(null);
  const results = useRef<HTMLDivElement>(null);

  const refresh = useCallback(() => {
    api
      .lab(symbol ?? undefined)
      .then((next) => {
        setInfo(next);
        setLoadError(null);
        setDraft((current) => current ?? restoreDraft(next, loadDraft()));
      })
      .catch((e: unknown) => setLoadError(message(e)));
  }, [symbol]);
  useEffect(refresh, [refresh]);
  const list = useCallback(() => {
    api
      .setups()
      .then(setSaved)
      .catch(() => undefined); // the list is a convenience: the form works without it
  }, []);
  useEffect(list, [list]);
  const download = useHistoryDownload(info?.symbol ?? null, refresh);

  const update = useCallback((change: (draft: Draft) => Draft) => {
    setNote(null);
    setDraft((current) => {
      if (!current) return current;
      const next = change(current);
      keepDraft(next);
      return next;
    });
  }, []);

  const base = info ? defaultBase(info) : "1m";
  const history = info ? stored(info, base) : null;
  const firstStored = history?.first ?? null;
  const lastStored = history?.last ?? null;
  // a preset follows the stored history as it grows; a form without a period gets the last month
  useEffect(() => {
    if (!history) return;
    setDraft((current) => {
      if (!current) return current;
      const preset = current.preset ?? (current.from && current.to ? null : "month");
      return preset ? { ...current, ...presetPeriod(history, preset), preset } : current;
    });
    // the stored span as two strings: a fresh object for the same history must not run this again
  }, [firstStored, lastStored, draft === null]);

  if (loadError && !info) {
    return (
      <main className="mx-auto max-w-[1680px] p-2">
        <div role="alert" className="panel px-4 py-3" style={{ borderLeft: "3px solid var(--color-bad)" }}>
          <h2 className="m-0 text-[15px] font-semibold">The setups could not be loaded</h2>
          <p className="m-0 mt-1 text-[13px] text-ink-2">{loadError}</p>
          <button type="button" className="btn mt-2" onClick={refresh}>
            Try again
          </button>
        </div>
      </main>
    );
  }
  if (!info || !draft) {
    return (
      <main className="mx-auto max-w-[1680px] p-2">
        <p className="note m-0 px-1 py-2">Loading the setups…</p>
      </main>
    );
  }

  const named = saved.find((s) => s.name.toLowerCase() === draft.name.trim().toLowerCase()) ?? null;
  const unchanged = named != null && sameRule(draft, named);
  const entry = (info.conditions ?? []).filter((c) => c.when === "entry");
  const minutes = stored(info, "1m");
  const missing = base === "1m" ? missingRanges(minutes, draft.from, draft.to, Date.now()) : [];
  const ex = info.execution;

  const run = async () => {
    const built = runBody(info.symbol, base, draft);
    if ("error" in built) {
      setError(built.error);
      return;
    }
    setError(null);
    setRunning(true);
    try {
      setReport(await api.runSetup(built.body));
      // the form is long on a phone: show where the answer is
      window.requestAnimationFrame(() => results.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    } catch (e) {
      setError(message(e));
    } finally {
      setRunning(false);
    }
  };

  const save = async () => {
    const built = definitionOf(draft);
    if ("error" in built) {
      setError(built.error);
      return;
    }
    setError(null);
    setSaving(true);
    try {
      const kept = await api.saveSetup(info.symbol, built.definition);
      setSaved((before) => (before.some((s) => s.id === kept.id) ? before.map((s) => (s.id === kept.id ? kept : s)) : [...before, kept]));
      setNote(`Saved as “${kept.name}”. It is kept on the server: every device that signs in sees it.`);
    } catch (e) {
      setError(message(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id: number) => {
    setAsking(null);
    try {
      await api.deleteSetup(id);
      setNote("Deleted.");
    } catch (e) {
      setError(message(e));
    }
    list();
  };

  return (
    <main className="mx-auto flex max-w-[1680px] flex-col gap-2 p-2">
      <Panel
        title="My setups"
        aside={<span>a rule of your own from the methods, run on the stored provider history of {info.symbol}; reads only</span>}
      >
        <form
          className="flex flex-col gap-3 px-3 py-3"
          onSubmit={(event) => {
            event.preventDefault();
            void run();
          }}
        >
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
            <span className="note">Saved:</span>
            {saved.length === 0 ? <span className="note">none yet</span> : null}
            {saved.map((s) => (
              <button
                key={s.id}
                type="button"
                className="btn"
                aria-pressed={named?.id === s.id}
                title="Put this setup into the form"
                onClick={() => {
                  setError(null);
                  setAsking(null);
                  update((d) => draftOf(info, s, d));
                }}
              >
                {s.name}
              </button>
            ))}
            <button
              type="button"
              className="btn"
              onClick={() => {
                setError(null);
                setAsking(null);
                update((d) => ({ ...d, name: "", methods: [], skip: [] }));
              }}
            >
              New setup
            </button>
          </div>

          <label className="flex max-w-[44ch] flex-col gap-0.5">
            <span className="note">Name</span>
            <input
              className="field"
              type="text"
              maxLength={80}
              value={draft.name}
              placeholder="What you call this rule"
              onChange={(e) => update((d) => ({ ...d, name: e.target.value }))}
            />
          </label>

          <div className="flex flex-col gap-1">
            <span className="note">
              Methods: tick a strategy under the bars it should read ({draft.methods.length} of at most {MAX_METHODS} ticked).
              Every strategy runs with its default parameters.
            </span>
            <Methods info={info} chosen={draft.methods} onToggle={(id) => update((d) => ({ ...d, methods: toggleMethod(d.methods, id) }))} />
          </div>

          <fieldset className="m-0 flex flex-col gap-1.5 border border-line px-3 pb-2.5 pt-1">
            <legend className="note px-1">Open a trade when</legend>
            <label className="inline-flex items-center gap-2 text-[13px]">
              <input type="radio" name="setup-mode" checked={draft.mode === "any"} onChange={() => update((d) => ({ ...d, mode: "any" }))} />
              any one of the ticked methods gives a signal
            </label>
            <label className="inline-flex flex-wrap items-center gap-2 text-[13px]">
              <input type="radio" name="setup-mode" checked={draft.mode === "all"} onChange={() => update((d) => ({ ...d, mode: "all" }))} />
              all of them point the same way, the oldest signal no more than
              <input
                className="field num w-[7ch]"
                type="number"
                inputMode="decimal"
                min={0}
                step="any"
                value={draft.within}
                disabled={draft.mode !== "all"}
                aria-label="Minutes within which all methods have to agree"
                onChange={(e) => update((d) => ({ ...d, within: e.target.value }))}
              />
              minutes old
            </label>
          </fieldset>

          <div className="grid grid-cols-2 items-end gap-x-3 gap-y-2 sm:grid-cols-3 lg:grid-cols-6">
            <label className="flex min-w-0 flex-col gap-0.5" title="Where a trade that goes wrong is closed: this far against the entry">
              <span className="note">
                Stop <span className="text-ink-3">(pips against)</span>
              </span>
              <input
                className="field num"
                type="number"
                inputMode="decimal"
                min={0}
                step="any"
                value={draft.stop}
                onChange={(e) => update((d) => ({ ...d, stop: e.target.value }))}
              />
            </label>
            <label className="flex min-w-0 flex-col gap-0.5" title="Where a trade takes its profit, after the spread">
              <span className="note">
                Target <span className="text-ink-3">(pips)</span>
              </span>
              <input
                className="field num"
                type="number"
                inputMode="decimal"
                min={0}
                step="any"
                value={draft.target}
                onChange={(e) => update((d) => ({ ...d, target: e.target.value }))}
              />
            </label>
            <label
              className="col-span-2 flex min-w-0 flex-col gap-0.5 sm:col-span-1"
              title="A signal is taken only when its strategy sees at least this much room after the spread. 0 takes every signal that sees more than the spread."
            >
              <span className="note">
                Room a signal must see <span className="text-ink-3">(pips; 0 = nearly every signal)</span>
              </span>
              <input
                className="field num"
                type="number"
                inputMode="decimal"
                min={0}
                step="any"
                value={draft.room}
                onChange={(e) => update((d) => ({ ...d, room: e.target.value }))}
              />
            </label>
          </div>
          <p className="note m-0 max-w-[130ch]">
            The rest is as in the strategy lab: one order of {units(ex.clip_units)} {info.unit_currency} at a time and nothing
            added to it, spread {ex.spread_pips} pip, entries from {ex.entry_start_utc} to {ex.entry_end_utc} UTC, at most{" "}
            {ex.max_hold_minutes} minutes in a trade, {ex.cooldown_minutes} minutes of pause after one.
          </p>

          {entry.length ? (
            <details
              className="border border-line"
              open={leaveOut || draft.skip.length > 0}
              onToggle={(event) => setLeaveOut(event.currentTarget.open)}
            >
              <summary className="cursor-pointer px-3 py-1.5 text-[13px]">
                <span className="font-semibold">Leave out</span>{" "}
                <span className="note">signals by what is known when the trade would be opened</span>
                {draft.skip.length ? <span className="note num"> · {draft.skip.length} ticked</span> : null}
              </summary>
              <div className="flex flex-col gap-1.5 border-t border-line px-3 py-2">
                {entry.map((c) => (
                  <label key={c.key} className="inline-flex items-center gap-2 text-[13px]" title={c.label}>
                    <input
                      type="checkbox"
                      checked={draft.skip.includes(c.key)}
                      onChange={() =>
                        update((d) => ({ ...d, skip: d.skip.includes(c.key) ? d.skip.filter((k) => k !== c.key) : [...d.skip, c.key] }))
                      }
                    />
                    signals {shortLabel(c)}
                  </label>
                ))}
                <p className="note m-0 max-w-[110ch]">
                  Tick one after the table “Why trades failed” has pointed at it, run again, and judge the new run by its later
                  days: a filter chosen on the same days it is judged on always looks good.
                </p>
              </div>
            </details>
          ) : null}

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
                      aria-pressed={draft.from === span.from && draft.to === span.to}
                      onClick={() => update((d) => ({ ...d, ...span, preset: preset.key }))}
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
                value={draft.from}
                onChange={(e) => update((d) => ({ ...d, from: e.target.value, to: d.to || e.target.value, preset: null }))}
              />
            </label>
            <label className="flex flex-col gap-0.5">
              <span className="note">To (last day)</span>
              <input
                className="field num"
                type="date"
                value={draft.to}
                onChange={(e) => update((d) => ({ ...d, to: e.target.value, from: d.from || e.target.value, preset: null }))}
              />
            </label>
          </div>

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
                No history of {info.symbol} is stored yet, so there is nothing to run a setup on. A year of one-minute bars is
                about 370,000 rows from the provider.
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

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <button type="submit" className="btn btn-primary px-4 py-1.5" disabled={running || !history}>
              {running ? "Running…" : "Run this setup"}
            </button>
            <button type="button" className="btn px-3 py-1.5" disabled={saving || unchanged} onClick={() => void save()}>
              {saving ? "Saving…" : unchanged ? "Saved" : named ? `Save the changes to “${named.name}”` : "Save"}
            </button>
            {named && asking !== named.id ? (
              <button type="button" className="btn px-3 py-1.5" onClick={() => setAsking(named.id)}>
                Delete
              </button>
            ) : null}
            {named && asking === named.id ? (
              <span className="inline-flex flex-wrap items-center gap-2 text-[13px]" role="group" aria-label="Confirm deleting">
                Delete “{named.name}” from the server?
                <button type="button" className="btn" onClick={() => void remove(named.id)}>
                  Yes, delete it
                </button>
                <button type="button" className="btn" onClick={() => setAsking(null)}>
                  Keep it
                </button>
              </span>
            ) : null}
            {running ? <span className="note">A year of one-minute bars takes a few seconds.</span> : null}
            {note && !error ? (
              <span role="status" className="note">
                {note}
              </span>
            ) : null}
            {error ? (
              <span role="alert" className="text-[13px]">
                <span aria-hidden style={{ color: "var(--color-bad)" }}>
                  ✕
                </span>{" "}
                {error}
              </span>
            ) : null}
          </div>
        </form>
      </Panel>

      {report ? (
        <div ref={results} className="flex scroll-mt-2 flex-col gap-2">
          <SetupResults report={report} info={info} />
        </div>
      ) : (
        <p className="note m-0 max-w-[110ch] px-1 py-2">
          A run shows every trade the rule would have made, the result of each day and of the whole period, and what the
          market was doing around the trades that failed. It is what the rule would have done on past prices under the costs
          assumed: not a forecast, and no rule here is known to be profitable in the future.
        </p>
      )}
    </main>
  );
}
