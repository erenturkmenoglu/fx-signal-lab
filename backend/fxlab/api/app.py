"""FastAPI application: REST API, live WebSocket stream and (optionally) the built dashboard.

The ingestion service runs inside this process (one process, one provider
connection).  If start-up fails for a reason the operator must fix - most notably the
data-mode guard - the API still comes up in a degraded state and reports the reason,
instead of crash-looping without explanation.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from fxlab import __version__
from fxlab.api.routes import router
from fxlab.config import Settings, get_settings
from fxlab.domain.instruments import InstrumentRegistry
from fxlab.logging import configure_logging, get_logger, register_secret
from fxlab.pipeline.ingestion import IngestionService
from fxlab.pipeline.payloads import obs_payload
from fxlab.storage.db import make_engine

log = get_logger(__name__)

LIVE_BACKLOG_SECONDS = 300


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(settings.log_level, settings.log_format)
        register_secret(settings.api_key)
        app.state.startup_error = None
        app.state.service = None
        engine = make_engine(settings.database_url)
        service: IngestionService | None = None
        try:
            registry = InstrumentRegistry.from_yaml(settings.instruments_file)
            service = IngestionService(settings, registry, engine)
            await service.start()
            app.state.service = service
        except Exception as exc:
            app.state.startup_error = f"{type(exc).__name__}: {exc}"
            log.critical("startup_failed", error=app.state.startup_error)
            if service is not None:
                with contextlib.suppress(Exception):
                    await service.stop()
                service = None
        try:
            yield
        finally:
            if service is not None:
                await service.stop()
            await engine.dispose()

    app = FastAPI(
        title="FX Signal Lab",
        version=__version__,
        description="Market-data ingestion, 1-second observation grid and monitoring API (Phase 1).",
        lifespan=lifespan,
    )
    app.include_router(router, prefix="/api")

    @app.websocket("/ws/live")
    async def live(ws: WebSocket) -> None:
        """Live stream: a ``hello`` snapshot, then ``obs`` / ``status`` / ``event`` / ``resync`` messages."""
        await ws.accept()
        service: IngestionService | None = app.state.service
        if service is None or not service.started:
            await ws.send_json({"type": "error", "error": app.state.startup_error or "service not running"})
            await ws.close(code=1011)
            return
        queue = service.hub.subscribe()
        try:
            await ws.send_json(_hello(service))
            receiver = asyncio.create_task(_drain(ws))
            try:
                while True:
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait({getter, receiver}, return_when=asyncio.FIRST_COMPLETED)
                    if receiver in done:
                        getter.cancel()
                        break
                    message = getter.result()
                    if message.get("type") == "resync":
                        message = _hello(service)
                    await ws.send_json(message)
            finally:
                receiver.cancel()
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            service.hub.unsubscribe(queue)

    static_dir = settings.static_dir
    if static_dir is not None and Path(static_dir).is_dir():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="dashboard")
    return app


def _hello(service: IngestionService) -> dict[str, Any]:
    recent = {}
    for item in service.status()["instruments"]:
        rt = service.runtime(item["symbol"])
        if rt is not None:
            recent[item["symbol"]] = [
                obs_payload(o, rt.inst) for o in service.recent_observations(item["symbol"], LIVE_BACKLOG_SECONDS)
            ]
    return {"type": "hello", "status": service.status(), "recent": recent}


async def _drain(ws: WebSocket) -> None:
    """Consume client frames so a disconnect is noticed promptly."""
    with contextlib.suppress(WebSocketDisconnect, RuntimeError):
        while True:
            await ws.receive_text()
