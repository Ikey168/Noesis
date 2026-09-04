"""
Local metrics for NLP job monitoring.

Records NLP processing metrics to a local newline-delimited JSON file
(replaces the deprecated AWS CloudWatch integration). Metrics are appended
to ``metrics.jsonl`` under a directory resolved from the environment:
``NOESIS_METRICS_DIR`` if set, otherwise ``NOESIS_LOG_DIR`` (default
``./logs``). Uses the Python standard library only -- no cloud SDK
dependencies.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config.env import resolve_env


def _get_metrics_dir() -> str:
    """Return the local metrics directory.

    Resolution order:
        1. ``NOESIS_METRICS_DIR``
        2. ``NOESIS_LOG_DIR``
        3. ``./logs`` (default)
    """
    metrics_dir = resolve_env("METRICS_DIR") or resolve_env("LOG_DIR", "./logs")
    os.makedirs(metrics_dir, exist_ok=True)
    return metrics_dir


class MetricsStorageError(Exception):
    """Raised when local metric storage fails."""


class NLPMetrics:
    """Record metrics for NLP processing jobs to a local JSON-lines file."""

    def __init__(
        self, namespace: str = "Production/DataPipeline", region: Optional[str] = None
    ):
        """Initialize the local metrics recorder.

        Args:
            namespace: Logical metrics namespace recorded with each metric.
            region: Deprecated, kept for backward compatibility (ignored).
        """
        self.namespace = namespace
        self.region = region  # Deprecated, unused
        self.logger = logging.getLogger(__name__)

        self.metrics_dir = _get_metrics_dir()
        self.metrics_file = os.path.join(self.metrics_dir, "metrics.jsonl")

    def _write_metric_records(self, metric_data: List[Dict[str, Any]]) -> None:
        """Append metric records (JSON lines) to the local metrics file.

        Args:
            metric_data: List of metric dicts in CloudWatch-style format.
        """
        try:
            timestamp = datetime.now(tz=timezone.utc).isoformat()
            with open(self.metrics_file, "a") as f:
                for record in metric_data:
                    record = dict(record)
                    record["Namespace"] = self.namespace
                    record.setdefault("Timestamp", timestamp)
                    f.write(json.dumps(record) + "\n")
        except (OSError, ValueError) as e:
            self.logger.error("Error writing metrics to local file: {0}".format(e))
            raise MetricsStorageError(str(e)) from e

    def put_metric(
        self,
        metric_name: str,
        value: float,
        unit: str = "None",
        dimensions: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Record a single metric locally.

        Args:
            metric_name: Name of the metric.
            value: Numeric metric value.
            unit: Unit of the metric (CloudWatch-style label).
            dimensions: Optional list of ``{"Name": ..., "Value": ...}`` dicts.
        """
        self._write_metric_records(
            [
                {
                    "MetricName": metric_name,
                    "Value": value,
                    "Unit": unit,
                    "Dimensions": dimensions or [],
                }
            ]
        )

    def emit_processing_time(self, job_id: str, duration_seconds: float) -> None:
        """Record job processing time metric.

        Args:
            job_id: Unique job identifier
            duration_seconds: Processing time in seconds
        """
        self.put_metric(
            "NLPProcessingTime",
            duration_seconds,
            unit="Seconds",
            dimensions=[{"Name": "JobId", "Value": job_id}],
        )

    def emit_document_count(self, job_id: str, count: int) -> None:
        """Record number of documents processed.

        Args:
            job_id: Unique job identifier
            count: Number of documents
        """
        self.put_metric(
            "DocumentsProcessed",
            count,
            unit="Count",
            dimensions=[{"Name": "JobId", "Value": job_id}],
        )

    def emit_job_status(self, job_id: str, status: str) -> None:
        """Record job status (success/failure).

        Args:
            job_id: Unique job identifier
            status: Job status ('success' or 'failure')
        """
        value = 1 if status == "success" else 0

        self.put_metric(
            "NLPJobsCompleted",
            value,
            unit="Count",
            dimensions=[
                {"Name": "JobId", "Value": job_id},
                {"Name": "Status", "Value": status},
            ],
        )

    def emit_error_count(
        self, job_id: str, error_count: int, error_type: Optional[str] = None
    ) -> None:
        """Record number of processing errors.

        Args:
            job_id: Unique job identifier
            error_count: Number of errors
            error_type: Type of error (optional)
        """
        dimensions = [{"Name": "JobId", "Value": job_id}]
        if error_type:
            dimensions.append({"Name": "ErrorType", "Value": error_type})

        self.put_metric(
            "ProcessingErrors",
            error_count,
            unit="Count",
            dimensions=dimensions,
        )

    def emit_batch_metrics(self, batch_metrics: Dict[str, Any]) -> None:
        """Record multiple metrics for a processing batch.

        Args:
            batch_metrics: Dictionary containing:
                - job_id: Unique job identifier
                - duration: Processing time in seconds
                - doc_count: Number of documents processed
                - success: Whether batch completed successfully
                - errors: Number of errors encountered
                - error_type: Type of errors (optional)
        """
        job_id = batch_metrics["job_id"]

        # Record individual metrics
        self.emit_processing_time(job_id, batch_metrics["duration"])
        self.emit_document_count(job_id, batch_metrics["doc_count"])
        self.emit_job_status(
            job_id, "success" if batch_metrics["success"] else "failure"
        )

        if batch_metrics.get("errors", 0) > 0:
            self.emit_error_count(
                job_id, batch_metrics["errors"], batch_metrics.get("error_type")
            )

    def read_metrics(
        self, metric_name: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """Read recorded metrics from the local metrics file.

        Args:
            metric_name: If provided, only return records with this metric name.

        Returns:
            List of metric records (most useful for tests/local inspection).
        """
        records: List[Dict[str, Any]] = []
        if not os.path.exists(self.metrics_file):
            return records

        with open(self.metrics_file, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if metric_name is not None and record.get("MetricName") != metric_name:
                    continue
                records.append(record)
        return records
