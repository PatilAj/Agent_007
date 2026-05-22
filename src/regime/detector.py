"""
Market regime classifier.

Pure-compute classifier that turns a snapshot of recent indicator values
(EMA20, EMA50, ATR14, last close) into a RegimeLabel + confidence score.

The classification rules are intentionally simple for v1:

  • Direction & trend strength: signed EMA spread, normalized by close.
    abs(spread) >= TREND_THRESHOLD → TRENDING_UP / TRENDING_DOWN
    abs(spread) <  TREND_THRESHOLD → RANGE
  • Volatility overlay: ATR / close.
    > HIGH_VOL_THRESHOLD → HIGH_VOL (overrides direction if very pronounced)
    < LOW_VOL_THRESHOLD  → LOW_VOL  (only when direction is RANGE)

Why not ADX? Equivalent information for our purposes (EMA spread as
trend-strength proxy is well-correlated with ADX). Keeping ADX out of v1
to keep the math simple and incremental. Easy to add later.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from src.core.events import RegimeLabel

# Thresholds (% of close price)
TREND_THRESHOLD = 0.0015   # 0.15% — EMA20 vs EMA50 gap to call it trending
HIGH_VOL_THRESHOLD = 0.012  # 1.2% — ATR/close above this is "high vol"
LOW_VOL_THRESHOLD = 0.002   # 0.2% — ATR/close below this in range = "low vol"


@dataclass
class IndicatorSnapshot:
    """A snapshot of the latest indicator values for one (token, resolution)."""

    close: float
    ema20: float | None = None
    ema50: float | None = None
    atr14: float | None = None
    rsi14: float | None = None
    vwap: float | None = None

    @property
    def has_required(self) -> bool:
        """True if we have enough indicators to classify."""
        return self.ema20 is not None and self.ema50 is not None and self.close > 0


def classify(snap: IndicatorSnapshot) -> tuple[RegimeLabel, float, dict[str, Any]]:
    """Pure function: return (label, score 0..1, rationale dict)."""
    if not snap.has_required:
        return RegimeLabel.UNKNOWN, 0.0, {"reason": "missing_indicators"}

    # Direction: signed spread, normalized
    assert snap.ema20 is not None and snap.ema50 is not None  # has_required
    spread = (snap.ema20 - snap.ema50) / snap.close
    abs_spread = abs(spread)

    # Volatility: ATR as % of price
    atr_pct: float | None = None
    if snap.atr14 is not None and snap.close > 0:
        atr_pct = snap.atr14 / snap.close

    # Score = how strongly we cleared the trend threshold, capped to [0, 1]
    # Smooth ramp: at 1x threshold -> 0.5, at 3x -> ~1.0
    score = min(1.0, abs_spread / (TREND_THRESHOLD * 2))

    rationale: dict[str, Any] = {
        "ema20": snap.ema20,
        "ema50": snap.ema50,
        "spread_pct": spread,
        "atr_pct": atr_pct,
    }

    # High-vol override: when volatility is extreme, label dominates direction
    if atr_pct is not None and atr_pct >= HIGH_VOL_THRESHOLD:
        rationale["reason"] = "atr_above_high_vol_threshold"
        return RegimeLabel.HIGH_VOL, min(1.0, atr_pct / HIGH_VOL_THRESHOLD - 1.0 + 0.5), rationale

    if abs_spread >= TREND_THRESHOLD:
        label = RegimeLabel.TRENDING_UP if spread > 0 else RegimeLabel.TRENDING_DOWN
        rationale["reason"] = "ema_spread_above_trend_threshold"
        return label, score, rationale

    # Range — check low-vol overlay
    if atr_pct is not None and atr_pct < LOW_VOL_THRESHOLD:
        rationale["reason"] = "range_with_low_atr"
        return RegimeLabel.LOW_VOL, 0.6, rationale

    rationale["reason"] = "ema_spread_below_trend_threshold"
    return RegimeLabel.RANGE, 1.0 - score, rationale


class RegimeDetector:
    """Stateful per-(token, resolution) regime classifier.

    Holds the latest indicator snapshot, calls classify() on each update,
    emits a RegimeUpdate only when the label changes (or score crosses a
    notable boundary) to avoid spamming downstream.
    """

    def __init__(self, instrument_token: int, symbol: str, resolution: str):
        self.instrument_token = instrument_token
        self.symbol = symbol
        self.resolution = resolution
        self._snap = IndicatorSnapshot(close=0.0)
        self._last_label: RegimeLabel | None = None

    def update_indicator(self, name: str, value: float | None) -> None:
        """Record the latest value for a named indicator."""
        if name == "ema20":
            self._snap.ema20 = value
        elif name == "ema50":
            self._snap.ema50 = value
        elif name == "atr14":
            self._snap.atr14 = value
        elif name == "rsi14":
            self._snap.rsi14 = value
        elif name == "vwap":
            self._snap.vwap = value
        # Unknown indicator names are silently ignored — additive forward-compat

    def update_close(self, close: float) -> None:
        self._snap.close = close

    def classify_current(self) -> tuple[RegimeLabel, float, dict[str, Any]]:
        return classify(self._snap)

    def should_emit(self, new_label: RegimeLabel) -> bool:
        """De-duplicate: only emit when the label transitions."""
        changed = new_label != self._last_label
        if changed:
            self._last_label = new_label
        return changed
