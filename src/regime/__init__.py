"""Phase 2: Market regime detection.

Stateful per-instrument classifier that consumes IndicatorUpdate events
and emits RegimeUpdate events labeling the current market regime
(trending / range / high-vol / low-vol).
"""
from src.regime.detector import RegimeDetector, classify
from src.regime.loop import run_regime_loop

__all__ = ["RegimeDetector", "classify", "run_regime_loop"]
