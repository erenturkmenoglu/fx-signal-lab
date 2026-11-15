"""Engine creation, migrations and storage policies."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from fxlab.logging import get_logger
from fxlab.storage.schema import HYPERTABLES

log = get_logger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[2]


def make_engine(database_url: str) -> AsyncEngine:
    return create_async_engine(database_url, pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800)


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    cfg.attributes["configure_logging"] = False
    return cfg


def upgrade_database_sync(database_url: str, revision: str = "head") -> None:
    """Apply migrations.  Must not be called from inside a running event loop."""
    command.upgrade(_alembic_config(database_url), revision)


async def upgrade_database(database_url: str, revision: str = "head") -> None:
    """Apply migrations from async code (runs Alembic in a worker thread)."""
    await asyncio.to_thread(upgrade_database_sync, database_url, revision)


async def timescale_available(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        found = await conn.scalar(text("SELECT count(*) FROM pg_extension WHERE extname = 'timescaledb'"))
    return bool(found)


_COMPRESSION = {
    "quote_ticks": ("instrument_id", "source_ts DESC"),
    "obs_1s": ("instrument_id", "ts DESC"),
    "bars": ("instrument_id, timeframe, source", "ts DESC"),
}


async def apply_storage_policies(
    engine: AsyncEngine,
    *,
    compress_after_days: int,
    retention_ticks_days: int,
    retention_obs_days: int,
) -> dict[str, Any]:
    """Configure TimescaleDB compression and retention from settings (idempotent).

    Policies are removed and re-created on every call so that changed settings take effect.

    Each statement runs in its own savepoint: a statement that fails (for example on an
    unexpected TimescaleDB version) is reported in the returned summary and in the logs
    but never prevents the application from starting.
    """
    summary: dict[str, Any] = {"timescaledb": False, "applied": [], "errors": []}
    if not await timescale_available(engine):
        log.warning(
            "storage_policies_skipped",
            reason="TimescaleDB extension not installed: no compression/retention policies (dev/test mode)",
        )
        return summary
    summary["timescaledb"] = True

    statements: list[tuple[str, str]] = []
    for table, _column, _interval in HYPERTABLES:
        segmentby, orderby = _COMPRESSION[table]
        statements.append(
            (
                f"compression settings {table}",
                f"ALTER TABLE {table} SET (timescaledb.compress, "
                f"timescaledb.compress_segmentby = '{segmentby}', timescaledb.compress_orderby = '{orderby}')",
            )
        )
        after = compress_after_days if table != "bars" else max(compress_after_days, 30)
        # remove + add instead of "if not exists": an existing policy would silently keep its old
        # interval when COMPRESS_AFTER_DAYS changes (chunks that are already compressed stay so)
        statements.extend(
            [
                (f"compression reset {table}", f"SELECT remove_compression_policy('{table}', if_exists => TRUE)"),
                (
                    f"compression policy {table}",
                    f"SELECT add_compression_policy('{table}', INTERVAL '{after} days')",
                ),
            ]
        )
    for table, days in (("quote_ticks", retention_ticks_days), ("obs_1s", retention_obs_days)):
        statements.append((f"retention reset {table}", f"SELECT remove_retention_policy('{table}', if_exists => TRUE)"))
        if days > 0:
            statements.append(
                (
                    f"retention policy {table}",
                    f"SELECT add_retention_policy('{table}', INTERVAL '{days} days')",
                )
            )

    async with engine.connect() as conn:
        try:
            async with conn.begin():
                rows = await conn.execute(text("SELECT hypertable_name FROM timescaledb_information.hypertables"))
                hypertables = {r[0] for r in rows}
        except Exception as exc:
            hypertables = set()
            summary["errors"].append({"policy": "list hypertables", "error": str(exc)[:300]})
        summary["hypertables"] = sorted(hypertables)
        for table, _column, _interval in HYPERTABLES:
            if table not in hypertables:
                summary["errors"].append(
                    {"policy": f"hypertable {table}", "error": f"{table} is a plain table, not a hypertable"}
                )
        for label, sql in statements:
            try:
                async with conn.begin():
                    await conn.execute(text(sql))
                summary["applied"].append(label)
            except Exception as exc:
                summary["errors"].append({"policy": label, "error": str(exc)[:300]})
                log.error("storage_policy_failed", policy=label, error=str(exc)[:300])
    log.info("storage_policies", applied=len(summary["applied"]), errors=len(summary["errors"]))
    return summary
