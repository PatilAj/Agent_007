"""Logging smoke tests."""
from __future__ import annotations

import warnings

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


def test_console_mode_does_not_warn_about_format_exc_info():
    """ConsoleRenderer formats exceptions itself; format_exc_info on top warns."""
    configure_logging(level="INFO", format="console")
    log = get_logger("test_warn")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            log.exception("intentional_test_exception")
    bad = [w for w in caught if "format_exc_info" in str(w.message)]
    assert not bad, f"structlog warned about format_exc_info: {[str(w.message) for w in bad]}"
