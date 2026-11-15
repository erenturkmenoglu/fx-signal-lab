import { useRef } from "react";
import { dateTimeUTC, duration, fixed, signed, splitPrice } from "../format";
import type { InstrumentStatus } from "../types";
import { Delta, ObsPill, StatePill } from "./ui";

/** Remembers whether a value last moved up or down. */
function useDirection(value: number | null): "up" | "down" | null {
  const memory = useRef<{ last: number | null; direction: "up" | "down" | null }>({ last: null, direction: null });
  const m = memory.current;
  if (value != null && m.last != null && value !== m.last) m.direction = value > m.last ? "up" : "down";
  if (value != null) m.last = value;
  return m.direction;
}

function Side({ label, value, inst }: { label: string; value: number | null; inst: InstrumentStatus }) {
  const direction = useDirection(value);
  const parts = value != null ? splitPrice(value, inst.price_precision, inst.pip_size) : null;
  return (
    <div className="min-w-0">
      <div className="note flex items-center gap-2">
        {label}
        <span
          aria-hidden
          className="text-[11px]"
          style={{
            color: direction === "up" ? "var(--color-up)" : "var(--color-down)",
            visibility: direction ? "visible" : "hidden",
          }}
        >
          {direction === "down" ? "▼" : "▲"}
        </span>
      </div>
      {parts ? (
        // the dealing-screen convention: small big figure, large pips, small fractional pip
        <div className="num flex items-baseline leading-none" aria-label={`${label} ${value}`}>
          <span className="text-[22px] text-ink-2">{parts.big}</span>
          <span className="text-[46px] font-medium tracking-tight">{parts.pips}</span>
          <span className="self-start pt-1 text-[20px] text-ink-2">{parts.frac}</span>
        </div>
      ) : (
        <div className="num text-[46px] leading-none text-ink-3">—</div>
      )}
    </div>
  );
}

export function QuoteBoard({ inst, now }: { inst: InstrumentStatus; now: number }) {
  const gate = inst.signal_gate;
  const hasQuote = inst.bid != null;
  return (
    <section className="panel" aria-label={`${inst.symbol} quote`}>
      <header className="panel-head">
        <h2 className="m-0 flex items-baseline gap-2 text-[15px] font-semibold">
          <span className="num">
            {inst.symbol.slice(0, 3)}/{inst.symbol.slice(3)}
          </span>
          <span className="note font-normal">{inst.description}</span>
        </h2>
        <div className="flex items-center gap-4">
          <ObsPill status={inst.market_open ? inst.status : null} />
          <span className="note" title="Age of the quote in force, from its provider timestamp">
            quote age <span className="num text-ink">{duration(inst.quote_age_ms)}</span>
          </span>
        </div>
      </header>

      <div className="grid grid-cols-2 gap-x-6 gap-y-3 px-3 py-3 sm:grid-cols-[auto_auto_1fr]">
        <Side label="Bid" value={inst.bid} inst={inst} />
        <Side label="Ask" value={inst.ask} inst={inst} />
        <dl className="col-span-2 m-0 grid grid-cols-[max-content_1fr] content-center gap-x-4 gap-y-0.5 text-[13px] sm:col-span-1">
          <dt className="text-ink-3">Spread</dt>
          <dd className="num m-0">{inst.spread_pips != null ? `${fixed(inst.spread_pips, 1)} pips` : "—"}</dd>
          <dt className="text-ink-3" title="Against the last quote before the 17:00 New York rollover">
            Change on day
          </dt>
          <dd className="m-0">
            {inst.change ? (
              <Delta
                value={inst.change.pips}
                text={`${signed(inst.change.pips, 1)} pips  ${signed(inst.change.pct, 2)}%`}
              />
            ) : (
              <span className="note">no reference yet (needs data from before the rollover)</span>
            )}
          </dd>
          <dt className="text-ink-3">Quotes</dt>
          <dd className="num m-0">
            {fixed(inst.quotes_per_s_10s, 1)}/s <span className="note">10 s avg</span>
          </dd>
        </dl>
      </div>

      <footer className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line px-3 py-1.5 text-[13px]">
        {!inst.supported ? (
          <StatePill tone="bad">Not available from the provider. No data is shown for this pair.</StatePill>
        ) : !inst.market_open ? (
          <StatePill tone="idle">
            Market closed
            {inst.next_open_ms
              ? `. Opens ${dateTimeUTC(inst.next_open_ms)} UTC, in ${duration(inst.next_open_ms - now)}`
              : ""}
          </StatePill>
        ) : gate.allowed ? (
          <StatePill tone="ok" title="Feed connected, quote fresh, clocks consistent">
            Data quality OK
          </StatePill>
        ) : (
          <StatePill tone={hasQuote ? "warn" : "bad"} title="No signal may be generated from this data">
            Signals suppressed: {gate.reason.toLowerCase()}
          </StatePill>
        )}
        <span className="note">
          stale after <span className="num">{duration(inst.stale_after_ms)}</span>
        </span>
      </footer>
    </section>
  );
}
