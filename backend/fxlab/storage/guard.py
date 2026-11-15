"""Data-mode guard: one database holds data from exactly one kind of source.

The first start writes the data mode (provider + feed host) into ``app_meta``.  Any
later start with a different mode is refused.  This makes it impossible to

* pollute a database of real market data with synthetic mock prices, or
* present a database of mock/test data as real after switching providers,

by accident.  Use a separate database per mode (e.g. ``fxlab`` and ``fxlab_mock``).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncEngine

from fxlab.config import Settings
from fxlab.storage import repo

META_KEY = "data_mode"


class DataModeMismatchError(RuntimeError):
    pass


def data_mode(settings: Settings) -> dict[str, Any]:
    if settings.data_provider == "mock":
        return {"provider": "mock", "synthetic": True, "feed_host": None}
    return {
        "provider": "massive",
        "synthetic": False,
        "feed_host": urlparse(settings.massive_ws_url).hostname,
    }


async def enforce_data_mode(engine: AsyncEngine, settings: Settings) -> dict[str, Any]:
    wanted = data_mode(settings)
    async with engine.begin() as conn:
        existing = await repo.get_meta(conn, META_KEY)
        if existing is None:
            await repo.set_meta(conn, META_KEY, wanted)
            return wanted
    if existing != wanted:
        raise DataModeMismatchError(
            f"This database was initialised for data mode {existing} but the application is "
            f"configured for {wanted}. Refusing to mix data from different sources in one "
            "database. Point DATABASE_URL at a different database (for example 'fxlab_mock' "
            "for DATA_PROVIDER=mock), or drop this one if its contents are disposable."
        )
    return wanted
