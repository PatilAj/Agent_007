"""
Telegram bot — phone-side interface to the trading agent.

Two concurrent jobs (both run inside `run_bot_forever`):

  1. Forwarder: subscribes to stream:notifications on Redis, forwards each
     NotificationEvent to the configured Telegram chat as a message.
  2. Command handler: long-polls Telegram for slash commands and answers
     them. Commands are gated by a chat-ID whitelist so only the configured
     user can drive the bot.

Read-only commands (safe):
  /status   — agent process state + last tick + open positions
  /signals  — last 10 SignalCandidates from the journal
  /trades   — open + today's closed trades with P&L
  /pnl      — today's daily P&L row
  /health   — runs the same checks as scripts/health_check
  /help     — list commands

Control commands (act on state):
  /kill        — arm the local kill switch + Redis flag (blocks new orders)
  /unkill      — disarm
  /start_agent — launch the ingestor as a subprocess (laptop must be on)
  /stop_agent  — terminate the running ingestor (if any)

Every command logs the requesting chat_id even when authorised.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytz
from sqlalchemy import select
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

from src.core.bus import STREAM_NOTIFICATIONS, EventBus
from src.core.config import settings
from src.core.kill_switch import get_kill_switch
from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import DailyPnL, Signal, Trade
from src.notifications.alerts import (
    SEVERITY_PREFIX,
    AlertSeverity,
    NotificationEvent,
)

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")

# Track the ingestor subprocess started via /start_agent (if any)
_ingestor_proc: subprocess.Popen | None = None


# ----------------- helpers -----------------


def _authorised(update: Update) -> bool:
    """Reject any chat_id that isn't the configured operator."""
    expected = settings.telegram_chat_id.strip()
    if not expected:
        log.warning("bot_command_blocked_no_chat_configured")
        return False
    if update.effective_chat is None:
        return False
    return str(update.effective_chat.id) == expected


async def _reject(update: Update) -> None:
    if update.effective_chat is not None:
        log.warning(
            "bot_unauthorized_chat",
            received=update.effective_chat.id,
            expected=settings.telegram_chat_id,
        )
    if update.message:
        await update.message.reply_text("Unauthorized chat.")


def _fmt_severity(sev: AlertSeverity) -> str:
    return SEVERITY_PREFIX.get(sev, "[INFO]")


# ----------------- read-only command handlers -----------------


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await _reject(update)
        return
    text = (
        "Commands:\n"
        "/status   - agent + system state\n"
        "/signals  - last 10 signals\n"
        "/trades   - open + today's closed trades\n"
        "/pnl      - today's net P&L\n"
        "/health   - full health check\n"
        "/kill     - arm the kill switch\n"
        "/unkill   - disarm the kill switch\n"
        "/start_agent - launch the ingestor (laptop must be on)\n"
        "/stop_agent  - terminate the ingestor\n"
        "/help     - this list"
    )
    await update.message.reply_text(text)


async def cmd_status(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await _reject(update)
        return
    ingestor_running = _ingestor_proc is not None and _ingestor_proc.poll() is None
    ks = get_kill_switch()

    async with get_session() as s:
        open_count = (
            await s.execute(
                select(Trade).where(Trade.closed_at.is_(None))
            )
        ).scalars().all()
        open_count = len(open_count)

    lines = [
        f"mode: {settings.mode}",
        f"live_enabled: {settings.enable_live}",
        f"ingestor: {'running' if ingestor_running else 'stopped'}",
        f"kill_switch_local: {'ARMED' if ks.armed else 'disarmed'}",
        f"open_positions: {open_count}",
    ]
    await update.message.reply_text("\n".join(lines))


async def cmd_signals(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await _reject(update)
        return
    today = datetime.now(tz=timezone.utc).astimezone(IST).date()
    async with get_session() as s:
        rows = list((await s.execute(
            select(Signal).order_by(Signal.ts.desc()).limit(10)
        )).scalars().all())
    if not rows:
        await update.message.reply_text("No signals in the journal yet.")
        return
    today_count = sum(1 for r in rows if r.ts.astimezone(IST).date() == today)
    if today_count == 0:
        lines = ["No signals generated today yet. Showing last 10 (historical):"]
    else:
        lines = [f"{today_count} signal(s) today. Last 10:"]
    for r in rows:
        ist = r.ts.astimezone(IST)
        # Show the date for anything not from today so stale signals are obvious.
        when = ist.strftime("%H:%M") if ist.date() == today else ist.strftime("%m-%d %H:%M")
        verdict = "ok" if r.accepted else f"REJ({r.rejection_reason or '?'})"
        lines.append(
            f"{when} {r.strategy_id} {r.underlying} {r.side} {r.option_type} "
            f"conf={r.confidence:.0f} -> {verdict}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_trades(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await _reject(update)
        return
    today_ist = datetime.now(tz=timezone.utc).astimezone(IST).date()
    today_start = datetime.combine(today_ist, datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
    async with get_session() as s:
        open_rows = list((await s.execute(
            select(Trade).where(Trade.closed_at.is_(None)).order_by(Trade.opened_at.desc())
        )).scalars().all())
        closed_rows = list((await s.execute(
            select(Trade)
            .where(Trade.closed_at.is_not(None))
            .where(Trade.opened_at >= today_start)
            .order_by(Trade.closed_at.desc())
            .limit(10)
        )).scalars().all())

    lines = []
    if open_rows:
        lines.append(f"Open ({len(open_rows)}):")
        for r in open_rows:
            lines.append(f"  {r.tradingsymbol} {r.side} {r.qty} entry={r.entry_price}")
    else:
        lines.append("Open: none")

    if closed_rows:
        lines.append(f"\nClosed today ({len(closed_rows)}):")
        for r in closed_rows:
            sign = "+" if (r.net_pnl or 0) >= 0 else ""
            lines.append(
                f"  {r.tradingsymbol} {r.side} pnl={sign}{r.net_pnl or 0} ({r.exit_reason})"
            )
    else:
        lines.append("\nClosed today: none")
    await update.message.reply_text("\n".join(lines))


async def cmd_pnl(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await _reject(update)
        return
    today_ist = datetime.now(tz=timezone.utc).astimezone(IST).date()
    start = datetime.combine(today_ist, datetime.min.time(), tzinfo=IST).astimezone(timezone.utc)
    end = datetime.combine(today_ist, datetime.max.time(), tzinfo=IST).astimezone(timezone.utc)
    async with get_session() as s:
        row = (await s.execute(
            select(DailyPnL).where(DailyPnL.trade_date >= start).where(DailyPnL.trade_date <= end)
        )).scalar_one_or_none()
    if row is None:
        await update.message.reply_text("No P&L row for today yet (no closed trades).")
        return
    sign = "+" if row.net_pnl >= 0 else ""
    await update.message.reply_text(
        f"Today net_pnl: {sign}{row.net_pnl}\n"
        f"trades: {row.trade_count}  wins: {row.win_count}  losses: {row.loss_count}\n"
        f"consecutive_losses: {row.consecutive_losses}\n"
        f"halted: {row.halted}"
    )


async def cmd_health(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await _reject(update)
        return
    # Lightweight health: DB + Redis + token freshness
    lines = ["Health check:"]
    try:
        from src.data.db import ping
        lines.append(f"  postgres: {'OK' if await ping() else 'FAIL'}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  postgres: FAIL ({e})")
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        await client.aclose()
        lines.append("  redis: OK")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  redis: FAIL ({e})")
    try:
        from src.auth.kite_session import get_active_token
        t = await get_active_token()
        lines.append(f"  kite_token: {'active' if t else 'none'}")
    except Exception as e:  # noqa: BLE001
        lines.append(f"  kite_token: FAIL ({e})")
    await update.message.reply_text("\n".join(lines))


# ----------------- control commands -----------------


async def cmd_kill(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await _reject(update)
        return
    ks = get_kill_switch()
    ks.arm(reason=f"telegram by {update.effective_chat.id}")
    # Also set the Redis flag so other processes (if any) honour it
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.set("kill_switch:armed", "1")
        await client.aclose()
    except Exception as e:  # noqa: BLE001
        log.warning("kill_redis_set_failed", error=str(e))
    await update.message.reply_text("Kill switch ARMED. No new orders will be sent.")


async def cmd_unkill(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not _authorised(update):
        await _reject(update)
        return
    ks = get_kill_switch()
    ks.disarm()
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.delete("kill_switch:armed")
        await client.aclose()
    except Exception as e:  # noqa: BLE001
        log.warning("unkill_redis_del_failed", error=str(e))
    await update.message.reply_text("Kill switch disarmed.")


async def cmd_start_agent(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    global _ingestor_proc
    if not _authorised(update):
        await _reject(update)
        return
    if _ingestor_proc is not None and _ingestor_proc.poll() is None:
        await update.message.reply_text(f"Ingestor already running (PID {_ingestor_proc.pid}).")
        return
    project_root = Path(__file__).resolve().parents[2]
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        _ingestor_proc = subprocess.Popen(
            [sys.executable, "-X", "utf8", "-m", "src.workers.ingestor"],
            cwd=str(project_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Failed to start: {e}")
        return
    await update.message.reply_text(
        f"Ingestor started (PID {_ingestor_proc.pid}). "
        "Note: any OTP prompt cannot be answered remotely — refresh token in advance."
    )


async def cmd_stop_agent(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    global _ingestor_proc
    if not _authorised(update):
        await _reject(update)
        return
    if _ingestor_proc is None or _ingestor_proc.poll() is not None:
        await update.message.reply_text("No running ingestor tracked by this bot.")
        _ingestor_proc = None
        return
    pid = _ingestor_proc.pid
    try:
        # On Windows, send CTRL_BREAK to the process group we created
        if hasattr(signal, "CTRL_BREAK_EVENT"):
            _ingestor_proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            _ingestor_proc.terminate()
        try:
            _ingestor_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _ingestor_proc.kill()
        _ingestor_proc = None
        await update.message.reply_text(f"Ingestor stopped (was PID {pid}).")
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"Stop failed: {e}")


# ----------------- notification forwarder -----------------


async def _forward_notifications(application: Application, bus: EventBus) -> None:
    """Consume stream:notifications and forward each event to the configured chat."""
    chat_id = settings.telegram_chat_id.strip()
    if not chat_id:
        log.warning("notification_forwarder_no_chat_configured")
        return

    group = "telegram-bot"
    consumer = "tb-1"
    log.info("notification_forwarder_starting")
    async for msg_id, payload in bus.consume(STREAM_NOTIFICATIONS, group, consumer, count=10):
        try:
            ev = NotificationEvent.model_validate(payload)
            text = f"{_fmt_severity(ev.severity)} {ev.title}\n{ev.body}"
            await application.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:  # noqa: BLE001
            log.exception("notification_forward_failed", error=str(e))
        finally:
            await bus.ack(STREAM_NOTIFICATIONS, group, msg_id)


# ----------------- main entry point -----------------


def build_application() -> Application:
    token = settings.telegram_bot_token.get_secret_value().strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set in .env")
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))  # /start shows help
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CommandHandler("trades", cmd_trades))
    app.add_handler(CommandHandler("pnl", cmd_pnl))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("unkill", cmd_unkill))
    app.add_handler(CommandHandler("start_agent", cmd_start_agent))
    app.add_handler(CommandHandler("stop_agent", cmd_stop_agent))
    return app


async def run_bot_forever() -> None:
    """Run the bot: command poller + alert forwarder, concurrently."""
    bus = await EventBus.connect()
    app = build_application()

    log.info("telegram_bot_starting")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    # Send startup ping so you know it's alive
    chat_id = settings.telegram_chat_id.strip()
    if chat_id:
        try:
            await app.bot.send_message(chat_id=chat_id, text="[OK] Bot online")
        except Exception as e:  # noqa: BLE001
            log.warning("bot_startup_ping_failed", error=str(e))

    try:
        await _forward_notifications(app, bus)
    finally:
        log.info("telegram_bot_stopping")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await bus.close()
