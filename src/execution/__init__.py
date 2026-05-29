"""Phase 4: Execution layer.

Receives OrderRequest events, fills (paper or real), records the lifecycle
in Orders + Trades + DailyPnL, and emits OrderUpdate so downstream listeners
(position tracker, dashboards) stay in sync.

For now only PaperExecutor is implemented. ShadowExecutor and LiveExecutor
swap in later — they share the same `run_executor_loop` entry point.
"""
from src.execution.paper import PaperExecutor, run_executor_loop
from src.execution.position_watcher import run_position_watcher

__all__ = ["PaperExecutor", "run_executor_loop", "run_position_watcher"]
