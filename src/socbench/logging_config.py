"""Structured logging configuration shared by every entrypoint.

Logs are line-delimited JSON by default so they can be ingested by any log
viewer. Pass ``--log-format human`` on the CLI to flip to a console-friendly
format during local iteration.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Literal

try:
    from pythonjsonlogger.json import JsonFormatter  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover: fallback for older python-json-logger
    from pythonjsonlogger.jsonlogger import JsonFormatter  # type: ignore[import-not-found,no-redef]

LogFormat = Literal["json", "human"]

_HUMAN_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


def configure_logging(level: str | int = "INFO", *, fmt: LogFormat | None = None) -> None:
    """Idempotent global logger configuration.

    The default format follows the env var ``SOCBENCH_LOG_FORMAT`` if set, else
    ``json``. The default level follows ``SOCBENCH_LOG_LEVEL`` if set, else
    the value passed in.
    """
    env_fmt = os.environ.get("SOCBENCH_LOG_FORMAT", "").strip().lower()
    env_level = os.environ.get("SOCBENCH_LOG_LEVEL", "").strip()

    chosen_fmt: LogFormat = (
        fmt
        if fmt is not None
        else (env_fmt if env_fmt in ("json", "human") else "json")  # type: ignore[assignment]
    )
    chosen_level = env_level or level

    root = logging.getLogger()
    root.setLevel(chosen_level)
    for h in list(root.handlers):
        root.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    if chosen_fmt == "json":
        handler.setFormatter(
            JsonFormatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s",
                rename_fields={"asctime": "ts", "levelname": "level", "name": "logger"},
            )
        )
    else:
        handler.setFormatter(logging.Formatter(_HUMAN_FORMAT))
    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
