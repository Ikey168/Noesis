"""
Local monitoring integration for NeuroNews scraper.
Tracks execution success/failure rates and performance metrics using local
log and metrics files (replaces the deprecated AWS CloudWatch integration).
"""

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from logging.handlers import RotatingFileHandler
from typing import List, Optional

from src.config.env import resolve_env


def _get_log_dir() -> str:
    """Return the local log directory (env NOESIS_LOG_DIR, default ./logs)."""
    log_dir = resolve_env("LOG_DIR", "./logs") or "./logs"
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def _sanitize_name(name: str) -> str:
    """Sanitize a log group/stream style name for use as a filename."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "default"


class MetricsStorageError(Exception):
    """Raised when local metric/log storage fails (replaces botocore ClientError)."""


class ScrapingStatus(Enum):
    """Enumeration for scraping status types."""

    SUCCESS = "success"
    FAILURE = "failure"
    PARTIAL = "partial"
    TIMEOUT = "timeout"
    CAPTCHA_BLOCKED = "captcha_blocked"
    IP_BLOCKED = "ip_blocked"


@dataclass
class ScrapingMetrics:
    """Data class for scraping execution metrics."""

    url: str
    status: ScrapingStatus
    timestamp: float
    duration_ms: int
    articles_scraped: int = 0
    error_message: Optional[str] = None
    retry_count: int = 0
    proxy_used: Optional[str] = None
    user_agent: Optional[str] = None
    response_code: Optional[int] = None
    captcha_encountered: bool = False
    ip_blocked: bool = False


class LocalMetricsLogger:
    """Local file-based integration for scraper monitoring and alerting."""

    def __init__(
        self,
        region_name: str = "local",
        namespace: str = "NeuroNews/Scraper",
        log_group: str = "neuronews-scraper",
    ):
        """
        Initialize local metrics logger.

        Args:
            region_name: Deprecated, kept for backward compatibility (ignored)
            namespace: Namespace recorded in metric records
            log_group: Logical log group name (maps to a local log file)
        """
        self.region_name = region_name  # Deprecated, unused
        self.namespace = namespace
        self.log_group = log_group

        # Local storage paths
        self.log_dir = _get_log_dir()
        self.log_file = os.path.join(
            self.log_dir, "{0}.log".format(_sanitize_name(log_group))
        )
        self.metrics_file = os.path.join(self.log_dir, "scraper_metrics.jsonl")
        self.alarms_file = os.path.join(self.log_dir, "scraper_alarms.jsonl")

        # Setup logging
        self.logger = logging.getLogger(__name__)

        # Dedicated rotating file logger for log entries (one file per log group)
        self._file_logger = logging.getLogger(
            "neuronews.scraper.{0}".format(_sanitize_name(log_group))
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

        # Metrics buffer for batch sending
        self.metrics_buffer: List[ScrapingMetrics] = []
        self.buffer_size = 10

        # Ensure log group (local directory/file) exists
        self._ensure_log_group_exists()

    def _ensure_log_group_exists(self):
        """Create the local log directory/file if it doesn't exist."""
        try:
            os.makedirs(self.log_dir, exist_ok=True)
            if not os.path.exists(self.log_file):
                open(self.log_file, "a").close()
                self.logger.info("Created local log file: {0}".format(self.log_file))
            else:
                self.logger.debug("Log file already exists: {0}".format(self.log_file))
        except OSError as e:
            self.logger.error("Error creating log file: {0}".format(e))

    async def log_scraping_attempt(self, metrics: ScrapingMetrics):
        """
        Log a scraping attempt with detailed metrics.

        Args:
            metrics: ScrapingMetrics object with execution details
        """
        # Add to buffer
        self.metrics_buffer.append(metrics)

        # Send to local log file
        await self._send_log_entry(metrics)

        # Record metrics locally
        await self._send_cloudwatch_metrics(metrics)

        # Check if buffer is full and send batch metrics
        if len(self.metrics_buffer) >= self.buffer_size:
            await self._send_batch_metrics()
            self.metrics_buffer.clear()

    async def _send_log_entry(self, metrics: ScrapingMetrics):
        """Write a detailed log entry to the local log file."""
        try:
            log_entry = {
                "timestamp": datetime.fromtimestamp(
                    metrics.timestamp, tz=timezone.utc
                ).isoformat(),
                "log_stream": f"scraper-{datetime.now().strftime('%Y-%m-%d')}",
                "url": metrics.url,
                "status": metrics.status.value,
                "duration_ms": metrics.duration_ms,
                "articles_scraped": metrics.articles_scraped,
                "retry_count": metrics.retry_count,
                "proxy_used": metrics.proxy_used,
                "response_code": metrics.response_code,
                "captcha_encountered": metrics.captcha_encountered,
                "ip_blocked": metrics.ip_blocked,
                "error_message": metrics.error_message,
            }

            self._file_logger.info(json.dumps(log_entry))

        except (OSError, MetricsStorageError, ValueError) as e:
            self.logger.error("Error writing log entry to local log: {0}".format(e))

    def _write_metric_records(self, metric_data: List[dict]):
        """Append metric records (JSON lines) to the local metrics file."""
        with open(self.metrics_file, "a") as f:
            for record in metric_data:
                record = dict(record)
                record["Namespace"] = self.namespace
                # Normalize timestamps to ISO strings for JSON serialization
                ts = record.get("Timestamp")
                if isinstance(ts, datetime):
                    record["Timestamp"] = ts.isoformat()
                f.write(json.dumps(record) + "\n")

    async def _send_cloudwatch_metrics(self, metrics: ScrapingMetrics):
        """Record metrics locally for monitoring and alerting.

        Deprecated name kept for backward compatibility (was the CloudWatch sender).
        """
        try:
            metric_data = []

            # Success/Failure metric
            metric_data.append(
                {
                    "MetricName": "ScrapingAttempts",
                    "Dimensions": [
                        {"Name": "Status", "Value": metrics.status.value},
                        {
                            "Name": "URL",
                            "Value": self._get_domain_from_url(metrics.url),
                        },
                    ],
                    "Value": 1,
                    "Unit": "Count",
                    "Timestamp": datetime.fromtimestamp(
                        metrics.timestamp, tz=timezone.utc
                    ),
                }
            )

            # Duration metric
            metric_data.append(
                {
                    "MetricName": "ScrapingDuration",
                    "Dimensions": [
                        {"Name": "URL", "Value": self._get_domain_from_url(metrics.url)}
                    ],
                    "Value": metrics.duration_ms,
                    "Unit": "Milliseconds",
                    "Timestamp": datetime.fromtimestamp(
                        metrics.timestamp, tz=timezone.utc
                    ),
                }
            )

            # Articles scraped metric
            if metrics.articles_scraped > 0:
                metric_data.append(
                    {
                        "MetricName": "ArticlesScraped",
                        "Dimensions": [
                            {
                                "Name": "URL",
                                "Value": self._get_domain_from_url(metrics.url),
                            }
                        ],
                        "Value": metrics.articles_scraped,
                        "Unit": "Count",
                        "Timestamp": datetime.fromtimestamp(
                            metrics.timestamp, tz=timezone.utc
                        ),
                    }
                )

            # Retry count metric
            if metrics.retry_count > 0:
                metric_data.append(
                    {
                        "MetricName": "RetryCount",
                        "Dimensions": [
                            {
                                "Name": "URL",
                                "Value": self._get_domain_from_url(metrics.url),
                            }
                        ],
                        "Value": metrics.retry_count,
                        "Unit": "Count",
                        "Timestamp": datetime.fromtimestamp(
                            metrics.timestamp, tz=timezone.utc
                        ),
                    }
                )

            # CAPTCHA encounters
            if metrics.captcha_encountered:
                metric_data.append(
                    {
                        "MetricName": "CaptchaEncounters",
                        "Dimensions": [
                            {
                                "Name": "URL",
                                "Value": self._get_domain_from_url(metrics.url),
                            }
                        ],
                        "Value": 1,
                        "Unit": "Count",
                        "Timestamp": datetime.fromtimestamp(
                            metrics.timestamp, tz=timezone.utc
                        ),
                    }
                )

            # IP blocks
            if metrics.ip_blocked:
                metric_data.append(
                    {
                        "MetricName": "IPBlocks",
                        "Dimensions": [
                            {
                                "Name": "URL",
                                "Value": self._get_domain_from_url(metrics.url),
                            }
                        ],
                        "Value": 1,
                        "Unit": "Count",
                        "Timestamp": datetime.fromtimestamp(
                            metrics.timestamp, tz=timezone.utc
                        ),
                    }
                )

            # Write metrics to the local metrics file
            self._write_metric_records(metric_data)

        except (OSError, MetricsStorageError, ValueError) as e:
            self.logger.error("Error writing metrics to local file: {0}".format(e))

    async def _send_batch_metrics(self):
        """Send aggregated metrics from buffer."""
        if not self.metrics_buffer:
            return

        try:
            # Calculate aggregate metrics
            total_attempts = len(self.metrics_buffer)
            successful_attempts = sum(
                1 for m in self.metrics_buffer if m.status == ScrapingStatus.SUCCESS
            )
            avg_duration = (
                sum(m.duration_ms for m in self.metrics_buffer) / total_attempts
            )
            total_articles = sum(m.articles_scraped for m in self.metrics_buffer)

            # Success rate metric
            success_rate = (successful_attempts / total_attempts) * 100

            metric_data = [
                {
                    "MetricName": "SuccessRate",
                    "Value": success_rate,
                    "Unit": "Percent",
                    "Timestamp": datetime.now(tz=timezone.utc),
                },
                {
                    "MetricName": "AverageDuration",
                    "Value": avg_duration,
                    "Unit": "Milliseconds",
                    "Timestamp": datetime.now(tz=timezone.utc),
                },
                {
                    "MetricName": "TotalArticlesScraped",
                    "Value": total_articles,
                    "Unit": "Count",
                    "Timestamp": datetime.now(tz=timezone.utc),
                },
            ]

            self._write_metric_records(metric_data)

        except (OSError, MetricsStorageError, ValueError) as e:
            self.logger.error("Error writing batch metrics: {0}".format(e))

    def _get_domain_from_url(self, url: str) -> str:
        """Extract domain from URL for grouping metrics."""
        try:
            from urllib.parse import urlparse

            return urlparse(url).netloc
        except Exception:
            return "unknown"

    def _read_metric_records(self, metric_name: str, hours: int) -> List[dict]:
        """Read metric records for the last N hours from the local metrics file."""
        records = []
        if not os.path.exists(self.metrics_file):
            return records

        cutoff = datetime.now(tz=timezone.utc).timestamp() - hours * 3600
        with open(self.metrics_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("MetricName") != metric_name:
                    continue
                ts = record.get("Timestamp")
                try:
                    record_time = datetime.fromisoformat(ts).timestamp()
                except (TypeError, ValueError):
                    continue
                if record_time >= cutoff:
                    records.append(record)
        return records

    async def get_success_rate(self, hours: int = 24) -> float:
        """
        Get success rate for the last N hours.

        Args:
            hours: Number of hours to look back

        Returns:
            Success rate as percentage
        """
        try:
            datapoints = self._read_metric_records("SuccessRate", hours)

            if datapoints:
                # Return average success rate
                return sum(dp["Value"] for dp in datapoints) / len(datapoints)
            else:
                return 0.0

        except (OSError, MetricsStorageError, ValueError, KeyError) as e:
            self.logger.error("Error getting success rate: {0}".format(e))
            return 0.0

    async def get_failure_count(self, hours: int = 1) -> int:
        """
        Get failure count for the last N hours.

        Args:
            hours: Number of hours to look back

        Returns:
            Number of failures
        """
        try:
            datapoints = self._read_metric_records("ScrapingAttempts", hours)

            failures = [
                dp
                for dp in datapoints
                if any(
                    d.get("Name") == "Status" and d.get("Value") == "failure"
                    for d in dp.get("Dimensions", [])
                )
            ]

            if failures:
                return int(sum(dp["Value"] for dp in failures))
            else:
                return 0

        except (OSError, MetricsStorageError, ValueError, KeyError) as e:
            self.logger.error("Error getting failure count: {0}".format(e))
            return 0

    async def create_alarm(
        self,
        alarm_name: str,
        metric_name: str,
        threshold: float,
        comparison_operator: str = "GreaterThanThreshold",
        sns_topic_arn: Optional[str] = None,
    ):
        """
        Record a local alarm definition for monitoring.

        Args:
            alarm_name: Name of the alarm
            metric_name: Metric to monitor
            threshold: Threshold value for alarm
            comparison_operator: Comparison operator for threshold
            sns_topic_arn: Deprecated, kept for backward compatibility (ignored)
        """
        try:
            alarm_record = {
                "AlarmName": alarm_name,
                "ComparisonOperator": comparison_operator,
                "EvaluationPeriods": 2,
                "MetricName": metric_name,
                "Namespace": self.namespace,
                "Period": 300,  # 5 minutes
                "Statistic": "Average",
                "Threshold": threshold,
                "ActionsEnabled": True,
                "AlarmDescription": "Alarm for {0} in NeuroNews scraper".format(
                    metric_name
                ),
                "Unit": "Count",
                "CreatedAt": datetime.now(tz=timezone.utc).isoformat(),
            }

            with open(self.alarms_file, "a") as f:
                f.write(json.dumps(alarm_record) + "\n")

            self.logger.info("Created local alarm definition: {0}".format(alarm_name))

        except (OSError, ValueError) as e:
            self.logger.error("Error creating alarm: {0}".format(e))

    async def flush_metrics(self):
        """Flush any remaining metrics in buffer."""
        if self.metrics_buffer:
            await self._send_batch_metrics()
            self.metrics_buffer.clear()


# Backward-compatibility alias (deprecated name from the old CloudWatch integration)
CloudWatchLogger = LocalMetricsLogger
