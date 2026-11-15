"""Backtests against the stored history: the part that touches the database.

Backtests read the provider's historical bars (``source = massive_agg``, bid prices) and
nothing else: never the live series, never anything synthetic.  They only read.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.domain.instruments import InstrumentRegistry
from fxlab.domain.timeframes import TIMEFRAMES, Timeframe, get_timeframe
from fxlab.research.bars import PROVIDER_SOURCE, Bars, load_provider_bars
from fxlab.research.board import BoardRequest, Progress, build_board, prepare_board, window_signals
from fxlab.research.report import BacktestError, BacktestRequest, build_report, default_execution, prepare
from fxlab.research.strategies import STRATEGIES
from fxlab.storage import schema as s
from fxlab.timeutil import DAY_MS, dt_to_ms, iso, ms_to_dt

#: the longest period one test may cover (a year of minute bars is about 370 000 bars)
MAX_RANGE_MS = 3 * 366 * DAY_MS
#: the most bars one test may load: each costs about 40 bytes in memory and the server is small
MAX_BARS = 3_000_000
#: bars loaded before a period so that indicators have settled when it begins: three weeks
RUN_IN_MS = 21 * DAY_MS


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
        # bars a strategy can read: aligned to the UTC clock, a minute or longer
        "timeframes": [tf.code for tf in TIMEFRAMES.values() if tf.anchor == "utc" and tf.seconds >= 60],
        "pip_size": inst.pip_size,
        "price_precision": inst.price_precision,
        "session_tz": inst.session.tz,
        "execution": {name: getattr(ex, name) for name in ex.__dataclass_fields__},
        "history": await history_inventory(engine, symbol),
    }


def check_request(registry: InstrumentRegistry, request: BacktestRequest) -> None:
    """Everything that can be said about a request without reading data.  Raises ``BacktestError``."""
    if request.symbol not in registry:
        raise BacktestError(f"unknown instrument {request.symbol!r}")
    if request.end_ms <= request.start_ms:
        raise BacktestError("end must be after start")
    if request.end_ms - request.start_ms > MAX_RANGE_MS:
        raise BacktestError(f"one test covers at most {MAX_RANGE_MS // DAY_MS} days")
    prepare(registry.get(request.symbol), request)


async def _stored_bars(
    engine: AsyncEngine,
    symbol: str,
    base_tf: Timeframe,
    start_ms: int,
    end_ms: int,
    *,
    lead_ms: int = RUN_IN_MS,
    tail_ms: int = 0,
) -> Bars:
    """The provider's bars of an instrument for a period, or a ``BacktestError`` that says what to do.

    With the bars of the period come those of ``lead_ms`` before it (as far as they are stored:
    the run-in on which indicators settle) and of ``tail_ms`` after it.
    """
    instrument_id = await _instrument_id(engine, symbol)
    bars = None
    if instrument_id is not None:
        b = s.bars
        stored = select(
            func.count(), func.count().filter(b.c.ts >= ms_to_dt(start_ms), b.c.ts < ms_to_dt(end_ms))
        ).where(
            b.c.instrument_id == instrument_id,
            b.c.timeframe == base_tf.code,
            b.c.source == PROVIDER_SOURCE,
            b.c.ts >= ms_to_dt(start_ms - lead_ms),
            b.c.ts < ms_to_dt(end_ms + tail_ms),
        )
        async with engine.connect() as conn:
            loaded, inside = (int(value) for value in (await conn.execute(stored)).one())
        if loaded > MAX_BARS:
            raise BacktestError(
                f"this period holds {loaded:,} {base_tf.code} bars; one test loads at most {MAX_BARS:,}. "
                "Choose a shorter period or longer base bars."
            )
        if inside:
            bars = await load_provider_bars(
                engine, instrument_id, base_tf.code, base_tf.seconds, start_ms - lead_ms, end_ms + tail_ms
            )
    if bars is None or len(bars) == 0:
        raise BacktestError(
            f"no {base_tf.code} history of {symbol} is stored between {iso(start_ms)} and "
            f"{iso(end_ms)}. Download it first: fxlab history --symbol {symbol} "
            f"--timeframe {base_tf.code} --start ... --end ..."
        )
    return bars


async def run_backtest(engine: AsyncEngine, registry: InstrumentRegistry, request: BacktestRequest) -> dict[str, Any]:
    """Load the bars of the request and run it.  Raises ``BacktestError`` with a message fit for the user."""
    check_request(registry, request)
    inst = registry.get(request.symbol)
    base_tf = prepare(inst, request).base_tf  # everything about the request is checked before any data is read
    bars = await _stored_bars(engine, request.symbol, base_tf, request.start_ms, request.end_ms)
    return build_report(bars, inst, request)


# --- many methods at once ------------------------------------------------------------------

#: the longest window of signals (the chart under it holds 20 000 bars at most anyway)
MAX_WINDOW_MS = 62 * DAY_MS


def check_board(registry: InstrumentRegistry, request: BoardRequest, *, window: bool = False) -> None:
    """Everything that can be said about a board request without reading data."""
    if request.symbol not in registry:
        raise BacktestError(f"unknown instrument {request.symbol!r}")
    if request.end_ms <= request.start_ms:
        raise BacktestError("end must be after start")
    longest = MAX_WINDOW_MS if window else MAX_RANGE_MS
    if request.end_ms - request.start_ms > longest:
        raise BacktestError(f"{'a window of signals' if window else 'one run'} covers at most {longest // DAY_MS} days")
    prepare_board(registry.get(request.symbol), request)


async def run_board(
    engine: AsyncEngine, registry: InstrumentRegistry, request: BoardRequest, progress: Progress | None = None
) -> dict[str, Any]:
    """Every method on the stored history of a period."""
    check_board(registry, request)
    inst = registry.get(request.symbol)
    base_tf = prepare_board(inst, request).base_tf
    bars = await _stored_bars(engine, request.symbol, base_tf, request.start_ms, request.end_ms)
    return build_board(bars, inst, request, progress)


async def run_window(engine: AsyncEngine, registry: InstrumentRegistry, request: BoardRequest) -> dict[str, Any]:
    """Every signal of every method inside a window, with the bars to draw them on."""
    check_board(registry, request, window=True)
    inst = registry.get(request.symbol)
    base_tf = prepare_board(inst, request).base_tf
    # with the run-in before the window come the bars of a day after it, for its last trades to finish
    bars = await _stored_bars(engine, request.symbol, base_tf, request.start_ms, request.end_ms, tail_ms=DAY_MS)
    return window_signals(bars, inst, request)
