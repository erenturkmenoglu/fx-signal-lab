"""The strategy lab: what can be tested, and running a test.

A backtest runs in a process of its own.  It works one processor core for seconds and holds
a year of bars in memory; neither may get in the way of the live feed this process exists
for.  The child is the command an operator would type (``fxlab backtest --request -``), so
the dashboard and the command line cannot give different answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request, Response

from fxlab.api.routes import get_service
from fxlab.logging import get_logger
from fxlab.research.report import BacktestError, BacktestRequest
from fxlab.services.backtest import check_request, describe_lab

log = get_logger(__name__)
router = APIRouter()

#: a run that takes longer than this is stopped (a year of minute bars takes a few seconds)
MAX_RUN_SECONDS = 180.0


@router.get("/lab")
async def lab(request: Request, symbol: str | None = None) -> dict[str, Any]:
    """Strategies with their parameters, the trading assumptions of the instrument, the stored history."""
    service = get_service(request)
    active = service.registry.active()
    chosen = (symbol or (active[0].symbol if active else "")).upper()
    try:
        return await describe_lab(service.engine, service.registry, chosen)
    except BacktestError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None


def _lock(request: Request) -> asyncio.Lock:
    lock: asyncio.Lock | None = getattr(request.app.state, "backtest_lock", None)
    if lock is None:
        lock = request.app.state.backtest_lock = asyncio.Lock()
    return lock


@router.post("/backtests")
async def run_backtest(request: Request, body: dict[str, Any] = Body(...)) -> Response:  # noqa: B008
    """Run a strategy on the stored provider history and return the whole report.

    Body: ``{"symbol", "strategy", "start", "end", "timeframe"?, "base_timeframe"?, "params"?,
    "execution"?, "split"?}``.  One run at a time.
    """
    service = get_service(request)
    try:  # what is wrong with the request itself is said at once, without starting anything
        check_request(service.registry, BacktestRequest.from_mapping(body))
    except BacktestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    lock = _lock(request)
    if lock.locked():
        raise HTTPException(status_code=409, detail="another backtest is running; try again when it has finished")
    settings = service.settings
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "INSTRUMENTS_FILE": str(settings.instruments_file),
    }
    async with lock:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "fxlab.cli",
            "backtest",
            "--request",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(json.dumps(body).encode()), MAX_RUN_SECONDS)
        except TimeoutError:
            raise HTTPException(
                status_code=504,
                detail=f"the backtest took longer than {MAX_RUN_SECONDS:.0f} seconds and was stopped; "
                "choose a shorter period",
            ) from None
        finally:
            if proc.returncode is None:  # timed out, or the caller went away: nothing is left running
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()
    if proc.returncode == 0:
        return Response(content=out, media_type="application/json")  # passed on as it is: megabytes of JSON
    if proc.returncode == 2:  # the request could not be run as asked (most often: no history stored)
        try:
            detail = str(json.loads(out)["error"])
        except (ValueError, KeyError, TypeError):
            detail = "the backtest could not be run"
        raise HTTPException(status_code=400, detail=detail)
    log.error("backtest_failed", exit_code=proc.returncode, stderr=err.decode(errors="replace")[-2000:])
    raise HTTPException(status_code=500, detail="the backtest stopped with an error; see the application log")
