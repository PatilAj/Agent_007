"""
Unit tests for the risk engine — pure-function gates.

The async `assess()` orchestrator pulls from DB + Kite, which we don't
exercise here. We test the cheap, in-process gates that catch the bulk of
rejections (market window, sizing, spread) so any change to those is loud.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from src.core.events import OptionType, SignalCandidate, Side
from src.core.kill_switch import get_kill_switch
from src.risk.engine import RiskEngine


def make_signal(
    *,
    ts: datetime | None = None,
    symbol: str = "NIFTY 50",
    side: Side = Side.BUY,
    option_type: OptionType = OptionType.CE,
) -> SignalCandidate:
    # 2026-05-22 (Thu) 04:30 UTC = 10:00 IST — inside market window
    return SignalCandidate(
        event_id=str(uuid.uuid4()),
        ts=ts or datetime(2026, 5, 22, 4, 30, tzinfo=timezone.utc),
        strategy_id="t",
        underlying=symbol,
        side=side,
        option_type=option_type,
        confidence=70.0,
        rationale=["t"],
    )


# ----------------- kill switch -----------------


def test_local_kill_switch_blocks():
    eng = RiskEngine()
    ks = get_kill_switch()
    ks.arm(reason="test")
    try:
        v = eng._gate_local_kill_switch()
        assert v is not None and v.accepted is False
        assert v.gate == "local_kill_switch"
    finally:
        ks.disarm()


def test_local_kill_switch_disarmed_returns_none():
    eng = RiskEngine()
    ks = get_kill_switch()
    ks.disarm()
    assert eng._gate_local_kill_switch() is None


# ----------------- market window -----------------


def test_market_window_blocks_before_open():
    eng = RiskEngine()
    # 03:00 UTC = 08:30 IST — before 09:15
    sig = make_signal(ts=datetime(2026, 5, 22, 3, 0, tzinfo=timezone.utc))
    v = eng._gate_market_window(sig)
    assert v is not None and v.gate == "market_window"


def test_market_window_blocks_after_no_entry_after():
    eng = RiskEngine()
    # 09:20 UTC = 14:50 IST — past 14:45 cutoff
    sig = make_signal(ts=datetime(2026, 5, 22, 9, 20, tzinfo=timezone.utc))
    v = eng._gate_market_window(sig)
    assert v is not None and v.gate == "market_window"


def test_market_window_blocks_weekend():
    eng = RiskEngine()
    sig = make_signal(ts=datetime(2026, 5, 23, 5, 0, tzinfo=timezone.utc))  # Sat 10:30 IST
    v = eng._gate_market_window(sig)
    assert v is not None
    assert v.gate == "market_window"


def test_market_window_passes_at_10_am():
    eng = RiskEngine()
    v = eng._gate_market_window(make_signal())  # default ts is 10:00 IST Thursday
    assert v is None


# ----------------- spread gate -----------------


def test_spread_gate_rejects_wide_spread():
    eng = RiskEngine()
    # bid=100, ask=110 → mid=105 → spread=10/105 = 9.52% — way above 2% default
    v = eng._gate_spread(bid=100, ask=110)
    assert v is not None and v.gate == "spread"


def test_spread_gate_passes_tight_spread():
    eng = RiskEngine()
    # bid=100, ask=100.5 → mid=100.25 → spread=0.5/100.25 = 0.5%
    assert eng._gate_spread(bid=100, ask=100.5) is None


def test_spread_gate_rejects_missing_bid_or_ask():
    eng = RiskEngine()
    assert eng._gate_spread(bid=0, ask=100) is not None
    assert eng._gate_spread(bid=100, ask=0) is not None


# ----------------- liquidity gate -----------------


def test_liquidity_rejects_low_premium():
    eng = RiskEngine()
    v = eng._gate_liquidity(premium=2.0, oi=999_999)  # below premium_min_inr=5
    assert v is not None and v.gate == "liquidity_premium"


def test_liquidity_rejects_low_oi():
    eng = RiskEngine()
    v = eng._gate_liquidity(premium=100.0, oi=1000)  # below min_oi_index=5000
    assert v is not None and v.gate == "liquidity_oi"


def test_liquidity_passes_healthy():
    eng = RiskEngine()
    assert eng._gate_liquidity(premium=100.0, oi=1_000_000) is None


# ----------------- sizing -----------------


def _pin_capital(monkeypatch, capital: float, pct: float = 5.0) -> None:
    """Pin the sizing budget so these arithmetic tests don't depend on the
    ambient (per-mode) config — paper mode now runs at Rs 3L."""
    from src.risk import engine as risk_engine

    monkeypatch.setattr(risk_engine.settings.risk, "slot_capital_inr", capital)
    monkeypatch.setattr(risk_engine.settings.risk, "max_premium_per_trade_pct", pct)


def test_sizing_returns_lots_within_budget(monkeypatch):
    _pin_capital(monkeypatch, 50000.0)
    eng = RiskEngine()
    # premium 50, lot 75 → per-lot 3750; slot 50000 × 5% → budget=2500
    lots, total, err = eng._size_position(premium=50.0, lot_size=75)
    # 2500 // 3750 = 0 lots → reject (per-lot cost exceeds budget)
    assert lots == 0
    assert err is not None


def test_sizing_accepts_when_budget_allows(monkeypatch):
    _pin_capital(monkeypatch, 50000.0)
    eng = RiskEngine()
    # premium 10, lot 75 → per-lot 750; budget 2500 → 3 lots → total 2250
    lots, total, err = eng._size_position(premium=10.0, lot_size=75)
    assert lots == 3
    assert err is None
    assert total == pytest.approx(2250.0)


def test_sizing_rejects_zero_premium():
    eng = RiskEngine()
    lots, total, err = eng._size_position(premium=0.0, lot_size=75)
    assert lots == 0
    assert err is not None
