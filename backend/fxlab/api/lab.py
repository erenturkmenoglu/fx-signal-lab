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
from fxlab.research.board import BoardRequest
from fxlab.research.report import BacktestError, BacktestRequest
from fxlab.services.backtest import check_board, check_request, describe_lab

log = get_logger(__name__)
router = APIRouter()

#: a run that takes longer than this is stopped (a year of minute bars takes a few seconds)
MAX_RUN_SECONDS = 180.0
#: the same for a board, which runs every strategy on every bar length (a year: about ten seconds)
MAX_BOARD_SECONDS = 900.0
#: how long a request for the signals of a window waits for another run to finish before giving up
MAX_WAIT_SECONDS = 20.0


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


def _child_env(request: Request) -> dict[str, str]:
    """The environment of a child process: this process's own, with the settings it was started with."""
    settings = get_service(request).settings
    return {
        **os.environ,
        "DATABASE_URL": settings.database_url,
        "INSTRUMENTS_FILE": str(settings.instruments_file),
    }


async def _spawn(command: str, env: dict[str, str], *extra: str) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "fxlab.cli",
        command,
        "--request",
        "-",
        *extra,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )


async def _stop(proc: asyncio.subprocess.Process) -> None:
    """Nothing is left running: a child that has not ended is ended."""
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()


def _refusal(out: bytes, what: str) -> str:
    """The reason a child gave (exit code 2) for not running a request."""
    try:
        return str(json.loads(out)["error"])
    except (ValueError, KeyError, TypeError):
        return f"{what} could not be run"


async def _answer(command: str, request: Request, body: dict[str, Any], what: str) -> Response:
    """Run one child to its end and pass its answer on.  The caller holds the lock."""
    proc = await _spawn(command, _child_env(request))
    try:
        out, err = await asyncio.wait_for(proc.communicate(json.dumps(body).encode()), MAX_RUN_SECONDS)
    except TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"{what} took longer than {MAX_RUN_SECONDS:.0f} seconds and was stopped; choose a shorter period",
        ) from None
    finally:
        await _stop(proc)  # timed out, or the caller went away
    if proc.returncode == 0:
        return Response(content=out, media_type="application/json")  # passed on as it is: megabytes of JSON
    if proc.returncode == 2:  # the request could not be run as asked (most often: no history stored)
        raise HTTPException(status_code=400, detail=_refusal(out, what))
    log.error("child_failed", command=command, exit_code=proc.returncode, stderr=err.decode(errors="replace")[-2000:])
    raise HTTPException(status_code=500, detail=f"{what} stopped with an error; see the application log")


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
    async with lock:
        return await _answer("backtest", request, body, "the backtest")


# --- the signal board: every method at once -------------------------------------------------


def _board(request: Request) -> dict[str, Any]:
    """What this application knows about board runs: the current or last run, and the last good result."""
    state: dict[str, Any] | None = getattr(request.app.state, "board", None)
    if state is None:
        state = request.app.state.board = {"run": None, "result": None, "task": None, "next_id": 1}
    return state


def _board_view(state: dict[str, Any]) -> dict[str, Any]:
    result = state["result"]
    return {
        "run": state["run"],
        "result": None if result is None else {k: v for k, v in result.items() if k != "bytes"},
    }


async def _board_job(
    state: dict[str, Any], run: dict[str, Any], body: dict[str, Any], env: dict[str, str], now: Any
) -> None:
    """One board run from start to end, in a child process whose progress lines are followed."""
    proc = await _spawn("board", env, "--progress")
    noise: list[str] = []

    async def follow() -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line.startswith("PROGRESS "):
                _, done, total, label = line.split(" ", 3)
                run.update(done=int(done), total=int(total), label=label)
            elif line:
                noise.append(line)
                del noise[:-20]

    async def answer() -> bytes:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(json.dumps(body).encode())
        await proc.stdin.drain()
        proc.stdin.close()
        return await proc.stdout.read()

    try:
        out, _ = await asyncio.wait_for(asyncio.gather(answer(), follow()), MAX_BOARD_SECONDS)
        await proc.wait()
        if proc.returncode == 0:
            run.update(status="done")
            state["result"] = {"id": run["id"], "finished_ms": now(), "size": len(out), "bytes": out}
        elif proc.returncode == 2:
            run.update(status="failed", error=_refusal(out, "the board"))
        else:
            log.error("board_failed", exit_code=proc.returncode, stderr="\n".join(noise)[-2000:])
            run.update(status="failed", error="the run stopped with an error; see the application log")
    except TimeoutError:
        run.update(status="failed", error=f"the run took longer than {MAX_BOARD_SECONDS:.0f} seconds and was stopped")
    except asyncio.CancelledError:
        run.update(status="failed", error="the application was stopped while the run was in progress")
        raise
    except Exception as exc:  # the run must never be left "running" for ever
        log.error("board_failed", error=f"{type(exc).__name__}: {exc}")
        run.update(status="failed", error="the run stopped with an error; see the application log")
    finally:
        await _stop(proc)
        run["finished_ms"] = now()


@router.get("/board")
async def board_state(request: Request) -> dict[str, Any]:
    """The current or last board run (with its progress) and whether a finished result is there."""
    get_service(request)
    return _board_view(_board(request))


@router.post("/board/runs", status_code=202)
async def start_board(request: Request, body: dict[str, Any] = Body(...)) -> dict[str, Any]:  # noqa: B008
    """Start a run of every strategy on every bar length.  It runs in the background: poll ``GET /board``.

    Body: ``{"symbol", "start", "end", "base_timeframe"?, "timeframes"?, "strategies"?, "execution"?, "split"?}``.
    """
    service = get_service(request)
    try:
        check_board(service.registry, BoardRequest.from_mapping(body))
    except BacktestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    state = _board(request)
    if state["run"] is not None and state["run"]["status"] == "running":
        raise HTTPException(status_code=409, detail="a board run is in progress; wait for it to finish")
    run = {
        "id": state["next_id"],
        "status": "running",
        "started_ms": service.clock.now_ms(),
        "finished_ms": None,
        "done": 0,
        "total": None,
        "label": "",
        "error": None,
        "request": body,
    }
    state["next_id"] += 1
    state["run"] = run
    state["task"] = asyncio.create_task(_board_job(state, run, body, _child_env(request), service.clock.now_ms))
    return _board_view(state)


@router.get("/board/result")
async def board_result(request: Request) -> Response:
    """The last finished board, whole."""
    get_service(request)
    result = _board(request)["result"]
    if result is None:
        raise HTTPException(status_code=404, detail="no board has been run since the application started")
    return Response(content=result["bytes"], media_type="application/json")


@router.post("/board/signals")
async def board_signals(request: Request, body: dict[str, Any] = Body(...)) -> Response:  # noqa: B008
    """Every signal of every method inside a window, with the bars to draw them on.

    Body as for a board run, ``start`` and ``end`` being the window, plus ``chart_timeframe``.
    """
    service = get_service(request)
    try:
        check_board(service.registry, BoardRequest.from_mapping(body), window=True)
    except BacktestError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    lock = _lock(request)
    try:  # a short job: it waits its turn behind a backtest instead of being turned away
        await asyncio.wait_for(lock.acquire(), MAX_WAIT_SECONDS)
    except TimeoutError:
        raise HTTPException(status_code=409, detail="another run is in progress; try again in a moment") from None
    try:
        return await _answer("signals", request, body, "the window of signals")
    finally:
        lock.release()


async def stop_runs(app: Any) -> None:
    """At shutdown: a board run still in progress is ended with its child process."""
    state: dict[str, Any] | None = getattr(app.state, "board", None)
    task: asyncio.Task[None] | None = state["task"] if state else None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
