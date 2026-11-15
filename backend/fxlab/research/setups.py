"""A setup: the owner's own rule for when to trade, put together from the methods that exist.

A setup names methods (a strategy reading bars of one length, with its default parameters),
says whether a signal of any one of them is enough or all of them have to agree, and brings
its own target and stop.  It is traded and reported exactly like a single strategy, by the
same engine, and every trade is then described by what the market was doing around it
(``fxlab.research.context``), so that the trades that failed can be asked why.

A setup may also leave signals out under conditions that are known when a trade would be
opened (against the momentum, against the slope of the trend channel, outside the channel).
That is how a finding from the "why" table is tried out: tick the condition, run again, and
look at the out-of-sample column.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np

from fxlab.domain.instruments import Instrument
from fxlab.domain.timeframes import TIMEFRAMES, Timeframe, get_timeframe
from fxlab.research import context as ctx
from fxlab.research.bars import Bars, resample
from fxlab.research.engine import Execution, Signals
from fxlab.research.report import BacktestError, report_of, resolve_execution, trading_bars
from fxlab.research.strategies import STRATEGIES, Strategy
from fxlab.timeutil import MINUTE_MS, iso, parse_time

#: the most methods one setup may name
MAX_METHODS = 12
MODES = ("any", "all")


@dataclass(frozen=True)
class SetupRequest:
    symbol: str
    start_ms: int
    end_ms: int
    name: str = "Setup"
    methods: tuple[str, ...] = ()  # "rsi_reversion@5m": a strategy and the bars it reads
    mode: str = "any"  # any: a signal of one method is enough.  all: they have to agree
    within_minutes: float = 30.0  # for "all": how old the signals of the others may be
    skip: tuple[str, ...] = ()  # conditions known at entry under which a signal is not taken
    base_timeframe: str = "1m"
    execution: dict[str, Any] = field(default_factory=dict)
    split: str = "auto"

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> SetupRequest:
        known = {
            "symbol",
            "start",
            "end",
            "name",
            "methods",
            "mode",
            "within_minutes",
            "skip",
            "base_timeframe",
            "execution",
            "split",
        }
        unknown = sorted(set(data) - known)
        if unknown:
            raise BacktestError(f"unknown field {unknown[0]!r}; known: {', '.join(sorted(known))}")
        for name in ("symbol", "start", "end"):
            if data.get(name) in (None, ""):
                raise BacktestError(f"{name} is required")
        for name in ("methods", "skip"):
            value = data.get(name) or []
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise BacktestError(f"{name} must be a list of names")
        if not isinstance(data.get("execution") or {}, dict):
            raise BacktestError("execution must be an object of names and values")
        try:
            start_ms, end_ms = parse_time(data["start"]), parse_time(data["end"])
        except (ValueError, TypeError):
            raise BacktestError("start and end must be dates or times (ISO-8601)") from None
        try:
            within = float(data.get("within_minutes", 30.0))
        except (ValueError, TypeError):
            raise BacktestError("within_minutes must be a number") from None
        return cls(
            symbol=str(data["symbol"]).upper(),
            start_ms=start_ms,
            end_ms=end_ms,
            name=str(data.get("name") or "Setup").strip()[:80] or "Setup",
            methods=tuple(data.get("methods") or ()),
            mode=str(data.get("mode") or "any"),
            within_minutes=within,
            skip=tuple(data.get("skip") or ()),
            base_timeframe=str(data.get("base_timeframe") or "1m"),
            execution=dict(data.get("execution") or {}),
            split=str(data.get("split") or "auto"),
        )


@dataclass(frozen=True)
class SetupPlan:
    base_tf: Timeframe
    members: tuple[tuple[Strategy, Timeframe], ...]
    execution: Execution
    voters: tuple[tuple[Strategy, Timeframe], ...]  # the momentum methods the context listens to


def _bars_of(code: str, base: Timeframe, what: str) -> Timeframe:
    try:
        tf = get_timeframe(code)
    except ValueError as exc:
        raise BacktestError(str(exc)) from None
    if tf.anchor != "utc" or tf.seconds % base.seconds != 0:
        raise BacktestError(f"{what}: {tf.code} bars cannot be built from {base.code} bars")
    return tf


def momentum_voters(base_tf: Timeframe) -> tuple[tuple[Strategy, Timeframe], ...]:
    """Every momentum method on every bar length from the base (a minute at least) up to an hour."""
    lengths = [
        tf
        for tf in TIMEFRAMES.values()
        if tf.anchor == "utc" and tf.seconds >= max(60, base_tf.seconds) and tf.seconds % base_tf.seconds == 0
    ]
    return tuple((s, tf) for tf in lengths for s in STRATEGIES.values() if s.style == "momentum")


def prepare_setup(inst: Instrument, request: SetupRequest) -> SetupPlan:
    """Check a request.  Cheap: no data is touched."""
    try:
        base_tf = get_timeframe(request.base_timeframe)
    except ValueError as exc:
        raise BacktestError(str(exc)) from None
    if base_tf.anchor != "utc":
        raise BacktestError(f"trades cannot be simulated on {base_tf.code} bars; use bars of one hour or less")
    if not request.methods:
        raise BacktestError("a setup needs at least one method")
    if len(request.methods) > MAX_METHODS:
        raise BacktestError(f"a setup names at most {MAX_METHODS} methods")
    if len(set(request.methods)) != len(request.methods):
        raise BacktestError("a setup names each method once")
    members = []
    for name in request.methods:
        key, _, code = name.partition("@")
        if key not in STRATEGIES or not code:
            raise BacktestError(f"unknown method {name!r}; a method is a strategy and its bars, like rsi_reversion@5m")
        members.append((STRATEGIES[key], _bars_of(code, base_tf, name)))
    if request.mode not in MODES:
        raise BacktestError(f"mode must be one of: {', '.join(MODES)}")
    if not request.within_minutes > 0:
        raise BacktestError("within_minutes must be positive")
    unknown = [key for key in request.skip if key not in ctx.ENTRY_CONDITIONS]
    if unknown:
        raise BacktestError(
            f"unknown condition {unknown[0]!r}; signals can be left out when: {', '.join(ctx.ENTRY_CONDITIONS)}"
        )
    if request.split not in ("auto", "none"):
        try:
            date.fromisoformat(request.split)
        except ValueError:
            raise BacktestError(f"split must be 'auto', 'none' or a date (YYYY-MM-DD), not {request.split!r}") from None
    return SetupPlan(base_tf, tuple(members), resolve_execution(inst, request.execution), momentum_voters(base_tf))


def combine(base: Bars, members: Sequence[tuple[Bars, Signals]], mode: str, within_ms: int) -> Signals:
    """The signals of a setup, one entry per bar of ``base``.

    A member's signal is known when its bar has closed; it is put on the base bar that closes
    at that moment (or, across a hole in the data, on the first one that closes after it), so
    the engine acts on it exactly when it would act on the member alone.

    ``any``: every signal counts; a buy and a sell at the same moment cancel.  ``all``: a signal
    is given when a member signals and each of the others has signalled the same way no longer
    than ``within_ms`` ago.  The potential is the furthest any member sees.
    """
    n = len(base)
    closes = base.ts + base.span_ms
    position = np.arange(n)
    longs = np.zeros(n, dtype=np.int16)  # members signalling (any) or standing (all) on each side
    shorts = np.zeros(n, dtype=np.int16)
    event = np.zeros(n, dtype=bool)  # a member signals here
    reach_long = np.full(n, np.nan)
    reach_short = np.full(n, np.nan)
    for bars, signals in members:
        own = np.flatnonzero(signals.direction)
        known = bars.ts[own] + bars.span_ms
        at = np.searchsorted(closes, known, side="left")
        inside = at < n
        own, known, at = own[inside], known[inside], at[inside]
        direction = np.zeros(n, dtype=np.int8)
        reach = np.full(n, np.nan)
        moment = np.zeros(n, dtype=np.int64)
        direction[at] = signals.direction[own]  # across a hole two signals can meet: the later one stands
        reach[at] = signals.potential_pips[own]
        moment[at] = known
        event |= direction != 0
        if mode == "all":  # what the member last said, while that is recent enough
            last = np.maximum.accumulate(np.where(direction != 0, position, -1))
            said = last >= 0
            last = np.maximum(last, 0)
            recent = said & (closes - moment[last] <= within_ms)
            direction = np.where(recent, direction[last], 0).astype(np.int8)
            reach = np.where(recent, reach[last], np.nan)
        longs += direction > 0
        shorts += direction < 0
        reach_long = np.fmax(reach_long, np.where(direction > 0, reach, np.nan))
        reach_short = np.fmax(reach_short, np.where(direction < 0, reach, np.nan))
    if mode == "all":
        buy = event & (longs == len(members))
        sell = event & (shorts == len(members))
    else:
        buy, sell = longs > shorts, shorts > longs
    out = np.zeros(n, dtype=np.int8)
    out[buy] = 1
    out[sell] = -1
    potential = np.full(n, np.nan)
    potential[buy] = reach_long[buy]
    potential[sell] = reach_short[sell]
    return Signals(out, potential)


def build_setup(base: Bars, inst: Instrument, request: SetupRequest) -> dict[str, Any]:
    """Run a setup on ``base``: the provider's bars of the period, with a run-in before it if there is one."""
    plan = prepare_setup(inst, request)
    if plan.base_tf.seconds != base.seconds:
        raise BacktestError("the bars given are not of the base timeframe")
    ex = plan.execution
    warnings: list[str] = []
    base, shown = trading_bars(base, inst, request.start_ms, warnings)

    resampled: dict[int, Bars] = {}

    def signals_of(strategy: Strategy, tf: Timeframe) -> tuple[Bars, Signals]:
        if tf.seconds not in resampled:
            resampled[tf.seconds] = resample(base, tf.seconds)
        bars = resampled[tf.seconds]
        return bars, strategy.compute(bars, strategy.resolve(), ex.pip_size)

    cache: dict[tuple[str, str], tuple[Bars, Signals]] = {}
    for strategy, tf in (*plan.members, *plan.voters):
        if (strategy.key, tf.code) not in cache:
            cache[(strategy.key, tf.code)] = signals_of(strategy, tf)
    signals = combine(
        base,
        [cache[(s.key, tf.code)] for s, tf in plan.members],
        request.mode,
        int(request.within_minutes * MINUTE_MS),
    )
    context = ctx.build_context(base, [cache[(s.key, tf.code)] for s, tf in plan.voters])

    # signals left out because of what was known when they came
    closes = base.ts + base.span_ms
    left_out: dict[str, int] = {}
    if request.skip:
        held = ctx.entry_conditions(context, closes, signals.direction)
        direction = signals.direction.copy()
        potential = signals.potential_pips.copy()
        in_period = closes >= request.start_ms
        for key in request.skip:
            drop = held[key] & (direction != 0)
            left_out[key] = int(np.count_nonzero(drop & in_period))
            direction[drop] = 0
            potential[drop] = np.nan
        signals = Signals(direction, potential)

    labels = {key: label for key, label, _ in ctx.CONDITIONS}
    report, trades = report_of(
        base=base,
        shown=shown,
        inst=inst,
        ex=ex,
        signal_bars=base,
        signals=signals,
        start_ms=request.start_ms,
        end_ms=request.end_ms,
        split=request.split,
        tf=plan.base_tf,
        base_tf=plan.base_tf,
        warnings=warnings,
        request={
            "symbol": inst.symbol,
            "strategy": "setup",
            "strategy_title": request.name,
            "setup": {
                "name": request.name,
                "methods": [f"{s.key}@{tf.code}" for s, tf in plan.members],
                "mode": request.mode,
                "within_minutes": request.within_minutes,
                "skip": list(request.skip),
            },
            "start": iso(request.start_ms),
            "end": iso(request.end_ms),
            "timeframe": plan.base_tf.code,
            "base_timeframe": plan.base_tf.code,
            "params": {},
            "execution": asdict(ex),
            "split": request.split,
        },
    )
    for key, count in left_out.items():
        report["signals"]["signals"] += count
        report["signals"][f"skipped: {labels[key].lower()}"] = count

    for row, tags in zip(report["trades"], ctx.describe_trades(context, trades), strict=True):
        row.update(tags)
    report["why"] = ctx.explain(report["trades"])
    split_day = report["split_day"]
    if split_day is not None:  # the same question on each part: a reason has to hold on the part it was not found on
        for name, later in (("in_sample", False), ("out_of_sample", True)):
            report[name]["why"] = ctx.explain([r for r in report["trades"] if (r["day"] >= split_day) == later])
    report["context"] = {
        "grid": f"{context.seconds // 60}m",
        "voters": context.voters,
        "threshold": context.threshold,
        "fresh_bars": ctx.FRESH_BARS,
        "channel_hours": ctx.CHANNEL_HOURS,
        "channel_width": ctx.CHANNEL_WIDTH,
        "conditions": [{"key": key, "label": label, "when": when} for key, label, when in ctx.CONDITIONS],
    }
    return report
