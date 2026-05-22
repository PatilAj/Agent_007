"""Phase 2: Technical indicators.

Stateful, incremental indicator computation per (instrument, resolution).
Consumes BarEvent, emits IndicatorUpdate.
"""
from src.indicators.calculator import IndicatorCalculator
from src.indicators.loop import run_indicator_loop, warmup_calculators

__all__ = ["IndicatorCalculator", "run_indicator_loop", "warmup_calculators"]
