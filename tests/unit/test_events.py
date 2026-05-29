"""Event schema tests."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.core.events import (
    BarEvent,
    OptionContract,
    OptionType,
    OrderRequest,
    OrderStatus,
    OrderUpdate,
    RegimeLabel,
    RegimeUpdate,
    Side,
    SignalCandidate,
    TickEvent,
)


def _ts() -> datetime:
    return datetime.now(tz=timezone.utc)


def _eid() -> str:
    return str(uuid.uuid4())


def test_tick_event_round_trip():
    t = TickEvent(
        event_id=_eid(),
        ts=_ts(),
        instrument_token=256265,
        symbol="NIFTY 50",
        ltp=Decimal("23456.75"),
        bid=Decimal("23456.50"),
        ask=Decimal("23457.00"),
        volume=1000,
    )
    j = t.model_dump_json()
    t2 = TickEvent.model_validate_json(j)
    assert t == t2


def test_bar_event_resolution_constraint():
    valid = BarEvent(
        event_id=_eid(),
        ts=_ts(),
        instrument_token=1,
        symbol="X",
        resolution="5minute",
        bar_ts=_ts(),
        o=Decimal("100"),
        h=Decimal("101"),
        l=Decimal("99.5"),
        c=Decimal("100.5"),
        v=1000,
    )
    assert valid.resolution == "5minute"

    with pytest.raises(ValidationError):
        BarEvent(
            event_id=_eid(),
            ts=_ts(),
            instrument_token=1,
            symbol="X",
            resolution="7minute",  # not allowed
            bar_ts=_ts(),
            o=Decimal("100"),
            h=Decimal("101"),
            l=Decimal("99.5"),
            c=Decimal("100.5"),
            v=1000,
        )


def test_events_are_frozen():
    t = TickEvent(
        event_id=_eid(),
        ts=_ts(),
        instrument_token=1,
        symbol="X",
        ltp=Decimal("100"),
    )
    with pytest.raises(ValidationError):
        t.ltp = Decimal("200")  # frozen=True must prevent this


def test_signal_candidate_full():
    s = SignalCandidate(
        event_id=_eid(),
        ts=_ts(),
        strategy_id="S01_VWAP_TREND",
        underlying="NIFTY 50",
        side=Side.BUY,
        option_type=OptionType.CE,
        confidence=78.5,
        rationale=["price > vwap", "ema20 > ema50", "rsi 62"],
        suggested_sl_pct=30.0,
        suggested_target_pct=60.0,
    )
    assert s.confidence == 78.5
    assert len(s.rationale) == 3


def test_order_request_idempotency_key():
    s = SignalCandidate(
        event_id=_eid(),
        ts=_ts(),
        strategy_id="S",
        underlying="NIFTY 50",
        side=Side.BUY,
        option_type=OptionType.CE,
        confidence=80,
        rationale=[],
    )
    contract = OptionContract(
        instrument_token=1,
        tradingsymbol="NIFTY26MAY24500CE",
        underlying="NIFTY 50",
        expiry=_ts(),
        strike=Decimal("24500"),
        option_type=OptionType.CE,
        lot_size=75,
    )
    o = OrderRequest(
        event_id=_eid(),
        ts=_ts(),
        client_order_id=_eid(),
        strategy_id="S",
        signal_event_id=s.event_id,
        contract=contract,
        side=Side.BUY,
        qty=75,
    )
    assert o.client_order_id != o.event_id  # two distinct UUIDs
    assert o.product == "MIS"


def test_regime_label_enum():
    r = RegimeUpdate(
        event_id=_eid(),
        ts=_ts(),
        instrument_token=256265,
        symbol="NIFTY 50",
        resolution="1minute",
        label=RegimeLabel.TRENDING_UP,
        score=0.8,
        rationale={"adx": 28},
    )
    assert r.label == RegimeLabel.TRENDING_UP
    assert r.instrument_token == 256265


def test_order_status_values():
    assert OrderStatus.COMPLETE != OrderStatus.OPEN
    upd = OrderUpdate(
        event_id=_eid(),
        ts=_ts(),
        client_order_id=_eid(),
        broker_order_id="BO123",
        status=OrderStatus.COMPLETE,
        filled_qty=75,
        avg_fill_price=Decimal("12.50"),
    )
    assert upd.status == OrderStatus.COMPLETE
