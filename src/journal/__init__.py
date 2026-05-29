"""Phase 3: Signal & order journal.

Persistent audit trail of every SignalCandidate the system emits, plus
the verdict that the risk engine ultimately produces (accepted / rejected,
with reason).
"""
from src.journal.order_journal import (
    close_trade,
    insert_order_from_request,
    mark_order_filled,
    open_trade,
    upsert_daily_pnl,
)
from src.journal.signal_journal import (
    mark_signal_outcome,
    persist_signal,
    recent_signals,
)

__all__ = [
    "persist_signal",
    "mark_signal_outcome",
    "recent_signals",
    "insert_order_from_request",
    "mark_order_filled",
    "open_trade",
    "close_trade",
    "upsert_daily_pnl",
]
