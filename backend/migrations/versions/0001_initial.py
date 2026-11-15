"""Initial schema: instruments, ticks, 1-second observations, bars, monitoring tables.

Revision ID: 0001
Revises:
Create Date: 2026-11-14

When the TimescaleDB extension is available the three time-series tables are
converted to hypertables.  On plain PostgreSQL the migration still succeeds and the
tables stay ordinary tables (development / test mode; a notice is printed).
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
F8 = sa.Float(53)

HYPERTABLES = (
    ("quote_ticks", "source_ts", "1 day"),
    ("obs_1s", "ts", "1 day"),
    ("bars", "ts", "30 days"),
)


def _enable_timescaledb(bind: sa.Connection) -> bool:
    """Enable the extension before any table exists.  False means: plain PostgreSQL tables."""
    plain = "Tables are created as plain PostgreSQL tables; compression and retention policies will not apply."
    if bind.execute(sa.text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")).scalar():
        return True  # already enabled in this database (the timescale/timescaledb image does that)
    if not bind.execute(sa.text("SELECT 1 FROM pg_available_extensions WHERE name = 'timescaledb'")).scalar():
        print(  # noqa: T201 - migrations talk to the operator
            "NOTICE: TimescaleDB extension is not available on this server. "
            "Tables are created as plain PostgreSQL tables (fine for development/tests; "
            "use the timescale/timescaledb image in production)."
        )
        return False
    # Without the preloaded library CREATE EXTENSION fails with a FATAL error that closes the
    # connection, which no savepoint can catch - so check first.
    preloaded = bind.execute(sa.text("SELECT current_setting('shared_preload_libraries', true)")).scalar() or ""
    if "timescaledb" not in preloaded:
        print(  # noqa: T201
            "WARNING: TimescaleDB is installed on this server but not loaded "
            f"(shared_preload_libraries = '{preloaded}'). {plain}"
        )
        return False
    try:
        with bind.begin_nested():
            bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
    except Exception as exc:  # noqa: BLE001
        print(f"WARNING: TimescaleDB could not be enabled in this database ({exc}). {plain}")  # noqa: T201
        return False
    return True


def upgrade() -> None:
    bind = op.get_bind()
    timescale = _enable_timescaledb(bind)

    op.create_table(
        "instruments",
        sa.Column("id", sa.SmallInteger, sa.Identity(), nullable=False),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("base", sa.Text, nullable=False),
        sa.Column("quote", sa.Text, nullable=False),
        sa.Column("pip_size", F8, nullable=False),
        sa.Column("price_precision", sa.SmallInteger, nullable=False),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("provider_status", sa.Text, nullable=False, server_default="unverified"),
        sa.Column("provider_checked_at", TS),
        sa.Column("provider_detail", JSONB),
        sa.Column("config", JSONB, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_instruments"),
        sa.UniqueConstraint("symbol", name="uq_instruments_symbol"),
    )

    op.create_table(
        "quote_ticks",
        sa.Column("instrument_id", sa.SmallInteger, nullable=False),
        sa.Column("source_ts", TS, nullable=False),
        sa.Column("recv_ts", TS),
        sa.Column("bid", F8, nullable=False),
        sa.Column("ask", F8, nullable=False),
        sa.Column("exchange", sa.Integer),
        sa.Column("flags", sa.SmallInteger, nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_quote_ticks_instrument_id_instruments"),
    )
    op.create_index("ix_quote_ticks_instrument_source_ts", "quote_ticks", ["instrument_id", "source_ts"])

    op.create_table(
        "obs_1s",
        sa.Column("instrument_id", sa.SmallInteger, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("bid", F8),
        sa.Column("ask", F8),
        sa.Column("mid_open", F8),
        sa.Column("mid_high", F8),
        sa.Column("mid_low", F8),
        sa.Column("n_quotes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("quote_ts", TS),
        sa.Column("age_ms", sa.BigInteger),
        sa.Column("source", sa.Text, nullable=False, server_default="live"),
        sa.PrimaryKeyConstraint("instrument_id", "ts", name="pk_obs_1s"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_obs_1s_instrument_id_instruments"),
        sa.CheckConstraint("status IN ('fresh', 'carried', 'stale', 'missing')", name="ck_obs_1s_status_valid"),
    )

    op.create_table(
        "bars",
        sa.Column("instrument_id", sa.SmallInteger, nullable=False),
        sa.Column("timeframe", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("open", F8, nullable=False),
        sa.Column("high", F8, nullable=False),
        sa.Column("low", F8, nullable=False),
        sa.Column("close", F8, nullable=False),
        sa.Column("bid_close", F8),
        sa.Column("ask_close", F8),
        sa.Column("n_quotes", sa.Integer),
        sa.Column("n_fresh", sa.Integer),
        sa.Column("n_valid", sa.Integer),
        sa.Column("n_expected", sa.Integer),
        sa.Column("n_backfilled", sa.Integer),
        sa.Column("complete", sa.Boolean),
        sa.Column("price_basis", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("instrument_id", "timeframe", "source", "ts", name="pk_bars"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_bars_instrument_id_instruments"),
    )

    op.create_table(
        "connection_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("provider", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("detail", JSONB, nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_connection_events"),
    )
    op.create_index("ix_connection_events_ts", "connection_events", ["ts"])

    op.create_table(
        "data_gaps",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("instrument_id", sa.SmallInteger, nullable=False),
        sa.Column("start_ts", TS, nullable=False),
        sa.Column("end_ts", TS, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("detected_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", TS),
        sa.Column("detail", JSONB),
        sa.PrimaryKeyConstraint("id", name="pk_data_gaps"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_data_gaps_instrument_id_instruments"),
        sa.UniqueConstraint("instrument_id", "start_ts", "kind", name="uq_data_gaps_instrument_id"),
    )
    op.create_index("ix_data_gaps_status", "data_gaps", ["status"])

    op.create_table(
        "history_jobs",
        sa.Column("id", sa.BigInteger, sa.Identity(), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("symbol", sa.Text, nullable=False),
        sa.Column("timeframe", sa.Text, nullable=False),
        sa.Column("start_ts", TS, nullable=False),
        sa.Column("end_ts", TS, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("rows_written", sa.Integer, nullable=False, server_default="0"),
        sa.Column("pages", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
        sa.PrimaryKeyConstraint("id", name="pk_history_jobs"),
    )

    op.create_table(
        "ingest_stats_1m",
        sa.Column("instrument_id", sa.SmallInteger, nullable=False),
        sa.Column("ts", TS, nullable=False),
        sa.Column("n_received", sa.Integer, nullable=False),
        sa.Column("n_accepted", sa.Integer, nullable=False),
        sa.Column("n_duplicate", sa.Integer, nullable=False),
        sa.Column("n_late", sa.Integer, nullable=False),
        sa.Column("n_rejected", sa.Integer, nullable=False),
        sa.Column("rejected", JSONB),
        sa.Column("lat_n", sa.Integer),
        sa.Column("lat_p50_ms", F8),
        sa.Column("lat_p95_ms", F8),
        sa.Column("lat_p99_ms", F8),
        sa.Column("lat_max_ms", F8),
        sa.Column("lat_resolution_ms", sa.Integer),
        sa.PrimaryKeyConstraint("instrument_id", "ts", name="pk_ingest_stats_1m"),
        sa.ForeignKeyConstraint(["instrument_id"], ["instruments.id"], name="fk_ingest_stats_1m_instrument_id_instruments"),
    )

    op.create_table(
        "app_meta",
        sa.Column("key", sa.Text, nullable=False),
        sa.Column("value", JSONB, nullable=False),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("key", name="pk_app_meta"),
    )

    # --- TimescaleDB (optional) --------------------------------------------------------
    if timescale:
        for table, column, interval in HYPERTABLES:
            # generalized hypertable API (TimescaleDB >= 2.13).  Each conversion runs in its own
            # savepoint: if one fails the schema is still usable (plain table) and the problem is
            # reported here and on the System Health panel instead of blocking start-up.
            try:
                with bind.begin_nested():
                    bind.execute(
                        sa.text(
                            f"SELECT create_hypertable('{table}', by_range('{column}', INTERVAL '{interval}'), "
                            # our own keys/indexes already lead with instrument_id + time
                            "create_default_indexes => FALSE, if_not_exists => TRUE)"
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                print(  # noqa: T201
                    f"WARNING: could not convert {table} to a hypertable ({exc}). It stays a plain table; "
                    "compression and retention policies will not apply to it."
                )


def downgrade() -> None:
    for table in (
        "app_meta",
        "ingest_stats_1m",
        "history_jobs",
        "data_gaps",
        "connection_events",
        "bars",
        "obs_1s",
        "quote_ticks",
        "instruments",
    ):
        op.drop_table(table)
