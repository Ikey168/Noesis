"""
Logging configuration for the API.

Replaces the deprecated AWS CloudWatch request logging middleware with a
rotating local log file written under NOESIS_LOG_DIR (default ./logs).
"""

import json
import logging
import os
import re
from datetime import datetime
from logging.handlers import RotatingFileHandler
from typing import Optional

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from src.config.env import resolve_env


def _get_log_dir() -> str:
    """Return the local log directory (env NOESIS_LOG_DIR, default ./logs)."""
    log_dir = resolve_env("LOG_DIR", "./logs") or "./logs"
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _sanitize_name(name: str) -> str:
    """Sanitize a log group style name for use as a filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "default"


class LocalRequestLogger(BaseHTTPMiddleware):
    """Middleware for logging API requests to a rotating local log file."""

    def __init__(self, app, log_group: str, aws_region: Optional[str] = None):
        """Initialize the logger.

        Args:
            app: FastAPI application
            log_group: Logical log group name (maps to a local log file)
            aws_region: Deprecated, kept for backward compatibility (ignored)
        """
        super().__init__(app)
        self.log_group = log_group
        self.aws_region = aws_region  # Deprecated, unused
        self.log_stream = f"api-{datetime.now().strftime('%Y%m%d')}"

        # Build the local log file path from the log group name
        log_dir = _get_log_dir()
        self.log_file = os.path.join(
            log_dir, "{0}.log".format(_sanitize_name(log_group))
        )

        # Dedicated rotating file logger for API request entries
        self._file_logger = logging.getLogger(
            "neuronews.api.{0}".format(_sanitize_name(log_group))
        )
        self._file_logger.setLevel(logging.INFO)
        self._file_logger.propagate = False
        if not any(
            isinstance(h, RotatingFileHandler)
            and getattr(h, "baseFilename", None) == os.path.abspath(self.log_file)
            for h in self._file_logger.handlers
        ):
            handler = RotatingFileHandler(
                self.log_file, maxBytes=10 * 1024 * 1024, backupCount=5
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._file_logger.addHandler(handler)

    async def dispatch(self, request: Request, call_next) -> Response:
        """Log request and response details to the local log file."""
        start_time = datetime.now()

        # Process the request
        try:
            response = await call_next(request)
            status_code = response.status_code
            error = None
        except Exception as e:
            status_code = 500
            error = str(e)
            raise
        finally:
            # Calculate duration
            duration = (datetime.now() - start_time).total_seconds()

            # Prepare log entry
            log_entry = {
                "timestamp": start_time.strftime("%Y-%m-%d %H:%M:%S"),
                "log_stream": self.log_stream,
                "request_id": request.headers.get("X-Request-ID", ""),
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration": "{0:.3f}s".format(duration),
            }

            if error:
                log_entry["error"] = error

            # Write to the local log file
            try:
                self._file_logger.info(json.dumps(log_entry))
            except (OSError, ValueError) as e:
                logging.error("Failed to write logs to local file: {0}".format(e))

        if error:
            raise

        return response


def configure_logging(app, log_group: str, aws_region: Optional[str] = None):
    """Configure API logging middleware.

    Args:
        app: FastAPI application
        log_group: Logical log group name (maps to a local log file)
        aws_region: Deprecated, kept for backward compatibility (ignored)
    """
    app.add_middleware(LocalRequestLogger, log_group=log_group, aws_region=aws_region)


# Backward-compatibility alias (deprecated name from the old CloudWatch integration)
CloudWatchLogger = LocalRequestLogger
