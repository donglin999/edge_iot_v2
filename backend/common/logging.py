"""Unified logging configuration for the platform."""
from __future__ import annotations

import logging
import logging.config
from pathlib import Path
from typing import Any, Dict

from django.conf import settings


def setup_logging(log_dir: Path = None) -> None:
    """
    Setup unified logging configuration.

    Args:
        log_dir: Directory for log files (optional)
    """
    if log_dir is None:
        log_dir = Path(settings.BASE_DIR) / "logs"

    log_dir.mkdir(parents=True, exist_ok=True)

    logging_config: Dict[str, Any] = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "verbose": {
                "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
                "style": "{",
            },
            "simple": {
                "format": "{levelname} {asctime} {module} {message}",
                "style": "{",
            },
        },
        "filters": {
            "require_debug_false": {
                "()": "django.utils.log.RequireDebugFalse",
            },
            "require_debug_true": {
                "()": "django.utils.log.RequireDebugTrue",
            },
        },
        "handlers": {
            "console": {
                "level": "INFO",
                "class": "logging.StreamHandler",
                "formatter": "simple",
            },
            "file": {
                "level": "INFO",
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "application.log"),
                "maxBytes": 10485760,  # 10MB
                "backupCount": 5,
                "formatter": "verbose",
            },
            "acquisition_file": {
                "level": "DEBUG",
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "acquisition.log"),
                "maxBytes": 10485760,
                "backupCount": 10,
                "formatter": "verbose",
            },
            "error_file": {
                "level": "ERROR",
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(log_dir / "error.log"),
                "maxBytes": 10485760,
                "backupCount": 5,
                "formatter": "verbose",
            },
        },
        "loggers": {
            "django": {
                "handlers": ["console", "file"],
                "level": "INFO",
            },
            "acquisition": {
                "handlers": ["console", "acquisition_file", "error_file"],
                "level": "DEBUG",
                "propagate": False,
            },
            "storage": {
                "handlers": ["console", "file", "error_file"],
                "level": "INFO",
                "propagate": False,
            },
            "configuration": {
                "handlers": ["console", "file"],
                "level": "INFO",
                "propagate": False,
            },
        },
        "root": {
            "handlers": ["console", "file", "error_file"],
            "level": "INFO",
        },
    }

    logging.config.dictConfig(logging_config)


class LoggerMixin:
    """Mixin to add logging capabilities to classes."""

    @property
    def logger(self) -> logging.Logger:
        """Get logger instance for this class."""
        if not hasattr(self, "_logger"):
            self._logger = logging.getLogger(f"{self.__class__.__module__}.{self.__class__.__name__}")
        return self._logger
