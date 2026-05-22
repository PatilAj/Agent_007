"""
Phase 1 ingestion worker.

Wires the data pipeline:

    [Kite WSS] ──► [Event Bus: ticks]
                          │
            ┌─────────────┴─────────────┐
            ▼                           ▼
    [Tick Writer]               [Bar Aggregator]
            │                           │
            ▼                           ▼
    [TimescaleDB: ticks]        [Event Bus: bars]
                                        │
                                        ▼
                                [Candle Writer]
                                        │
                                        ▼
                                [TimescaleDB: candles]

Each consumer reads from the bus independently (separate consumer groups),
so a slow DB writer can't block the in-memory aggregator.

Run as: python -m src.workers.ingestor
"""
from __future__ import annotations

import asyncio
import signal
import sys
from typing import Any

from sqlalchemy import select

from src.broker.instrument_catalog import find_underlying_token
from src.broker.kite_ws import WSSManager
from src.core.bus import (
    STREAM_BARS_1M,
    EventBus,
)
from src.core.config import settings
from src.core.kill_switch import get_kill_switch, install_signal_handlers
from src.core.logging import configure_logging, get_logger
from src.data.backfill import backfill_many
from src.data.bar_aggregator import run_aggregator_loop
from src.data.db import get_session
from src.data.models import Instrument
from src.data.repositories.candles import run_candle_writer_loop
from src.data.repositories.ticks import run_tick_writer_loop
from src.indicators import run_indicator_loop, warmup_calculators
from src.regime import run_regime_loop

log = get_logger(__name__)


async def build_subscription_list() -> tuple[list[int], dict[int, str]]:
    """
    Decide which instruments to subscribe to.

    Phase 1: just the configured underlying indices (spot tokens).
    Phase 3 will add ATM±N option strikes for each underlying.
    """
    tokens: list[int] = []
    symbol_map: dict[int, str] = {}

    for u in settings.instruments.underlyings:
        tok = await find_underlying_token(u.symbol, u.exchange)
        if tok is None:
            log.warning("underlying_token_not_found", symbol=u.symbol)
            continue
        tokens.append(tok)
        symbol_map[tok] = u.symbol

    if not tokens:
        # Fallback: pull a few index tokens by name search
        async with get_session() as s:
            stmt = (
                select(Instrument.instrument_token, Instrument.tradingsymbol)
                .where(Instrument.exchange == "NSE")
                .where(Instrument.instrument_type == "EQ")
                .limit(0)  # don't blindly subscribe — fail loudly
            )
            _ = (await s.execute(stmt)).all()

    log.info("subscription_list_built", count=len(tokens), tokens=tokens)
    return tokens, symbol_map


async def warmup(tokens: list[int]) -> None:
    """Backfill recent history so indicators start warm."""
    log.info("warmup_starting", tokens=len(tokens))
    days = settings.data.historical_warmup_days
    resolutions = settings.data.candle_resolutions
    results = await backfill_many(tokens, resolutions, days=days)
    total = sum(v for v in results.values() if v > 0)
    log.info("warmup_done", total_rows=total)


async def main() -> int:
    configure_logging(level=settings.log_level, format="console", log_dir=settings.logging.log_dir)
    log.info("ingestor_starting", mode=settings.mode)

    install_signal_handlers()

    bus = await EventBus.connect()
    log.info("bus_connected")

    # Build subscription list
    tokens, symbol_map = await build_subscription_list()
    if not tokens:
        log.error("no_instruments_to_subscribe")
        return 1

    # Warmup historical candles (Phase 1)
    await warmup(tokens)

    # Warmup indicator calculators from DB (Phase 2)
    tokens_with_symbols = [(t, symbol_map.get(t, f"TOKEN_{t}")) for t in tokens]
    n_replayed = await warmup_calculators(
        tokens_with_symbols, resolutions=["1minute"], lookback=200
    )
    log.info("indicator_warmup_total", bars_replayed=n_replayed)

    # Start the WSS manager
    wss = WSSManager(bus, symbol_lookup=lambda t: symbol_map.get(t, f"TOKEN_{t}"))
    await wss.start(tokens)

    # Launch consumers as tasks
    tick_writer_task = asyncio.create_task(run_tick_writer_loop(bus))
    aggregator_task = asyncio.create_task(run_aggregator_loop(bus))
    candle_writer_task = asyncio.create_task(
        run_candle_writer_loop(bus, stream=STREAM_BARS_1M)
    )
    indicator_task = asyncio.create_task(run_indicator_loop(bus, stream=STREAM_BARS_1M))
    regime_task = asyncio.create_task(run_regime_loop(bus))

    # Wait for kill signal
    stop_event = asyncio.Event()

    def _on_signal(signame: str) -> None:
        log.warning("signal_received", signame=signame)
        get_kill_switch().arm(reason=f"signal:{signame}")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal, sig.name)
        except (NotImplementedError, RuntimeError):
            pass

    log.info("ingestor_running")
    try:
        await stop_event.wait()
    finally:
        log.info("ingestor_shutting_down")
        wss.stop()
        consumer_tasks = (
            tick_writer_task,
            aggregator_task,
            candle_writer_task,
            indicator_task,
            regime_task,
        )
        for t in consumer_tasks:
            t.cancel()
        await asyncio.gather(*consumer_tasks, return_exceptions=True)
        await bus.close()
        log.info("ingestor_stopped")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        # Ctrl+C — cleanup already happened in main()'s finally block.
        sys.exit(0)
