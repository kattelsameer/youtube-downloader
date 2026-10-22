"""Logging setup shared by the main process and the download workers.

INFO lines go to stdout as plain text; warnings and errors go to stderr
with a "WARNING:"/"ERROR:" prefix.
"""

from __future__ import annotations

import logging
import sys

LOGGER_NAME = "ytdl"


class _LevelPrefixFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return message if record.levelno == logging.INFO else f"{record.levelname}: {message}"


class _BelowLevel(logging.Filter):
    def __init__(self, level: int):
        super().__init__()
        self.level = level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno < self.level


def make_output_safe() -> None:
    """Never crash on characters the console can't show (emoji titles on a
    cp1252 Windows console, say): print them as \\U escapes instead."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="backslashreplace")
            except (ValueError, OSError):  # e.g. a closed or unusual stream
                pass


def configure_logging(level: str = "INFO") -> logging.Logger:
    make_output_safe()
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    out = logging.StreamHandler(sys.stdout)
    out.addFilter(_BelowLevel(logging.WARNING))
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.WARNING)
    for handler in (out, err):
        handler.setFormatter(_LevelPrefixFormatter("%(message)s"))
        logger.addHandler(handler)
    logger.setLevel(level)
    return logger


def set_level(level: str) -> None:
    logging.getLogger(LOGGER_NAME).setLevel(level)
