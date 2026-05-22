"""
Kill switch CLI.

Usage:
  python -m scripts.kill_switch on  [--reason "manual halt before news"]
  python -m scripts.kill_switch off
  python -m scripts.kill_switch status

Arms/disarms the cluster-level (Redis) kill switch. The local-process flag is
not affected here — that's controlled by signal handlers and risk events.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

import redis.asyncio as aioredis
from rich.console import Console

from src.core.config import settings
from src.core.kill_switch import (
    REDIS_KEY,
    arm_redis_kill_switch,
    disarm_redis_kill_switch,
)

console = Console()


async def status(client: aioredis.Redis) -> None:
    val = await client.get(REDIS_KEY)
    reason = await client.get(f"{REDIS_KEY}:reason")
    if val:
        console.print(f"[red]ARMED[/red] — reason: {reason or '<none>'}")
    else:
        console.print("[green]DISARMED[/green]")


async def main() -> int:
    parser = argparse.ArgumentParser(prog="kill_switch")
    parser.add_argument("action", choices=["on", "off", "status"])
    parser.add_argument("--reason", default="manual")
    args = parser.parse_args()

    client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        if args.action == "on":
            await arm_redis_kill_switch(client, reason=args.reason)
            console.print(f"[red]Kill switch ARMED.[/red] Reason: {args.reason}")
        elif args.action == "off":
            await disarm_redis_kill_switch(client)
            console.print("[green]Kill switch DISARMED.[/green]")
        else:
            await status(client)
        return 0
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
