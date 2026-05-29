"""Phase 3: Option contract selection.

Given a SignalCandidate (which names the underlying + CE/PE), pick the
specific option contract (strike + expiry) to trade.
"""
from src.options.selector import (
    OptionSelectionError,
    UNDERLYING_NAME_MAP,
    pick_expiry,
    pick_strike,
    select_option_for_signal,
)

__all__ = [
    "OptionSelectionError",
    "UNDERLYING_NAME_MAP",
    "pick_expiry",
    "pick_strike",
    "select_option_for_signal",
]
