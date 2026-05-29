"""Phase 5: Telegram notifications + mobile control.

Two-piece design:

  - `alerts.py` is *producer-side*: small helpers any worker can call to
     push a one-line notification to the bus. No Telegram dependency here.
  - `bot.py` is *consumer-side*: the actual Telegram bot that subscribes
     to the notifications stream, forwards each event to the user's chat,
     and also handles slash commands.

This split means strategies / risk / executor / watcher don't import
python-telegram-bot, and switching to a different notifier (email, Slack,
push-only) later is a one-file change.
"""
from src.notifications.alerts import (
    AlertSeverity,
    NotificationEvent,
    notify,
    notify_error,
    notify_fill,
    notify_kill_switch,
    notify_position_exit,
    notify_signal,
)

__all__ = [
    "AlertSeverity",
    "NotificationEvent",
    "notify",
    "notify_signal",
    "notify_fill",
    "notify_position_exit",
    "notify_kill_switch",
    "notify_error",
]
