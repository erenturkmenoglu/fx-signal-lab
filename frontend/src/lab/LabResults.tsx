import { useEffect, useMemo, useState } from "react";
import { dateTimeUTC, fixed, price } from "../format";
import { Panel, Result } from "../components/ui";
import { DayChart, EquityChart } from "./LabCharts";
import {
  type Day,
  type Group,
  REASONS,
  type Report,
  type Summary,
  type Trade,
  money,
  percent,
  ratio,
  signedMoney,
  signedPips,
  units,
} from "./model";

type Row = { label: string; help?: string; cell: (s: Summary) => React.ReactNode };

const ROWS: Row[] = [
  { label: "Trades", cell: (s) => s.trades.toLocaleString("en-US") },
  { label: "Net result", cell: (s) => <Result value={s.net_pnl} /> },
  {
    label: "in pips on one clip",
    help: "The net result divided by the value of one pip on one clip, so that runs with different clip sizes compare.",
    cell: (s) => signedPips(s.net_pips),
  },
  { label: "Winning trades", help: "Never judge a strategy by this alone.", cell: (s) => percent(s.win_rate) },
  {
    label: "Profit factor",
    help: "Money won per unit of money lost. Above 1 the strategy made money; empty when no trade lost.",
    cell: (s) => ratio(s.profit_factor),
  },
  { label: "Average per trade", cell: (s) => signedMoney(s.expectancy) },
  { label: "Average win", cell: (s) => money(s.average_win) },
  { label: "Average loss", cell: (s) => money(s.average_loss) },
  { label: "Largest loss", cell: (s) => money(s.largest_loss) },
  {
    label: "Largest fall from a high",
    help: "The furthest the running total fell below an earlier high, counted on closed trades.",
    cell: (s) => money(s.max_drawdown),
  },
  { label: "Longest losing run", cell: (s) => `${s.longest_losing_run} trades` },
  { label: "Largest position", cell: (s) => units(s.max_units) },
  { label: "Trades that added", cell: (s) => s.trades_with_adds.toLocaleString("en-US") },
  { label: "Average time in a trade", cell: (s) => (s.average_minutes == null ? "—" : `${fixed(s.average_minutes, 0)} min`) },
  { label: "Days with a trade", cell: (s) => `${s.days_traded} of ${s.days}` },
  { label: "Winning / losing days", cell: (s) => `${s.days_positive} / ${s.days_negative}` },
  { label: "Worst day", cell: (s) => signedMoney(s.worst_day) },
  { label: "Best day", cell: (s) => signedMoney(s.best_day) },
  {
    label: "Sharpe ratio",
    help: "Mean daily result over its day-to-day spread, scaled to a year. Not given for fewer than 20 days.",
    cell: (s) => ratio(s.daily_sharpe),
  },
];

function SummaryTable({ report }: { report: Report }) {
  const columns: { name: string; note: string; summary: Summary }[] = [
    { name: "Whole period", note: `${report.data.trading_days} days`, summary: report.summary },
  ];
  if (report.in_sample && report.out_of_sample) {
    columns.push({ name: "In-sample", note: `to ${report.in_sample.last_day ?? "—"}`, summary: report.in_sample.summary });
    columns.push({ name: "Out-of-sample", note: `from ${report.split_day}`, summary: report.out_of_sample.summary });
  }
  if (report.without_adding) {
    columns.push({ name: "One clip only", note: "never adding", summary: report.without_adding.summary });
  }
  // the names of the rows stay in place while the columns are scrolled sideways on a narrow screen
  const pinned = { position: "sticky", left: 0, background: "var(--color-panel)" } as const;
  return (
    <Panel title="Result" aside={<span>money in {report.currency}, after the costs assumed</span>}>
      <div className="overflow-x-auto">
        <table className="data">
          <thead>
            <tr>
              <th style={pinned} />
              {columns.map((c) => (
                <th key={c.name} className="r align-bottom">
                  <span className="text-ink">{c.name}</span>
                  <br />
                  <span className="font-normal">{c.note}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {ROWS.map((row) => (
              <tr key={row.label}>
                <td className="text-ink-2" style={pinned} title={row.help}>
                  {row.label}
                </td>
                {columns.map((c) => (
                  <td key={c.name} className="r num">
                    {row.cell(c.summary)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="note m-0 border-t border-line px-3 py-2">
        {report.in_sample
          ? "Choose parameters looking at the in-sample column only; the out-of-sample column is the check, and it stops being one each time it is used to choose. "
          : "The period is not split: every number here is in-sample. "}
        {report.without_adding
          ? "“One clip only” trades the same signals without ever adding to a position: adding raises the share of winning trades and makes the rare loss several times larger."
          : ""}
      </p>
    </Panel>
  );
}

function groupRows(groups: Record<string, Group>, name: (key: string) => string) {
  return Object.entries(groups).map(([key, g]) => (
    <tr key={key}>
      <td>{name(key)}</td>
      <td className="r num">{g.trades.toLocaleString("en-US")}</td>
      <td className="r num">{percent(g.win_rate)}</td>
      <td className="r num">
        <Result value={g.pnl} />
      </td>
    </tr>
  ));
}

function Breakdowns({ report }: { report: Report }) {
  const s = report.summary;
  const head = (first: string) => (
    <thead>
      <tr>
        <th>{first}</th>
        <th className="r">Trades</th>
        <th className="r">Winning</th>
        <th className="r">Result</th>
      </tr>
    </thead>
  );
  return (
    <div className="grid grid-cols-1 gap-2 lg:grid-cols-3">
      <Panel title="How trades ended">
        <div className="overflow-x-auto">
          <table className="data">
            {head("Closed by")}
            <tbody>{groupRows(s.by_reason, (key) => REASONS[key as keyof typeof REASONS] ?? key)}</tbody>
            {head("Direction")}
            <tbody>{groupRows(s.by_direction, (key) => (key === "long" ? "Bought (long)" : "Sold (short)"))}</tbody>
          </table>
        </div>
      </Panel>
      <Panel title="By hour of entry" aside={<span>UTC</span>}>
        <div className="max-h-[260px] overflow-auto">
          <table className="data">
            {head("Hour")}
            <tbody>{groupRows(s.by_entry_hour_utc, (key) => `${key.padStart(2, "0")}:00`)}</tbody>
          </table>
        </div>
      </Panel>
      <Panel title="Signals" aside={<span>why some were not traded</span>}>
        <div className="overflow-x-auto">
          <table className="data">
            <tbody>
              {Object.entries(report.signals).map(([name, count]) => (
                <tr key={name}>
                  <td className={name.startsWith("skipped") ? "text-ink-2" : ""}>{name}</td>
                  <td className="r num">{count.toLocaleString("en-US")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Panel>
    </div>
  );
}

function DaysTable({ days, selected, onSelect }: { days: Day[]; selected: string | null; onSelect: (day: string) => void }) {
  const newestFirst = useMemo(() => [...days].reverse(), [days]);
  return (
    <Panel title="Days" aside={<span>newest first; a trade belongs to the trading day it was closed in</span>}>
      <div className="max-h-[320px] overflow-auto">
        <table className="data">
          <thead>
            <tr>
              <th>Trading day</th>
              <th className="r">Trades</th>
              <th className="r">Won</th>
              <th className="r">Result</th>
              <th className="r">Pips</th>
              <th className="r">Total</th>
              <th className="r">Largest position</th>
            </tr>
          </thead>
          <tbody>
            {newestFirst.map((d) => (
              <tr
                key={d.day}
                onClick={() => onSelect(d.day)}
                className="cursor-pointer hover:bg-raise"
                style={d.day === selected ? { background: "var(--color-raise)" } : undefined}
                aria-selected={d.day === selected}
              >
                <td className="num">
                  <button type="button" className="num cursor-pointer border-0 bg-transparent p-0 text-ink underline-offset-2 hover:underline">
                    {d.day}
                  </button>
                </td>
                <td className="r num">{d.trades}</td>
                <td className="r num">{d.trades ? d.wins : ""}</td>
                <td className="r num">{d.trades ? <Result value={d.pnl} /> : <span className="text-ink-3">no trade</span>}</td>
                <td className="r num">{d.trades ? signedPips(d.pips) : ""}</td>
                <td className="r num">{signedMoney(d.cumulative)}</td>
                <td className="r num">{d.trades ? units(d.max_units) : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

const TRADE_LIMIT = 400;

function TradesTable({
  trades,
  total,
  precision,
  title,
  focus,
  onFocus,
}: {
  trades: Trade[];
  total: number;
  precision: number;
  title: string;
  focus: number | null;
  onFocus: (trade: Trade) => void;
}) {
  const shown = trades.slice(-TRADE_LIMIT);
  return (
    <Panel
      title={title}
      aside={
        <span>
          {trades.length > shown.length ? `the last ${shown.length} of ${trades.length}; ` : ""}
          {total.toLocaleString("en-US")} trades in the whole run; times in UTC; click a trade to see its levels
        </span>
      }
    >
      <div className="max-h-[420px] overflow-auto">
        <table className="data">
          <thead>
            <tr>
              <th className="r">#</th>
              <th>Entry</th>
              <th>Side</th>
              <th className="r">Clips</th>
              <th className="r">Average entry</th>
              <th>Exit</th>
              <th className="r">Exit price</th>
              <th>Ended by</th>
              <th className="r">Pips</th>
              <th className="r">Result</th>
              <th className="r">Held</th>
              <th className="r" title="What the signal saw, after the spread">
                Potential
              </th>
              <th className="r" title="The furthest the price went against the first entry while the position was open">
                Worst
              </th>
            </tr>
          </thead>
          <tbody>
            {shown.length === 0 ? (
              <tr>
                <td colSpan={13} className="text-ink-3">
                  No trade on this day.
                </td>
              </tr>
            ) : null}
            {shown.map((t) => (
              <tr
                key={t.n}
                onClick={() => onFocus(t)}
                className="cursor-pointer hover:bg-raise"
                style={t.n === focus ? { background: "var(--color-raise)" } : undefined}
                aria-selected={t.n === focus}
              >
                <td className="r num">
                  <button
                    type="button"
                    className="num cursor-pointer border-0 bg-transparent p-0 text-ink-3 underline-offset-2 hover:underline"
                    aria-label={`Show the levels of trade ${t.n}`}
                  >
                    {t.n}
                  </button>
                </td>
                <td className="num">{dateTimeUTC(t.entry_ms)}</td>
                <td>
                  <span aria-hidden style={{ color: t.direction === "long" ? "var(--color-up)" : "var(--color-down)" }}>
                    {t.direction === "long" ? "▲" : "▼"}
                  </span>{" "}
                  {t.direction === "long" ? "Buy" : "Sell"}
                </td>
                <td className="r num">{t.clips}</td>
                <td className="r num">{price(t.average_price, precision)}</td>
                <td className="num">{dateTimeUTC(t.exit_ms).slice(11)}</td>
                <td className="r num">{price(t.exit_price, precision)}</td>
                <td>{REASONS[t.reason]}</td>
                <td className="r num">{signedPips(t.pips)}</td>
                <td className="r num">
                  <Result value={t.pnl} />
                </td>
                <td className="r num">{fixed(t.minutes, 0)} min</td>
                <td className="r num">{fixed(t.potential_pips, 1)}</td>
                <td className="r num">{fixed(t.adverse_pips, 1)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}

export function LabResults({
  report,
  precision,
  storedTimeframes,
}: {
  report: Report;
  precision: number;
  storedTimeframes: string[];
}) {
  const lastTraded = useMemo(() => [...report.daily].reverse().find((d) => d.trades > 0)?.day ?? null, [report.daily]);
  const [day, setDay] = useState<string | null>(lastTraded);
  const [focus, setFocus] = useState<number | null>(null);
  const [all, setAll] = useState(false);
  useEffect(() => {
    setDay(lastTraded);
    setFocus(null);
  }, [lastTraded, report]);

  const selected = report.daily.find((d) => d.day === day) ?? null;
  const dayTrades = useMemo(() => report.trades.filter((t) => t.day === day), [report.trades, day]);
  const focused = dayTrades.find((t) => t.n === focus) ?? null;
  const req = report.request;

  const selectDay = (next: string) => {
    setDay(next);
    setFocus(null);
  };
  const focusTrade = (trade: Trade) => {
    setDay(trade.day);
    setFocus(trade.n === focus ? null : trade.n);
  };

  return (
    <>
      <div className="panel px-3 py-2">
        <p className="m-0 text-[14px]">
          <strong className="font-semibold">{req.strategy_title}</strong> on {req.symbol}: signals from {req.timeframe} bars,
          trades simulated on {report.data.base_bars.toLocaleString("en-US")} {req.base_timeframe} {report.data.price_basis}{" "}
          bars from {report.data.first_day} to {report.data.last_day} ({report.data.trading_days} trading days).
          {report.data.run_in_from ? ` The indicators had the bars from ${report.data.run_in_from.slice(0, 10)} on to settle.` : ""}
        </p>
        <p className="note m-0 mt-1">
          Clips of {units(req.execution.clip_units)} {report.unit_currency}, at most {req.execution.max_clips}; target{" "}
          {req.execution.target_pips} and stop {req.execution.stop_pips} pips; spread {req.execution.spread_pips}; a signal needs{" "}
          {req.execution.min_potential_pips} pips of potential. Parameters:{" "}
          {Object.entries(req.params)
            .map(([name, value]) => `${name} ${value}`)
            .join(", ") || "none"}
          .
        </p>
      </div>

      {report.warnings.length ? (
        <div role="alert" className="panel px-4 py-3" style={{ borderLeft: "3px solid var(--color-amber)" }}>
          <h2 className="m-0 text-[14px] font-semibold">Read this result with care</h2>
          <ul className="m-0 mt-1 list-disc pl-5 text-[13px] text-ink-2">
            {report.warnings.map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
        </div>
      ) : null}

      <SummaryTable report={report} />
      <EquityChart report={report} selected={day} onSelect={selectDay} />

      <div className="grid grid-cols-1 gap-2 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        {selected ? (
          <DayChart
            symbol={req.symbol}
            precision={precision}
            report={report}
            day={selected}
            trades={dayTrades}
            focus={focused}
            storedTimeframes={storedTimeframes}
          />
        ) : (
          <Panel title="Price chart">
            <p className="note m-0 px-3 py-3">Choose a day to see its bars and trades.</p>
          </Panel>
        )}
        <DaysTable days={report.daily} selected={day} onSelect={selectDay} />
      </div>

      <div className="flex items-center gap-1" role="group" aria-label="Which trades to list">
        <button type="button" className="btn" aria-pressed={!all} onClick={() => setAll(false)}>
          Trades of {day ?? "the day"}
        </button>
        <button type="button" className="btn" aria-pressed={all} onClick={() => setAll(true)}>
          All trades
        </button>
      </div>
      <TradesTable
        trades={all ? report.trades : dayTrades}
        total={report.trades.length}
        precision={precision}
        title={all ? "All trades" : `Trades of ${day ?? "the day"}`}
        focus={focus}
        onFocus={focusTrade}
      />

      <Breakdowns report={report} />

      <p className="note m-0 px-1">
        A backtest shows what a rule would have done on past prices under the costs assumed. It is not a forecast, and no
        strategy here is known to be profitable in the future. Inside one bar the order of events is unknown; the worse order
        is always assumed.
      </p>
    </>
  );
}
