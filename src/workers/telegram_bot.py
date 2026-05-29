"""
Telegram bot worker.

Run as: python -m src.workers.telegram_bot
Wraps `run_bot_forever` with the same lifecycle conventions as ingestor.py.
"""
from __future__ import annotations

import asyncio
import sys

from src.core.logging import configure_logging
from src.notifications.bot import run_bot_forever


def main() -> int:
    configure_logging(level="INFO", format="console")
    try:
        asyncio.run(run_bot_forever())
        return 0
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
