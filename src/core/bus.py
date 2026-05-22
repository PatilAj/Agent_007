"""
Event bus over Redis Streams.

Streams give us:
- Durable, time-ordered event log
- Consumer groups (each consumer sees each event once)
- Replay capability (for backtests over recorded live data)
- Built-in backpressure

Stream naming convention:
    stream:ticks
    stream:bars.1minute
    stream:bars.5minute
    stream:indicators
    stream:signals
    stream:orders
    stream:risk
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import redis.asyncio as aioredis
from pydantic import BaseModel

from src.core.config import settings
from src.core.logging import get_logger

log = get_logger(__name__)

# Stream name constants
STREAM_TICKS = "stream:ticks"
STREAM_BARS_1M = "stream:bars.1minute"
STREAM_BARS_5M = "stream:bars.5minute"
STREAM_INDICATORS = "stream:indicators"
STREAM_REGIME = "stream:regime"
STREAM_SIGNALS = "stream:signals"
STREAM_ORDERS = "stream:orders"
STREAM_ORDER_UPDATES = "stream:order_updates"
STREAM_RISK_EVENTS = "stream:risk"
STREAM_HEALTH = "stream:health"

MAX_STREAM_LEN = 1_000_000   # cap stream length (~1M events, auto-trimmed)


class EventBus:
    """Thin wrapper around Redis Streams keyed to typed events."""

    def __init__(self, redis: aioredis.Redis):
        self.redis = redis

    @classmethod
    async def connect(cls) -> "EventBus":
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        return cls(client)

    async def close(self) -> None:
        await self.redis.aclose()

    async def publish(self, stream: str, event: BaseModel) -> str:
        payload = {"data": event.model_dump_json()}
        msg_id: str = await self.redis.xadd(
            stream,
            payload,
            maxlen=MAX_STREAM_LEN,
            approximate=True,
        )
        return msg_id

    async def ensure_group(self, stream: str, group: str) -> None:
        """Create consumer group if it does not exist (idempotent)."""
        try:
            await self.redis.xgroup_create(stream, group, id="0", mkstream=True)
            log.info("consumer_group_created", stream=stream, group=group)
        except aioredis.ResponseError as e:
            if "BUSYGROUP" in str(e):
                return
            raise

    async def consume(
        self,
        stream: str,
        group: str,
        consumer: str,
        block_ms: int = 1000,
        count: int = 10,
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Stream consumer loop. Yields (message_id, parsed_payload)."""
        await self.ensure_group(stream, group)
        while True:
            resp = await self.redis.xreadgroup(
                groupname=group,
                consumername=consumer,
                streams={stream: ">"},
                count=count,
                block=block_ms,
            )
            if not resp:
                continue
            for _stream_name, messages in resp:
                for msg_id, fields in messages:
                    try:
                        raw = fields.get("data") or fields.get(b"data")
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        payload = json.loads(raw) if raw else {}
                        yield msg_id, payload
                    except Exception as e:  # noqa: BLE001
                        log.exception("event_parse_failed", error=str(e), msg_id=msg_id)
                        # ack bad message to avoid poison-pill
                        await self.redis.xack(stream, group, msg_id)

    async def ack(self, stream: str, group: str, msg_id: str) -> None:
        await self.redis.xack(stream, group, msg_id)
