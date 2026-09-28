"""Logging setup and the macOS notification."""

from __future__ import annotations

import logging
import subprocess

from . import config

LOGGER_NAME = "gdrive_auto_expire"

_configured = False


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def setup_logging(verbose: bool = False, to_file: bool = True) -> logging.Logger:
    """Attach handlers once. Cron redirects stdout to the log file as well, so
    the file handler is what guarantees a record when it does not."""
    global _configured
    logger = get_logger()
    if _configured:
        return logger
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    if to_file:
        try:
            config.ensure_dirs()
            handler = logging.FileHandler(config.log_path())
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(message)s")
            )
            logger.addHandler(handler)
        except OSError:
            # An unwritable log must not take the sweep down with it.
            pass

    _configured = True
    return logger


def notify(title: str, message: str) -> bool:
    """Best-effort macOS notification.

    From cron this runs outside the Aqua session bootstrap namespace and may
    simply not appear; there is no reliable way to detect that, so failure is
    silent and the log remains the source of truth.
    """
    script = (
        f"display notification {_as_applescript(message)} "
        f"with title {_as_applescript(title)}"
    )
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=10,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _as_applescript(text: str) -> str:
    """AppleScript string literal: escape backslashes and double quotes."""
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
