"""Many methods at once: every strategy on every bar length, under the same trading rules.

Two things are built here from bars that are already loaded (no database, no network):

* the **board**: each method (a strategy on one bar length, with its default parameters)
  traded over a period, with what it made and, for every trading day, how many of its trades
  worked and how many did not;
* the **signals of a window**: every signal every method gave in, say, a week, and what
  became of it - traded and how it ended, or not traded and why.

A board invites a mistake, and the result says so itself: with dozens of methods, the best
one over any period looks good by chance alone.  That is why every line carries its result
on the earlier and on the later part of the period separately, and why a method that knows
nothing (random entries) is always on the board to compare against.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

import numpy as np

from fxlab.domain.instruments import Instrument
from fxlab.domain.timeframes import TIMEFRAMES, Timeframe, get_timeframe
from fxlab.research import engine, metrics
from fxlab.research.bars import Bars, resample
from fxlab.research.engine import Execution, Outcome
from fxlab.research.report import (
    BacktestError,
    data_section,
    inside_the_trading_week,
    resolve_execution,
    split_day_of,
)
from fxlab.research.strategies import STRATEGIES, Strategy
from fxlab.timeutil import iso, parse_time

#: reports progress: (methods done, methods in all, the one just finished)
Progress = Callable[[int, int, str], None]

#: short codes for what became of a signal that was not traded
SKIP_CODES = {
    engine.SKIP_OFF: "off",
    engine.SKIP_POTENTIAL: "potential",
    engine.SKIP_BUSY: "busy",
    engine.SKIP_HOURS: "hours",
    engine.SKIP_NO_PRICE: "no_price",
    engine.SKIP_DAY_END: "day_end",
}
#: the most bars a chart is given in one answer
MAX_CHART_BARS = 20_000


@dataclass(frozen=True)
class BoardRequest:
    symbol: str
    start_ms: int
    end_ms: int
    base_timeframe: str = "1m"
    timeframes: tuple[str, ...] = ()  # bar lengths the methods read; empty = all of a minute to an hour
    strategies: tuple[str, ...] = ()  # empty = all
    execution: dict[str, Any] = field(default_factory=dict)
    split: str = "auto"
    chart_timeframe: str = "5m"  # for the signals of a window: the bars drawn under them

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> BoardRequest:
        known = {
            "symbol",
            "start",
            "end",
            "base_timeframe",
            "timeframes",
            "strategies",
            "execution",
            "split",
            "chart_timeframe",
        }
        unknown = sorted(set(data) - known)
        if unknown:
            raise BacktestError(f"unknown field {unknown[0]!r}; known: {', '.join(sorted(known))}")
        for name in ("symbol", "start", "end"):
            if data.get(name) in (None, ""):
                raise BacktestError(f"{name} is required")
        for name in ("timeframes", "strategies"):
            value = data.get(name) or []
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise BacktestError(f"{name} must be a list of names")
        if not isinstance(data.get("execution") or {}, dict):
            raise BacktestError("execution must be an object of names and values")
        try:
            start_ms, end_ms = parse_time(data["start"]), parse_time(data["end"])
        except (ValueError, TypeError):
            raise BacktestError("start and end must be dates or times (ISO-8601)") from None
        return cls(
            symbol=str(data["symbol"]).upper(),
            start_ms=start_ms,
            end_ms=end_ms,
            base_timeframe=str(data.get("base_timeframe") or "1m"),
            timeframes=tuple(data.get("timeframes") or ()),
            strategies=tuple(data.get("strategies") or ()),
            execution=dict(data.get("execution") or {}),
            split=str(data.get("split") or "auto"),
            chart_timeframe=str(data.get("chart_timeframe") or "5m"),
        )


@dataclass(frozen=True)
class BoardPlan:
    base_tf: Timeframe
    timeframes: tuple[Timeframe, ...]
    strategies: tuple[Strategy, ...]
    execution: Execution
    chart_tf: Timeframe


def _timeframe(code: str, base: Timeframe, what: str) -> Timeframe:
    try:
        tf = get_timeframe(code)
    except ValueError as exc:
        raise BacktestError(str(exc)) from None
    if tf.anchor != "utc":
        raise BacktestError(f"{tf.code} bars follow the trading day; {what} are of one hour or less")
    if tf.seconds % base.seconds != 0:
        raise BacktestError(f"{tf.code} bars cannot be built from {base.code} bars")
    return tf


def prepare_board(inst: Instrument, request: BoardRequest) -> BoardPlan:
    """Check a request.  Cheap: no data is touched."""
    try:
        base_tf = get_timeframe(request.base_timeframe)
    except ValueError as exc:
        raise BacktestError(str(exc)) from None
    if base_tf.anchor != "utc":
        raise BacktestError(f"trades cannot be simulated on {base_tf.code} bars; use bars of one hour or less")
    codes = request.timeframes or tuple(
        tf.code
        for tf in TIMEFRAMES.values()
        if tf.anchor == "utc" and tf.seconds >= max(60, base_tf.seconds) and tf.seconds % base_tf.seconds == 0
    )
    timeframes = tuple(_timeframe(code, base_tf, "the bars a method reads") for code in dict.fromkeys(codes))
    unknown = [key for key in request.strategies if key not in STRATEGIES]
    if unknown:
        raise BacktestError(f"unknown strategy {unknown[0]!r}; there are: {', '.join(STRATEGIES)}")
    strategies = tuple(STRATEGIES[key] for key in dict.fromkeys(request.strategies or tuple(STRATEGIES)))
    if request.split not in ("auto", "none"):
        try:
            date.fromisoformat(request.split)
        except ValueError:
            raise BacktestError(f"split must be 'auto', 'none' or a date (YYYY-MM-DD), not {request.split!r}") from None
    return BoardPlan(
        base_tf,
        timeframes,
        strategies,
        resolve_execution(inst, request.execution),
        _timeframe(request.chart_timeframe, base_tf, "the bars of a chart"),
    )


def _tally(
    outcome: Outcome, day_index: dict[int, int], days: int, split: int | None, inst: Instrument
) -> dict[str, Any]:
    """What a run amounts to, in total and for each trading day."""
    trades = outcome.trades
    # to the cent, as a report shows them: a trade that made 0.00 is neither a win nor a loss
    pnl = np.round(np.asarray([t.pnl for t in trades], dtype=np.float64), 2)
    exits = np.asarray([t.exit_ms for t in trades], dtype=np.int64)
    numbers = metrics.trading_day_numbers(exits, inst.session.tz, inst.session.daily_rollover)
    day = np.asarray([day_index.get(int(n), -1) for n in numbers], dtype=np.int64)
    inside = day >= 0  # a trade always ends on a day of the data; this guards against a calendar surprise
    won, lost = pnl > 0, pnl < 0

    def per_day(weights: Any) -> Any:
        return np.bincount(day[inside], weights=np.asarray(weights, dtype=np.float64)[inside], minlength=days)

    day_pnl = np.round(per_day(pnl), 2)
    day_trades = per_day(np.ones(len(pnl)))
    running = np.cumsum(pnl)
    drawdown = float((np.maximum.accumulate(np.maximum(running, 0.0)) - running).max()) if len(pnl) else 0.0
    profit, loss = float(pnl[won].sum()), float(-pnl[lost].sum())

    def part(mask: Any) -> dict[str, Any]:
        return {
            "trades": int(mask.sum()),
            "wins": int((won & mask).sum()),
            "losses": int((lost & mask).sum()),
            "net": round(float(pnl[mask].sum()), 2),
        }

    traded = day_trades > 0
    out: dict[str, Any] = {
        **part(np.ones(len(pnl), dtype=bool)),
        "profit_factor": round(profit / loss, 3) if loss > 0 else None,
        "largest_loss": round(float(pnl.min()), 2) if lost.any() else None,
        "max_drawdown": round(drawdown, 2),
        "max_units": max((t.units for t in trades), default=0.0),
        "days_traded": int(traded.sum()),
        "days_won": int((day_pnl[traded] > 0).sum()),
        "days_lost": int((day_pnl[traded] < 0).sum()),
        "best_day": round(float(day_pnl[traded].max()), 2) if traded.any() else None,
        "worst_day": round(float(day_pnl[traded].min()), 2) if traded.any() else None,
        "daily": {
            "trades": day_trades.astype(int).tolist(),
            "wins": per_day(won).astype(int).tolist(),
            "losses": per_day(lost).astype(int).tolist(),
            "pnl": day_pnl.tolist(),
        },
    }
    if split is not None:
        out["in_sample"] = part(inside & (day < split))
        out["out_of_sample"] = part(inside & (day >= split))
    return out


def build_board(
    base: Bars, inst: Instrument, request: BoardRequest, progress: Progress | None = None
) -> dict[str, Any]:
    """Every method of ``request`` traded on ``base`` (the provider's bars of the period, already loaded)."""
    plan = prepare_board(inst, request)
    if plan.base_tf.seconds != base.seconds:
        raise BacktestError("the bars given are not of the base timeframe")
    if len(base) == 0:
        raise BacktestError("there are no bars in this range")
    warnings: list[str] = []
    asked = len(base)
    base = inside_the_trading_week(base, inst)
    if len(base) < asked:
        warnings.append(
            f"{asked - len(base):,} bars lie outside the instrument's trading week (weekend quotes) and were left out."
        )
    if len(base) == 0:
        raise BacktestError("there are no bars inside the trading week in this range")

    ex = plan.execution
    tz, rollover = inst.session.tz, inst.session.daily_rollover
    spans = metrics.day_spans(base.ts, tz, rollover)
    day_names = sorted(spans)
    numbers = np.unique(metrics.trading_day_numbers(base.ts, tz, rollover))
    day_index = {int(n): k for k, n in enumerate(numbers)}
    split_day = split_day_of(request.split, day_names, warnings)
    split = None if split_day is None else sum(1 for d in day_names if d < split_day)

    variants: list[tuple[str, Execution]] = [("one", replace(ex, max_clips=1))]
    if ex.max_clips > 1:
        variants.insert(0, ("all", ex))
    total = len(plan.timeframes) * len(plan.strategies)
    combos: list[dict[str, Any]] = []
    for tf in plan.timeframes:
        signal_bars = resample(base, tf.seconds)
        for strategy in plan.strategies:
            signals = strategy.compute(signal_bars, strategy.resolve(), ex.pip_size)
            combo: dict[str, Any] = {
                "id": f"{strategy.key}@{tf.code}",
                "strategy": strategy.key,
                "title": strategy.title,
                "short": strategy.short or strategy.key,
                "family": strategy.family,
                "timeframe": tf.code,
                "signals": int(np.count_nonzero(signals.direction)),
            }
            for name, rules in variants:
                combo[name] = _tally(
                    engine.run(base, signal_bars, signals, rules), day_index, len(day_names), split, inst
                )
            combos.append(combo)
            if progress is not None:
                progress(len(combos), total, combo["id"])

    if len(combos) > 1:
        warnings.append(
            f"{len(combos)} methods were tried on the same prices: the best of so many looks good by chance alone. "
            "Compare the earlier with the later part of the period, and everything with the random entries."
        )
    return {
        "request": {
            "symbol": inst.symbol,
            "start": iso(request.start_ms),
            "end": iso(request.end_ms),
            "base_timeframe": plan.base_tf.code,
            "timeframes": [tf.code for tf in plan.timeframes],
            "strategies": [s.key for s in plan.strategies],
            "execution": asdict(ex),
            "split": request.split,
        },
        "currency": inst.quote,
        "unit_currency": inst.base,
        "pip_value": ex.clip_units * ex.pip_size,
        "data": data_section(base, plan.base_tf, plan.base_tf, len(base), day_names),
        "days": day_names,
        "day_spans": [list(spans[d]) for d in day_names],
        "split_day": split_day,
        "variants": [name for name, _ in variants],
        "combos": combos,
        "warnings": warnings,
    }


def window_signals(base: Bars, inst: Instrument, request: BoardRequest, run_in_ms: int) -> dict[str, Any]:
    """Every signal of every method between ``request.start_ms`` and ``request.end_ms``.

    ``base`` starts ``run_in_ms`` before the window so that indicators have settled and the
    engine knows whether a position was open when the window began, and it may run on past the
    end so that a trade opened late in the window can finish.  Only signals and bars of the
    window itself are returned.
    """
    plan = prepare_board(inst, request)
    if plan.base_tf.seconds != base.seconds:
        raise BacktestError("the bars given are not of the base timeframe")
    base = inside_the_trading_week(base, inst) if len(base) else base
    shown = (base.ts >= request.start_ms) & (base.ts < request.end_ms)
    if not bool(shown.any()):
        raise BacktestError("there are no bars in this window")
    chart = resample(
        Bars(base.ts[shown], base.open[shown], base.high[shown], base.low[shown], base.close[shown], base.seconds),
        plan.chart_tf.seconds,
    )
    if len(chart) > MAX_CHART_BARS:
        raise BacktestError(
            f"this window has {len(chart):,} {plan.chart_tf.code} bars; a chart is given at most {MAX_CHART_BARS:,}. "
            "Choose a shorter window or longer bars."
        )
    ex = plan.execution
    found: list[dict[str, Any]] = []
    for tf in plan.timeframes:
        signal_bars = resample(base, tf.seconds)
        for strategy in plan.strategies:
            signals = strategy.compute(signal_bars, strategy.resolve(), ex.pip_size)
            outcome = engine.run(base, signal_bars, signals, ex)
            trades = iter(outcome.trades)
            for bar, decision in outcome.decisions:
                trade = next(trades) if decision == "traded" else None
                known_ms = int(signal_bars.ts[bar]) + signal_bars.span_ms
                if not request.start_ms <= known_ms < request.end_ms:
                    continue
                item: dict[str, Any] = {
                    "method": f"{strategy.key}@{tf.code}",
                    "t": known_ms,
                    "d": 1 if signals.direction[bar] > 0 else -1,
                    "potential": round(float(signals.potential_pips[bar]) - ex.spread_pips, 1),
                    "status": SKIP_CODES.get(decision, decision),
                }
                if np.isnan(item["potential"]):
                    item["potential"] = None
                if trade is not None:
                    item.update(
                        status=trade.reason,
                        pnl=round(trade.pnl, 2),
                        clips=trade.clips,
                        entry_ms=trade.entry_ms,
                        entry_price=trade.fills[0].price,
                        exit_ms=trade.exit_ms,
                        exit_price=trade.exit_price,
                    )
                found.append(item)
    found.sort(key=lambda item: (item["t"], item["method"]))
    return {
        "request": {
            "symbol": inst.symbol,
            "start": iso(request.start_ms),
            "end": iso(request.end_ms),
            "base_timeframe": plan.base_tf.code,
            "chart_timeframe": plan.chart_tf.code,
            "timeframes": [tf.code for tf in plan.timeframes],
            "strategies": [s.key for s in plan.strategies],
            "execution": asdict(ex),
        },
        "currency": inst.quote,
        "run_in_from": iso(int(base.ts[0])),
        "methods": [
            {
                "id": f"{s.key}@{tf.code}",
                "strategy": s.key,
                "short": s.short or s.key,
                "title": s.title,
                "timeframe": tf.code,
            }
            for tf in plan.timeframes
            for s in plan.strategies
        ],
        "bars": {
            "timeframe": plan.chart_tf.code,
            "seconds": plan.chart_tf.seconds,
            "t": chart.ts.tolist(),
            "o": chart.open.tolist(),
            "h": chart.high.tolist(),
            "l": chart.low.tolist(),
            "c": chart.close.tolist(),
        },
        "signals": found,
    }
