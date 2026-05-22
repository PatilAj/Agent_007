"""Initial schema: ORM tables + TimescaleDB hypertables

Revision ID: 0001_init
Revises:
Create Date: 2026-05-19

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_init"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- Enable TimescaleDB extension (no-op if not available; will fail loudly) ---
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")

    # --- instruments ---
    op.create_table(
        "instruments",
        sa.Column("instrument_token", sa.BigInteger, primary_key=True),
        sa.Column("exchange_token", sa.BigInteger, nullable=True),
        sa.Column("tradingsymbol", sa.String(64), nullable=False, index=True),
        sa.Column("name", sa.String(128), nullable=True),
        sa.Column("exchange", sa.String(16), nullable=False),
        sa.Column("segment", sa.String(16), nullable=False),
        sa.Column("instrument_type", sa.String(16), nullable=False),
        sa.Column("expiry", sa.DateTime(timezone=True), nullable=True),
        sa.Column("strike", sa.Numeric(12, 2), nullable=True),
        sa.Column("lot_size", sa.Integer, server_default="1", nullable=False),
        sa.Column("tick_size", sa.Float, server_default="0.05", nullable=False),
        sa.Column("underlying", sa.String(64), nullable=True, index=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_instruments_expiry", "instruments", ["expiry"])
    op.create_index(
        "ix_instruments_underlying_expiry_type",
        "instruments",
        ["underlying", "expiry", "instrument_type"],
    )

    # --- kite tokens ---
    op.create_table(
        "kite_tokens",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("api_key", sa.String(64), nullable=False),
        sa.Column("access_token", sa.Text, nullable=False),
        sa.Column("public_token", sa.Text, nullable=True),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.true(), nullable=False),
    )

    # --- signals ---
    op.create_table(
        "signals",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("event_id", sa.String(36), unique=True, nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column("strategy_id", sa.String(64), nullable=False, index=True),
        sa.Column("underlying", sa.String(64), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("option_type", sa.String(4), nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("rationale", postgresql.JSONB, nullable=False),
        sa.Column("indicators_snapshot", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("accepted", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("rejection_reason", sa.String(256), nullable=True),
    )

    # --- orders ---
    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("client_order_id", sa.String(36), unique=True, nullable=False),
        sa.Column("broker_order_id", sa.String(32), unique=True, nullable=True),
        sa.Column("signal_event_id", sa.String(36), sa.ForeignKey("signals.event_id"), nullable=True),
        sa.Column("strategy_id", sa.String(64), nullable=False),
        sa.Column("instrument_token", sa.BigInteger, nullable=False),
        sa.Column("tradingsymbol", sa.String(64), nullable=False, index=True),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("order_type", sa.String(16), nullable=False),
        sa.Column("price", sa.Numeric(12, 2), nullable=True),
        sa.Column("trigger_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("product", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, index=True),
        sa.Column("filled_qty", sa.Integer, server_default="0", nullable=False),
        sa.Column("avg_fill_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("rejection_reason", sa.Text, nullable=True),
        sa.Column("placed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    # --- trades ---
    op.create_table(
        "trades",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("entry_order_id", sa.BigInteger, sa.ForeignKey("orders.id"), nullable=False),
        sa.Column("exit_order_id", sa.BigInteger, sa.ForeignKey("orders.id"), nullable=True),
        sa.Column("strategy_id", sa.String(64), nullable=False, index=True),
        sa.Column("tradingsymbol", sa.String(64), nullable=False),
        sa.Column("side", sa.String(8), nullable=False),
        sa.Column("qty", sa.Integer, nullable=False),
        sa.Column("entry_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("exit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("gross_pnl", sa.Numeric(14, 2), nullable=True),
        sa.Column("net_pnl", sa.Numeric(14, 2), nullable=True),
        sa.Column("r_multiple", sa.Float, nullable=True),
        sa.Column("hold_seconds", sa.Integer, nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.String(64), nullable=True),
    )

    # --- risk events ---
    op.create_table(
        "risk_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("gate", sa.String(32), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("context", postgresql.JSONB, nullable=False, server_default="{}"),
    )

    # --- daily pnl ---
    op.create_table(
        "daily_pnl",
        sa.Column("trade_date", sa.DateTime(timezone=True), primary_key=True),
        sa.Column("gross_pnl", sa.Numeric(14, 2), server_default="0", nullable=False),
        sa.Column("net_pnl", sa.Numeric(14, 2), server_default="0", nullable=False),
        sa.Column("trade_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("win_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("loss_count", sa.Integer, server_default="0", nullable=False),
        sa.Column("consecutive_losses", sa.Integer, server_default="0", nullable=False),
        sa.Column("halted", sa.Boolean, server_default=sa.false(), nullable=False),
        sa.Column("halt_reason", sa.String(128), nullable=True),
    )

    # --- TimescaleDB hypertables: ticks ---
    op.execute(
        """
        CREATE TABLE ticks (
            ts            TIMESTAMPTZ NOT NULL,
            instrument_token BIGINT NOT NULL,
            ltp           NUMERIC(12,2) NOT NULL,
            bid           NUMERIC(12,2),
            ask           NUMERIC(12,2),
            volume        BIGINT,
            oi            BIGINT
        );
        """
    )
    op.execute("SELECT create_hypertable('ticks', 'ts', chunk_time_interval => INTERVAL '7 days');")
    op.execute("CREATE INDEX ix_ticks_token_ts ON ticks (instrument_token, ts DESC);")

    # --- candles ---
    op.execute(
        """
        CREATE TABLE candles (
            ts            TIMESTAMPTZ NOT NULL,
            instrument_token BIGINT NOT NULL,
            resolution    VARCHAR(16) NOT NULL,
            o             NUMERIC(12,2) NOT NULL,
            h             NUMERIC(12,2) NOT NULL,
            l             NUMERIC(12,2) NOT NULL,
            c             NUMERIC(12,2) NOT NULL,
            v             BIGINT NOT NULL DEFAULT 0,
            oi            BIGINT,
            PRIMARY KEY (instrument_token, resolution, ts)
        );
        """
    )
    op.execute("SELECT create_hypertable('candles', 'ts', chunk_time_interval => INTERVAL '30 days');")
    op.execute("CREATE INDEX ix_candles_token_res_ts ON candles (instrument_token, resolution, ts DESC);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS candles CASCADE;")
    op.execute("DROP TABLE IF EXISTS ticks CASCADE;")
    op.drop_table("daily_pnl")
    op.drop_table("risk_events")
    op.drop_table("trades")
    op.drop_table("orders")
    op.drop_table("signals")
    op.drop_table("kite_tokens")
    op.drop_index("ix_instruments_underlying_expiry_type", table_name="instruments")
    op.drop_index("ix_instruments_expiry", table_name="instruments")
    op.drop_table("instruments")
