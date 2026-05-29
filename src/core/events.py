"""
Event contracts.

Every cross-module message is a typed Pydantic event. This file is the
single source of truth for the system's internal protocol.

Adding a field requires bumping the event version (semver style).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class EventBase(BaseModel):
    """Base class for all bus events."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str  # UUID
    ts: datetime  # event time, UTC tz-aware
    version: str = "1.0"


# --- Market data ---

class TickEvent(EventBase):
    instrument_token: int
    symbol: str
    ltp: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int | None = None
    oi: int | None = None


class BarEvent(EventBase):
    instrument_token: int
    symbol: str
    resolution: Literal["1minute", "3minute", "5minute", "15minute", "1day"]
    bar_ts: datetime  # bar close time
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    v: int
    oi: int | None = None


# --- Indicators ---

class IndicatorUpdate(EventBase):
    instrument_token: int
    symbol: str
    resolution: str
    bar_ts: datetime
    name: str  # "ema20", "rsi14", "vwap", "supertrend"
    value: float | None
    extras: dict[str, Any] = Field(default_factory=dict)


# --- Regime ---

class RegimeLabel(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE = "range"
    HIGH_VOL = "high_vol"
    LOW_VOL = "low_vol"
    UNKNOWN = "unknown"


class RegimeUpdate(EventBase):
    instrument_token: int
    symbol: str
    resolution: str
    label: RegimeLabel
    score: float  # 0-1 confidence
    rationale: dict[str, Any] = Field(default_factory=dict)


# --- Strategy signals ---

class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OptionType(str, Enum):
    CE = "CE"
    PE = "PE"


class SignalCandidate(EventBase):
    """Raw signal from a strategy — not yet sized or risk-checked."""

    strategy_id: str
    underlying: str  # "NIFTY 50"
    side: Side
    option_type: OptionType
    confidence: float  # 0-100
    rationale: list[str]  # human-readable reasons
    indicators_snapshot: dict[str, Any] = Field(default_factory=dict)
    suggested_sl_pct: float | None = None
    suggested_target_pct: float | None = None


# --- Option contract selection ---

class OptionContract(BaseModel):
    model_config = ConfigDict(frozen=True)

    instrument_token: int
    tradingsymbol: str   # "NIFTY24DEC24500CE"
    underlying: str
    expiry: datetime
    strike: Decimal
    option_type: OptionType
    lot_size: int


# --- Orders ---

class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    PARTIAL = "PARTIAL"


class OrderRequest(EventBase):
    """Risk-approved order, ready to send to the broker."""

    client_order_id: str  # UUID, idempotency key
    strategy_id: str
    signal_event_id: str
    contract: OptionContract
    side: Side
    qty: int   # number of shares (lots × lot_size)
    order_type: Literal["MARKET", "LIMIT", "SL", "SL-M"] = "LIMIT"
    price: Decimal | None = None
    trigger_price: Decimal | None = None
    product: Literal["MIS", "NRML"] = "MIS"
    validity: Literal["DAY", "IOC"] = "DAY"
    tag: str | None = None


class OrderUpdate(EventBase):
    """Broker callback: order status changed."""

    client_order_id: str
    broker_order_id: str | None
    status: OrderStatus
    filled_qty: int
    avg_fill_price: Decimal | None
    rejection_reason: str | None = None


# --- Risk events ---

class RiskRejection(EventBase):
    signal_event_id: str
    gate: str
    reason: str
    context: dict[str, Any] = Field(default_factory=dict)


# --- System ---

class HealthEvent(EventBase):
    component: str
    status: Literal["healthy", "degraded", "down"]
    latency_ms: float | None = None
    details: dict[str, Any] = Field(default_factory=dict)
