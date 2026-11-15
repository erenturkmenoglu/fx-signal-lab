// Setups, the owner's own rules: shapes of the backend payloads (backend/fxlab/research/setups.py
// and context.py) and the pure functions between the form, the request and the tables.  No React,
// no network.

import { type Kind, PRESETS, type Preset, kindOf } from "../board/model";
import type { LabInfo, Part, Report, StrategyInfo, Trade } from "../lab/model";

export const MAX_METHODS = 12;
export type Mode = "any" | "all";

export interface Condition {
  key: string;
  label: string;
  /** entry: known when a trade is opened, so trades can be left out by it.  after: seen only while it was open */
  when: "entry" | "after";
}

/** What is kept of a setup: its rule.  The instrument and the period belong to a run. */
export interface Definition {
  name: string;
  methods: string[]; // "rsi_reversion@5m": a strategy and the bars it reads
  mode: Mode; // any: one signal is enough.  all: every method has to agree
  within_minutes: number; // for "all": how old the signals of the others may be
  skip: string[]; // conditions known at entry under which a signal is not taken
  execution: Record<string, number>; // what differs from the instrument's rules: target, stop, room needed
}

export interface Saved extends Definition {
  id: number;
  updated_ms: number;
}

export interface SetupBody extends Definition {
  symbol: string;
  start: string;
  end: string;
  base_timeframe: string;
}

/** The form: what was typed. */
export interface Draft {
  name: string;
  methods: string[];
  mode: Mode;
  within: string; // minutes
  stop: string; // pips
  target: string; // pips
  room: string; // pips a signal has to see
  skip: string[];
  from: string; // YYYY-MM-DD, first day (UTC)
  to: string; // last day, included
  preset: Preset | null; // a period that moves along with the stored history
}

// --- what a run gives back ---------------------------------------------------------------------

/** What was going on around a trade. */
export interface Around {
  votes: number; // momentum methods pointing up minus those pointing down when it was opened
  momentum: -1 | 0 | 1; // +1: momentum was with the trade, -1: against it, 0: not decided
  against_momentum: boolean;
  against_channel: boolean;
  outside_channel: boolean;
  momentum_turned: boolean;
  left_channel: boolean;
}

export type SetupTrade = Trade & Around;

export interface WhyGroup {
  trades: number;
  wins: number;
  losses: number;
  win_rate: number | null;
  net: number;
}

export interface WhyCondition extends Condition {
  with: WhyGroup; // the trades the condition holds for
  without: WhyGroup; // ... and the others
  failed_with: number;
  /** how much more often the trades it holds for worked (a share: 0.05 = five points); null when a group is empty */
  difference: number | null;
  /** how large such a difference gets by chance between two groups of these sizes */
  noise: number | null;
}

export interface Why {
  trades: number;
  failed: number;
  failed_unexplained: number; // failed trades none of the conditions holds for
  conditions: WhyCondition[];
}

export interface SetupReport extends Report {
  request: Report["request"] & {
    setup: { name: string; methods: string[]; mode: Mode; within_minutes: number; skip: string[] };
  };
  trades: SetupTrade[];
  why: Why;
  in_sample?: Part & { why: Why };
  out_of_sample?: Part & { why: Why };
  context: {
    grid: string;
    voters: number;
    threshold: number;
    fresh_bars: number;
    channel_hours: number;
    channel_width: number;
    conditions: Condition[];
  };
}

// --- the methods a setup can name ---------------------------------------------------------------

export const methodId = (strategy: string, timeframe: string) => `${strategy}@${timeframe}`;

export interface Group {
  kind: Kind;
  title: string;
  note: string;
  strategies: StrategyInfo[];
}

const GROUPS: { kind: Kind; title: string; note: string }[] = [
  { kind: "reversion", title: "Mean reversion", note: "trade against a stretched move: bands, oscillators, levels" },
  { kind: "momentum", title: "Momentum", note: "trade with a move: trend followers and breakouts" },
  { kind: "other", title: "Neither", note: "the clock, and random entries to compare against" },
];

/** The strategies by the kind of signal they give, mean reversion first; kinds without a strategy are left out. */
export function groups(strategies: StrategyInfo[]): Group[] {
  return GROUPS.map((group) => ({ ...group, strategies: strategies.filter((s) => kindOf(s) === group.kind) })).filter(
    (group) => group.strategies.length > 0,
  );
}

/** How a method is called in a sentence: "RSI 5m".  A method the server no longer knows keeps its name. */
export function labeller(strategies: StrategyInfo[]): (method: string) => string {
  const short = new Map(strategies.map((s) => [s.key, s.short ?? s.title]));
  return (method) => {
    const [key = "", timeframe = ""] = method.split("@");
    return `${short.get(key) ?? key} ${timeframe}`.trim();
  };
}

/** Tick or untick a method.  A setup names at most ``MAX_METHODS``: one more is not taken. */
export function toggleMethod(methods: string[], id: string): string[] {
  if (methods.includes(id)) return methods.filter((m) => m !== id);
  return methods.length >= MAX_METHODS ? methods : [...methods, id];
}

// --- the form ----------------------------------------------------------------------------------

const DAY_MS = 86_400_000;
const isDay = (value: unknown): value is string => typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value);
const text = (value: unknown): value is string => typeof value === "string";
const number = (value: number) => String(Number(value.toFixed(4)));

/**
 * What the form shows before anything is typed: the owner's first rule.  "Whenever RSI or
 * Bollinger gives a signal, stop five pips against, target five pips": every signal is taken,
 * so the room a signal must see starts at zero; stop and target are the instrument's own.
 */
export function defaultDraft(info: LabInfo): Draft {
  const known = new Set(info.strategies.map((s) => s.key));
  const timeframe = info.timeframes.includes("5m") ? "5m" : (info.timeframes[0] ?? "5m");
  const methods = ["rsi_reversion", "bollinger_reversion"].filter((key) => known.has(key)).map((key) => methodId(key, timeframe));
  return {
    name: methods.length === 2 ? "RSI or Bollinger" : "My setup",
    methods,
    mode: "any",
    within: "30",
    stop: number(info.execution.stop_pips),
    target: number(info.execution.target_pips),
    room: "0",
    skip: [],
    from: "",
    to: "",
    preset: "month",
  };
}

const entryConditions = (info: LabInfo) => (info.conditions ?? []).filter((c) => c.when === "entry").map((c) => c.key);

function knownMethods(info: LabInfo, methods: unknown): string[] {
  if (!Array.isArray(methods)) return [];
  const strategies = new Set(info.strategies.map((s) => s.key));
  const out: string[] = [];
  for (const method of methods) {
    if (!text(method) || out.includes(method)) continue;
    const [key = "", timeframe = ""] = method.split("@");
    if (strategies.has(key) && info.timeframes.includes(timeframe)) out.push(method);
  }
  return out.slice(0, MAX_METHODS);
}

/** A stored form laid over today's defaults: methods and conditions the server no longer knows are dropped. */
export function restoreDraft(info: LabInfo, saved: unknown): Draft {
  const draft = defaultDraft(info);
  if (!saved || typeof saved !== "object") return draft;
  const old = saved as Partial<Record<keyof Draft, unknown>>;
  if (text(old.name)) draft.name = old.name.slice(0, 80);
  if (Array.isArray(old.methods)) draft.methods = knownMethods(info, old.methods);
  if (old.mode === "any" || old.mode === "all") draft.mode = old.mode;
  for (const field of ["within", "stop", "target", "room"] as const) {
    const value = old[field];
    if (text(value)) draft[field] = value;
  }
  if (Array.isArray(old.skip)) {
    const allowed = entryConditions(info);
    draft.skip = old.skip.filter((key): key is string => text(key) && allowed.includes(key));
  }
  if (isDay(old.from)) draft.from = old.from;
  if (isDay(old.to)) draft.to = old.to;
  draft.preset = old.preset === null ? null : (PRESETS.find((p) => p.key === old.preset)?.key ?? draft.preset);
  return draft;
}

/** A saved setup put into the form; the period chosen stays. */
export function draftOf(info: LabInfo, saved: Definition, current: Draft): Draft {
  const execution = saved.execution ?? {};
  const typed = (name: string, fallback: number) => number(Number.isFinite(Number(execution[name])) ? Number(execution[name]) : fallback);
  const allowed = entryConditions(info);
  return {
    ...current,
    name: saved.name,
    methods: knownMethods(info, saved.methods),
    mode: saved.mode === "all" ? "all" : "any",
    within: number(saved.within_minutes > 0 ? saved.within_minutes : 30),
    stop: typed("stop_pips", info.execution.stop_pips),
    target: typed("target_pips", info.execution.target_pips),
    room: typed("min_potential_pips", info.execution.min_potential_pips),
    skip: (saved.skip ?? []).filter((key) => allowed.includes(key)),
  };
}

/** The rule a form describes, or the reason it is not one yet.  The server checks every value again. */
export function definitionOf(draft: Draft): { definition: Definition } | { error: string } {
  const name = draft.name.trim();
  if (!name) return { error: "Give the setup a name." };
  if (draft.methods.length === 0) return { error: "Tick at least one method." };
  const amount = (typed: string, label: string, least: number): number | string => {
    const value = Number(typed);
    if (typed.trim() === "" || !Number.isFinite(value)) return `Fill in “${label}”.`;
    if (least > 0 ? value <= 0 : value < 0) return `“${label}” must be ${least > 0 ? "more than zero" : "zero or more"}.`;
    return value;
  };
  const stop = amount(draft.stop, "Stop", 1);
  if (typeof stop === "string") return { error: stop };
  const target = amount(draft.target, "Target", 1);
  if (typeof target === "string") return { error: target };
  const room = amount(draft.room, "Room a signal must see", 0);
  if (typeof room === "string") return { error: room };
  let within = 30;
  if (draft.mode === "all") {
    const typed = amount(draft.within, "within … minutes", 1);
    if (typeof typed === "string") return { error: typed };
    within = typed;
  }
  return {
    definition: {
      name: name.slice(0, 80),
      methods: draft.methods,
      mode: draft.mode,
      within_minutes: within,
      skip: draft.skip,
      execution: { stop_pips: stop, target_pips: target, min_potential_pips: room },
    },
  };
}

/** The request for a run of the form over its period (both days included), or the reason it cannot be sent. */
export function runBody(symbol: string, base: string, draft: Draft): { body: SetupBody } | { error: string } {
  const built = definitionOf(draft);
  if ("error" in built) return built;
  const start = Date.parse(`${draft.from}T00:00:00Z`);
  const end = Date.parse(`${draft.to}T00:00:00Z`);
  if (Number.isNaN(start) || Number.isNaN(end)) return { error: "Enter the first and the last day of the period." };
  if (end < start) return { error: "The last day is before the first day." };
  return {
    body: {
      symbol,
      start: `${draft.from}T00:00:00Z`,
      end: new Date(end + DAY_MS).toISOString(),
      base_timeframe: base,
      ...built.definition,
    },
  };
}

/** Whether the form still says what a saved setup says (the period aside). */
export function sameRule(draft: Draft, saved: Definition): boolean {
  const built = definitionOf(draft);
  if ("error" in built) return false;
  const d = built.definition;
  const numbers = (execution: Record<string, number>) =>
    ["stop_pips", "target_pips", "min_potential_pips"].map((name) => Number(execution[name])).join("/");
  return (
    d.name === saved.name &&
    d.mode === saved.mode &&
    (d.mode === "any" || d.within_minutes === saved.within_minutes) &&
    [...d.methods].sort().join() === [...saved.methods].sort().join() &&
    [...d.skip].sort().join() === [...(saved.skip ?? [])].sort().join() &&
    numbers(d.execution) === numbers(saved.execution ?? {})
  );
}

// --- the result in words ------------------------------------------------------------------------

/** When a setup opens a trade: "any of RSI 5m, BB 5m signals". */
export function ruleText(setup: { methods: string[]; mode: Mode; within_minutes: number }, label: (method: string) => string): string {
  const names = setup.methods.map(label);
  if (names.length === 1) return `${names[0]} signals`;
  if (setup.mode === "all") {
    return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]} all point the same way within ${number(setup.within_minutes)} minutes`;
  }
  return `any of ${names.join(", ")} signals`;
}

export interface Headline {
  trades: number;
  worked: number;
  failed: number;
  flat: number; // closed at exactly zero
  net: number;
  averageWin: number | null;
  averageLoss: number | null;
  days: number;
  daysUp: number;
  daysDown: number;
}

/** What was asked for first: how many trades, how many worked, what they made. */
export function headlineOf(report: Report): Headline {
  const s = report.summary;
  return {
    trades: s.trades,
    worked: s.wins,
    failed: s.losses,
    flat: s.trades - s.wins - s.losses,
    net: s.net_pnl,
    averageWin: s.average_win,
    averageLoss: s.average_loss,
    days: s.days,
    daysUp: s.days_positive,
    daysDown: s.days_negative,
  };
}

/** A few words for each condition, for a cell of a table; the server's own label where there are none. */
const SHORT: Record<string, string> = {
  against_momentum: "against the momentum",
  against_channel: "against the channel's slope",
  outside_channel: "outside the channel",
  momentum_turned: "momentum turned",
  left_channel: "price left the channel",
};

export const shortLabel = (condition: { key: string; label: string }) => SHORT[condition.key] ?? condition.label.toLowerCase();

/** The same with a capital letter, for the head of a line. */
export function lineLabel(condition: { key: string; label: string }): string {
  const text = shortLabel(condition);
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** What was going on around a trade, in a few words: the momentum it was opened on, then what else held. */
export function aroundText(trade: SetupTrade, conditions: Condition[]): string {
  const reading = trade.votes > 0 ? `+${trade.votes}` : trade.votes < 0 ? `−${Math.abs(trade.votes)}` : "0";
  const momentum = trade.momentum > 0 ? "with the momentum" : trade.momentum < 0 ? "against the momentum" : "momentum not decided";
  const rest = conditions
    .filter((c) => c.key !== "against_momentum" && (trade as unknown as Record<string, unknown>)[c.key] === true)
    .map(shortLabel);
  return [`${momentum} (reading ${reading})`, ...rest].join(", ");
}

/**
 * What a line of the "why" table amounts to.  none: the condition never held.  all: it held for
 * every trade, so there is nothing to compare with.  chance: the two groups differ by no more than
 * luck gives.  worse / better: trades it held for worked clearly less / more often.
 */
export type Verdict = "none" | "all" | "chance" | "worse" | "better";

export function verdictOf(condition: WhyCondition): Verdict {
  if (condition.with.trades === 0) return "none";
  if (condition.without.trades === 0) return "all";
  const difference = condition.difference ?? 0;
  const noise = condition.noise ?? 0;
  if (Math.abs(difference) <= noise) return "chance";
  return difference < 0 ? "worse" : "better";
}

export const VERDICT_TEXT: Record<Verdict, string> = {
  none: "never held",
  all: "held for every trade: nothing to compare with",
  chance: "no more than chance gives",
  worse: "worked clearly less often with it",
  better: "worked clearly more often with it",
};

/** A share as percentage points with a sign: "−13.9", "+4.2", "0.0". */
export function points(share: number | null | undefined): string {
  if (share == null) return "—";
  const value = Math.abs(share * 100).toFixed(1);
  if (Number(value) === 0) return value;
  return share > 0 ? `+${value}` : `−${value}`;
}
