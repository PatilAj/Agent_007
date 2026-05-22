"""
Daily token refresh worker.

Run as: `python -m src.workers.token_refresh`

In production: schedule via APScheduler from a long-running supervisor or as a
cron job at 07:30 IST. The token is valid until ~06:00 IST next day, so
refreshing at 07:30 gives the freshest token before market open at 09:15.

Idempotent: safe to run multiple times.
"""
from __future__ import annotations

import asyncio
import sys

from src.auth.kite_session import get_active_token, login_and_store
from src.core.config import settings
from src.core.logging import configure_logging, get_logger

log = get_logger(__name__)


async def main() -> int:
    configure_logging(level=settings.log_level, format="console", log_dir=settings.logging.log_dir)

    existing = await get_active_token()
    if existing:
        log.info("token_already_active_skipping")
        return 0

    log.info("token_refresh_starting")
    try:
        token = await login_and_store()
        log.info("token_refresh_done", token_preview=token[:8] + "...")
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("token_refresh_failed", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
