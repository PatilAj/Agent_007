"""
Kite Connect WebSocket manager.

Responsibilities:
  - Maintain a single WSS connection to Kite
  - Handle subscribe/unsubscribe for up to 3000 instruments
  - Auto-reconnect with exponential backoff
  - Parse binary tick frames into TickEvent
  - Push ticks onto the event bus (stream:ticks)
  - Emit health events on connect/disconnect

The official KiteTicker is callback-based and runs its own twisted reactor.
We wrap it in an asyncio-friendly interface using a queue bridge.

CRITICAL safety notes:
  - The WSS thread MUST NOT block. Tick processing must be O(1).
  - All DB writes happen in a separate consumer task reading from the bus.
  - On disconnect, do NOT retry forever during market hours without alerting.
"""
from __future__ import annotations

import asyncio
import threading
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from src.auth.kite_session import ensure_valid_token
from src.core.bus import STREAM_TICKS, EventBus
from src.core.config import settings
from src.core.events import HealthEvent, TickEvent
from src.core.logging import get_logger

log = get_logger(__name__)


class WSSManager:
    """Asyncio-friendly wrapper around KiteTicker."""

    def __init__(
        self,
        bus: EventBus,
        symbol_lookup: Callable[[int], str] | None = None,
    ):
        self.bus = bus
        self.symbol_lookup = symbol_lookup or (lambda tok: f"TOKEN_{tok}")
        self._ticker: Any | None = None
        self._subscribed: set[int] = set()
        self._connected = threading.Event()
        self._stop_flag = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reconnect_count = 0
        self._last_tick_ts: float = 0.0
        self._tick_count = 0

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    @property
    def seconds_since_last_tick(self) -> float:
        if self._last_tick_ts == 0:
            return -1
        return time.monotonic() - self._last_tick_ts

    @property
    def tick_count(self) -> int:
        return self._tick_count

    async def start(self, tokens: list[int]) -> None:
        """Connect and subscribe. Returns once connection is open."""
        from kiteconnect import KiteTicker

        self._loop = asyncio.get_running_loop()
        api_key = settings.kite_api_key.get_secret_value()
        access_token = await ensure_valid_token()

        if len(tokens) > settings.data.websocket_max_instruments:
            raise ValueError(
                f"Too many tokens: {len(tokens)} > "
                f"{settings.data.websocket_max_instruments}"
            )

        ticker = KiteTicker(api_key, access_token)

        # --- callbacks ---
        def on_connect(ws: Any, response: Any) -> None:
            log.info("wss_connected", reconnect_count=self._reconnect_count)
            self._connected.set()
            self._reconnect_count = 0
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_FULL, tokens)
            self._subscribed = set(tokens)
            self._publish_health("healthy", {"subscribed": len(tokens)})

        def on_close(ws: Any, code: int, reason: str) -> None:
            log.warning("wss_closed", code=code, reason=reason)
            self._connected.clear()
            self._publish_health("degraded", {"close_code": code, "reason": reason})

        def on_error(ws: Any, code: int, reason: str) -> None:
            log.error("wss_error", code=code, reason=reason)
            self._publish_health("degraded", {"error_code": code, "reason": reason})

        def on_reconnect(ws: Any, attempts_count: int) -> None:
            self._reconnect_count = attempts_count
            log.warning("wss_reconnecting", attempt=attempts_count)

        def on_noreconnect(ws: Any) -> None:
            log.error("wss_no_reconnect_giving_up")
            self._publish_health("down", {"reason": "max_reconnects_exceeded"})

        def on_ticks(ws: Any, ticks: list[dict[str, Any]]) -> None:
            # WSS callback thread — must not block.
            self._last_tick_ts = time.monotonic()
            self._tick_count += len(ticks)
            for raw in ticks:
                try:
                    event = self._parse_tick(raw)
                    # Schedule publish on the event loop without blocking
                    asyncio.run_coroutine_threadsafe(
                        self.bus.publish(STREAM_TICKS, event), self._loop
                    )
                except Exception as e:  # noqa: BLE001
                    log.exception("tick_parse_failed", error=str(e))

        ticker.on_connect = on_connect
        ticker.on_close = on_close
        ticker.on_error = on_error
        ticker.on_reconnect = on_reconnect
        ticker.on_noreconnect = on_noreconnect
        ticker.on_ticks = on_ticks

        self._ticker = ticker

        # KiteTicker.connect(threaded=True) spawns its own reactor thread AND passes
        # installSignalHandlers=False to twisted, which avoids the harmless but noisy
        # "signal only works in main thread" warning we'd get from a manual thread.
        ticker.connect(threaded=True, disable_ssl_verification=False)

        # Wait up to 10s for the connection to establish
        for _ in range(100):
            if self._connected.is_set():
                return
            await asyncio.sleep(0.1)
        raise TimeoutError("WSS did not connect within 10s")

    def stop(self) -> None:
        self._stop_flag.set()
        if self._ticker:
            try:
                self._ticker.close(code=1000, reason="shutdown")
            except Exception as e:  # noqa: BLE001
                log.exception("wss_close_error", error=str(e))

    def _parse_tick(self, raw: dict[str, Any]) -> TickEvent:
        tok = int(raw["instrument_token"])
        return TickEvent(
            event_id=str(uuid.uuid4()),
            ts=datetime.now(tz=timezone.utc),
            instrument_token=tok,
            symbol=self.symbol_lookup(tok),
            ltp=Decimal(str(raw.get("last_price", 0))),
            bid=Decimal(str(raw["depth"]["buy"][0]["price"]))
            if raw.get("depth") and raw["depth"].get("buy")
            else None,
            ask=Decimal(str(raw["depth"]["sell"][0]["price"]))
            if raw.get("depth") and raw["depth"].get("sell")
            else None,
            volume=int(raw["volume_traded"]) if raw.get("volume_traded") else None,
            oi=int(raw["oi"]) if raw.get("oi") else None,
        )

    def _publish_health(self, status: str, details: dict[str, Any]) -> None:
        if not self._loop:
            return
        ev = HealthEvent(
            event_id=str(uuid.uuid4()),
            ts=datetime.now(tz=timezone.utc),
            component="wss",
            status=status,  # type: ignore[arg-type]
            details=details,
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self.bus.publish("stream:health", ev), self._loop
            )
        except Exception as e:  # noqa: BLE001
            log.exception("health_publish_failed", error=str(e))


class WSSWatchdog:
    """Background task: alerts and reconnects if no ticks during market hours."""

    def __init__(self, manager: WSSManager, max_silence_seconds: float = 30.0):
        self.manager = manager
        self.max_silence = max_silence_seconds

    async def run(self) -> None:
        from src.core.clock import get_clock

        clock = get_clock()
        while True:
            await asyncio.sleep(10)
            if not clock.is_market_open():
                continue
            since = self.manager.seconds_since_last_tick
            if since > self.max_silence:
                log.warning(
                    "wss_stall_detected",
                    seconds_silent=since,
                    tick_count_total=self.manager.tick_count,
                )
                # Force reconnect path
                self.manager.stop()
                # Caller is expected to restart manager
                return
