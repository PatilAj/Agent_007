"""Logging smoke tests."""
from __future__ import annotations

from src.core.logging import configure_logging, get_logger


def test_configure_logging_json():
    configure_logging(level="INFO", format="json")
    log = get_logger("test")
    # Should not raise
    log.info("test_message", key1="value1", key2=42)


def test_configure_logging_console():
    configure_logging(level="DEBUG", format="console")
    log = get_logger("test")
    log.debug("debug_message")
    log.info("info_message")
    log.warning("warning_message")
