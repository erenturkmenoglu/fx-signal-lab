import type { ReactNode } from "react";
import type { ObsStatus } from "../types";

/* State is never colour alone: every pill is a glyph (shape) + a word; only the glyph is coloured. */

const OBS: Record<ObsStatus, { glyph: string; color: string; label: string; title: string }> = {
  fresh: { glyph: "●", color: "var(--color-ok)", label: "FRESH", title: "A quote arrived during the last second" },
  carried: {
    glyph: "○",
    color: "var(--color-ink-2)",
    label: "CARRIED",
    title: "No new quote in the last second; the previous quote is carried forward and still valid",
  },
  stale: {
    glyph: "▲",
    color: "var(--color-warn)",
    label: "STALE",
    title: "The last quote is older than this pair's stale threshold; signals are suppressed",
  },
  missing: { glyph: "✕", color: "var(--color-bad)", label: "MISSING", title: "No valid quote is known" },
};

export function ObsPill({ status }: { status: ObsStatus | null }) {
  if (!status) return <span className="text-ink-3">—</span>;
  const s = OBS[status];
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap" title={s.title}>
      <span aria-hidden style={{ color: s.color }}>
        {s.glyph}
      </span>
      <span className="text-[12px] font-medium tracking-wide">{s.label}</span>
    </span>
  );
}

export type Tone = "ok" | "warn" | "bad" | "idle";

const TONE: Record<Tone, { glyph: string; color: string }> = {
  ok: { glyph: "●", color: "var(--color-ok)" },
  warn: { glyph: "▲", color: "var(--color-warn)" },
  bad: { glyph: "✕", color: "var(--color-bad)" },
  idle: { glyph: "○", color: "var(--color-ink-3)" },
};

export function StatePill({ tone, children, title }: { tone: Tone; children: ReactNode; title?: string }) {
  const t = TONE[tone];
  return (
    <span className="inline-flex items-baseline gap-1.5" title={title}>
      <span aria-hidden style={{ color: t.color }}>
        {t.glyph}
      </span>
      <span className="text-[12px] font-medium tracking-wide">{children}</span>
    </span>
  );
}

export function Panel({
  title,
  aside,
  children,
  className = "",
}: {
  title: string;
  aside?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      <header className="panel-head">
        <h2 className="panel-title">{title}</h2>
        {aside ? <div className="note flex flex-wrap items-center gap-x-3 gap-y-1">{aside}</div> : null}
      </header>
      {children}
    </section>
  );
}

/** Direction as a signed arrow: shape and sign carry the meaning, colour only supports it. */
export function Delta({ value, text }: { value: number | null | undefined; text: string }) {
  if (value == null) return <span className="text-ink-3">—</span>;
  const up = value > 0;
  const flat = value === 0;
  return (
    <span className="num whitespace-nowrap">
      <span aria-hidden style={{ color: flat ? "var(--color-ink-3)" : up ? "var(--color-up)" : "var(--color-down)" }}>
        {flat ? "■" : up ? "▲" : "▼"}
      </span>{" "}
      {text}
    </span>
  );
}
