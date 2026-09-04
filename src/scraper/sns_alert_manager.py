"""
Local alerting integration for sending alerts when scrapers fail multiple times.
Provides intelligent alerting with rate limiting and escalation.

Replaces the deprecated AWS SNS integration: alerts are appended as JSON
lines to a local alerts file under NOESIS_LOG_DIR (default ./logs) and
optionally POSTed to a webhook URL from NOESIS_ALERT_WEBHOOK_URL.
"""

import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from src.config.env import resolve_env


def _get_log_dir() -> str:
    """Return the local log directory (env NOESIS_LOG_DIR, default ./logs)."""
    log_dir = resolve_env("LOG_DIR", "./logs") or "./logs"
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


class AlertDeliveryError(Exception):
    """Raised when local alert delivery fails (replaces botocore ClientError)."""


class AlertSeverity(Enum):
    """Alert severity levels."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AlertType(Enum):
    """Types of alerts."""

    SCRAPER_FAILURE = "scraper_failure"
    HIGH_FAILURE_RATE = "high_failure_rate"
    CAPTCHA_BLOCKING = "captcha_blocking"
    IP_BLOCKING = "ip_blocking"
    SYSTEM_ERROR = "system_error"
    PERFORMANCE_DEGRADATION = "performance_degradation"


@dataclass
class Alert:
    """Data class for alert information."""

    alert_type: AlertType
    severity: AlertSeverity
    title: str
    message: str
    timestamp: float
    metadata: Dict[str, Any]

    def to_message(self) -> str:
        """Convert alert to JSON message format."""
        alert_data = {
            "timestamp": datetime.fromtimestamp(
                self.timestamp, tz=timezone.utc
            ).isoformat(),
            "severity": self.severity.value,
            "alert_type": self.alert_type.value,
            "title": self.title,
            "message": self.message,
            "metadata": self.metadata,
        }
        return json.dumps(alert_data, indent=2)

    # Backward-compatibility alias (deprecated name from the old SNS integration)
    to_sns_message = to_message


class LocalAlertManager:
    """Local alerting integration for scraper failure alerts and notifications."""

    def __init__(
        self,
        topic_arn: str = "local-alerts",
        region_name: str = "local",
        rate_limit_window: int = 3600,
        max_alerts_per_window: int = 10,
    ):
        """
        Initialize local alert manager.

        Args:
            topic_arn: Logical alert channel name (deprecated AWS-style argument,
                       kept for backward compatibility; only used as a label)
            region_name: Deprecated, kept for backward compatibility (ignored)
            rate_limit_window: Time window for rate limiting (seconds)
            max_alerts_per_window: Maximum alerts per window
        """
        self.topic_arn = topic_arn
        self.region_name = region_name  # Deprecated, unused
        self.rate_limit_window = rate_limit_window
        self.max_alerts_per_window = max_alerts_per_window

        # Local alert storage and optional webhook delivery
        self.alerts_file = os.path.join(_get_log_dir(), "alerts.jsonl")
        self.webhook_url = resolve_env("ALERT_WEBHOOK_URL")

        # Setup logging
        self.logger = logging.getLogger(__name__)

        # Rate limiting tracking
        self.alert_history: List[float] = []
        self.muted_until: float = 0.0

        # Alert thresholds
        self.failure_rate_threshold = 50.0  # 50% failure rate
        self.consecutive_failure_threshold = 5
        self.response_time_threshold = 30000  # 30 seconds

    def _deliver_alert(self, subject: str, alert: Alert) -> str:
        """
        Deliver an alert: append it to the local alerts file and optionally
        POST it to the configured webhook URL (fail soft).

        Returns:
            A local message id for the delivered alert
        """
        message_id = "local-{0}".format(int(alert.timestamp * 1000))

        record = {
            "message_id": message_id,
            "channel": self.topic_arn,
            "subject": subject,
            "timestamp": datetime.fromtimestamp(
                alert.timestamp, tz=timezone.utc
            ).isoformat(),
            "severity": alert.severity.value,
            "alert_type": alert.alert_type.value,
            "title": alert.title,
            "message": alert.message,
            "metadata": alert.metadata,
        }

        # Append to the local alerts file (JSON lines)
        with open(self.alerts_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        # Optionally POST to a webhook (fail soft)
        if self.webhook_url:
            try:
                request = urllib.request.Request(
                    self.webhook_url,
                    data=json.dumps(record).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=10):
                    pass
            except Exception as e:
                self.logger.warning(
                    "Failed to POST alert to webhook: {0}".format(str(e))
                )

        return message_id

    async def send_alert(self, alert: Alert, force: bool = False) -> bool:
        """
        Send an alert locally with rate limiting.

        Args:
            alert: Alert object to send
            force: Force send even if rate limited

        Returns:
            True if alert was sent, False if rate limited
        """
        current_time = time.time()

        # Check if we're in a mute period
        if current_time < self.muted_until and not force:
            self.logger.debug("Alert muted due to rate limiting")
            return False

        # Check rate limiting
        if not force and not self._is_within_rate_limit():
            # Mute alerts for the next hour if we've exceeded the limit
            self.muted_until = current_time + self.rate_limit_window
            self.logger.warning(
                "Alert rate limit exceeded, muting alerts for 1 hour")

            # Send a rate limit notification
            await self._send_rate_limit_notification()
            return False

        try:
            # Prepare alert message
            subject = f"[{alert.severity.value}] NeuroNews Scraper Alert: {alert.title}"

            # Deliver locally (file + optional webhook)
            message_id = self._deliver_alert(subject, alert)

            # Track alert for rate limiting
            self.alert_history.append(current_time)
            self._cleanup_old_alerts()

            self.logger.info(
                f"Sent alert: {alert.title} (MessageId: {message_id})"
            )
            return True

        except (OSError, AlertDeliveryError, ValueError) as e:
            self.logger.error(f"Error sending alert: {str(e)}")
            return False

    def _is_within_rate_limit(self) -> bool:
        """Check if we're within the rate limit."""
        current_time = time.time()
        cutoff_time = current_time - self.rate_limit_window

        # Count alerts in the current window
        recent_alerts = [t for t in self.alert_history if t > cutoff_time]
        return len(recent_alerts) < self.max_alerts_per_window

    def _cleanup_old_alerts(self):
        """Remove old alerts from history to prevent memory growth."""
        current_time = time.time()
        cutoff_time = current_time - self.rate_limit_window
        self.alert_history = [t for t in self.alert_history if t > cutoff_time]

    async def _send_rate_limit_notification(self):
        """Send a notification that alerts are being rate limited."""
        alert = Alert(
            alert_type=AlertType.SYSTEM_ERROR,
            severity=AlertSeverity.WARNING,
            title="Alert Rate Limit Exceeded",
            message="Too many alerts have been sent in a short period. Alerts will be muted for 1 hour.",
            timestamp=time.time(),
            metadata={
                "rate_limit_window": self.rate_limit_window,
                "max_alerts_per_window": self.max_alerts_per_window,
            },
        )

        # Send without rate limiting
        try:
            subject = "[{0}] NeuroNews Scraper Alert: {1}".format(
                alert.severity.value, alert.title
            )

            self._deliver_alert(subject, alert)
        except (OSError, AlertDeliveryError, ValueError) as e:
            self.logger.error(
                "Error sending rate limit notification: {0}".format(str(e)))

    async def alert_scraper_failure(
        self,
        url: str,
        failure_reason: str,
        retry_count: int = 0,
        error_details: Optional[str] = None,
    ):
        """
        Send alert for scraper failure.

        Args:
            url: URL that failed
            failure_reason: Reason for failure
            retry_count: Number of retry attempts
            error_details: Detailed error information
        """
        # Determine severity based on retry count
        if retry_count >= self.consecutive_failure_threshold:
            severity = AlertSeverity.ERROR
        elif retry_count >= 2:
            severity = AlertSeverity.WARNING
        else:
            severity = AlertSeverity.INFO

        alert = Alert(
            alert_type=AlertType.SCRAPER_FAILURE,
            severity=severity,
            title="Scraper Failure - {0} Attempts".format(retry_count + 1),
            message="Failed to scrape URL: {0}\nReason: {1}\nRetry Count: {2}".format(
                url, failure_reason, retry_count
            ),
            timestamp=time.time(),
            metadata={
                "url": url,
                "failure_reason": failure_reason,
                "retry_count": retry_count,
                "error_details": error_details,
            },
        )

        await self.send_alert(alert)

    async def alert_high_failure_rate(
        self, failure_rate: float, time_period: int, failed_count: int, total_count: int
    ):
        """
        Send alert for high failure rate.

        Args:
            failure_rate: Failure rate percentage
            time_period: Time period in hours
            failed_count: Number of failed attempts
            total_count: Total number of attempts
        """
        severity = AlertSeverity.CRITICAL if failure_rate >= 80 else AlertSeverity.ERROR

        alert = Alert(
            alert_type=AlertType.HIGH_FAILURE_RATE,
            severity=severity,
            title="High Failure Rate Detected: {0:.1f}%".format(failure_rate),
            message="Scraper failure rate is {0:.1f}% over the last {1} hours. Failed: {2}/{3} attempts".format(
                failure_rate, time_period, failed_count, total_count
            ),
            timestamp=time.time(),
            metadata={
                "failure_rate": failure_rate,
                "time_period_hours": time_period,
                "failed_count": failed_count,
                "total_count": total_count,
            },
        )

        await self.send_alert(alert)

    async def alert_captcha_blocking(
        self, url: str, captcha_count: int, time_period: int
    ):
        """
        Send alert for CAPTCHA blocking issues.

        Args:
            url: URL experiencing CAPTCHA issues
            captcha_count: Number of CAPTCHA encounters
            time_period: Time period in hours
        """
        alert = Alert(
            alert_type=AlertType.CAPTCHA_BLOCKING,
            severity=AlertSeverity.WARNING,
            title="Frequent CAPTCHA Encounters",
            message="Encountered {0} CAPTCHAs for {1} in the last {2} hours. This may indicate detection by anti-bot systems.".format(
                captcha_count, url, time_period
            ),
            timestamp=time.time(),
            metadata={
                "url": url,
                "captcha_count": captcha_count,
                "time_period_hours": time_period,
            },
        )

        await self.send_alert(alert)

    async def alert_ip_blocking(
        self, url: str, blocked_ips: List[str], time_period: int
    ):
        """
        Send alert for IP blocking issues.

        Args:
            url: URL experiencing IP blocking
            blocked_ips: List of blocked IP addresses
            time_period: Time period in hours
        """
        alert = Alert(
            alert_type=AlertType.IP_BLOCKING,
            severity=AlertSeverity.ERROR,
            title="IP Blocking Detected",
            message="Multiple IPs blocked for {0} in the last {1} hours. Blocked IPs: {2}{3}".format(
                url,
                time_period,
                ", ".join(blocked_ips[:5]),
                "..." if len(blocked_ips) > 5 else "",
            ),
            timestamp=time.time(),
            metadata={
                "url": url,
                "blocked_ips": blocked_ips,
                "time_period_hours": time_period,
            },
        )

        await self.send_alert(alert)

    async def alert_performance_degradation(
        self, avg_response_time: float, threshold: float, time_period: int
    ):
        """
        Send alert for performance degradation.

        Args:
            avg_response_time: Average response time in milliseconds
            threshold: Response time threshold
            time_period: Time period in hours
        """
        alert = Alert(
            alert_type=AlertType.PERFORMANCE_DEGRADATION,
            severity=AlertSeverity.WARNING,
            title="Performance Degradation Detected",
            message="Average response time ({0:.0f}ms) exceeds threshold ({1:.0f}ms) "
            "over the last {2} hours.".format(
                avg_response_time, threshold, time_period
            ),
            timestamp=time.time(),
            metadata={
                "avg_response_time_ms": avg_response_time,
                "threshold_ms": threshold,
                "time_period_hours": time_period,
            },
        )

        await self.send_alert(alert)

    async def alert_system_error(
        self, error_type: str, error_message: str, component: str
    ):
        """
        Send alert for system errors.

        Args:
            error_type: Type of error
            error_message: Error message
            component: Component that experienced the error
        """
        alert = Alert(
            alert_type=AlertType.SYSTEM_ERROR,
            severity=AlertSeverity.ERROR,
            title="System Error in {0}".format(component),
            message="Error Type: {0} Message: {1} Component: {2}".format(error_type, error_message, component),
            timestamp=time.time(),
            metadata={
                "error_type": error_type,
                "error_message": error_message,
                "component": component,
            },
        )

        await self.send_alert(alert)

    async def test_alert_system(self):
        """Send a test alert to verify the system is working."""
        alert = Alert(
            alert_type=AlertType.SYSTEM_ERROR,
            severity=AlertSeverity.INFO,
            title="Test Alert",
            message="This is a test alert to verify the local alert system is working properly.",
            timestamp=time.time(),
            metadata={"test": True},
        )

        return await self.send_alert(alert, force=True)

    def set_failure_rate_threshold(self, threshold: float):
        """Set the failure rate threshold for alerts."""
        self.failure_rate_threshold = threshold
        self.logger.info("Set failure rate threshold to {0}%".format(threshold))

    def set_consecutive_failure_threshold(self, threshold: int):
        """Set the consecutive failure threshold for alerts."""
        self.consecutive_failure_threshold = threshold
        self.logger.info("Set consecutive failure threshold to {0}".format(threshold))

    def set_response_time_threshold(self, threshold: float):
        """Set the response time threshold for alerts."""
        self.response_time_threshold = threshold
        self.logger.info("Set response time threshold to {0}ms".format(threshold))


# Backward-compatibility alias (deprecated name from the old SNS integration)
SNSAlertManager = LocalAlertManager
