"""
Unit tests for the regime classifier and stateful RegimeDetector.

The classifier is a pure function: given an IndicatorSnapshot, return
(label, score, rationale). We hand-pick snapshots that should fall into
each regime bucket and verify the boundaries.
"""
from __future__ import annotations

from src.core.events import RegimeLabel
from src.regime.detector import (
    HIGH_VOL_THRESHOLD,
    LOW_VOL_THRESHOLD,
    TREND_THRESHOLD,
    IndicatorSnapshot,
    RegimeDetector,
    classify,
)


# ----------------- classify() — pure function -----------------


def test_unknown_when_emas_missing():
    snap = IndicatorSnapshot(close=100.0)
    label, score, rat = classify(snap)
    assert label == RegimeLabel.UNKNOWN
    assert score == 0.0
    assert rat["reason"] == "missing_indicators"


def test_trending_up_when_ema20_above_ema50_beyond_threshold():
    close = 100.0
    # spread = (101 - 100) / 100 = 0.01, well above TREND_THRESHOLD (0.0015)
    snap = IndicatorSnapshot(close=close, ema20=101.0, ema50=100.0, atr14=0.5)
    label, score, _ = classify(snap)
    assert label == RegimeLabel.TRENDING_UP
    assert 0.0 < score <= 1.0


def test_trending_down_when_ema20_below_ema50_beyond_threshold():
    snap = IndicatorSnapshot(close=100.0, ema20=99.0, ema50=100.0, atr14=0.5)
    label, _, _ = classify(snap)
    assert label == RegimeLabel.TRENDING_DOWN


def test_range_when_spread_below_threshold():
    # spread = 0.001, below TREND_THRESHOLD 0.0015
    snap = IndicatorSnapshot(close=100.0, ema20=100.1, ema50=100.0, atr14=0.5)
    label, _, rat = classify(snap)
    assert label == RegimeLabel.RANGE
    assert rat["reason"] == "ema_spread_below_trend_threshold"


def test_high_vol_overrides_direction():
    # Strong uptrend AND huge ATR — HIGH_VOL wins
    snap = IndicatorSnapshot(
        close=100.0, ema20=105.0, ema50=100.0, atr14=2.0  # atr_pct = 2% > 1.2%
    )
    label, _, rat = classify(snap)
    assert label == RegimeLabel.HIGH_VOL
    assert rat["reason"] == "atr_above_high_vol_threshold"


def test_low_vol_only_applies_in_range():
    # Range + tiny ATR
    snap = IndicatorSnapshot(close=100.0, ema20=100.1, ema50=100.0, atr14=0.1)
    label, _, rat = classify(snap)
    assert label == RegimeLabel.LOW_VOL
    assert rat["reason"] == "range_with_low_atr"


def test_low_vol_does_not_override_trending():
    # Strong trend, low ATR — trend label still wins
    snap = IndicatorSnapshot(close=100.0, ema20=101.0, ema50=100.0, atr14=0.1)
    label, _, _ = classify(snap)
    assert label == RegimeLabel.TRENDING_UP


def test_score_increases_with_spread():
    weak = IndicatorSnapshot(close=100.0, ema20=100.2, ema50=100.0, atr14=0.5)
    strong = IndicatorSnapshot(close=100.0, ema20=102.0, ema50=100.0, atr14=0.5)
    _, weak_score, _ = classify(weak)
    _, strong_score, _ = classify(strong)
    assert strong_score > weak_score


def test_classify_thresholds_are_sane():
    """Sanity: thresholds are ordered such that LOW < HIGH and TREND is small."""
    assert LOW_VOL_THRESHOLD < HIGH_VOL_THRESHOLD
    assert TREND_THRESHOLD < 0.05  # well under 5%


# ----------------- RegimeDetector — state machine -----------------


def test_detector_emits_on_label_change():
    det = RegimeDetector(1, "X", "1minute")
    # Feed enough to enter TRENDING_UP
    det.update_indicator("ema20", 102.0)
    det.update_indicator("ema50", 100.0)
    det.update_close(102.0)
    label1, _, _ = det.classify_current()
    assert det.should_emit(label1)  # first time → emit
    assert not det.should_emit(label1)  # same label again → don't emit

    # Transition to TRENDING_DOWN
    det.update_indicator("ema20", 98.0)
    det.update_close(98.0)
    label2, _, _ = det.classify_current()
    assert label2 == RegimeLabel.TRENDING_DOWN
    assert det.should_emit(label2)


def test_detector_handles_partial_indicator_state():
    det = RegimeDetector(1, "X", "1minute")
    det.update_indicator("ema20", 100.0)  # missing ema50
    det.update_close(100.0)
    label, _, _ = det.classify_current()
    assert label == RegimeLabel.UNKNOWN
