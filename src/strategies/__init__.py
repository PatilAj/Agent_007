"""Phase 3: Strategy engine.

Strategies consume IndicatorUpdate + RegimeUpdate events and emit
SignalCandidate events. Each strategy is a stateful class that holds
just enough state to decide whether to trade on each new event.

The runner loop owns the dispatch — strategies themselves do not touch
the bus or DB. This keeps strategies pure-ish and testable.
"""
from src.strategies.base import Strategy
from src.strategies.ema_regime import EMARegimeStrategy
from src.strategies.orb_breakout import ORBBreakoutStrategy
from src.strategies.runner import run_strategy_loop

__all__ = [
    "Strategy",
    "EMARegimeStrategy",
    "ORBBreakoutStrategy",
    "run_strategy_loop",
]
