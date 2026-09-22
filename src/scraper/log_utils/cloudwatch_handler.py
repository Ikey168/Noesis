"""
Local file logging handler for NeuroNews scrapers.

Replaces the deprecated AWS CloudWatch Logs handler with a rotating local
file handler. Filenames are derived from the old log group/stream concepts.
"""

import logging
import os
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler

from src.config.env import resolve_env


def _get_log_dir() -> str:
    """Return the local log directory (env NOESIS_LOG_DIR, default ./logs)."""
    log_dir = resolve_env("LOG_DIR", "./logs") or "./logs"
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _sanitize_name(name: str) -> str:
    """Sanitize a log group/stream style name for use as a filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "default"


class LogStreamError(Exception):
    """Raised when local log storage fails (replaces botocore ClientError)."""


class LocalFileLoggingHandler(logging.Handler):
    """
    A logging handler that writes logs to a rotating local file.

    One file is created per log group/stream combination under the directory
    given by the NOESIS_LOG_DIR environment variable (default ./logs).
    """

    def __init__(
        self,
        log_group_name,
        log_stream_name=None,
        aws_access_key_id=None,
        aws_secret_access_key=None,
        aws_region=None,
    ):
        """
        Initialize the local file logging handler.

        Args:
            log_group_name (str): Logical log group name (maps to a file prefix)
            log_stream_name (str, optional): Logical log stream name.
                                            If not provided, a name will be generated.
            aws_access_key_id: Deprecated, kept for backward compatibility (ignored)
            aws_secret_access_key: Deprecated, kept for backward compatibility (ignored)
            aws_region: Deprecated, kept for backward compatibility (ignored)
        """
        super().__init__()
        self.log_group_name = log_group_name
        self.log_stream_name = (
            log_stream_name or f"scraper-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        )

        # Build the local log file path from group/stream names
        self.log_dir = _get_log_dir()
        self.log_file = os.path.join(
            self.log_dir,
            "{0}-{1}.log".format(
                _sanitize_name(self.log_group_name),
                _sanitize_name(self.log_stream_name),
            ),
        )

        self._file_handler = None
        self._create_log_group_and_stream()

    def _create_log_group_and_stream(self):
        """Create the local log directory and rotating file handler."""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            self._file_handler = RotatingFileHandler(
                self.log_file, maxBytes=10 * 1024 * 1024, backupCount=5
            )
        except OSError as e:
            raise LogStreamError(
                "Failed to create local log file {0}: {1}".format(self.log_file, e)
            )

    def setFormatter(self, fmt):
        """Set the formatter on both this handler and the underlying file handler."""
        super().setFormatter(fmt)
        if self._file_handler is not None:
            self._file_handler.setFormatter(fmt)

    def emit(self, record):
        """
        Write a log record to the local log file.

        Args:
            record: The log record to send
        """
        try:
            if self._file_handler is not None:
                self._file_handler.emit(record)

        except Exception:
            # Don't raise exceptions from the logging handler
            self.handleError(record)

    def close(self):
        """Close the underlying file handler."""
        if self._file_handler is not None:
            self._file_handler.close()
        super().close()


def configure_cloudwatch_logging(settings, spider_name):
    """
    Configure local file logging for a Scrapy spider.

    Deprecated name kept for backward compatibility (was the CloudWatch setup).

    Args:
        settings: Scrapy settings object
        spider_name: Name of the spider

    Returns:
        LocalFileLoggingHandler: The configured handler, or None if file logging is disabled
    """
    # Check if file logging is enabled (CLOUDWATCH_* keys are deprecated aliases)
    if not (
        settings.getbool("LOCAL_FILE_LOGGING_ENABLED", False)
        or settings.getbool("CLOUDWATCH_LOGGING_ENABLED", False)
    ):
        return None

    # Get logging settings (CLOUDWATCH_* keys are deprecated aliases)
    log_group_name = settings.get(
        "LOCAL_LOG_GROUP", settings.get("CLOUDWATCH_LOG_GROUP", "NeuroNews-Scraper")
    )
    log_stream_prefix = settings.get(
        "LOCAL_LOG_STREAM_PREFIX",
        settings.get("CLOUDWATCH_LOG_STREAM_PREFIX", "scraper"),
    )

    # Generate a log stream name
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_stream_name = "{0}-{1}-{2}".format(log_stream_prefix, spider_name, timestamp)

    # Create and configure the handler
    handler = LocalFileLoggingHandler(
        log_group_name=log_group_name,
        log_stream_name=log_stream_name,
    )

    # Set the formatter
    formatter = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    handler.setFormatter(formatter)

    # Set the log level (CLOUDWATCH_LOG_LEVEL is a deprecated alias)
    log_level = settings.get(
        "LOCAL_LOG_LEVEL", settings.get("CLOUDWATCH_LOG_LEVEL", "INFO")
    )
    handler.setLevel(getattr(logging, log_level))

    return handler


# Backward-compatibility aliases (deprecated names from the old CloudWatch integration)
CloudWatchLoggingHandler = LocalFileLoggingHandler
configure_local_file_logging = configure_cloudwatch_logging
