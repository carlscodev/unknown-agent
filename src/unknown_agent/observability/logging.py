"""Logging setup: readable on a TTY, machine-parsable everywhere else.

Two sinks, one configuration:

* **stderr human log** - Rich-formatted, level-coloured, timestamped on request.
  It never touches stdout, which is reserved for command output so that
  ``ua ... --json | jq`` and shell pipelines stay safe.
* **file log** - rotating JSON lines with run/step correlation ids at
  ``<state_dir>/logs/unknown-agent.log`` and, for long runs, mirrored into the
  project's ``.unknown/logs/``.

Secrets are redacted in both sinks by :mod:`unknown_agent.config.secrets`.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

from unknown_agent.config.secrets import redact_text

LOGGER_NAME = "unknown_agent"
_STANDARD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename", "module", "exc_info",
    "exc_text", "stack_info", "lineno", "funcName", "created", "msecs", "relativeCreated", "thread",
    "threadName", "processName", "process", "taskName", "message", "asctime",
}

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class RedactingFilter(logging.Filter):
    """Strip credentials from every message and structured field."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_text(record.msg)
        for key in ("run_id", "step", "tool", "task"):
            value = getattr(record, key, None)
            if isinstance(value, str):
                setattr(record, key, redact_text(value))
        return True


class JsonFormatter(logging.Formatter):
    """Structured JSON line formatter for files and CI aggregation."""

    def __init__(self, *, include_timestamps: bool = True) -> None:
        super().__init__()
        self.include_timestamps = include_timestamps

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if self.include_timestamps:
            payload["ts"] = self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z")
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact_text(json.dumps(payload, default=str, ensure_ascii=False))


class RichConsoleHandler(logging.Handler):
    """Level-coloured console handler that defers to the shared Rich console."""

    def __init__(self, console: Any, *, include_timestamps: bool = True, verbosity: int = 0) -> None:
        super().__init__()
        self.console = console
        self.include_timestamps = include_timestamps
        self.verbosity = verbosity

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - presentation
        try:
            style = {
                logging.DEBUG: "dim",
                logging.INFO: "cyan",
                logging.WARNING: "yellow",
                logging.ERROR: "red",
                logging.CRITICAL: "bold red",
            }.get(record.levelno, "white")
            prefix = ""
            if self.include_timestamps:
                prefix += f"[dim]{self.formatTime(record, '%H:%M:%S')}[/dim] "
            context = []
            for attr in ("run_id", "step", "tool", "gate"):
                value = getattr(record, attr, None)
                if value not in (None, ""):
                    context.append(f"{attr}={value}")
            prefix += f"[{style}]{record.levelname.lower():8}[/{style}]"
            if context:
                prefix += f" [dim]({' '.join(str(c) for c in context)})[/dim]"
            message = redact_text(record.getMessage())
            self.console.print(f"{prefix} {message}", highlight=False, soft_wrap=True)
            if record.exc_info and record.levelno >= logging.WARNING:
                self.console.print_exception(show_locals=False)
        except Exception:  # pragma: no cover - logging must never break the app
            self.handleError(record)

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - stdlib name
        """Swallow presentation failures instead of printing a traceback.

        The stdlib implementation writes "--- Logging error ---" plus a full
        traceback to stderr, which in a CLI lands in the user's face and, worse,
        corrupts ``--json`` output that a script is piping into ``jq``.  A log line
        that cannot be rendered is worth exactly nothing to the operator, so it is
        dropped: the run's own error reporting is the channel that matters.
        """

        return None


_CONFIGURED = False


def setup_logging(
    *,
    level: str = "info",
    fmt: str = "text",
    console: Any | None = None,
    log_file: Path | None = None,
    file_enabled: bool = True,
    include_timestamps: bool = True,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
    verbosity: int = 0,
    force: bool = False,
) -> logging.Logger:
    """Configure the root ``unknown_agent`` logger exactly once per process."""

    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)
    if _CONFIGURED and not force:
        logger.setLevel(LEVELS.get(level, logging.INFO))
        return logger
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(LEVELS.get(level, logging.INFO))

    redactor = RedactingFilter()
    use_json_console = fmt == "json" or not sys.stderr.isatty()

    if console is not None:
        handler: logging.Handler = RichConsoleHandler(console, include_timestamps=include_timestamps and verbosity > 0)
        handler.setLevel(logging.DEBUG if verbosity > 1 else LEVELS.get(level, logging.INFO))
    elif use_json_console:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonFormatter(include_timestamps=include_timestamps))
        handler.setLevel(logging.DEBUG if verbosity > 1 else LEVELS.get(level, logging.INFO))
    else:  # pragma: no cover - non-rich fallback
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    handler.addFilter(redactor)
    logger.addHandler(handler)

    if file_enabled and log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
            )
            file_handler.setFormatter(JsonFormatter(include_timestamps=include_timestamps))
            file_handler.setLevel(logging.DEBUG)
            file_handler.addFilter(redactor)
            logger.addHandler(file_handler)
        except OSError:  # pragma: no cover - read-only state dir
            logger.warning("Could not open log file %s; continuing with console logging only.", log_file)

    _CONFIGURED = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a child logger; ``get_logger('tools.executor')`` -> ``unknown_agent.tools.executor``."""

    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)


def set_level(level: str) -> None:
    logging.getLogger(LOGGER_NAME).setLevel(LEVELS.get(level, logging.INFO))


def log_file_path(state_dir: Path) -> Path:
    return state_dir / "logs" / "unknown-agent.log"


__all__ = [
    "LOGGER_NAME",
    "LEVELS",
    "setup_logging",
    "get_logger",
    "set_level",
    "log_file_path",
    "JsonFormatter",
    "RichConsoleHandler",
]
