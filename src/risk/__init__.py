"""Phase 4: Risk engine, position tracker, daily PnL gates.

The risk engine sits between the strategy (which produces SignalCandidate)
and the executor (which sends OrderRequest to the broker). Its job is to
either approve a signal — sized, gated, with SL/TP attached — or reject
it with an audited reason.
"""
from src.risk.engine import RiskEngine, RiskVerdict, run_risk_loop
from src.risk.position_tracker import PositionTracker, OpenPosition

__all__ = [
    "RiskEngine",
    "RiskVerdict",
    "run_risk_loop",
    "PositionTracker",
    "OpenPosition",
]
