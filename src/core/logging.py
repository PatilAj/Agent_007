"""
Structured logging.

All logs go through structlog → JSON in production, pretty in dev.
Every log line includes timestamp, level, module, and arbitrary kwargs.

Usage:
    from src.core.logging import get_logger
    log = get_logger(__name__)
    log.info("order_placed", symbol="NIFTY26MAY24500CE", qty=75, side="BUY")
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog
from structlog.contextvars import merge_contextvars
from structlog.processors import (
    JSONRenderer,
    StackInfoRenderer,
    TimeStamper,
    add_log_level,
    format_exc_info,
)
from structlog.stdlib import BoundLogger


def configure_logging(level: str = "INFO", format: str = "json", log_dir: str | None = None) -> None:
    """Call once at process startup."""

    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_dir:
        log_path = Path(log_dir)
        log_path.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path / "app.log")
        handlers.append(fh)

    # stdlib basic config
    logging.basicConfig(
        format="%(message)s",
        level=log_level,
        handlers=handlers,
        force=True,
    )

    shared_processors: list = [
        merge_contextvars,
        add_log_level,
        TimeStamper(fmt="iso", utc=True),
        StackInfoRenderer(),
    ]

    # ConsoleRenderer formats exceptions itself; adding format_exc_info on top
    # makes structlog emit a UserWarning per call. JSONRenderer needs it.
    if format == "json":
        shared_processors.append(format_exc_info)
        renderer = JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> BoundLogger:
    return structlog.get_logger(name)
