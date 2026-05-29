"""
Daily paper-test injection.

Once per IST weekday at a fixed time (default 10:30 IST), inject ONE
direction-aware test signal so the full pipeline — risk gate → executor →
fill → position watcher exit — is exercised even on days when the organic
strategies (ema_regime_v2, ORB) stay flat.

Entry filters (target ~50%+ win rate, ~Rs 600-1000/day average):
  - Multi-timeframe agreement: yesterday's daily candle AND today's 15m EMA
    spread must agree on direction. If they disagree, SKIP the day.
  - No-trade zones: skip the lunch chop (12:30-13:30 IST) and anything past
    14:30 IST (theta dominates with too little time for the move to play out).

Exit (fixed, no trailing/scaling per user preference):
  - SL  = 5% of premium (~ -Rs 500-750 on current 2-lot sizing)
  - TP  = 10% of premium (~ +Rs 1000-1500)
  - 15:15 IST square-off as backstop.

Other guardrails:
  - Paper mode only. Never runs in shadow/live.
  - Skips weekends.
  - Skips when the local kill switch is armed.
  - Idempotent per IST day: a second start the same day won't double-fire.
  - 60s startup grace so the watcher can square off any stale overnight trade
    before the daily injection (avoids duplicate_symbol rejection).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, time, timedelta, timezone

import pytz
from sqlalchemy import select

from src.core.bus import STREAM_SIGNALS, EventBus
from src.core.config import settings
from src.core.events import OptionType, Side, SignalCandidate
from src.core.kill_switch import get_kill_switch
from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import Signal
from src.journal import persist_signal
from src.notifications import notify_signal

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")
NIFTY_TOKEN = 256265
STRATEGY_ID = "daily_paper_test"
DEFAULT_FIRE_HOUR, DEFAULT_FIRE_MINUTE = 10, 30
STARTUP_GRACE_SECONDS = 60

# Fixed exit levels — user prefers simple, no trailing/scaling.
SL_PCT = 5.0
TP_PCT = 10.0

# No-trade zones (IST): lunch chop, and the last hour where theta dominates.
LUNCH_START = time(12, 30)
LUNCH_END = time(13, 30)
NO_ENTRY_AFTER = time(14, 30)


def _ema(values: list[float], period: int) -> float | None:
    if not values:
        return None
    k = 2 / (period + 1)
    e = values[0]
    for v in values[1:]:
        e = v * k + e * (1 - k)
    return e


def _no_trade_zone(now_ist: datetime) -> str | None:
    """Return a reason string if `now_ist` falls inside a no-trade zone, else None."""
    t = now_ist.time()
    if LUNCH_START <= t < LUNCH_END:
        return f"lunch_zone_{LUNCH_START:%H:%M}_{LUNCH_END:%H:%M}"
    if t >= NO_ENTRY_AFTER:
        return f"after_no_entry_{NO_ENTRY_AFTER:%H:%M}"
    return None


async def _15m_direction() -> tuple[int, str]:
    """Return (+1/-1/0, reason) — 15m EMA20-vs-EMA50 sign from fresh Kite candles."""
    from src.broker.kite_client import get_kite_client

    client = get_kite_client()
    to_dt = datetime.now(tz=IST)
    rows = await client.historical_data(
        instrument_token=NIFTY_TOKEN,
        from_dt=to_dt - timedelta(days=7),
        to_dt=to_dt,
        interval="15minute",
        oi=False,
    )
    closes = [float(r["close"]) for r in rows]
    e20, e50 = _ema(closes, 20), _ema(closes, 50)
    if e20 is None or e50 is None:
        return 0, "15m_data_insufficient"
    if e20 > e50:
        return 1, f"15m_up EMA20={e20:.0f}>EMA50={e50:.0f}"
    if e20 < e50:
        return -1, f"15m_down EMA20={e20:.0f}<EMA50={e50:.0f}"
    return 0, "15m_flat"


async def _daily_direction() -> tuple[int, str]:
    """Return (+1/-1/0, reason) — direction of the most recent COMPLETED daily candle."""
    from src.broker.kite_client import get_kite_client

    client = get_kite_client()
    to_dt = datetime.now(tz=IST)
    rows = await client.historical_data(
        instrument_token=NIFTY_TOKEN,
        from_dt=to_dt - timedelta(days=10),
        to_dt=to_dt,
        interval="day",
        oi=False,
    )
    # Last entry is today (forming); previous-completed is the one before that.
    if len(rows) < 2:
        return 0, "daily_data_insufficient"
    prev = rows[-2]
    o, c = float(prev["open"]), float(prev["close"])
    if c > o:
        return 1, f"daily_up O={o:.0f}<C={c:.0f}"
    if c < o:
        return -1, f"daily_down O={o:.0f}>C={c:.0f}"
    return 0, "daily_flat"


async def _decide_direction() -> tuple[OptionType | None, str]:
    """Multi-timeframe filter: daily AND 15m must agree; otherwise skip."""
    intraday, intraday_reason = await _15m_direction()
    daily, daily_reason = await _daily_direction()
    if intraday > 0 and daily > 0:
        return OptionType.CE, f"agree_up | {daily_reason} | {intraday_reason}"
    if intraday < 0 and daily < 0:
        return OptionType.PE, f"agree_down | {daily_reason} | {intraday_reason}"
    return None, f"disagree | {daily_reason} | {intraday_reason}"


async def _already_fired_today() -> bool:
    """Idempotency guard — true if a daily_paper_test signal exists today (IST)."""
    today_ist = datetime.now(tz=timezone.utc).astimezone(IST).date()
    today_utc = IST.localize(
        datetime.combine(today_ist, datetime.min.time())
    ).astimezone(timezone.utc)
    async with get_session() as s:
        existing = (
            await s.execute(
                select(Signal.id)
                .where(Signal.strategy_id == STRATEGY_ID)
                .where(Signal.ts >= today_utc)
                .limit(1)
            )
        ).scalar_one_or_none()
    return existing is not None


async def _inject(bus: EventBus, opt: OptionType, reason: str) -> None:
    now = datetime.now(tz=timezone.utc)
    sig = SignalCandidate(
        event_id=str(uuid.uuid4()),
        ts=now,
        strategy_id=STRATEGY_ID,
        underlying="NIFTY 50",
        side=Side.BUY,
        option_type=opt,
        confidence=70.0,
        rationale=[
            "daily paper validation",
            f"direction: {reason}",
            f"fixed exits SL={SL_PCT:.0f}% TP={TP_PCT:.0f}%",
        ],
        indicators_snapshot={},
        suggested_sl_pct=SL_PCT,
        suggested_target_pct=TP_PCT,
    )
    await persist_signal(sig)
    await bus.publish(STREAM_SIGNALS, sig)
    await notify_signal(
        bus,
        strategy_id=sig.strategy_id,
        underlying=sig.underlying,
        side=sig.side.value,
        option_type=opt.value,
        confidence=sig.confidence,
    )
    log.info("daily_test_injected", option_type=opt.value, reason=reason)


def _next_fire_seconds(hour: int, minute: int) -> tuple[float, bool]:
    """Return (sleep_seconds, today_already_past).

    If today's fire time hasn't passed yet, sleep until today.
    If it has passed, sleep until tomorrow's slot.
    """
    now_ist = datetime.now(tz=timezone.utc).astimezone(IST)
    today_slot = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_ist < today_slot:
        return (today_slot - now_ist).total_seconds(), False
    tomorrow_slot = today_slot + timedelta(days=1)
    return (tomorrow_slot - now_ist).total_seconds(), True


async def _maybe_fire(bus: EventBus) -> None:
    """Fire the daily test if today's slot has been hit and all conditions allow."""
    now_ist = datetime.now(tz=timezone.utc).astimezone(IST)
    if now_ist.weekday() >= 5:
        log.info("daily_test_skip_weekend")
        return
    if get_kill_switch().armed:
        log.info("daily_test_skip_kill_switch")
        return
    if await _already_fired_today():
        log.info("daily_test_skip_already_fired_today")
        return
    zone = _no_trade_zone(now_ist)
    if zone is not None:
        log.info("daily_test_skip_no_trade_zone", zone=zone)
        return
    opt, reason = await _decide_direction()
    if opt is None:
        log.info("daily_test_skip_filter_disagree", reason=reason)
        return
    try:
        await _inject(bus, opt, reason)
    except Exception as e:  # noqa: BLE001
        log.exception("daily_test_inject_failed", error=str(e))


async def run_daily_test_loop(
    bus: EventBus,
    *,
    fire_hour: int = DEFAULT_FIRE_HOUR,
    fire_minute: int = DEFAULT_FIRE_MINUTE,
) -> None:
    """One-shot-per-day paper-mode test injection. No-op in non-paper modes."""
    if settings.mode != "paper":
        log.info("daily_test_skip_non_paper_mode", mode=settings.mode)
        return
    log.info("daily_test_starting", fire_hour=fire_hour, fire_minute=fire_minute)

    # Wait briefly so the position watcher can square off any stale overnight
    # trade before we try to inject (avoids duplicate_symbol on same ATM strike).
    await asyncio.sleep(STARTUP_GRACE_SECONDS)

    # Catch-up: if we started after today's slot and haven't fired, fire now.
    sleep_s, today_passed = _next_fire_seconds(fire_hour, fire_minute)
    if today_passed:
        await _maybe_fire(bus)

    while True:
        try:
            await asyncio.sleep(sleep_s)
            await _maybe_fire(bus)
            sleep_s, _ = _next_fire_seconds(fire_hour, fire_minute)
        except asyncio.CancelledError:
            log.info("daily_test_cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("daily_test_loop_error", error=str(e))
            await asyncio.sleep(60)
            sleep_s, _ = _next_fire_seconds(fire_hour, fire_minute)
