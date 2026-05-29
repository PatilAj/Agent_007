"""
Strategy base interface.

Every strategy is a stateful class with three entry points:
  - on_bar(bar)          → SignalCandidate | None   (raw OHLC, e.g. ORB)
  - on_indicator(update) → SignalCandidate | None   (EMA/RSI/ATR updates)
  - on_regime(update)    → SignalCandidate | None   (regime transitions)

A strategy returns a SignalCandidate when it wants to trade, or None
otherwise. Strategies should NOT publish to the bus, write to the DB,
or call the broker — the runner handles all I/O.

Strategies maintain their own state (recent indicators, range trackers,
cooldowns) — keep it minimal and explicit so behaviour is easy to reason about.

Each entry point has a default no-op implementation so strategies only
override the ones they actually use.
"""
from __future__ import annotations

from abc import ABC

from src.core.events import BarEvent, IndicatorUpdate, RegimeUpdate, SignalCandidate


class Strategy(ABC):
    """Abstract base for strategies. All three entry points are optional."""

    #: Unique strategy identifier (used in journal queries + telemetry).
    id: str

    def on_bar(self, bar: BarEvent) -> SignalCandidate | None:
        """React to a closed bar (OHLC). Default: ignore."""
        return None

    def on_indicator(self, update: IndicatorUpdate) -> SignalCandidate | None:
        """React to an indicator value change. Default: ignore."""
        return None

    def on_regime(self, update: RegimeUpdate) -> SignalCandidate | None:
        """React to a regime label change. Default: ignore."""
        return None
