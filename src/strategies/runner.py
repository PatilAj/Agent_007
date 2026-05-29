"""
Strategy runner loop.

Owns:
  - The collection of active Strategy instances
  - Consuming RegimeUpdate (primary trigger) and IndicatorUpdate events
  - Routing each event to every strategy, collecting any SignalCandidate
  - Persisting each candidate to the signal journal
  - Publishing each candidate to stream:signals (for the risk engine in Phase 4)

This is the only place where strategies touch the outside world. Strategies
themselves remain pure-ish state machines.
"""
from __future__ import annotations

import asyncio

from src.core.bus import (
    STREAM_BARS_1M,
    STREAM_INDICATORS,
    STREAM_REGIME,
    STREAM_SIGNALS,
    EventBus,
)
from src.core.events import BarEvent, IndicatorUpdate, RegimeUpdate, SignalCandidate
from src.core.logging import get_logger
from src.journal import persist_signal
from src.notifications import notify_signal
from src.strategies.base import Strategy
from src.strategies.ema_regime import EMARegimeStrategy
from src.strategies.orb_breakout import ORBBreakoutStrategy

log = get_logger(__name__)


def default_strategies() -> list[Strategy]:
    """Strategies loaded by default. Independent edges — they coexist safely
    because the risk engine blocks duplicate same-symbol entries."""
    return [EMARegimeStrategy(), ORBBreakoutStrategy()]


async def _emit(bus: EventBus, sig: SignalCandidate) -> None:
    """Persist + publish a single SignalCandidate."""
    try:
        await persist_signal(sig)
    except Exception as e:  # noqa: BLE001
        log.exception("signal_persist_failed", event_id=sig.event_id, error=str(e))
    try:
        await bus.publish(STREAM_SIGNALS, sig)
    except Exception as e:  # noqa: BLE001
        log.exception("signal_publish_failed", event_id=sig.event_id, error=str(e))
    await notify_signal(
        bus,
        strategy_id=sig.strategy_id,
        underlying=sig.underlying,
        side=sig.side.value,
        option_type=sig.option_type.value,
        confidence=sig.confidence,
    )


async def _consume_regime(
    bus: EventBus,
    strategies: list[Strategy],
    group: str = "strategy-runner",
    consumer: str = "sr-1",
) -> None:
    stream = STREAM_REGIME
    log.info("strategy_regime_consumer_starting", stream=stream)
    async for msg_id, payload in bus.consume(stream, group, consumer, count=20):
        try:
            update = RegimeUpdate.model_validate(payload)
            for strat in strategies:
                sig = strat.on_regime(update)
                if sig is not None:
                    await _emit(bus, sig)
        except Exception as e:  # noqa: BLE001
            log.exception("strategy_regime_failed", error=str(e))
        finally:
            await bus.ack(stream, group, msg_id)


async def _consume_indicators(
    bus: EventBus,
    strategies: list[Strategy],
    group: str = "strategy-runner-ind",
    consumer: str = "sri-1",
) -> None:
    stream = STREAM_INDICATORS
    log.info("strategy_indicator_consumer_starting", stream=stream)
    async for msg_id, payload in bus.consume(stream, group, consumer, count=50):
        try:
            update = IndicatorUpdate.model_validate(payload)
            for strat in strategies:
                sig = strat.on_indicator(update)
                if sig is not None:
                    await _emit(bus, sig)
        except Exception as e:  # noqa: BLE001
            log.exception("strategy_indicator_failed", error=str(e))
        finally:
            await bus.ack(stream, group, msg_id)


async def _consume_bars(
    bus: EventBus,
    strategies: list[Strategy],
    group: str = "strategy-runner-bars",
    consumer: str = "srb-1",
) -> None:
    """Bar stream — used by strategies that need raw OHLC (e.g. ORB)."""
    stream = STREAM_BARS_1M
    log.info("strategy_bar_consumer_starting", stream=stream)
    async for msg_id, payload in bus.consume(stream, group, consumer, count=20):
        try:
            bar = BarEvent.model_validate(payload)
            for strat in strategies:
                sig = strat.on_bar(bar)
                if sig is not None:
                    await _emit(bus, sig)
        except Exception as e:  # noqa: BLE001
            log.exception("strategy_bar_failed", error=str(e))
        finally:
            await bus.ack(stream, group, msg_id)


async def run_strategy_loop(
    bus: EventBus,
    strategies: list[Strategy] | None = None,
) -> None:
    """Run the strategy engine: consume bars + indicators + regime concurrently."""
    strategies = strategies or default_strategies()
    log.info(
        "strategy_loop_starting",
        strategies=[s.id for s in strategies],
    )
    await asyncio.gather(
        _consume_bars(bus, strategies),
        _consume_regime(bus, strategies),
        _consume_indicators(bus, strategies),
    )
