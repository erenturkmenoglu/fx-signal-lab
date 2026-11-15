import { duration, fixed, price, signed } from "../format";
import type { InstrumentStatus } from "../types";
import { Delta, ObsPill, Panel, StatePill } from "./ui";

export function MarketMonitor({
  instruments,
  selected,
  onSelect,
}: {
  instruments: InstrumentStatus[];
  selected: string | null;
  onSelect: (symbol: string) => void;
}) {
  return (
    <Panel title="Market monitor" aside={<span>{instruments.length} active pair{instruments.length === 1 ? "" : "s"}</span>}>
      <div className="overflow-x-auto">
        <table className="data">
          <thead>
            <tr>
              <th>Pair</th>
              <th className="r">Bid</th>
              <th className="r">Ask</th>
              <th className="r">Spread</th>
              <th className="r">Change on day</th>
              <th className="r">Quote age</th>
              <th>Last second</th>
              <th className="r">Quotes/s</th>
              <th>Signals</th>
            </tr>
          </thead>
          <tbody>
            {instruments.map((inst) => {
              const active = inst.symbol === selected;
              return (
                <tr
                  key={inst.symbol}
                  onClick={() => onSelect(inst.symbol)}
                  className="cursor-pointer hover:bg-raise"
                  style={active ? { background: "var(--color-raise)", boxShadow: "inset 2px 0 0 var(--color-amber)" } : undefined}
                >
                  <td>
                    <button
                      type="button"
                      className="num cursor-pointer border-0 bg-transparent p-0 font-medium text-ink"
                      aria-pressed={active}
                      onClick={() => onSelect(inst.symbol)}
                    >
                      {inst.symbol.slice(0, 3)}/{inst.symbol.slice(3)}
                    </button>
                  </td>
                  <td className="r num">{price(inst.bid, inst.price_precision)}</td>
                  <td className="r num">{price(inst.ask, inst.price_precision)}</td>
                  <td className="r num">{inst.spread_pips != null ? fixed(inst.spread_pips, 1) : "—"}</td>
                  <td className="r">
                    <Delta value={inst.change?.pips} text={inst.change ? `${signed(inst.change.pips, 1)} pips` : ""} />
                  </td>
                  <td className="r num">{duration(inst.quote_age_ms)}</td>
                  <td>
                    {inst.market_open ? <ObsPill status={inst.status} /> : <StatePill tone="idle">Market closed</StatePill>}
                  </td>
                  <td className="r num">{fixed(inst.quotes_per_s_10s, 1)}</td>
                  <td>
                    {inst.signal_gate.allowed ? (
                      <StatePill tone="ok">Data OK</StatePill>
                    ) : (
                      <StatePill tone={inst.market_open ? "warn" : "idle"}>{inst.signal_gate.reason.toLowerCase()}</StatePill>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </Panel>
  );
}
