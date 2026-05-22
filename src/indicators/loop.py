"""
Indicator consumer loop.

Consumes BarEvents from a bar stream, routes each bar to a stateful
IndicatorCalculator (one per (instrument_token, resolution)), and
publishes IndicatorUpdate events to stream:indicators.

On startup, calculators are warmed from the DB so live updates emit
real values from bar #1 instead of waiting 14+ bars for RSI/ATR.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from src.core.bus import STREAM_BARS_1M, STREAM_INDICATORS, EventBus
from src.core.events import BarEvent
from src.core.logging import get_logger
from src.data.repositories.candles import fetch_candles
from src.indicators import IndicatorCalculator

log = get_logger(__name__)


# Module-level registry so warmup populates the same calculators the loop uses.
_calculators: dict[tuple[int, str], IndicatorCalculator] = {}


def _get_calculator(token: int, symbol: str, resolution: str) -> IndicatorCalculator:
    key = (token, resolution)
    calc = _calculators.get(key)
    if calc is None:
        calc = IndicatorCalculator(token, symbol, resolution)
        _calculators[key] = calc
    return calc


async def warmup_calculators(
    tokens_with_symbols: list[tuple[int, str]],
    resolutions: list[str],
    lookback: int = 200,
) -> int:
    """Bootstrap calculator state by replaying historical candles from DB.

    Returns total bars replayed.
    """
    total = 0
    for token, symbol in tokens_with_symbols:
        for resolution in resolutions:
            rows = await fetch_candles(token, resolution, limit=lookback)
            if not rows:
                continue
            bars = [
                BarEvent(
                    event_id=str(uuid.uuid4()),
                    ts=r["ts"] if r["ts"].tzinfo else r["ts"].replace(tzinfo=timezone.utc),
                    bar_ts=r["ts"] if r["ts"].tzinfo else r["ts"].replace(tzinfo=timezone.utc),
                    instrument_token=token,
                    symbol=symbol,
                    resolution=resolution,  # type: ignore[arg-type]
                    o=Decimal(str(r["o"])),
                    h=Decimal(str(r["h"])),
                    l=Decimal(str(r["l"])),
                    c=Decimal(str(r["c"])),
                    v=int(r["v"] or 0),
                    oi=int(r["oi"]) if r.get("oi") is not None else None,
                )
                for r in rows
            ]
            calc = _get_calculator(token, symbol, resolution)
            calc.warmup(bars)
            total += len(bars)
            log.info(
                "indicator_warmup_done",
                token=token,
                resolution=resolution,
                bars=len(bars),
            )
    return total


async def run_indicator_loop(
    bus: EventBus,
    stream: str = STREAM_BARS_1M,
    group: str = "indicator-calc",
    consumer: str = "ic-1",
) -> None:
    log.info("indicator_loop_starting", stream=stream)
    async for msg_id, payload in bus.consume(stream, group, consumer, count=20):
        try:
            bar = BarEvent.model_validate(payload)
            calc = _get_calculator(bar.instrument_token, bar.symbol, bar.resolution)
            updates = calc.update(bar)
            for ev in updates:
                await bus.publish(STREAM_INDICATORS, ev)
        except Exception as e:  # noqa: BLE001
            log.exception("indicator_update_failed", error=str(e))
        finally:
            await bus.ack(stream, group, msg_id)
