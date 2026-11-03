"""Logging setup: console output plus a size-rotated log file, with secret redaction."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class RedactSecretsFilter(logging.Filter):
    """Replaces any configured secret value that ends up in a log message with ``***``."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [s for s in secrets if s and len(s) >= 4]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = message
        for secret in self._secrets:
            redacted = redacted.replace(secret, "***")
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


def secret_values() -> list[str]:
    return [os.environ.get(name, "") for name in ("TAPO_PASSWORD", "ENERGY_API_TOKEN")]


def configure_logging(
    *,
    level: str = "INFO",
    directory: Path | None = None,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
    filename: str = "collector.log",
) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)
    redact = RedactSecretsFilter(secret_values())
    formatter = logging.Formatter(FORMAT)
    # Timestamps in log files are local time of the workshop computer; measurements are always UTC.

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.addFilter(redact)
    root.addHandler(console)

    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(directory / filename, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redact)
        root.addHandler(file_handler)

    # python-kasa and httpx are chatty at DEBUG/INFO; keep them at WARNING unless debugging.
    if level != "DEBUG":
        for noisy in ("kasa", "httpx", "httpcore", "aiohttp"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
