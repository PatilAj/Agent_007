"""
Signal journal — persistence layer for SignalCandidate events.

Every signal emitted by any strategy lands here, regardless of whether
the risk engine eventually accepts it. The journal is the source of
truth for "what did the system want to do, and what actually happened?"
analysis.

Table schema lives in src.data.models.Signal (already migrated in 0001_init).
"""
from __future__ import annotations

from sqlalchemy import select, update

from src.core.events import SignalCandidate
from src.core.logging import get_logger
from src.data.db import get_session
from src.data.models import Signal

log = get_logger(__name__)


async def persist_signal(sig: SignalCandidate, *, accepted: bool = False) -> None:
    """Insert a SignalCandidate into the journal. Idempotent on event_id."""
    async with get_session() as s:
        # Avoid duplicate inserts if a signal is replayed
        existing = await s.execute(select(Signal.id).where(Signal.event_id == sig.event_id))
        if existing.scalar_one_or_none() is not None:
            log.debug("signal_already_journaled", event_id=sig.event_id)
            return

        s.add(
            Signal(
                event_id=sig.event_id,
                ts=sig.ts,
                strategy_id=sig.strategy_id,
                underlying=sig.underlying,
                side=sig.side.value,
                option_type=sig.option_type.value,
                confidence=sig.confidence,
                rationale=list(sig.rationale),
                indicators_snapshot=dict(sig.indicators_snapshot),
                accepted=accepted,
                rejection_reason=None,
                suggested_sl_pct=sig.suggested_sl_pct,
                suggested_target_pct=sig.suggested_target_pct,
            )
        )
    log.info(
        "signal_journaled",
        event_id=sig.event_id,
        strategy=sig.strategy_id,
        underlying=sig.underlying,
        side=sig.side.value,
        option_type=sig.option_type.value,
        confidence=round(sig.confidence, 2),
    )


async def mark_signal_outcome(
    event_id: str,
    *,
    accepted: bool,
    rejection_reason: str | None = None,
) -> None:
    """Update an existing signal row with the risk-engine verdict."""
    async with get_session() as s:
        await s.execute(
            update(Signal)
            .where(Signal.event_id == event_id)
            .values(accepted=accepted, rejection_reason=rejection_reason)
        )
    log.info(
        "signal_outcome_recorded",
        event_id=event_id,
        accepted=accepted,
        rejection_reason=rejection_reason,
    )


async def recent_signals(strategy_id: str | None = None, limit: int = 50) -> list[Signal]:
    """For dashboards / inspection."""
    async with get_session() as s:
        stmt = select(Signal).order_by(Signal.ts.desc()).limit(limit)
        if strategy_id is not None:
            stmt = stmt.where(Signal.strategy_id == strategy_id)
        return list((await s.execute(stmt)).scalars().all())
