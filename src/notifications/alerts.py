"""
Alert producers.

Workers call these helpers to push a notification onto stream:notifications.
The Telegram bot (a separate process / task) consumes the stream and forwards
each event to the configured chat.

We use the bus instead of calling Telegram directly so:
  - The producer code stays sync-style and lightweight
  - If the bot is down, alerts queue in Redis (durable up to MAX_STREAM_LEN)
  - Switching to a different notifier later is a one-file change
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import Field

from src.core.bus import STREAM_NOTIFICATIONS, EventBus
from src.core.events import EventBase
from src.core.logging import get_logger

log = get_logger(__name__)


class AlertSeverity(str, Enum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    CRITICAL = "critical"


# Pretty prefixes (text only, no emojis — Telegram renders them but we keep
# the codebase emoji-free for logs / tests grep-ability)
SEVERITY_PREFIX: dict[AlertSeverity, str] = {
    AlertSeverity.INFO: "[INFO]",
    AlertSeverity.SUCCESS: "[OK]",
    AlertSeverity.WARNING: "[WARN]",
    AlertSeverity.CRITICAL: "[CRITICAL]",
}


class NotificationEvent(EventBase):
    """One human-readable alert ready to forward to Telegram."""

    severity: AlertSeverity = AlertSeverity.INFO
    title: str
    body: str
    extras: dict[str, Any] = Field(default_factory=dict)


async def notify(
    bus: EventBus,
    *,
    title: str,
    body: str,
    severity: AlertSeverity = AlertSeverity.INFO,
    extras: dict[str, Any] | None = None,
) -> None:
    """Low-level: publish a NotificationEvent."""
    ev = NotificationEvent(
        event_id=str(uuid.uuid4()),
        ts=datetime.now(tz=timezone.utc),
        severity=severity,
        title=title,
        body=body,
        extras=extras or {},
    )
    try:
        await bus.publish(STREAM_NOTIFICATIONS, ev)
    except Exception as e:  # noqa: BLE001
        # Never let notification failure crash a hot path
        log.warning("notify_publish_failed", error=str(e), title=title)


# ------------------------------------------------------------------
# Typed helpers — call these from workers
# ------------------------------------------------------------------


async def notify_signal(bus: EventBus, *, strategy_id: str, underlying: str, side: str,
                        option_type: str, confidence: float) -> None:
    await notify(
        bus,
        title="Signal",
        body=(
            f"{strategy_id} -> {side} {option_type} on {underlying}\n"
            f"confidence={confidence:.1f}"
        ),
        severity=AlertSeverity.INFO,
        extras={
            "strategy_id": strategy_id,
            "underlying": underlying,
            "side": side,
            "option_type": option_type,
            "confidence": confidence,
        },
    )


async def notify_fill(bus: EventBus, *, tradingsymbol: str, side: str, qty: int,
                      fill_price: Decimal) -> None:
    await notify(
        bus,
        title="Order filled",
        body=f"{side} {qty} {tradingsymbol} @ {fill_price}",
        severity=AlertSeverity.SUCCESS,
        extras={
            "tradingsymbol": tradingsymbol,
            "side": side,
            "qty": qty,
            "fill_price": float(fill_price),
        },
    )


async def notify_position_exit(bus: EventBus, *, tradingsymbol: str, exit_reason: str,
                               entry: Decimal, exit_price: Decimal, pnl: Decimal) -> None:
    sev = AlertSeverity.SUCCESS if pnl > 0 else AlertSeverity.WARNING
    sign = "+" if pnl >= 0 else ""
    await notify(
        bus,
        title="Position closed",
        body=(
            f"{tradingsymbol} ({exit_reason})\n"
            f"entry={entry}, exit={exit_price}, pnl={sign}{pnl}"
        ),
        severity=sev,
        extras={
            "tradingsymbol": tradingsymbol,
            "exit_reason": exit_reason,
            "entry": float(entry),
            "exit": float(exit_price),
            "pnl": float(pnl),
        },
    )


async def notify_kill_switch(bus: EventBus, *, reason: str) -> None:
    await notify(
        bus,
        title="Kill switch ARMED",
        body=f"No new orders will be sent. Reason: {reason}",
        severity=AlertSeverity.CRITICAL,
        extras={"reason": reason},
    )


async def notify_error(bus: EventBus, *, where: str, error: str) -> None:
    await notify(
        bus,
        title=f"Error in {where}",
        body=str(error)[:500],   # keep it readable
        severity=AlertSeverity.WARNING,
        extras={"where": where, "error": error},
    )
