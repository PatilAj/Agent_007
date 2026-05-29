"""
Backtest report.

Aggregates a list of BTTrade into per-strategy and overall performance metrics
and renders a plain-text report (phone/terminal friendly).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from src.backtest.simulator import BTTrade


@dataclass
class Stats:
    label: str
    n: int
    wins: int
    losses: int
    win_rate: float
    net_pnl: float
    gross_pnl: float
    costs: float
    avg_win: float
    avg_loss: float
    avg_pnl: float
    profit_factor: float
    max_drawdown: float
    avg_hold_min: float
    exit_reasons: Counter


def _max_drawdown(trades: list[BTTrade]) -> float:
    """Largest peak-to-trough drop on the cumulative net-P&L curve."""
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in sorted(trades, key=lambda x: x.exit_ts):
        equity += t.net_pnl
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
    return max_dd


def compute_stats(trades: list[BTTrade], label: str) -> Stats:
    n = len(trades)
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl <= 0]
    gross_win = sum(t.net_pnl for t in wins)
    gross_loss = sum(t.net_pnl for t in losses)
    net = sum(t.net_pnl for t in trades)
    pf = (gross_win / abs(gross_loss)) if gross_loss < 0 else (float("inf") if gross_win > 0 else 0.0)
    return Stats(
        label=label,
        n=n,
        wins=len(wins),
        losses=len(losses),
        win_rate=(len(wins) / n * 100.0) if n else 0.0,
        net_pnl=net,
        gross_pnl=sum(t.gross_pnl for t in trades),
        costs=sum(t.cost for t in trades),
        avg_win=(gross_win / len(wins)) if wins else 0.0,
        avg_loss=(gross_loss / len(losses)) if losses else 0.0,
        avg_pnl=(net / n) if n else 0.0,
        profit_factor=pf,
        max_drawdown=_max_drawdown(trades),
        avg_hold_min=(sum(t.hold_minutes for t in trades) / n) if n else 0.0,
        exit_reasons=Counter(t.exit_reason for t in trades),
    )


def _fmt_stats(s: Stats) -> str:
    pf = "inf" if s.profit_factor == float("inf") else f"{s.profit_factor:.2f}"
    reasons = "  ".join(f"{k}={v}" for k, v in sorted(s.exit_reasons.items()))
    return "\n".join(
        [
            f"  trades        : {s.n}   (wins {s.wins} / losses {s.losses})",
            f"  win rate      : {s.win_rate:.1f}%",
            f"  net P&L       : Rs {s.net_pnl:,.0f}   (gross {s.gross_pnl:,.0f}, costs {s.costs:,.0f})",
            f"  avg / trade   : Rs {s.avg_pnl:,.0f}",
            f"  avg win/loss  : Rs {s.avg_win:,.0f} / Rs {s.avg_loss:,.0f}",
            f"  profit factor : {pf}",
            f"  max drawdown  : Rs {s.max_drawdown:,.0f}",
            f"  avg hold      : {s.avg_hold_min:.0f} min",
            f"  exits         : {reasons}",
        ]
    )


def render_report(
    trades: list[BTTrade],
    *,
    window: str,
    iv: float,
    lot_size: int,
    cost_inr: float,
) -> str:
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append("BACKTEST REPORT  (Black-Scholes synthetic option pricing)")
    lines.append("=" * 64)
    lines.append(f"window={window}  iv={iv:.0%}  lot_size={lot_size}  cost/trade=Rs {cost_inr:.0f}")
    lines.append("Model estimate, not tick-exact. Per-signal (no portfolio cap).")
    lines.append("")

    if not trades:
        lines.append("No trades generated in this window.")
        lines.append("(Strategies stayed flat — e.g. no trending regime / no valid ORB.)")
        return "\n".join(lines)

    strategies = sorted({t.strategy_id for t in trades})
    for sid in strategies:
        subset = [t for t in trades if t.strategy_id == sid]
        lines.append(f"[{sid}]")
        lines.append(_fmt_stats(compute_stats(subset, sid)))
        lines.append("")

    if len(strategies) > 1:
        lines.append("[ALL STRATEGIES]")
        lines.append(_fmt_stats(compute_stats(trades, "ALL")))
        lines.append("")

    return "\n".join(lines)
