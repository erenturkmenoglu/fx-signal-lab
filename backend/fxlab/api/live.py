"""Live signals: what the page shows, the trend channels, and the settings of the rule.

The signals themselves arrive on the live stream (``/ws/live``, messages of type ``signal``);
these routes give a page its starting picture and keep the settings.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from fxlab.api.routes import get_service
from fxlab.pipeline.ingestion import IngestionService, InstrumentRuntime
from fxlab.research.live import LiveError

router = APIRouter()


def _runtime(service: IngestionService, symbol: str | None) -> InstrumentRuntime:
    active = service.registry.active()
    wanted = (symbol or (active[0].symbol if active else "")).upper()
    rt = service.runtime(wanted)
    if rt is None:
        raise HTTPException(status_code=404, detail=f"unknown or inactive instrument {wanted!r}")
    return rt


@router.get("/live/view")
async def view(
    request: Request, symbol: str | None = None, history: bool = False, seconds: bool = True
) -> JSONResponse:
    """The seconds of the trading day, its signals, the trend channels and the settings.

    ``history`` adds the one-minute closes the channels were fitted to (ten days by default);
    ``seconds=false`` leaves the day's seconds out, for a page that draws the minutes.
    """
    service = get_service(request)
    rt = _runtime(service, symbol)
    answer = await service.signals.view(service.feed(rt), history=history, seconds=seconds)
    gate = service.signal_gate(rt)
    answer["gate"] = {"allowed": gate.signals_allowed, "reason": gate.reason.value}
    answer["synthetic"] = service.synthetic
    answer["server_time_ms"] = service.clock.now_ms()
    return JSONResponse(answer)  # a day is 86 400 numbers: written out directly


@router.get("/live/channels")
async def channels(request: Request, symbol: str | None = None) -> dict[str, Any]:
    """The three trend channels as they stand now."""
    service = get_service(request)
    return await service.signals.channels(service.feed(_runtime(service, symbol)))


@router.get("/live/settings")
async def settings(request: Request) -> dict[str, Any]:
    """The settings of the rule, their defaults and what each may be."""
    return await get_service(request).signals.describe()


@router.put("/live/settings")
async def save_settings(request: Request, body: Any = Body(...)) -> dict[str, Any]:  # noqa: B008
    """Keep new settings; the day's signals are worked out again with them.  What is left out stays."""
    service = get_service(request)
    try:
        await service.signals.save_settings(body)
    except LiveError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return await service.signals.describe()
