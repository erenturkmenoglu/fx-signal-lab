"""One backtest from bars to a report (no database, no network: everything here is a pure function).

The report always shows three things next to the headline numbers, because each of them is a
way a good-looking backtest can mislead:

* the period is cut in two: the earlier part to choose parameters on ("in-sample"), the later
  part to check them on ("out-of-sample").  A result that only exists in-sample is a result of
  the choosing, not of the strategy;
* the same signals traded *without* adding to losing positions, because adding raises the
  share of winning trades while making the rare loss several times larger;
* why signals were not traded, so that a strategy that "works" on a handful of trades is
  seen for what it is.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
from datetime import date
from typing import Any

import numpy as np

from fxlab.domain.instruments import Instrument
from fxlab.domain.timeframes import Timeframe, get_timeframe
from fxlab.research import engine, metrics
from fxlab.research.bars import Bars, resample
from fxlab.research.engine import Execution, clock_ms
from fxlab.research.strategies import Strategy, get_strategy
from fxlab.timeutil import DAY_MS, HOUR_MS, MINUTE_MS, iso, parse_time

#: share of the trading days kept back for checking when the split is left to the system: 3 in 10
OUT_OF_SAMPLE_SHARE = (3, 10)
MIN_DAYS_FOR_SPLIT = 10
#: below this number of trades a result is an anecdote
FEW_TRADES = 30
#: settings that belong to the instrument, not to the person running a test
FIXED_BY_INSTRUMENT = ("pip_size", "session_tz", "day_end")


class BacktestError(ValueError):
    """The request cannot be run as asked (bad input, or no data)."""


@dataclass(frozen=True)
class BacktestRequest:
    symbol: str
    strategy: str
    start_ms: int
    end_ms: int
    timeframe: str | None = None  # bars the strategy reads; None = the strategy's usual one
    base_timeframe: str = "1m"  # bars the trades are simulated on
    params: dict[str, Any] = field(default_factory=dict)
    execution: dict[str, Any] = field(default_factory=dict)
    split: str = "auto"  # auto | none | YYYY-MM-DD (first out-of-sample trading day)

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> BacktestRequest:
        known = {"symbol", "strategy", "start", "end", "timeframe", "base_timeframe", "params", "execution", "split"}
        unknown = sorted(set(data) - known)
        if unknown:
            raise BacktestError(f"unknown field {unknown[0]!r}; known: {', '.join(sorted(known))}")
        for name in ("symbol", "strategy", "start", "end"):
            if data.get(name) in (None, ""):
                raise BacktestError(f"{name} is required")
        for name in ("params", "execution"):
            if not isinstance(data.get(name) or {}, dict):
                raise BacktestError(f"{name} must be an object of names and values")
        try:
            start_ms, end_ms = parse_time(data["start"]), parse_time(data["end"])
        except (ValueError, TypeError):
            raise BacktestError("start and end must be dates or times (ISO-8601)") from None
        return cls(
            symbol=str(data["symbol"]).upper(),
            strategy=str(data["strategy"]),
            start_ms=start_ms,
            end_ms=end_ms,
            timeframe=str(data["timeframe"]) if data.get("timeframe") else None,
            base_timeframe=str(data.get("base_timeframe") or "1m"),
            params=dict(data.get("params") or {}),
            execution=dict(data.get("execution") or {}),
            split=str(data.get("split") or "auto"),
        )


def _hhmm(ms: int) -> str:
    ms %= DAY_MS
    return f"{ms // HOUR_MS:02d}:{ms % HOUR_MS // MINUTE_MS:02d}"


def default_execution(inst: Instrument) -> Execution:
    """The trading assumptions for an instrument before the person changes any of them."""
    return Execution(
        pip_size=inst.pip_size,
        spread_pips=inst.costs.assumed_spread_pips,
        slippage_pips=inst.costs.slippage_pips,
        commission_per_million=inst.costs.commission_per_million,
        session_tz=inst.session.tz,
        day_end=inst.session.daily_rollover,
        # out of every position a quarter of an hour before the trading day ends
        flat_time=_hhmm(clock_ms(inst.session.daily_rollover) - 15 * MINUTE_MS),
    )


def resolve_execution(inst: Instrument, overrides: dict[str, Any]) -> Execution:
    for name in FIXED_BY_INSTRUMENT:
        if name in overrides:
            raise BacktestError(f"{name} is fixed by the instrument and cannot be changed for a test")
    try:
        return Execution.from_mapping(overrides, **asdict(default_execution(inst)))
    except ValueError as exc:
        raise BacktestError(str(exc)) from None


def signal_timeframe(strategy: Strategy, requested: str | None, base: Timeframe) -> Timeframe:
    try:
        tf = get_timeframe(requested or strategy.timeframe)
    except ValueError as exc:
        raise BacktestError(str(exc)) from None
    if tf.anchor != "utc":
        raise BacktestError(f"{tf.code} bars follow the trading day; strategies run on bars of one hour or less")
    if tf.seconds % base.seconds != 0:
        raise BacktestError(f"{tf.code} bars cannot be built from {base.code} bars")
    return tf


def split_day_of(split: str, day_names: list[str], warnings: list[str]) -> str | None:
    """The first out-of-sample trading day, or None when the period is not split."""
    if split == "none":
        return None
    if split == "auto":
        if len(day_names) < MIN_DAYS_FOR_SPLIT:
            warnings.append(
                f"Only {len(day_names)} trading day{'' if len(day_names) == 1 else 's'}: too few to keep some back "
                "for checking, so every number here is in-sample."
            )
            return None
        part, whole = OUT_OF_SAMPLE_SHARE
        return day_names[len(day_names) - max(1, len(day_names) * part // whole)]
    day = date.fromisoformat(split).isoformat()  # checked when the request was prepared
    if not day_names or day <= day_names[0] or day > day_names[-1]:
        warnings.append(f"The split date {day} lies outside the data: one side of the split is empty.")
    return day


def data_section(
    base: Bars, tf: Timeframe, base_tf: Timeframe, signal_bars: int, day_names: list[str]
) -> dict[str, Any]:
    steps = np.diff(base.ts)
    # a pause of more than 40 hours is a weekend; anything else longer than half an hour is a hole
    inside = steps[(steps > 30 * MINUTE_MS) & (steps < 40 * HOUR_MS)]
    return {
        "source": "provider aggregates (Massive)",
        "price_basis": "bid",
        "base_timeframe": base_tf.code,
        "signal_timeframe": tf.code,
        "base_bars": len(base),
        "signal_bars": signal_bars,
        "first_bar": iso(int(base.ts[0])),
        "last_bar": iso(int(base.ts[-1])),
        "trading_days": len(day_names),
        "first_day": day_names[0],
        "last_day": day_names[-1],
        "holes_over_30_minutes": len(inside),
        "longest_hole_minutes": round(float(inside.max()) / MINUTE_MS, 1) if len(inside) else 0.0,
    }


def inside_the_trading_week(base: Bars, inst: Instrument) -> Bars:
    """The bars that fall into the hours the instrument trades (for spot FX: not the weekend).

    A provider may publish the odd quote while the market is closed.  Nothing can be traded on
    it, so no signal may come from it and no position may be opened or closed on it.
    """
    opening = inst.make_calendar().open_intervals(int(base.ts[0]), int(base.ts[-1]) + 1)
    keep = np.zeros(len(base), dtype=bool)
    for start, end in opening:
        keep[int(np.searchsorted(base.ts, start, side="left")) : int(np.searchsorted(base.ts, end, side="left"))] = True
    if bool(keep.all()):
        return base
    return Bars(base.ts[keep], base.open[keep], base.high[keep], base.low[keep], base.close[keep], base.seconds)


def _part(rows: list[dict[str, Any]], days: list[dict[str, Any]], pip_value: float) -> dict[str, Any]:
    return {
        "first_day": days[0]["day"] if days else None,
        "last_day": days[-1]["day"] if days else None,
        "summary": metrics.summarize(rows, days, pip_value),
    }


@dataclass(frozen=True)
class Plan:
    """A request with everything in it checked and resolved; nothing has been loaded or run yet."""

    strategy: Strategy
    base_tf: Timeframe
    tf: Timeframe
    params: dict[str, int | float | str]
    execution: Execution


def prepare(inst: Instrument, request: BacktestRequest) -> Plan:
    """Check a request against the instrument and the strategy.  Cheap: no data is touched."""
    strategy = get_strategy_or_fail(request.strategy)
    try:
        base_tf = get_timeframe(request.base_timeframe)
    except ValueError as exc:
        raise BacktestError(str(exc)) from None
    if base_tf.anchor != "utc":
        raise BacktestError(f"trades cannot be simulated on {base_tf.code} bars; use bars of one hour or less")
    tf = signal_timeframe(strategy, request.timeframe, base_tf)
    try:
        params = strategy.resolve(request.params)
    except ValueError as exc:
        raise BacktestError(str(exc)) from None
    if request.split not in ("auto", "none"):
        try:
            date.fromisoformat(request.split)
        except ValueError:
            raise BacktestError(f"split must be 'auto', 'none' or a date (YYYY-MM-DD), not {request.split!r}") from None
    return Plan(strategy, base_tf, tf, params, resolve_execution(inst, request.execution))


def build_report(base: Bars, inst: Instrument, request: BacktestRequest) -> dict[str, Any]:
    """Run ``request`` on ``base`` (the provider's bars of the requested range, already loaded)."""
    plan = prepare(inst, request)
    strategy, base_tf, tf, params, ex = plan.strategy, plan.base_tf, plan.tf, plan.params, plan.execution
    if base_tf.seconds != base.seconds:
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
    signal_bars = resample(base, tf.seconds)
    signals = strategy.compute(signal_bars, params, ex.pip_size)
    outcome = engine.run(base, signal_bars, signals, ex)

    tz, rollover = inst.session.tz, inst.session.daily_rollover
    spans = metrics.day_spans(base.ts, tz, rollover)
    day_names = sorted(spans)
    pip_value = ex.clip_units * ex.pip_size  # quote currency per pip on one clip

    def rows_of(trades: list[engine.Trade]) -> list[dict[str, Any]]:
        exits = np.asarray([t.exit_ms for t in trades], dtype=np.int64)
        return metrics.trade_rows(trades, ex, metrics.trading_days(exits, tz, rollover))

    rows = rows_of(outcome.trades)
    daily = metrics.daily_rows(rows, day_names, pip_value, spans)
    report: dict[str, Any] = {
        "request": {
            "symbol": inst.symbol,
            "strategy": strategy.key,
            "strategy_title": strategy.title,
            "start": iso(request.start_ms),
            "end": iso(request.end_ms),
            "timeframe": tf.code,
            "base_timeframe": base_tf.code,
            "params": params,
            "execution": asdict(ex),
            "split": request.split,
        },
        "currency": inst.quote,
        "unit_currency": inst.base,
        "pip_value": pip_value,
        "data": data_section(base, tf, base_tf, len(signal_bars), day_names),
        "signals": dict(outcome.counters),
        "summary": metrics.summarize(rows, daily, pip_value),
        "daily": daily,
        "equity": metrics.equity_points(rows),
        "trades": rows,
    }

    split_day = split_day_of(request.split, day_names, warnings)
    report["split_day"] = split_day
    if split_day is not None:
        for name, later in (("in_sample", False), ("out_of_sample", True)):
            part_rows = [r for r in rows if (r["day"] >= split_day) == later]
            part_names = [d for d in day_names if (d >= split_day) == later]
            report[name] = _part(part_rows, metrics.daily_rows(part_rows, part_names, pip_value), pip_value)

    if ex.max_clips > 1:  # the same signals, one clip only: what adding to positions changed
        single = engine.run(base, signal_bars, signals, replace(ex, max_clips=1))
        single_rows = rows_of(single.trades)
        single_daily = metrics.daily_rows(single_rows, day_names, pip_value)
        report["without_adding"] = {
            "summary": metrics.summarize(single_rows, single_daily, pip_value),
            "daily": single_daily,
        }

    summary = report["summary"]
    if summary["trades"] == 0:
        warnings.append("No signal was traded: see the signal counts for the reasons.")
    elif summary["trades"] < FEW_TRADES:
        warnings.append(f"Only {summary['trades']} trades: too few to tell skill from luck.")
    if any(r["reason"] == "end_of_data" for r in rows):
        warnings.append("The last position was still open when the data ended; it is valued at the last price.")
    if request.start_ms + 4 * DAY_MS < int(base.ts[0]) or int(base.ts[-1]) + 4 * DAY_MS < request.end_ms:
        warnings.append(
            f"The stored history covers {iso(int(base.ts[0]))} to {iso(int(base.ts[-1]))} only, "
            "less than the period asked for."
        )
    report["warnings"] = warnings
    return report


def get_strategy_or_fail(key: str) -> Strategy:
    try:
        return get_strategy(key)
    except ValueError as exc:
        raise BacktestError(str(exc)) from None


# --- text ----------------------------------------------------------------------------------


def _money(value: float | None) -> str:
    return "-" if value is None else f"{value:,.0f}"


def _ratio(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _percent(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.1f}%"


_LINES: tuple[tuple[str, str, Any], ...] = (
    ("Trades", "trades", lambda v: f"{v:,}"),
    ("Net result", "net_pnl", _money),
    ("  in pips on one clip", "net_pips", lambda v: f"{v:,.1f}"),
    ("Winning trades", "win_rate", _percent),
    ("Profit factor", "profit_factor", _ratio),
    ("Average per trade", "expectancy", _money),
    ("Average win", "average_win", _money),
    ("Average loss", "average_loss", _money),
    ("Largest loss", "largest_loss", _money),
    ("Largest fall from a high", "max_drawdown", _money),
    ("Longest losing run", "longest_losing_run", lambda v: f"{v}"),
    ("Largest position", "max_units", lambda v: f"{v:,.0f}"),
    ("Trades that added", "trades_with_adds", lambda v: f"{v:,}"),
    ("Days traded", "days_traded", lambda v: f"{v}"),
    ("Worst day", "worst_day", _money),
    ("Best day", "best_day", _money),
    ("Sharpe (daily, yearly scale)", "daily_sharpe", _ratio),
)


def format_report(report: dict[str, Any], *, trades: int = 0, days: int = 0) -> str:
    """The report as text for a terminal."""
    req, data = report["request"], report["data"]
    ex = req["execution"]
    out = [
        f"{req['strategy_title']} on {req['symbol']}, {req['timeframe']} bars "
        f"(trades simulated on {req['base_timeframe']} {data['price_basis']} bars)",
        f"Data: {data['first_day']} to {data['last_day']}, {data['trading_days']} trading days, "
        f"{data['base_bars']:,} bars, {data['holes_over_30_minutes']} holes over 30 minutes",
        "Parameters: " + ", ".join(f"{k}={v}" for k, v in req["params"].items()),
        f"Trading: clips of {ex['clip_units']:,.0f} {report['unit_currency']}, at most {ex['max_clips']} "
        f"(one more every {ex['add_step_pips']:g} pips), target {ex['target_pips']:g}, stop {ex['stop_pips']:g} "
        f"beyond the last clip, spread {ex['spread_pips']:g}, slippage {ex['slippage_pips']:g}, "
        f"minimum potential {ex['min_potential_pips']:g} pips",
        f"         entries {ex['entry_start_utc']}-{ex['entry_end_utc']} UTC, flat at {ex['flat_time']} "
        f"{ex['session_tz']}, at most {ex['max_hold_minutes']:g} minutes in a position",
        "",
    ]
    columns: list[tuple[str, dict[str, Any]]] = [("Whole period", report["summary"])]
    if "in_sample" in report:
        columns.append(("In-sample", report["in_sample"]["summary"]))
        columns.append(("Out-of-sample", report["out_of_sample"]["summary"]))
    if "without_adding" in report:
        columns.append(("No adding", report["without_adding"]["summary"]))
    out.append(f"{'(money in ' + report['currency'] + ')':30}" + "".join(f"{name:>15}" for name, _ in columns))
    for label, key, fmt in _LINES:
        out.append(f"{label:30}" + "".join(f"{fmt(s[key]) if s[key] is not None else '-':>15}" for _, s in columns))
    if "in_sample" in report:
        first, second = report["in_sample"], report["out_of_sample"]
        out += [
            "",
            f"In-sample: {first['first_day']} to {first['last_day']} ({first['summary']['days']} days), to choose "
            f"parameters on.  Out-of-sample: {second['first_day']} to {second['last_day']} "
            f"({second['summary']['days']} days), to check them on.",
        ]
    out += ["", "Exits (whole period):"]
    for reason, g in sorted(report["summary"]["by_reason"].items(), key=lambda kv: -kv[1]["trades"]):
        out.append(f"  {reason:14}{g['trades']:>7,} trades{_money(g['pnl']):>14}")
    out += ["", "Signals:"]
    for name, count in report["signals"].items():
        out.append(f"  {name:48}{count:>8,}")
    if days:
        out += [
            "",
            f"Last {min(days, len(report['daily']))} days:",
            f"  {'day':12}{'trades':>7}{'result':>12}{'total':>14}",
        ]
        for d in report["daily"][-days:]:
            out.append(f"  {d['day']:12}{d['trades']:>7}{_money(d['pnl']):>12}{_money(d['cumulative']):>14}")
    if trades:
        out += [
            "",
            f"Last {min(trades, len(report['trades']))} trades:",
            f"  {'entry (UTC)':20}{'side':6}{'clips':>5}{'average':>10}{'exit':>10}{'pips':>8}{'result':>11}  reason",
        ]
        for t in report["trades"][-trades:]:
            out.append(
                f"  {str(iso(t['entry_ms']))[:19]:20}{t['direction']:6}{t['clips']:>5}{t['average_price']:>10.5f}"
                f"{t['exit_price']:>10.5f}{t['pips']:>8.1f}{_money(t['pnl']):>11}  {t['reason']}"
            )
    if report["warnings"]:
        out += ["", *("! " + w for w in report["warnings"])]
    out += [
        "",
        "A backtest shows what a rule would have done on past prices under the costs assumed above.",
        "It is not a forecast, and no strategy here is known to be profitable in the future.",
    ]
    return "\n".join(out)
