"""
Unit tests for the notifications layer.

We test the pure parts:
  - NotificationEvent shape + defaults
  - Severity prefixes
  - The `notify*` helpers publish well-formed events (mock the bus)
  - The `_authorised` check in the bot accepts the configured chat_id only

The actual Telegram HTTP layer is mocked / not exercised here.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.notifications.alerts import (
    SEVERITY_PREFIX,
    AlertSeverity,
    NotificationEvent,
    notify,
    notify_fill,
    notify_kill_switch,
    notify_position_exit,
    notify_signal,
)


def _make_bus_mock():
    bus = MagicMock()
    bus.publish = AsyncMock(return_value="msg-id-1")
    return bus


# ----------------- NotificationEvent shape -----------------


def test_notification_event_defaults():
    ev = NotificationEvent(
        event_id="x",
        ts=__import__("datetime").datetime.now(tz=__import__("datetime").timezone.utc),
        title="t",
        body="b",
    )
    assert ev.severity == AlertSeverity.INFO
    assert ev.extras == {}


def test_severity_prefix_table_covers_all_levels():
    for s in AlertSeverity:
        assert s in SEVERITY_PREFIX
        assert SEVERITY_PREFIX[s].startswith("[")


# ----------------- typed helpers publish well-formed events -----------------


@pytest.mark.asyncio
async def test_notify_signal_publishes_correctly():
    bus = _make_bus_mock()
    await notify_signal(
        bus,
        strategy_id="orb_breakout_v1",
        underlying="NIFTY 50",
        side="BUY",
        option_type="CE",
        confidence=72.5,
    )
    bus.publish.assert_awaited_once()
    stream, ev = bus.publish.await_args.args
    assert stream == "stream:notifications"
    assert ev.title == "Signal"
    assert "orb_breakout_v1" in ev.body
    assert "NIFTY 50" in ev.body
    assert ev.extras["confidence"] == 72.5


@pytest.mark.asyncio
async def test_notify_fill_marks_success():
    bus = _make_bus_mock()
    await notify_fill(
        bus,
        tradingsymbol="NIFTY25M2225000CE",
        side="BUY",
        qty=75,
        fill_price=Decimal("100.25"),
    )
    bus.publish.assert_awaited_once()
    _, ev = bus.publish.await_args.args
    assert ev.severity == AlertSeverity.SUCCESS
    assert "NIFTY25M2225000CE" in ev.body


@pytest.mark.asyncio
async def test_notify_position_exit_uses_warning_on_loss():
    bus = _make_bus_mock()
    await notify_position_exit(
        bus,
        tradingsymbol="X",
        exit_reason="stop_loss",
        entry=Decimal("100"),
        exit_price=Decimal("70"),
        pnl=Decimal("-30"),
    )
    _, ev = bus.publish.await_args.args
    assert ev.severity == AlertSeverity.WARNING
    assert "stop_loss" in ev.body


@pytest.mark.asyncio
async def test_notify_position_exit_uses_success_on_win():
    bus = _make_bus_mock()
    await notify_position_exit(
        bus,
        tradingsymbol="X",
        exit_reason="take_profit",
        entry=Decimal("100"),
        exit_price=Decimal("160"),
        pnl=Decimal("60"),
    )
    _, ev = bus.publish.await_args.args
    assert ev.severity == AlertSeverity.SUCCESS


@pytest.mark.asyncio
async def test_notify_kill_switch_is_critical():
    bus = _make_bus_mock()
    await notify_kill_switch(bus, reason="daily_loss")
    _, ev = bus.publish.await_args.args
    assert ev.severity == AlertSeverity.CRITICAL
    assert "daily_loss" in ev.body


@pytest.mark.asyncio
async def test_notify_swallows_publish_failure():
    """Notification publishing must never crash a hot path."""
    bus = MagicMock()
    bus.publish = AsyncMock(side_effect=RuntimeError("redis down"))
    # Should not raise
    await notify(bus, title="t", body="b")


# ----------------- bot chat-id auth -----------------


def test_authorised_accepts_configured_chat(monkeypatch):
    from src.notifications import bot as bot_module

    # Patch settings.telegram_chat_id
    monkeypatch.setattr(bot_module.settings, "telegram_chat_id", "12345")
    update = MagicMock()
    update.effective_chat.id = 12345
    assert bot_module._authorised(update) is True


def test_authorised_rejects_other_chat(monkeypatch):
    from src.notifications import bot as bot_module

    monkeypatch.setattr(bot_module.settings, "telegram_chat_id", "12345")
    update = MagicMock()
    update.effective_chat.id = 99999
    assert bot_module._authorised(update) is False


def test_authorised_rejects_when_no_chat_configured(monkeypatch):
    from src.notifications import bot as bot_module

    monkeypatch.setattr(bot_module.settings, "telegram_chat_id", "")
    update = MagicMock()
    update.effective_chat.id = 12345
    assert bot_module._authorised(update) is False
