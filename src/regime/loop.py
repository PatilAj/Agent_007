"""
Regime consumer loop.

Consumes IndicatorUpdate events from stream:indicators, maintains a
RegimeDetector per (token, resolution), and publishes RegimeUpdate
events to stream:regime when the regime label transitions.
"""
from __future__ import annotations

import uuid

from src.core.bus import STREAM_INDICATORS, STREAM_REGIME, EventBus
from src.core.events import IndicatorUpdate, RegimeUpdate
from src.core.logging import get_logger
from src.regime.detector import RegimeDetector

log = get_logger(__name__)

_detectors: dict[tuple[int, str], RegimeDetector] = {}


def _get_detector(token: int, symbol: str, resolution: str) -> RegimeDetector:
    key = (token, resolution)
    det = _detectors.get(key)
    if det is None:
        det = RegimeDetector(token, symbol, resolution)
        _detectors[key] = det
    return det


async def run_regime_loop(
    bus: EventBus,
    stream: str = STREAM_INDICATORS,
    group: str = "regime-detector",
    consumer: str = "rg-1",
) -> None:
    log.info("regime_loop_starting", stream=stream)
    async for msg_id, payload in bus.consume(stream, group, consumer, count=50):
        try:
            update = IndicatorUpdate.model_validate(payload)
            det = _get_detector(update.instrument_token, update.symbol, update.resolution)
            det.update_indicator(update.name, update.value)

            # The bar_ts close isn't on IndicatorUpdate; we approximate via the latest EMA20
            # (which equals close on the seed bar and tracks it closely afterwards).
            # For classification thresholds we only need close > 0 — any EMA suffices.
            if update.name == "ema20" and update.value is not None:
                det.update_close(update.value)

            label, score, rationale = det.classify_current()
            if det.should_emit(label):
                await bus.publish(
                    STREAM_REGIME,
                    RegimeUpdate(
                        event_id=str(uuid.uuid4()),
                        ts=update.ts,
                        label=label,
                        score=score,
                        rationale={
                            "instrument_token": update.instrument_token,
                            "symbol": update.symbol,
                            "resolution": update.resolution,
                            **rationale,
                        },
                    ),
                )
                log.info(
                    "regime_transition",
                    token=update.instrument_token,
                    resolution=update.resolution,
                    label=label.value,
                    score=round(score, 3),
                )
        except Exception as e:  # noqa: BLE001
            log.exception("regime_classify_failed", error=str(e))
        finally:
            await bus.ack(stream, group, msg_id)
