"""Persist the strategy's suggested SL/TP on the signals row.

Without these columns the live position_watcher cannot honour each signal's
own risk levels (falls back to hardcoded 30/60).

Revision ID: 0002_signal_sl_tp
Revises: 0001_init
Create Date: 2026-05-29
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_signal_sl_tp"
down_revision: Union[str, None] = "0001_init"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("signals", sa.Column("suggested_sl_pct", sa.Float(), nullable=True))
    op.add_column("signals", sa.Column("suggested_target_pct", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("signals", "suggested_target_pct")
    op.drop_column("signals", "suggested_sl_pct")
