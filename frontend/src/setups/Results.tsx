import { useEffect, useMemo, useState } from "react";
import { Panel, Result } from "../components/ui";
import { LabResults, type TradeColumn } from "../lab/LabResults";
import { type LabInfo, percent, signedMoney, units } from "../lab/model";
import {
  type SetupReport,
  type SetupTrade,
  VERDICT_TEXT,
  type Why,
  type WhyCondition,
  aroundText,
  headlineOf,
  labeller,
  lineLabel,
  points,
  ruleText,
  shortLabel,
  verdictOf,
} from "./model";

const chance = (line: WhyCondition) => (line.noise == null ? "—" : `±${(line.noise * 100).toFixed(1)}`);

/** What a line amounts to, in words; a finding carries a mark, the rest is quiet. */
function Reading({ line }: { line: WhyCondition }) {
  const verdict = verdictOf(line);
  const found = verdict === "worse" || verdict === "better";
  return (
    <span className={found ? "font-medium" : "text-ink-2"}>
      {found ? (
        <span aria-hidden style={{ color: "var(--color-amber)" }}>
          ●{" "}
        </span>
      ) : null}
      {VERDICT_TEXT[verdict]}
    </span>
  );
}

function WhyRows({ title, lines }: { title: string; lines: WhyCondition[] }) {
  return (
    <>
      <tr>
        <td colSpan={8} className="note" style={{ background: "var(--color-plane)", whiteSpace: "normal" }}>
          {title}
        </td>
      </tr>
      {lines.map((line) => (
        <tr key={line.key}>
          <td title={line.label}>{lineLabel(line)}</td>
          <td className="r num">{line.with.trades.toLocaleString("en-US")}</td>
          <td className="r num">{percent(line.with.win_rate)}</td>
          <td className="r num">{line.without.trades.toLocaleString("en-US")}</td>
          <td className="r num">{percent(line.without.win_rate)}</td>
          <td className="r num">{points(line.difference)}</td>
          <td className="r num">{chance(line)}</td>
          <td>
            <Reading line={line} />
          </td>
        </tr>
      ))}
    </>
  );
}

/** The same lines for a narrow screen: one short paragraph each instead of eight columns. */
function WhyList({ title, lines }: { title: string; lines: WhyCondition[] }) {
  return (
    <>
      <p className="note m-0 px-3 py-1.5" style={{ background: "var(--color-plane)" }}>
        {title}
      </p>
      <ul className="m-0 flex list-none flex-col p-0">
        {lines.map((line) => (
          <li key={line.key} className="border-b border-line px-3 py-2 text-[13px] last:border-b-0">
            <div className="flex flex-wrap items-baseline justify-between gap-x-3">
              <span className="font-medium">{lineLabel(line)}</span>
              <Reading line={line} />
            </div>
            <div className="mt-0.5 text-ink-2">
              With it: <span className="num text-ink">{line.with.trades.toLocaleString("en-US")}</span> trades,{" "}
              <span className="num text-ink">{percent(line.with.win_rate)}</span> worked. Without:{" "}
              <span className="num text-ink">{line.without.trades.toLocaleString("en-US")}</span> trades,{" "}
              <span className="num text-ink">{percent(line.without.win_rate)}</span> worked. Difference{" "}
              <span className="num text-ink">{points(line.difference)}</span> points; chance gives{" "}
              <span className="num text-ink">{chance(line)}</span>.
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

const KNOWN_AT_ENTRY = "Known when the trade was opened. A trade can be left out by these.";
const SEEN_AFTER =
  "Seen only while the trade was open. These describe how a trade went wrong; they are not known in time to leave it out.";

/** Whether what was going on around the trades tells the ones that failed from the ones that worked. */
function WhyPanel({ report }: { report: SetupReport }) {
  const parts = useMemo(() => {
    const out: { key: string; label: string; why: Why }[] = [{ key: "whole", label: "Whole period", why: report.why }];
    if (report.in_sample?.why && report.out_of_sample?.why) {
      out.push({ key: "earlier", label: `Earlier days (to ${report.in_sample.last_day ?? "—"})`, why: report.in_sample.why });
      out.push({ key: "later", label: `Later days (from ${report.split_day ?? "—"})`, why: report.out_of_sample.why });
    }
    return out;
  }, [report]);
  const [part, setPart] = useState("whole");
  useEffect(() => setPart("whole"), [report]);
  const why = parts.find((p) => p.key === part)?.why ?? report.why;
  const entry = why.conditions.filter((c) => c.when === "entry");
  const after = why.conditions.filter((c) => c.when === "after");
  const ctx = report.context;
  return (
    <Panel title="Why trades failed" aside={<span>what the market was doing around each trade, and whether that tells losers from winners</span>}>
      {parts.length > 1 ? (
        <div className="flex flex-wrap items-center gap-1 px-3 pt-2.5" role="group" aria-label="Part of the period">
          {parts.map((p) => (
            <button key={p.key} type="button" className="btn" aria-pressed={part === p.key} onClick={() => setPart(p.key)}>
              {p.label}
            </button>
          ))}
        </div>
      ) : null}
      {why.trades === 0 ? (
        <p className="note m-0 px-3 py-3">No trade in this part of the period, so there is nothing to explain.</p>
      ) : (
        <>
          <p className="m-0 max-w-[120ch] px-3 pt-2.5 text-[13px]">
            {why.failed.toLocaleString("en-US")} of {why.trades.toLocaleString("en-US")} trades closed with a loss.{" "}
            {why.failed
              ? `For ${why.failed_unexplained.toLocaleString("en-US")} of them none of the things below held: the price simply went the other way.`
              : ""}
          </p>
          <div className="mt-2 border-t border-line sm:hidden">
            <WhyList title={KNOWN_AT_ENTRY} lines={entry} />
            <WhyList title={SEEN_AFTER} lines={after} />
          </div>
          <div className="mt-2 hidden overflow-x-auto border-t border-line sm:block">
            <table className="data">
              <thead>
                <tr>
                  <th>Around the trade</th>
                  <th className="r" title="Trades this held for">
                    Trades with it
                  </th>
                  <th className="r" title="Share of those trades that closed with a profit">
                    Worked
                  </th>
                  <th className="r" title="Trades this did not hold for">
                    Trades without
                  </th>
                  <th className="r" title="Share of those trades that closed with a profit">
                    Worked
                  </th>
                  <th className="r" title="How much more often the trades with it worked, in percentage points. Negative: less often.">
                    Difference
                  </th>
                  <th className="r" title="How far apart two groups of these sizes land by chance alone (two standard errors), in percentage points">
                    Chance gives
                  </th>
                  <th>Reading</th>
                </tr>
              </thead>
              <tbody>
                <WhyRows title={KNOWN_AT_ENTRY} lines={entry} />
                <WhyRows title={SEEN_AFTER} lines={after} />
              </tbody>
            </table>
          </div>
        </>
      )}
      <div className="border-t border-line px-3 py-2">
        <p className="note m-0 max-w-[130ch]">
          Momentum: {ctx.voters} momentum methods each point the way of their last signal; {ctx.threshold} more pointing one
          way than the other make the state. Channel: a line through the last {ctx.channel_hours} hours of {ctx.grid} closes
          with a band of {ctx.channel_width} standard deviations. A line of the table is a finding only if its difference is
          larger than what chance gives, and five lines are five chances to be fooled: believe one when the later days show
          it too. To try one of the upper lines, tick it under “Leave out” in the form, run again, and judge by the later
          days. The lower lines nearly always differ: a trade that lost is a trade the price moved against.
        </p>
      </div>
    </Panel>
  );
}

/** What a run of a setup gave: the answer in a sentence, then everything the strategy lab shows, and the why table. */
export function SetupResults({ report, info }: { report: SetupReport; info: LabInfo }) {
  const label = useMemo(() => labeller(info.strategies), [info.strategies]);
  const h = headlineOf(report);
  const req = report.request;
  const ex = req.execution;
  const conditions = report.context.conditions;
  const column: TradeColumn = useMemo(
    () => ({
      title: "Around the trade",
      help: "The momentum the trade was opened on (the reading: momentum methods pointing up minus those pointing down), then what else held for it",
      cell: (trade) => aroundText(trade as SetupTrade, conditions),
      wide: true,
    }),
    [conditions],
  );
  const left = req.setup.skip.map((key) => shortLabel(conditions.find((c) => c.key === key) ?? { key, label: key }));
  const heading = (
    <div className="panel px-3 py-2.5">
      <p className="m-0 text-[15px]">
        {h.trades === 0 ? (
          <strong className="font-semibold">No trade was made in this period.</strong>
        ) : (
          <>
            <strong className="font-semibold">
              {h.trades.toLocaleString("en-US")} {h.trades === 1 ? "trade" : "trades"}: {h.worked.toLocaleString("en-US")} worked,{" "}
              {h.failed.toLocaleString("en-US")} failed{h.flat ? `, ${h.flat} closed at zero` : ""}.
            </strong>{" "}
            Result <Result value={h.net} /> {report.currency} over {h.days} trading days ({h.daysUp} up, {h.daysDown} down).
            {h.averageWin != null && h.averageLoss != null ? (
              <>
                {" "}
                A trade that worked made <span className="num">{signedMoney(h.averageWin)}</span> on average, one that failed{" "}
                <span className="num">{signedMoney(h.averageLoss)}</span>.
              </>
            ) : null}
          </>
        )}
      </p>
      <p className="note m-0 mt-1 max-w-[150ch]">
        <span className="text-ink-2">{req.setup.name}</span> on {req.symbol}, {report.data.first_day} to {report.data.last_day}: a
        trade is opened when {ruleText(req.setup, label)}. One order of {units(ex.clip_units)} {report.unit_currency} at a time,
        stop {ex.stop_pips} and target {ex.target_pips} pips, spread {ex.spread_pips};{" "}
        {ex.min_potential_pips > 0
          ? `a signal is taken when it sees at least ${ex.min_potential_pips} pips of room.`
          : "every signal is taken that sees more room than the spread costs."}
        {left.length ? ` Left out: signals ${left.join(", ")}.` : ""} The table “Signals” at the foot says how many signals
        there were and why some were not traded.
        {report.data.run_in_from ? ` The indicators had the bars from ${report.data.run_in_from.slice(0, 10)} on to settle.` : ""}
      </p>
    </div>
  );
  return (
    <LabResults
      report={report}
      precision={info.price_precision}
      storedTimeframes={info.history.map((x) => x.timeframe)}
      heading={heading}
      afterSummary={<WhyPanel report={report} />}
      tradeColumn={column}
      allTrades
    />
  );
}
