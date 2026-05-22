"""
Kill switch — defence in depth.

Three independent layers, any one of which halts new orders:
1. Process-level: `KillSwitch.armed` in-memory flag (set by signal handlers or CLI)
2. Cluster-level: Redis key `trading_agent:kill_switch` = "armed"
3. Env-level: `KILL_SWITCH=true`

Order placement must call `ensure_disarmed()` before every send.
"""
from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING

from src.core.exceptions import KillSwitchArmed
from src.core.logging import get_logger

if TYPE_CHECKING:
    import redis.asyncio as aioredis

log = get_logger(__name__)

REDIS_KEY = "trading_agent:kill_switch"
REDIS_VAL_ARMED = "armed"


class KillSwitch:
    """In-process kill switch."""

    def __init__(self) -> None:
        self._armed_local: bool = False
        self._reason: str = ""

    @property
    def armed(self) -> bool:
        return self._armed_local

    def arm(self, reason: str = "manual") -> None:
        self._armed_local = True
        self._reason = reason
        log.error("kill_switch_armed", reason=reason)

    def disarm(self) -> None:
        if self._armed_local:
            log.warning("kill_switch_disarmed", previous_reason=self._reason)
        self._armed_local = False
        self._reason = ""

    def reason(self) -> str:
        return self._reason


# global singleton — process scope
_switch = KillSwitch()


def get_kill_switch() -> KillSwitch:
    return _switch


def ensure_disarmed(env_killed: bool = False) -> None:
    """Raise KillSwitchArmed if any local layer is armed.

    Cluster-level (Redis) is checked separately via `check_redis_kill_switch`
    so this function can stay synchronous.
    """
    if env_killed:
        raise KillSwitchArmed("KILL_SWITCH env is set to true")
    if _switch.armed:
        raise KillSwitchArmed(f"Local kill switch armed: {_switch.reason()}")


async def check_redis_kill_switch(redis_client: "aioredis.Redis") -> None:
    val = await redis_client.get(REDIS_KEY)
    if val == REDIS_VAL_ARMED or val == REDIS_VAL_ARMED.encode():
        raise KillSwitchArmed("Cluster kill switch (Redis) armed")


async def arm_redis_kill_switch(redis_client: "aioredis.Redis", reason: str = "manual") -> None:
    await redis_client.set(REDIS_KEY, REDIS_VAL_ARMED)
    await redis_client.set(f"{REDIS_KEY}:reason", reason)
    log.error("redis_kill_switch_armed", reason=reason)


async def disarm_redis_kill_switch(redis_client: "aioredis.Redis") -> None:
    await redis_client.delete(REDIS_KEY)
    await redis_client.delete(f"{REDIS_KEY}:reason")
    log.warning("redis_kill_switch_disarmed")


def install_signal_handlers() -> None:
    """Trap SIGTERM/SIGINT and arm the kill switch before clean shutdown.

    Workers should also drain open positions on shutdown — orchestrated elsewhere.
    """
    loop = asyncio.get_event_loop()

    def _handler(signame: str) -> None:
        log.warning("signal_received", signal=signame)
        _switch.arm(reason=f"signal:{signame}")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handler, sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows or no running loop — fall back to default
            pass
