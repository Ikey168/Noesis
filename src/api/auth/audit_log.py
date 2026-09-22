"""
Security audit logging for authentication and authorization events.

Replaces the deprecated AWS CloudWatch / CloudWatch Logs integration: security
audit events and security metrics are appended as newline-delimited JSON to
local files under NOESIS_LOG_DIR (default ./logs):

  - audit_log.jsonl      one JSON object per security event
  - audit_metrics.jsonl  one JSON object per emitted security metric

Uses the Python standard library only (no boto3/botocore, no AWS account).
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Request

from src.config.env import resolve_env

# Configure logger
logger = logging.getLogger("security_audit")
logger.setLevel(logging.INFO)


def _get_log_dir() -> str:
    """Return the local log directory (env NOESIS_LOG_DIR, default ./logs)."""
    log_dir = resolve_env("LOG_DIR", "./logs") or "./logs"
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


class SecurityAuditLogger:
    """Handle security event logging and monitoring using local files."""

    def __init__(self, log_group: str = "neuronews-security"):
        """
        Initialize security logger.

        Args:
            log_group: Logical log group name (recorded in each event; no longer
                an AWS CloudWatch log group)
        """
        self.log_group = log_group

        # Local storage paths (newline-delimited JSON)
        self.log_dir = _get_log_dir()
        self.events_file = os.path.join(self.log_dir, "audit_log.jsonl")
        self.metrics_file = os.path.join(self.log_dir, "audit_metrics.jsonl")

        # Stream name kept for parity with prior log records
        self.stream_name = datetime.now(timezone.utc).strftime("%Y/%m/%d/security")

    def _format_log_event(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        request: Optional[Request] = None,
        user: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Format event data for logging.

        Args:
            event_type: Type of security event
            event_data: Event details
            request: Optional FastAPI request object
            user: Optional user data

        Returns:
            Formatted log event
        """
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "event_data": event_data,
        }

        if request:
            log_entry.update(
                {
                    "client_ip": request.client.host,
                    "method": request.method,
                    "path": str(request.url),
                    "user_agent": request.headers.get("user-agent"),
                    "request_id": request.headers.get("x-request-id"),
                }
            )

        if user:
            log_entry["user"] = {
                "id": user.get("sub"),
                "email": user.get("email"),
                "role": user.get("role"),
            }

        return log_entry

    def _write_record(self, file_path: str, record: Dict[str, Any]) -> None:
        """Append a single JSON record (one line) to a local file."""
        with open(file_path, "a") as f:
            f.write(json.dumps(record) + "\n")

    def _emit_metric(self, event_type: str) -> None:
        """Record a security metric locally (replaces CloudWatch put_metric_data)."""
        metric_record = {
            "Namespace": "NeuroNews/Security",
            "MetricName": "SecurityEvent{0}".format(event_type),
            "Value": 1,
            "Unit": "Count",
            "Dimensions": [{"Name": "Environment", "Value": "production"}],
            "Timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self._write_record(self.metrics_file, metric_record)

    async def log_security_event(
        self,
        event_type: str,
        event_data: Dict[str, Any],
        request: Optional[Request] = None,
        user: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Log a security event to the local audit log and record a metric.

        Args:
            event_type: Type of security event
            event_data: Event details
            request: Optional FastAPI request object
            user: Optional user data
        """
        log_entry = self._format_log_event(event_type, event_data, request, user)

        try:
            # Annotate with log group/stream for parity with the old records
            record = dict(log_entry)
            record["log_group"] = self.log_group
            record["log_stream"] = self.stream_name

            # Append the security event to the local audit log
            self._write_record(self.events_file, record)

            # Emit a local security metric
            self._emit_metric(event_type)

        except Exception as e:
            logger.error("Failed to log security event: {0}".format(e), extra=log_entry)

    async def log_auth_failure(
        self, reason: str, request: Request, user: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Log authentication/authorization failure.

        Args:
            reason: Reason for failure
            request: FastAPI request
            user: Optional user data
        """
        await self.log_security_event("AUTH_FAILURE", {"reason": reason}, request, user)

    async def log_auth_success(self, request: Request, user: Dict[str, Any]) -> None:
        """
        Log successful authentication.

        Args:
            request: FastAPI request
            user: User data
        """
        await self.log_security_event("AUTH_SUCCESS", {}, request, user)

    async def log_permission_denied(
        self, permission: str, request: Request, user: Dict[str, Any]
    ) -> None:
        """
        Log permission denial.

        Args:
            permission: Required permission that was missing
            request: FastAPI request
            user: User data
        """
        await self.log_security_event(
            "PERMISSION_DENIED", {"required_permission": permission}, request, user
        )


# Global security logger instance
security_logger = SecurityAuditLogger()
