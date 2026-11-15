"""What a list of trades amounts to: per trade, per day, and in total.

Money is in the quote currency of the pair (US dollars for EUR/USD).  "Pips" in the totals
are money divided by the value of one pip on one clip, so that results with different clip
sizes can be compared: 2 500 USD with 1 mn EUR clips is 25 pips.

A day is a trading day of the instrument: it ends at the daily rollover (17:00 New York for
spot FX), and a trade belongs to the day in which it was closed.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import numpy as np

from fxlab.research.bars import Ints, utc_offsets_ms
from fxlab.research.engine import Execution, Trade, clock_ms
from fxlab.timeutil import DAY_MS, HOUR_MS, MINUTE_MS

_EPOCH_ORDINAL = date(1970, 1, 1).toordinal()
#: fewer days than this say nothing about day-to-day variation
MIN_DAYS_FOR_SHARPE = 20
TRADING_DAYS_PER_YEAR = 252


def trading_day_numbers(ts: Ints, tz: str, rollover: str) -> Ints:
    """The trading day of each instant, as days since 1970-01-01: the day whose rollover comes next."""
    if len(ts) == 0:
        return np.empty(0, dtype=np.int64)
    # a day that ends at 17:00 starts 7 hours before midnight; one that ends at midnight is the calendar day
    shifted = ts + utc_offsets_ms(ts, tz) + (DAY_MS - clock_ms(rollover)) % DAY_MS
    return np.asarray(shifted // DAY_MS, dtype=np.int64)


def day_name(number: int) -> str:
    return date.fromordinal(_EPOCH_ORDINAL + int(number)).isoformat()


def trading_days(ts: Ints, tz: str, rollover: str) -> list[str]:
    """The trading day (ISO date) of each instant."""
    return [day_name(n) for n in trading_day_numbers(ts, tz, rollover)]


def trade_rows(trades: list[Trade], ex: Execution, days: list[str]) -> list[dict[str, Any]]:
    """Trades as plain dictionaries, oldest first, with their trading day."""
    rows = []
    for number, (t, day) in enumerate(zip(trades, days, strict=True), start=1):
        rows.append(
            {
                "n": number,
                "day": day,
                "direction": "long" if t.direction > 0 else "short",
                "signal_ms": t.signal_ms,
                "entry_ms": t.entry_ms,
                "entry_price": t.fills[0].price,
                "clips": t.clips,
                "units": t.units,
                "average_price": round(t.average_price, 7),
                "exit_ms": t.exit_ms,
                "exit_price": t.exit_price,
                "reason": t.reason,
                "stop_price": t.stop_price,
                "target_price": t.target_price,
                "pnl": round(t.pnl, 2),
                # what the position made per unit, in pips: exit against the average entry
                "pips": round(t.direction * (t.exit_price - t.average_price) / ex.pip_size, 2),
                "potential_pips": round(t.potential_pips, 2),
                "adverse_pips": round(t.adverse_pips, 2),
                "minutes": round((t.exit_ms - t.entry_ms) / MINUTE_MS, 1),
                "entry_hour_utc": t.entry_ms % DAY_MS // HOUR_MS,
                "fills": [[f.ts_ms, f.price, f.units] for f in t.fills],
            }
        )
    return rows


def day_spans(ts: Ints, tz: str, rollover: str) -> dict[str, tuple[int, int]]:
    """Each trading day in ``ts`` (ascending) with the times of its first and last instant."""
    numbers = trading_day_numbers(ts, tz, rollover)
    if len(numbers) == 0:
        return {}
    starts = np.flatnonzero(np.concatenate(([True], numbers[1:] != numbers[:-1])))
    ends = np.concatenate((starts[1:], [len(numbers)])) - 1
    return {day_name(numbers[a]): (int(ts[a]), int(ts[b])) for a, b in zip(starts, ends, strict=True)}


def daily_rows(
    rows: list[dict[str, Any]],
    all_days: list[str],
    pip_value: float,
    spans: dict[str, tuple[int, int]] | None = None,
) -> list[dict[str, Any]]:
    """One line per trading day in the data, days without a trade included.

    With ``spans`` each line also says when the first and the last bar of that day began,
    which is what a chart needs to show the day.
    """
    by_day: dict[str, list[dict[str, Any]]] = {day: [] for day in all_days}
    for row in rows:
        by_day.setdefault(row["day"], []).append(row)
    out = []
    total = 0.0
    for day in sorted(by_day):
        items = by_day[day]
        pnl = sum(r["pnl"] for r in items)
        total += pnl
        line = {
            "day": day,
            "trades": len(items),
            "wins": sum(1 for r in items if r["pnl"] > 0),
            "pnl": round(pnl, 2),
            "pips": round(pnl / pip_value, 2),
            "cumulative": round(total, 2),
            "max_units": max((r["units"] for r in items), default=0.0),
        }
        if spans is not None and day in spans:
            line["first_bar_ms"], line["last_bar_ms"] = spans[day]
        out.append(line)
    return out


def _drawdown(pnls: list[float]) -> float:
    """Largest fall of the running total from an earlier high (the start counts as a high of 0)."""
    peak = total = worst = 0.0
    for pnl in pnls:
        total += pnl
        peak = max(peak, total)
        worst = max(worst, peak - total)
    return worst


def _longest_losing_run(pnls: list[float]) -> int:
    longest = run = 0
    for pnl in pnls:
        run = run + 1 if pnl < 0 else 0
        longest = max(longest, run)
    return longest


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        g = out.setdefault(str(row[key]), {"trades": 0, "wins": 0, "pnl": 0.0})
        g["trades"] += 1
        g["wins"] += 1 if row["pnl"] > 0 else 0
        g["pnl"] += row["pnl"]
    for g in out.values():
        g["pnl"] = round(g["pnl"], 2)
        g["win_rate"] = round(g["wins"] / g["trades"], 4)
    return out


def summarize(rows: list[dict[str, Any]], days: list[dict[str, Any]], pip_value: float) -> dict[str, Any]:
    """The totals of ``rows`` (trades) over ``days`` (every trading day of the period, from ``daily_rows``)."""
    pnls = [float(r["pnl"]) for r in rows]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit, gross_loss = sum(wins), -sum(losses)
    net = sum(pnls)
    day_pnls = [float(d["pnl"]) for d in days]
    sharpe = None
    if len(day_pnls) >= MIN_DAYS_FOR_SHARPE:
        spread = float(np.std(day_pnls, ddof=1))
        if spread > 0:
            sharpe = round(float(np.mean(day_pnls)) / spread * TRADING_DAYS_PER_YEAR**0.5, 2)
    traded = [d for d in days if d["trades"]]
    return {
        "trades": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
        "net_pnl": round(net, 2),
        "net_pips": round(net / pip_value, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        # money won per unit of money lost; without a losing trade there is nothing to divide by
        "profit_factor": round(gross_profit / gross_loss, 3) if gross_loss > 0 else None,
        "expectancy": round(net / len(pnls), 2) if pnls else None,
        "average_win": round(gross_profit / len(wins), 2) if wins else None,
        "average_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "largest_win": round(max(wins), 2) if wins else None,
        "largest_loss": round(min(losses), 2) if losses else None,
        "max_drawdown": round(_drawdown(pnls), 2),
        "longest_losing_run": _longest_losing_run(pnls),
        "max_units": max((float(r["units"]) for r in rows), default=0.0),
        "average_clips": round(sum(r["clips"] for r in rows) / len(rows), 2) if rows else None,
        "trades_with_adds": sum(1 for r in rows if r["clips"] > 1),
        "average_minutes": round(sum(r["minutes"] for r in rows) / len(rows), 1) if rows else None,
        "worst_adverse_pips": max((float(r["adverse_pips"]) for r in rows), default=None),
        "by_reason": _group(rows, "reason"),
        "by_direction": _group(rows, "direction"),
        "by_entry_hour_utc": dict(sorted(_group(rows, "entry_hour_utc").items(), key=lambda kv: int(kv[0]))),
        "days": len(days),
        "days_traded": len(traded),
        "days_positive": sum(1 for d in traded if d["pnl"] > 0),
        "days_negative": sum(1 for d in traded if d["pnl"] < 0),
        "best_day": max((d["pnl"] for d in traded), default=None),
        "worst_day": min((d["pnl"] for d in traded), default=None),
        "average_day": round(sum(day_pnls) / len(day_pnls), 2) if day_pnls else None,
        # mean daily result over its day-to-day spread, scaled to a year; days without a trade count as 0
        "daily_sharpe": sharpe,
    }


def equity_points(rows: list[dict[str, Any]]) -> list[list[float]]:
    """Running total after each trade: [exit time ms, total]."""
    total = 0.0
    out = []
    for row in rows:
        total += row["pnl"]
        out.append([row["exit_ms"], round(total, 2)])
    return out
