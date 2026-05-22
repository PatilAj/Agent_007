"""
Daily instrument catalog refresh worker.

Run as: `python -m src.workers.refresh_instruments`

Should run after token_refresh succeeds, around 08:00 IST.
"""
from __future__ import annotations

import asyncio
import sys

from src.broker.instrument_catalog import count_instruments, refresh_instrument_catalog
from src.core.config import settings
from src.core.logging import configure_logging, get_logger

log = get_logger(__name__)


async def main() -> int:
    configure_logging(level=settings.log_level, format="console", log_dir=settings.logging.log_dir)
    log.info("instrument_refresh_starting")
    try:
        n = await refresh_instrument_catalog()
        total = await count_instruments()
        log.info("instrument_refresh_done", upserted=n, total_in_db=total)
        return 0
    except Exception as e:  # noqa: BLE001
        log.exception("instrument_refresh_failed", error=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
