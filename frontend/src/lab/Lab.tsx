import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import { Panel } from "../components/ui";
import { DownloadButtons, DownloadNote, useHistoryDownload } from "./history";
import { LabResults } from "./LabResults";
import {
  EXECUTION_FIELDS,
  EXECUTION_LABELS,
  type ExecutionField,
  type Form,
  type LabInfo,
  type ParamInfo,
  type Report,
  buildRequest,
  defaultForm,
  downloadRange,
  missingRanges,
  paramValue,
  restoreForm,
  stored,
  storedPeriod,
} from "./model";

const STORAGE_KEY = "fxlab.lab.form.v1";

function loadSaved(): unknown {
  try {
    const text = window.localStorage.getItem(STORAGE_KEY);
    return text ? JSON.parse(text) : null;
  } catch {
    return null; // private mode, or something else wrote there: start from the defaults
  }
}

function save(form: Form) {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(form));
  } catch {
    // a convenience only
  }
}

function Field({
  label,
  unit,
  help,
  children,
  wide = false,
}: {
  label: string;
  unit?: string;
  help?: string;
  children: React.ReactNode;
  wide?: boolean;
}) {
  return (
    <label className={`flex min-w-0 flex-col gap-0.5 ${wide ? "col-span-2" : ""}`} title={help}>
      <span className="note">
        {label}
        {unit ? <span className="text-ink-3"> ({unit})</span> : null}
      </span>
      {children}
    </label>
  );
}

function ParamInput({ param, value, onChange }: { param: ParamInfo; value: string; onChange: (value: string) => void }) {
  if (param.kind === "choice") {
    return (
      <select className="field" value={value} onChange={(e) => onChange(e.target.value)}>
        {param.choices.map((choice) => (
          <option key={choice} value={choice}>
            {choice.replace(/_/g, " ")}
          </option>
        ))}
      </select>
    );
  }
  return (
    <input
      className="field num"
      type="number"
      inputMode="decimal"
      step={param.kind === "int" ? 1 : "any"}
      min={param.minimum ?? undefined}
      max={param.maximum ?? undefined}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

export function Lab({ symbol }: { symbol: string | null }) {
  const [info, setInfo] = useState<LabInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<Report | null>(null);
  const results = useRef<HTMLDivElement>(null);

  const refresh = useCallback(() => {
    api
      .lab(symbol ?? undefined)
      .then((next) => {
        setInfo(next);
        setLoadError(null);
        setForm((current) => current ?? restoreForm(next, loadSaved()));
      })
      .catch((e: unknown) => setLoadError(e instanceof Error ? e.message : String(e)));
  }, [symbol]);
  useEffect(refresh, [refresh]);

  // a history download is watched until it ends, then the stored history is read again
  const download = useHistoryDownload(info?.symbol ?? null, refresh);

  // once history exists and no period has been chosen yet: the whole stored period
  useEffect(() => {
    if (!info) return;
    setForm((current) => {
      if (!current || current.from || current.to) return current;
      const period = storedPeriod(info, current.base) ?? storedPeriod(info, "1m");
      return period ? { ...current, base: stored(info, current.base) ? current.base : "1m", ...period } : current;
    });
  }, [info]);

  const update = useCallback((change: (form: Form) => Form) => {
    setForm((current) => {
      if (!current) return current;
      const next = change(current);
      save(next);
      return next;
    });
  }, []);

  const strategy = useMemo(() => info?.strategies.find((s) => s.key === form?.strategy) ?? null, [info, form?.strategy]);

  // a form that covered everything stored keeps doing so when more history has been downloaded
  const whole = info && form ? storedPeriod(info, form.base) : null;
  const wholeFrom = whole?.from ?? null;
  const wholeTo = whole?.to ?? null;
  const before = useRef<{ from: string; to: string } | null>(null);
  useEffect(() => {
    const was = before.current;
    before.current = wholeFrom && wholeTo ? { from: wholeFrom, to: wholeTo } : null;
    if (!was || !wholeFrom || !wholeTo || (was.from === wholeFrom && was.to === wholeTo)) return;
    update((f) => (f.from === was.from && f.to === was.to ? { ...f, from: wholeFrom, to: wholeTo } : f));
  }, [wholeFrom, wholeTo, update]);

  if (loadError && !info) {
    return (
      <main className="mx-auto max-w-[1680px] p-2">
        <div role="alert" className="panel px-4 py-3" style={{ borderLeft: "3px solid var(--color-bad)" }}>
          <h2 className="m-0 text-[15px] font-semibold">The strategy lab could not be loaded</h2>
          <p className="m-0 mt-1 text-[13px] text-ink-2">{loadError}</p>
          <button type="button" className="btn mt-2" onClick={refresh}>
            Try again
          </button>
        </div>
      </main>
    );
  }
  if (!info || !form) {
    return (
      <main className="mx-auto max-w-[1680px] p-2">
        <p className="note m-0 px-1 py-2">Loading the strategy lab…</p>
      </main>
    );
  }

  const history = stored(info, form.base);
  const period = storedPeriod(info, form.base);
  const bases = info.history.filter((h) => h.seconds <= 3600);

  const run = async () => {
    const built = buildRequest(info, form);
    if ("error" in built) {
      setError(built.error);
      return;
    }
    setError(null);
    setRunning(true);
    try {
      setReport(await api.backtest(built.body));
      // the form is long on a phone: show where the answer is
      window.requestAnimationFrame(() => results.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  // downloads bring one-minute bars: what is missing is said only when the run is simulated on those
  const minutes = stored(info, "1m");
  const missing = form.base === "1m" ? missingRanges(minutes, form.from, form.to, Date.now()) : [];

  const setExecution = (field: ExecutionField, value: string) =>
    update((f) => ({ ...f, execution: { ...f.execution, [field]: value } }));

  // the clip is typed in millions: 1 = 1,000,000 of the base currency
  const clipMillions = (() => {
    const raw = Number(form.execution.clip_units);
    return form.execution.clip_units.trim() !== "" && Number.isFinite(raw) ? String(raw / 1_000_000) : "";
  })();

  return (
    <main className="mx-auto flex max-w-[1680px] flex-col gap-2 p-2">
      <Panel title="Strategy lab" aside={<span>runs a strategy on the stored provider history of {info.symbol}; reads only</span>}>
        <form
          className="flex flex-col gap-3 px-3 py-3"
          onSubmit={(event) => {
            event.preventDefault();
            void run();
          }}
        >
          <div className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3 lg:grid-cols-6">
            <Field label="Strategy" wide>
              <select
                className="field"
                value={form.strategy}
                onChange={(e) => {
                  const next = info.strategies.find((s) => s.key === e.target.value);
                  update((f) => ({ ...f, strategy: e.target.value, timeframe: next?.timeframe ?? f.timeframe }));
                }}
              >
                {info.strategies.map((s) => (
                  <option key={s.key} value={s.key}>
                    {s.title}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="Signals from" unit="bars" help="The bars the strategy reads. A signal is known when its bar has closed.">
              <select className="field num" value={form.timeframe} onChange={(e) => update((f) => ({ ...f, timeframe: e.target.value }))}>
                {info.timeframes.map((tf) => (
                  <option key={tf} value={tf}>
                    {tf}
                  </option>
                ))}
              </select>
            </Field>
            <Field
              label="Simulated on"
              unit="stored bars"
              help="The stored bars on which entries, clips, stops and targets are worked out. Finer bars decide more exactly what happened first."
            >
              <select
                className="field num"
                value={form.base}
                disabled={bases.length === 0}
                onChange={(e) => {
                  const next = storedPeriod(info, e.target.value);
                  update((f) => ({ ...f, base: e.target.value, from: next?.from ?? f.from, to: next?.to ?? f.to }));
                }}
              >
                {bases.length === 0 ? <option value={form.base}>{form.base}</option> : null}
                {bases.map((h) => (
                  <option key={h.timeframe} value={h.timeframe}>
                    {h.timeframe}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="From" unit="first day">
              <input
                className="field num"
                type="date"
                value={form.from}
                onChange={(e) => update((f) => ({ ...f, from: e.target.value }))}
              />
            </Field>
            <Field label="To" unit="last day">
              <input
                className="field num"
                type="date"
                value={form.to}
                onChange={(e) => update((f) => ({ ...f, to: e.target.value }))}
              />
            </Field>
            <Field
              label="Out-of-sample"
              help="The later part of the period, kept back to check parameters that were chosen on the earlier part."
            >
              <select
                className="field"
                value={form.split}
                onChange={(e) => update((f) => ({ ...f, split: e.target.value as Form["split"] }))}
              >
                <option value="auto">last 3 days in 10</option>
                <option value="date">from a day</option>
                <option value="none">no split</option>
              </select>
            </Field>
            {form.split === "date" ? (
              <Field label="Out-of-sample from" unit="trading day">
                <input
                  className="field num"
                  type="date"
                  value={form.splitDate}
                  onChange={(e) => update((f) => ({ ...f, splitDate: e.target.value }))}
                />
              </Field>
            ) : null}
          </div>

          {strategy ? <p className="m-0 max-w-[110ch] text-[13px] text-ink-2">{strategy.summary}</p> : null}

          {history ? (
            <div className="flex flex-col gap-1.5">
              <p className="note m-0 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                <span>
                  Stored {history.timeframe} history: {history.first.slice(0, 10)} to {history.last.slice(0, 10)},{" "}
                  {history.bars.toLocaleString("en-US")} bars.
                </span>
                {period && (form.from !== period.from || form.to !== period.to) ? (
                  <button type="button" className="btn" onClick={() => update((f) => ({ ...f, from: period.from, to: period.to }))}>
                    Use all of it
                  </button>
                ) : null}
                <DownloadButtons history={minutes} missing={missing} download={download} />
                <DownloadNote download={download} />
              </p>
              {missing.length ? (
                <p role="status" className="m-0 max-w-[110ch] text-[13px]" style={{ borderLeft: "3px solid var(--color-amber)", paddingLeft: 10 }}>
                  The period chosen reaches outside the stored history. A run uses only the days that are stored: download the
                  rest first, or run on what is there.
                </p>
              ) : null}
            </div>
          ) : (
            <div role="alert" className="max-w-[110ch] text-[13px]" style={{ borderLeft: "3px solid var(--color-amber)", paddingLeft: 10 }}>
              <p className="m-0">
                No {form.base} history of {info.symbol} is stored yet, so there is nothing to test on. A year of one-minute
                bars is about 370,000 rows from the provider.
              </p>
              <p className="m-0 mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                <button
                  type="button"
                  className="btn btn-primary"
                  disabled={download.downloading}
                  onClick={() => void download.start(downloadRange(minutes, Date.now()))}
                >
                  {minutes ? "Update the one-minute history" : "Download the last 12 months"}
                </button>
                <button type="button" className="btn" onClick={refresh}>
                  Check again
                </button>
                <DownloadNote download={download} />
              </p>
            </div>
          )}

          {strategy && strategy.params.length ? (
            <fieldset className="m-0 border border-line px-3 pb-3 pt-1">
              <legend className="note px-1">{strategy.title}: parameters</legend>
              <div className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3 lg:grid-cols-6">
                {strategy.params.map((param) => (
                  <Field key={param.name} label={param.label} help={param.help}>
                    <ParamInput
                      param={param}
                      value={paramValue(form, strategy, param)}
                      onChange={(value) =>
                        update((f) => ({
                          ...f,
                          params: { ...f.params, [strategy.key]: { ...f.params[strategy.key], [param.name]: value } },
                        }))
                      }
                    />
                  </Field>
                ))}
              </div>
            </fieldset>
          ) : null}

          <fieldset className="m-0 border border-line px-3 pb-3 pt-1">
            <legend className="note px-1">How signals are traded</legend>
            <div className="grid grid-cols-2 gap-x-3 gap-y-2 sm:grid-cols-3 lg:grid-cols-6">
              {EXECUTION_FIELDS.map((field) => {
                const meta = EXECUTION_LABELS[field];
                if (field === "clip_units") {
                  return (
                    <Field key={field} label={meta.label} unit={`mn ${info.unit_currency}`} help={meta.help}>
                      <input
                        className="field num"
                        type="number"
                        inputMode="decimal"
                        step="any"
                        min={0}
                        value={clipMillions}
                        onChange={(e) =>
                          setExecution(field, e.target.value.trim() === "" ? "" : String(Number(e.target.value) * 1_000_000))
                        }
                      />
                    </Field>
                  );
                }
                const unit =
                  field === "flat_time" ? info.session_tz.replace(/^.*\//, "").replace(/_/g, " ") : meta.unit || undefined;
                return (
                  <Field key={field} label={meta.label} unit={unit} help={meta.help}>
                    <input
                      className="field num"
                      type={meta.time ? "time" : "number"}
                      inputMode={meta.time ? undefined : "decimal"}
                      step={meta.time ? undefined : field === "max_clips" ? 1 : "any"}
                      min={meta.time ? undefined : field === "max_clips" ? 1 : 0}
                      value={form.execution[field]}
                      onChange={(e) => setExecution(field, e.target.value)}
                    />
                  </Field>
                );
              })}
              <div className="col-span-2 flex items-end gap-4 pb-1">
                <label className="inline-flex items-center gap-1.5">
                  <input type="checkbox" checked={form.long} onChange={(e) => update((f) => ({ ...f, long: e.target.checked }))} />
                  take buy signals
                </label>
                <label className="inline-flex items-center gap-1.5">
                  <input type="checkbox" checked={form.short} onChange={(e) => update((f) => ({ ...f, short: e.target.checked }))} />
                  take sell signals
                </label>
              </div>
            </div>
          </fieldset>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <button type="submit" className="btn btn-primary px-4 py-1.5" disabled={running || !history}>
              {running ? "Running…" : "Run backtest"}
            </button>
            <button
              type="button"
              className="btn"
              disabled={running}
              onClick={() => {
                const fresh = defaultForm(info);
                save(fresh);
                setForm(fresh);
                setError(null);
              }}
            >
              Reset to defaults
            </button>
            {running ? <span className="note">A year of one-minute bars takes a few seconds.</span> : null}
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
          <LabResults report={report} precision={info.price_precision} storedTimeframes={info.history.map((h) => h.timeframe)} />
        </div>
      ) : (
        <p className="note m-0 px-1 py-2">
          A backtest shows what a rule would have done on past prices under the costs assumed. It is not a forecast, and no
          strategy here is known to be profitable in the future.
        </p>
      )}
    </main>
  );
}
