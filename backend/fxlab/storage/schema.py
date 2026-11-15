"""Database schema (SQLAlchemy Core).

Time-series tables (``quote_ticks``, ``obs_1s``, ``bars``) become TimescaleDB
hypertables when the extension is available (see the initial migration); on plain
PostgreSQL the very same tables work unchanged, which is what the test-suite uses.

Provenance is explicit everywhere:

* ``obs_1s.source``  - ``live`` | ``backfill`` | ``mock``
* ``bars.source``    - ``live`` (built by this system from ``obs_1s``) | ``massive_agg``
  (provider aggregates, BID based) | ``mock``;  ``bars.price_basis`` - ``mid`` | ``bid``
* ``quote_ticks.flags`` - bit 1 late (out of order), bit 2 backfilled, bit 4 synthetic
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB

TICK_FLAG_LATE = 1
TICK_FLAG_BACKFILL = 2
TICK_FLAG_SYNTHETIC = 4

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

TS = DateTime(timezone=True)
F8 = Float(53)

instruments = Table(
    "instruments",
    metadata,
    Column("id", SmallInteger, Identity(), primary_key=True),
    Column("symbol", Text, nullable=False, unique=True),
    Column("base", Text, nullable=False),
    Column("quote", Text, nullable=False),
    Column("pip_size", F8, nullable=False),
    Column("price_precision", SmallInteger, nullable=False),
    Column("active", Boolean, nullable=False, server_default=false()),
    # unverified | supported | unsupported  (result of the provider reference check)
    Column("provider_status", Text, nullable=False, server_default="unverified"),
    Column("provider_checked_at", TS),
    Column("provider_detail", JSONB),
    Column("config", JSONB, nullable=False),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
)

quote_ticks = Table(
    "quote_ticks",
    metadata,
    Column("instrument_id", SmallInteger, ForeignKey("instruments.id"), nullable=False),
    Column("source_ts", TS, nullable=False),
    Column("recv_ts", TS),  # NULL for backfilled ticks (no receipt time exists)
    Column("bid", F8, nullable=False),
    Column("ask", F8, nullable=False),
    Column("exchange", Integer),
    Column("flags", SmallInteger, nullable=False, server_default="0"),
    Index("ix_quote_ticks_instrument_source_ts", "instrument_id", "source_ts"),
)

obs_1s = Table(
    "obs_1s",
    metadata,
    Column("instrument_id", SmallInteger, ForeignKey("instruments.id"), primary_key=True),
    Column("ts", TS, primary_key=True),  # window start; window = [ts, ts + 1s)
    Column("status", Text, nullable=False),
    Column("bid", F8),
    Column("ask", F8),
    Column("mid_open", F8),
    Column("mid_high", F8),
    Column("mid_low", F8),
    Column("n_quotes", Integer, nullable=False, server_default="0"),
    Column("quote_ts", TS),
    Column("age_ms", BigInteger),
    Column("source", Text, nullable=False, server_default="live"),
    CheckConstraint("status IN ('fresh', 'carried', 'stale', 'missing')", name="status_valid"),
)

bars = Table(
    "bars",
    metadata,
    Column("instrument_id", SmallInteger, ForeignKey("instruments.id"), primary_key=True),
    Column("timeframe", Text, primary_key=True),
    Column("source", Text, primary_key=True),
    Column("ts", TS, primary_key=True),  # bar start
    Column("open", F8, nullable=False),
    Column("high", F8, nullable=False),
    Column("low", F8, nullable=False),
    Column("close", F8, nullable=False),
    Column("bid_close", F8),
    Column("ask_close", F8),
    Column("n_quotes", Integer),
    Column("n_fresh", Integer),
    Column("n_valid", Integer),
    Column("n_expected", Integer),
    Column("n_backfilled", Integer),
    Column("complete", Boolean),
    Column("price_basis", Text, nullable=False),
)

connection_events = Table(
    "connection_events",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("ts", TS, nullable=False, index=True),
    Column("provider", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("detail", JSONB, nullable=False),
)

data_gaps = Table(
    "data_gaps",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("instrument_id", SmallInteger, ForeignKey("instruments.id"), nullable=False),
    Column("start_ts", TS, nullable=False),
    Column("end_ts", TS, nullable=False),  # exclusive
    Column("kind", Text, nullable=False),  # hole | feed_outage
    # open | filled | no_provider_data | unfillable | failed
    Column("status", Text, nullable=False, server_default="open"),
    Column("detected_at", TS, nullable=False, server_default=func.now()),
    Column("resolved_at", TS),
    Column("detail", JSONB),
    UniqueConstraint("instrument_id", "start_ts", "kind"),
    Index("ix_data_gaps_status", "status"),
)

history_jobs = Table(
    "history_jobs",
    metadata,
    Column("id", BigInteger, Identity(), primary_key=True),
    Column("kind", Text, nullable=False),  # aggregates
    Column("symbol", Text, nullable=False),
    Column("timeframe", Text, nullable=False),
    Column("start_ts", TS, nullable=False),
    Column("end_ts", TS, nullable=False),
    Column("status", Text, nullable=False, server_default="queued"),  # queued|running|done|failed
    Column("rows_written", Integer, nullable=False, server_default="0"),
    Column("pages", Integer, nullable=False, server_default="0"),
    Column("error", Text),
    Column("created_at", TS, nullable=False, server_default=func.now()),
    Column("started_at", TS),
    Column("finished_at", TS),
)

ingest_stats_1m = Table(
    "ingest_stats_1m",
    metadata,
    Column("instrument_id", SmallInteger, ForeignKey("instruments.id"), primary_key=True),
    Column("ts", TS, primary_key=True),  # minute start
    Column("n_received", Integer, nullable=False),
    Column("n_accepted", Integer, nullable=False),
    Column("n_duplicate", Integer, nullable=False),
    Column("n_late", Integer, nullable=False),
    Column("n_rejected", Integer, nullable=False),
    Column("rejected", JSONB),
    Column("lat_n", Integer),
    Column("lat_p50_ms", F8),
    Column("lat_p95_ms", F8),
    Column("lat_p99_ms", F8),
    Column("lat_max_ms", F8),
    Column("lat_resolution_ms", Integer),
)

app_meta = Table(
    "app_meta",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", JSONB, nullable=False),
    Column("updated_at", TS, nullable=False, server_default=func.now()),
)

#: tables that become hypertables: (table, time column, chunk interval)
HYPERTABLES: tuple[tuple[str, str, str], ...] = (
    ("quote_ticks", "source_ts", "1 day"),
    ("obs_1s", "ts", "1 day"),
    ("bars", "ts", "30 days"),
)
