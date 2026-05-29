"""
Risk engine — the hard gate between strategy signals and orders.

Every SignalCandidate runs through `assess()`, which:

  1. Runs a stack of gates in order; the first to fail produces a `Rejection`.
     Gates are intentionally ordered cheapest-first so cheap rejects skip
     the expensive option-quote round-trip.
  2. If all gates pass, picks the option contract (Phase 3 selector),
     fetches its quote, sizes the position by available premium budget,
     and emits a fully-populated `OrderRequest`.
  3. Persists the verdict to:
       - `signals.accepted` / `signals.rejection_reason`
       - `risk_events`  (audit trail of every gate decision)

Gate ordering (cheap → expensive):

  A. local kill-switch
  B. config kill-switch (env var)
  C. market window (entry allowed at this time-of-day, IST)
  D. consecutive-losses cooldown
  E. trades-today count
  F. daily-loss cap
  G. concurrent-positions count
  H. duplicate position for same symbol
  I. ── option selected here (expensive: DB + maybe quote) ──
  J. liquidity (premium >= floor, OI >= floor)
  K. spread (bid-ask spread <= ceiling)
  L. premium cap (premium × lot_size <= max_premium_per_trade)
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from decimal import Decimal
from typing import Any

import pytz
from sqlalchemy import select

from src.broker.kite_client import get_kite_client
from src.core.bus import STREAM_ORDERS, STREAM_SIGNALS, EventBus
from src.core.config import settings
from src.core.events import (
    OptionContract,
    OptionType,
    OrderRequest,
    Side,
    SignalCandidate,
)
from src.core.kill_switch import check_redis_kill_switch, get_kill_switch
from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import RiskEvent
from src.journal.signal_journal import mark_signal_outcome
from src.options import select_option_for_signal
from src.risk.position_tracker import PositionTracker, get_position_tracker

log = get_logger(__name__)
IST = pytz.timezone("Asia/Kolkata")


@dataclass
class RiskVerdict:
    accepted: bool
    gate: str | None
    reason: str | None
    order: OrderRequest | None = None
    context: dict[str, Any] = field(default_factory=dict)


def _now_ist(now: datetime | None = None) -> datetime:
    return (now or datetime.now(tz=timezone.utc)).astimezone(IST)


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(hour=int(h), minute=int(m))


class RiskEngine:
    """Stateless validator + sizer. State lives in DB via PositionTracker."""

    def __init__(self, tracker: PositionTracker | None = None):
        self.tracker = tracker or get_position_tracker()

    # ----------------- cheap gates -----------------

    def _gate_local_kill_switch(self) -> RiskVerdict | None:
        ks = get_kill_switch()
        if ks.armed:
            return RiskVerdict(
                False, "local_kill_switch", ks.reason() or "armed",
            )
        return None

    def _gate_env_kill_switch(self) -> RiskVerdict | None:
        if settings.kill_switch:
            return RiskVerdict(False, "env_kill_switch", "KILL_SWITCH env var is true")
        return None

    def _gate_market_window(self, signal: SignalCandidate) -> RiskVerdict | None:
        cfg = settings.market
        now_ist = _now_ist(signal.ts)
        open_t = _parse_hhmm(cfg.open_time)
        no_entry_after = _parse_hhmm(cfg.no_entry_after)
        if now_ist.time() < open_t:
            return RiskVerdict(False, "market_window", f"before open {cfg.open_time}")
        if now_ist.time() >= no_entry_after:
            return RiskVerdict(
                False, "market_window", f"after no_entry_after {cfg.no_entry_after}",
                context={"now_ist": now_ist.strftime("%H:%M:%S")},
            )
        # Skip weekends (Indian market closed Sat=5, Sun=6)
        if now_ist.weekday() >= 5:
            return RiskVerdict(False, "market_window", "weekend")
        return None

    # ----------------- state-based gates -----------------

    async def _gate_consecutive_losses(self, signal: SignalCandidate) -> RiskVerdict | None:
        limit = settings.risk.max_consecutive_losses
        n = await self.tracker.consecutive_losses(signal.ts)
        if n >= limit:
            return RiskVerdict(
                False, "consecutive_losses",
                f"consecutive_losses={n} >= max={limit} (cooldown active)",
                context={"consecutive_losses": n, "limit": limit},
            )
        return None

    async def _gate_trades_today(self, signal: SignalCandidate) -> RiskVerdict | None:
        limit = settings.risk.max_trades_per_day
        n = await self.tracker.trade_count_today(signal.ts)
        if n >= limit:
            return RiskVerdict(
                False, "trades_today", f"trades_today={n} >= max={limit}",
                context={"trades_today": n, "limit": limit},
            )
        return None

    async def _gate_daily_loss(self, signal: SignalCandidate) -> RiskVerdict | None:
        cap = settings.risk.slot_capital_inr
        max_loss_pct = settings.risk.max_daily_loss_pct
        max_loss_inr = Decimal(str(cap)) * Decimal(str(max_loss_pct)) / Decimal("100")
        pnl = await self.tracker.daily_pnl(signal.ts)
        if pnl <= -max_loss_inr:
            return RiskVerdict(
                False, "daily_loss",
                f"daily_pnl={pnl} <= -max_loss={-max_loss_inr}",
                context={"daily_pnl": float(pnl), "max_daily_loss": float(max_loss_inr)},
            )
        return None

    async def _gate_concurrent_positions(self, signal: SignalCandidate) -> RiskVerdict | None:
        limit = settings.risk.max_concurrent_positions
        n = await self.tracker.count_open()
        if n >= limit:
            return RiskVerdict(
                False, "concurrent_positions",
                f"open={n} >= max={limit}",
                context={"open": n, "limit": limit},
            )
        return None

    # ----------------- expensive gates (need contract + quote) -----------------

    async def _gate_duplicate_symbol(self, contract: OptionContract) -> RiskVerdict | None:
        if await self.tracker.is_position_open_for(contract.tradingsymbol):
            return RiskVerdict(
                False, "duplicate_symbol",
                f"position already open for {contract.tradingsymbol}",
                context={"tradingsymbol": contract.tradingsymbol},
            )
        return None

    def _gate_liquidity(self, premium: float, oi: int) -> RiskVerdict | None:
        if premium < settings.risk.premium_min_inr:
            return RiskVerdict(
                False, "liquidity_premium",
                f"premium={premium} < min={settings.risk.premium_min_inr}",
            )
        if oi < settings.option_selector.min_oi_index:
            return RiskVerdict(
                False, "liquidity_oi",
                f"oi={oi} < min={settings.option_selector.min_oi_index}",
            )
        return None

    def _gate_spread(self, bid: float, ask: float) -> RiskVerdict | None:
        if bid <= 0 or ask <= 0:
            return RiskVerdict(False, "spread", "missing bid or ask")
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100 if mid > 0 else 1e9
        if spread_pct > settings.risk.spread_max_pct:
            return RiskVerdict(
                False, "spread",
                f"spread_pct={spread_pct:.2f} > max={settings.risk.spread_max_pct}",
                context={"bid": bid, "ask": ask, "spread_pct": spread_pct},
            )
        return None

    def _size_position(self, premium: float, lot_size: int) -> tuple[int, float, str | None]:
        """Return (lots, total_premium_inr, reject_reason | None)."""
        cap = settings.risk.slot_capital_inr
        max_prem_pct = settings.risk.max_premium_per_trade_pct
        budget = float(cap) * float(max_prem_pct) / 100.0
        per_lot_cost = premium * lot_size
        if per_lot_cost <= 0:
            return 0, 0.0, "per-lot cost zero"
        lots = int(budget // per_lot_cost)
        if lots < 1:
            return 0, 0.0, (
                f"premium*lot_size={per_lot_cost:.2f} > budget={budget:.2f} "
                f"(max_premium_per_trade_pct={max_prem_pct}% of {cap})"
            )
        return lots, lots * per_lot_cost, None

    # ----------------- main entry point -----------------

    async def assess(self, signal: SignalCandidate) -> RiskVerdict:
        # Cheap, in-process gates first
        for g in (
            self._gate_local_kill_switch(),
            self._gate_env_kill_switch(),
            self._gate_market_window(signal),
        ):
            if g is not None:
                return g

        for gate_fn in (
            self._gate_consecutive_losses,
            self._gate_trades_today,
            self._gate_daily_loss,
            self._gate_concurrent_positions,
        ):
            v = await gate_fn(signal)
            if v is not None:
                return v

        # Resolve actual option contract — uses DB + spot tick
        try:
            contract = await select_option_for_signal(signal.underlying, signal.option_type)
        except Exception as e:  # noqa: BLE001
            return RiskVerdict(
                False, "option_selection", str(e),
                context={"underlying": signal.underlying, "option_type": signal.option_type.value},
            )

        dup = await self._gate_duplicate_symbol(contract)
        if dup is not None:
            return dup

        # Fetch quote for the chosen contract
        try:
            kite = get_kite_client()
            quote_key = f"NFO:{contract.tradingsymbol}"
            q = await kite.quote([quote_key])
            row = q.get(quote_key) or next(iter(q.values()), {})
            last = float(row.get("last_price") or 0)
            depth = row.get("depth") or {}
            bid = float(depth.get("buy", [{}])[0].get("price") or 0) if depth.get("buy") else 0
            ask = float(depth.get("sell", [{}])[0].get("price") or 0) if depth.get("sell") else 0
            oi = int(row.get("oi") or 0)
        except Exception as e:  # noqa: BLE001
            return RiskVerdict(
                False, "quote_fetch", f"could not fetch quote for {contract.tradingsymbol}: {e}",
            )

        # Use mid as fair premium where available, else LTP
        premium = (bid + ask) / 2 if bid > 0 and ask > 0 else last
        if premium <= 0:
            return RiskVerdict(
                False, "quote_invalid", f"no price data on {contract.tradingsymbol}",
                context={"last": last, "bid": bid, "ask": ask},
            )

        liq = self._gate_liquidity(premium, oi)
        if liq is not None:
            return liq

        spr = self._gate_spread(bid, ask)
        if spr is not None and bid > 0 and ask > 0:
            return spr

        lots, total_premium, size_err = self._size_position(premium, contract.lot_size)
        if size_err:
            return RiskVerdict(
                False, "premium_cap", size_err,
                context={"premium": premium, "lot_size": contract.lot_size},
            )

        qty = lots * contract.lot_size
        # Limit price = ask + small buffer for taker fill; falls back to last
        limit_price = (ask * 1.002) if ask > 0 else (last * 1.002 if last > 0 else premium)

        order = OrderRequest(
            event_id=str(uuid.uuid4()),
            ts=signal.ts,
            client_order_id=str(uuid.uuid4()),
            strategy_id=signal.strategy_id,
            signal_event_id=signal.event_id,
            contract=contract,
            side=signal.side,
            qty=qty,
            order_type="LIMIT",
            price=Decimal(f"{limit_price:.2f}"),
            product="MIS",
            validity="DAY",
            tag=f"{signal.strategy_id}:{signal.underlying}",
        )

        return RiskVerdict(
            True, None, None, order=order,
            context={
                "premium": premium,
                "lots": lots,
                "qty": qty,
                "total_premium": total_premium,
                "bid": bid, "ask": ask, "oi": oi,
            },
        )


# ----------------- bus glue -----------------


async def _journal_risk_event(verdict: RiskVerdict, signal: SignalCandidate) -> None:
    async with get_session() as s:
        s.add(
            RiskEvent(
                event_type="signal_review",
                gate=verdict.gate,
                action="accepted" if verdict.accepted else "rejected",
                context={
                    "signal_event_id": signal.event_id,
                    "strategy": signal.strategy_id,
                    "underlying": signal.underlying,
                    "side": signal.side.value,
                    "option_type": signal.option_type.value,
                    "reason": verdict.reason,
                    **verdict.context,
                },
            )
        )


async def run_risk_loop(
    bus: EventBus,
    engine: RiskEngine | None = None,
    group: str = "risk-engine",
    consumer: str = "rk-1",
) -> None:
    engine = engine or RiskEngine()
    stream = STREAM_SIGNALS
    log.info("risk_loop_starting", stream=stream)
    async for msg_id, payload in bus.consume(stream, group, consumer, count=10):
        try:
            signal = SignalCandidate.model_validate(payload)
            verdict = await engine.assess(signal)
            await _journal_risk_event(verdict, signal)
            await mark_signal_outcome(
                signal.event_id,
                accepted=verdict.accepted,
                rejection_reason=verdict.reason if not verdict.accepted else None,
            )
            if verdict.accepted and verdict.order is not None:
                await bus.publish(STREAM_ORDERS, verdict.order)
                log.info(
                    "risk_signal_accepted",
                    signal_event_id=signal.event_id,
                    symbol=verdict.order.contract.tradingsymbol,
                    qty=verdict.order.qty,
                    price=float(verdict.order.price) if verdict.order.price else None,
                )
            else:
                log.info(
                    "risk_signal_rejected",
                    signal_event_id=signal.event_id,
                    gate=verdict.gate,
                    reason=verdict.reason,
                )
        except Exception as e:  # noqa: BLE001
            log.exception("risk_loop_error", error=str(e))
        finally:
            await bus.ack(stream, group, msg_id)
