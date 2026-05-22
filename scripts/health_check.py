"""
Phase 0 health check.

Run as: `python -m scripts.health_check`

Verifies:
  - Config loads cleanly
  - Postgres reachable
  - Redis reachable
  - Kite token state
  - Instrument count
  - Kill switch state

Designed to be safe to run anytime, including outside market hours.
Exit code 0 = all healthy. Non-zero = something needs attention.
"""
from __future__ import annotations

import asyncio
import sys
from typing import Awaitable, Callable

from rich.console import Console
from rich.table import Table

from src.core.config import settings
from src.core.kill_switch import check_redis_kill_switch, get_kill_switch
from src.core.logging import configure_logging

console = Console()


async def check_config() -> tuple[bool, str]:
    try:
        _ = settings.mode
        return True, f"mode={settings.mode}, live={settings.is_live}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def check_postgres() -> tuple[bool, str]:
    try:
        from src.data.db import ping

        ok = await ping()
        return ok, "reachable" if ok else "unreachable"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def check_redis() -> tuple[bool, str]:
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        await client.ping()
        await client.aclose()
        return True, "reachable"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def check_kite_token() -> tuple[bool, str]:
    try:
        from src.auth.kite_session import get_active_token

        token = await get_active_token()
        if token:
            return True, f"active (preview: {token[:6]}...)"
        return False, "no active token — run `python -m src.workers.token_refresh`"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def check_instruments() -> tuple[bool, str]:
    try:
        from src.broker.instrument_catalog import count_instruments

        n = await count_instruments()
        if n == 0:
            return False, "no instruments in DB — run `python -m src.workers.refresh_instruments`"
        return True, f"{n:,} instruments loaded"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


async def check_kill_switch() -> tuple[bool, str]:
    try:
        # local
        local = get_kill_switch()
        local_state = "ARMED" if local.armed else "disarmed"

        # redis
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            await check_redis_kill_switch(client)
            redis_state = "disarmed"
        except Exception:  # noqa: BLE001
            redis_state = "ARMED"
        await client.aclose()

        # env
        env_state = "ARMED" if settings.kill_switch else "disarmed"

        all_disarmed = local_state == "disarmed" and redis_state == "disarmed" and env_state == "disarmed"
        return all_disarmed, f"local={local_state}, redis={redis_state}, env={env_state}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


CHECKS: list[tuple[str, Callable[[], Awaitable[tuple[bool, str]]]]] = [
    ("Config", check_config),
    ("Postgres", check_postgres),
    ("Redis", check_redis),
    ("Kite Token", check_kite_token),
    ("Instrument Catalog", check_instruments),
    ("Kill Switch", check_kill_switch),
]


async def main() -> int:
    configure_logging(level="WARNING", format="console")
    table = Table(title="Trading Agent — Phase 0 Health Check")
    table.add_column("Component")
    table.add_column("Status")
    table.add_column("Detail", overflow="fold")

    all_ok = True
    for name, fn in CHECKS:
        try:
            ok, detail = await fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"check_threw: {e}"
        status = "[green]✓ OK[/green]" if ok else "[red]✗ FAIL[/red]"
        table.add_row(name, status, detail)
        if not ok:
            all_ok = False

    console.print(table)
    if all_ok:
        console.print("\n[green]All systems healthy.[/green]")
        return 0
    console.print("\n[yellow]One or more components need attention.[/yellow]")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
