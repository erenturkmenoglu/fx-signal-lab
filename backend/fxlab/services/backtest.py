"""Backtests against the stored history: the part that touches the database.

Backtests read the provider's historical bars (``source = massive_agg``, bid prices) and
nothing else: never the live series, never anything synthetic.  They only read.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.timeframes import get_timeframe
from fxlab.research.bars import PROVIDER_SOURCE, load_provider_bars
from fxlab.research.report import BacktestError, BacktestRequest, build_report, default_execution
from fxlab.research.strategies import STRATEGIES
from fxlab.storage import schema as s
from fxlab.timeutil import DAY_MS, dt_to_ms, iso, ms_to_dt

#: the longest period one test may cover (a year of minute bars is about 370 000 bars)
MAX_RANGE_MS = 3 * 366 * DAY_MS
#: the most bars one test may load: each costs about 40 bytes in memory and the server is small
MAX_BARS = 3_000_000


async def _instrument_id(engine: AsyncEngine, symbol: str) -> int | None:
    async with engine.connect() as conn:
        row = (await conn.execute(select(s.instruments.c.id).where(s.instruments.c.symbol == symbol))).first()
    return int(row[0]) if row else None


async def history_inventory(engine: AsyncEngine, symbol: str) -> list[dict[str, Any]]:
    """What provider history is stored for an instrument: one entry per timeframe."""
    instrument_id = await _instrument_id(engine, symbol)
    if instrument_id is None:
        return []
    b = s.bars
    query = (
        select(b.c.timeframe, func.count(), func.min(b.c.ts), func.max(b.c.ts))
        .where(b.c.instrument_id == instrument_id, b.c.source == PROVIDER_SOURCE)
        .group_by(b.c.timeframe)
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(query)).all()
    out = [
        {
            "timeframe": r[0],
            "seconds": get_timeframe(r[0]).seconds,
            "bars": int(r[1]),
            "first": iso(dt_to_ms(r[2])),
            "last": iso(dt_to_ms(r[3])),
        }
        for r in rows
    ]
    return sorted(out, key=lambda item: item["seconds"])


async def describe_lab(engine: AsyncEngine, registry: InstrumentRegistry, symbol: str) -> dict[str, Any]:
    """Everything a form needs to offer a test: strategies, default assumptions, stored history."""
    if symbol not in registry:
        raise BacktestError(f"unknown instrument {symbol!r}")
    inst = registry.get(symbol)
    ex = default_execution(inst)
    return {
        "symbol": inst.symbol,
        "currency": inst.quote,
        "unit_currency": inst.base,
        "strategies": [st.describe() for st in STRATEGIES.values()],
        "execution": {name: getattr(ex, name) for name in ex.__dataclass_fields__},
        "history": await history_inventory(engine, symbol),
    }


async def run_backtest(engine: AsyncEngine, registry: InstrumentRegistry, request: BacktestRequest) -> dict[str, Any]:
    """Load the bars of the request and run it.  Raises ``BacktestError`` with a message fit for the user."""
    if request.symbol not in registry:
        raise BacktestError(f"unknown instrument {request.symbol!r}")
    if request.end_ms <= request.start_ms:
        raise BacktestError("end must be after start")
    if request.end_ms - request.start_ms > MAX_RANGE_MS:
        raise BacktestError(f"one test covers at most {MAX_RANGE_MS // DAY_MS} days")
    inst = registry.get(request.symbol)
    try:
        base_tf = get_timeframe(request.base_timeframe)
    except ValueError as exc:
        raise BacktestError(str(exc)) from None
    if base_tf.anchor != "utc":
        raise BacktestError(f"trades cannot be simulated on {base_tf.code} bars; use bars of one hour or less")
    instrument_id = await _instrument_id(engine, request.symbol)
    bars = None
    if instrument_id is not None:
        b = s.bars
        count = select(func.count()).where(
            b.c.instrument_id == instrument_id,
            b.c.timeframe == base_tf.code,
            b.c.source == PROVIDER_SOURCE,
            b.c.ts >= ms_to_dt(request.start_ms),
            b.c.ts < ms_to_dt(request.end_ms),
        )
        async with engine.connect() as conn:
            stored = int((await conn.execute(count)).scalar_one())
        if stored > MAX_BARS:
            raise BacktestError(
                f"this period holds {stored:,} {base_tf.code} bars; one test loads at most {MAX_BARS:,}. "
                "Choose a shorter period or longer base bars."
            )
        bars = await load_provider_bars(
            engine, instrument_id, base_tf.code, base_tf.seconds, request.start_ms, request.end_ms
        )
    if bars is None or len(bars) == 0:
        raise BacktestError(
            f"no {base_tf.code} history of {request.symbol} is stored between {iso(request.start_ms)} and "
            f"{iso(request.end_ms)}. Download it first: fxlab history --symbol {request.symbol} "
            f"--timeframe {base_tf.code} --start ... --end ..."
        )
    return build_report(bars, inst, request)
