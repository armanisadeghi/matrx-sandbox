"""Structured logging configuration.

Provides JSON or text logging based on MATRX_LOG_FORMAT setting.
JSON format is designed for production log aggregation (CloudWatch, Datadog, etc.).
Text format is for local development readability.
"""

from __future__ import annotations

import logging
import sys

from orchestrator.config import settings


def setup_logging() -> None:
    """Configure structured logging for the application.

    Uses JSON format in production, text format for local dev.
    Respects MATRX_LOG_LEVEL and MATRX_LOG_FORMAT settings.
    """
    log_level = getattr(logging, settings.log_level, logging.INFO)
    root_logger = logging.getLogger()

    # Remove existing handlers to avoid duplicates on reload
    root_logger.handlers.clear()

    if settings.log_format == "json":
        _setup_json_logging(root_logger, log_level)
    else:
        _setup_text_logging(root_logger, log_level)

    # Quiet noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("docker").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _setup_json_logging(logger: logging.Logger, level: int) -> None:
    """Configure JSON structured logging using python-json-logger."""
    try:
        from pythonjsonlogger.json import JsonFormatter

        handler = logging.StreamHandler(sys.stdout)
        formatter = JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"asctime": "timestamp", "levelname": "level", "name": "logger"},
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        logger.setLevel(level)
    except ImportError:
        # Fallback to text if python-json-logger is not installed
        _setup_text_logging(logger, level)
        logging.getLogger(__name__).warning(
            "python-json-logger not installed — falling back to text logging"
        )


def _setup_text_logging(logger: logging.Logger, level: int) -> None:
    """Configure human-readable text logging for local development."""
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level)
