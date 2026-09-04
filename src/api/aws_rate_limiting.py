"""
Local Rate Limiting Integration (Issue #59)

Provides configuration and utilities for managing API usage plans, rate-limit
tiers, and rate-limiting metrics LOCALLY -- without any AWS account or boto3.

This replaces the deprecated AWS API Gateway / CloudWatch integration:
- Usage plans and per-user API key/tier assignments are kept in-process and
  persisted as JSON under NOESIS_LOG_DIR (default ./logs).
- Rate-limiting metrics are appended as JSON lines to a local metrics file.

Uses the Python standard library only.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from src.config.env import resolve_env

logger = logging.getLogger(__name__)


def _get_log_dir() -> str:
    """Return the local log/state directory (env NOESIS_LOG_DIR, default ./logs)."""
    log_dir = resolve_env("LOG_DIR", "./logs") or "./logs"
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


@dataclass
class UsagePlan:
    """Configuration for a local rate-limiting usage plan."""

    plan_id: str
    name: str
    description: str
    throttle_burst_limit: int
    throttle_rate_limit: float
    quota_limit: int
    quota_period: str  # DAY, WEEK, MONTH


@dataclass
class RateLimitConfiguration:
    """Local rate limiting configuration (usage plan tiers)."""

    # Usage Plans for different tiers
    FREE_PLAN = UsagePlan(
        plan_id="free-tier-plan",
        name="Free Tier",
        description="Free tier with basic rate limits",
        throttle_burst_limit=15,
        throttle_rate_limit=0.17,  # ~10 requests per minute
        quota_limit=1000,
        quota_period="DAY",
    )

    PREMIUM_PLAN = UsagePlan(
        plan_id="premium-tier-plan",
        name="Premium Tier",
        description="Premium tier with enhanced rate limits",
        throttle_burst_limit=150,
        throttle_rate_limit=1.67,  # ~100 requests per minute
        quota_limit=20000,
        quota_period="DAY",
    )

    ENTERPRISE_PLAN = UsagePlan(
        plan_id="enterprise-tier-plan",
        name="Enterprise Tier",
        description="Enterprise tier with high rate limits",
        throttle_burst_limit=1500,
        throttle_rate_limit=16.67,  # ~1000 requests per minute
        quota_limit=500000,
        quota_period="DAY",
    )


class LocalUsagePlanManager:
    """Manager for local rate-limiting usage plans and per-user assignments.

    State (usage plans, API keys, plan-key associations) is kept in memory and
    persisted to a JSON file under NOESIS_LOG_DIR so it survives restarts and
    can be inspected. No AWS resources are touched.
    """

    def __init__(self, region: str = None):
        # Kept for signature/backward compatibility; only used as a label.
        self.region = region or os.getenv("AWS_REGION", "us-east-1")
        self.api_id = os.getenv("API_GATEWAY_ID")
        self.stage_name = os.getenv("API_GATEWAY_STAGE", "prod")

        self.state_file = os.path.join(_get_log_dir(), "rate_limit_usage_plans.json")
        self._state = self._load_state()
        logger.info(
            "Local usage plan manager initialized (state: {0})".format(self.state_file)
        )

    # ------------------------------------------------------------------
    # Local state helpers
    # ------------------------------------------------------------------
    def _default_state(self) -> Dict[str, Any]:
        return {
            # name -> plan_id
            "usage_plans": {},
            # plan_id -> plan record
            "plans": {},
            # key_id -> {id, name, value, enabled, tags}
            "api_keys": {},
            # plan_id -> [key_id, ...]
            "plan_keys": {},
        }

    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_file, "r") as f:
                state = json.load(f)
            # Ensure all expected keys exist
            base = self._default_state()
            base.update(state)
            return base
        except (FileNotFoundError, ValueError, OSError):
            return self._default_state()

    def _save_state(self) -> None:
        try:
            with open(self.state_file, "w") as f:
                json.dump(self._state, f, indent=2)
        except OSError as e:
            logger.error("Failed to persist usage plan state: {0}".format(e))

    # ------------------------------------------------------------------
    # Usage plans
    # ------------------------------------------------------------------
    async def create_usage_plans(self) -> Dict[str, str]:
        """Create usage plans for different user tiers."""
        config = RateLimitConfiguration()
        plans = [config.FREE_PLAN, config.PREMIUM_PLAN, config.ENTERPRISE_PLAN]
        created_plans: Dict[str, str] = {}

        for plan in plans:
            try:
                plan_id = plan.plan_id
                self._state["plans"][plan_id] = {
                    "id": plan_id,
                    "name": plan.name,
                    "description": plan.description,
                    "throttle": {
                        "burstLimit": plan.throttle_burst_limit,
                        "rateLimit": plan.throttle_rate_limit,
                    },
                    "quota": {"limit": plan.quota_limit, "period": plan.quota_period},
                }

                plan_key = plan.name.lower().replace(" ", "_")
                self._state["usage_plans"][plan_key] = plan_id
                self._state["plan_keys"].setdefault(plan_id, [])
                created_plans[plan_key] = plan_id

                # Associate with API stage key, if configured
                if self.api_id and self.api_id not in self._state["plan_keys"][plan_id]:
                    self._state["plan_keys"][plan_id].append(self.api_id)

                logger.info(
                    "Created usage plan: {0} (ID: {1})".format(plan.name, plan_id)
                )

            except Exception as e:
                logger.error(
                    "Failed to create usage plan {0}: {1}".format(plan.name, e)
                )

        self._save_state()
        return created_plans

    async def get_usage_plans(self) -> Dict[str, str]:
        """Get existing usage plans (name -> plan_id)."""
        try:
            return dict(self._state["usage_plans"])
        except Exception as e:
            logger.error("Failed to get usage plans: {0}".format(e))
            return {}

    # ------------------------------------------------------------------
    # API keys / user assignment
    # ------------------------------------------------------------------
    async def create_api_key_for_user(
        self, user_id: str, api_key_value: str
    ) -> Optional[str]:
        """Create an API key for a user."""
        try:
            key_id = "key-{0}".format(user_id)
            self._state["api_keys"][key_id] = {
                "id": key_id,
                "name": "neuronews-user-{0}".format(user_id),
                "description": "API key for NeuroNews user {0}".format(user_id),
                "enabled": True,
                "value": api_key_value,
                "tags": {
                    "user_id": user_id,
                    "service": "neuronews",
                    "created": datetime.now(timezone.utc).isoformat(),
                },
            }
            self._save_state()
            logger.info("Created API key for user {0}: {1}".format(user_id, key_id))
            return key_id

        except Exception as e:
            logger.error(
                "Failed to create API key for user {0}: {1}".format(user_id, e)
            )
            return None

    async def assign_user_to_plan(self, user_id: str, tier: str, api_key: str) -> bool:
        """Assign a user to a specific usage plan."""
        plan_mapping = {
            "free": "free_tier",
            "premium": "premium_tier",
            "enterprise": "enterprise_tier",
        }

        plan_name = plan_mapping.get(tier)
        if not plan_name:
            logger.error("Unknown tier: {0}".format(tier))
            return False

        try:
            usage_plans = await self.get_usage_plans()
            plan_id = usage_plans.get(plan_name)

            if not plan_id:
                logger.error("Usage plan not found for tier: {0}".format(tier))
                return False

            key_id = await self.create_api_key_for_user(user_id, api_key)
            if not key_id:
                return False

            # Associate key with usage plan
            self._state["plan_keys"].setdefault(plan_id, [])
            if key_id not in self._state["plan_keys"][plan_id]:
                self._state["plan_keys"][plan_id].append(key_id)
            self._save_state()

            logger.info(
                "Assigned user {0} to {1} tier (plan: {2})".format(
                    user_id, tier, plan_id
                )
            )
            return True

        except Exception as e:
            logger.error(
                "Failed to assign user {0} to plan {1}: {2}".format(user_id, tier, e)
            )
            return False

    async def update_user_tier(
        self, user_id: str, old_tier: str, new_tier: str
    ) -> bool:
        """Update a user's tier by moving them to a different usage plan."""
        try:
            user_key = None
            for key in self._state["api_keys"].values():
                if key.get("tags", {}).get("user_id") == user_id:
                    user_key = key
                    break

            if not user_key:
                logger.error("API key not found for user: {0}".format(user_id))
                return False

            # Remove from old usage plan
            old_plans = await self.get_usage_plans()
            old_plan_id = old_plans.get("{0}_tier".format(old_tier))

            if old_plan_id:
                try:
                    keys = self._state["plan_keys"].get(old_plan_id, [])
                    if user_key["id"] in keys:
                        keys.remove(user_key["id"])
                    self._save_state()
                except Exception as e:
                    logger.warning("Could not remove from old plan: {0}".format(e))

            # Add to new usage plan
            return await self.assign_user_to_plan(
                user_id, new_tier, user_key["value"]
            )

        except Exception as e:
            logger.error("Failed to update user tier: {0}".format(e))
            return False

    # ------------------------------------------------------------------
    # Usage statistics / monitoring
    # ------------------------------------------------------------------
    async def get_usage_statistics(
        self, user_api_key: str, start_date: str, end_date: str
    ) -> Dict[str, Any]:
        """Get usage statistics for a user's API key.

        Aggregates locally recorded request-count metrics for the user that
        owns ``user_api_key`` within the [start_date, end_date] window.
        """
        try:
            key_id = None
            user_id = None
            for key in self._state["api_keys"].values():
                if key.get("value") == user_api_key:
                    key_id = key["id"]
                    user_id = key.get("tags", {}).get("user_id")
                    break

            if not key_id:
                logger.error("API key not found: {0}".format(user_api_key))
                return {}

            daily_breakdown = _aggregate_request_counts(
                user_id, start_date, end_date
            )
            total = sum(daily_breakdown.values())

            return {
                "period": "{0} to {1}".format(start_date, end_date),
                "total_requests": total,
                "daily_breakdown": daily_breakdown,
                "position": None,
                "start_date": start_date,
                "end_date": end_date,
            }

        except Exception as e:
            logger.error("Failed to get usage statistics: {0}".format(e))
            return {}

    async def monitor_throttling_events(self) -> List[Dict[str, Any]]:
        """Monitor local throttling events."""
        try:
            return [
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "api_id": self.api_id,
                    "stage": self.stage_name,
                    "throttled_requests": 0,
                    "total_requests": 0,
                    "throttle_rate": 0.0,
                }
            ]

        except Exception as e:
            logger.error("Failed to monitor throttling events: {0}".format(e))
            return []


def _metrics_file() -> str:
    """Path to the local rate-limiting metrics file (JSON lines)."""
    return os.path.join(_get_log_dir(), "rate_limit_metrics.jsonl")


def _aggregate_request_counts(
    user_id: Optional[str], start_date: str, end_date: str
) -> Dict[str, int]:
    """Aggregate RequestCount metrics per day for a user from the metrics file."""
    breakdown: Dict[str, int] = {}
    path = _metrics_file()
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("metric_name") != "RequestCount":
                    continue
                if user_id is not None and record.get("user_id") != user_id:
                    continue
                ts = record.get("timestamp", "")
                day = ts[:10]
                if start_date and day < start_date:
                    continue
                if end_date and day > end_date:
                    continue
                breakdown[day] = breakdown.get(day, 0) + int(record.get("value", 0))
    except (FileNotFoundError, OSError):
        return breakdown
    return breakdown


class LocalMetricsRecorder:
    """Records rate-limiting metrics and alarms to local files.

    Replaces the deprecated AWS CloudWatch integration: metrics are appended as
    JSON lines to a local metrics file and alarm definitions are persisted to a
    local JSON file under NOESIS_LOG_DIR.
    """

    def __init__(self, region: str = None):
        # Kept for signature/backward compatibility; only used as a label.
        self.region = region or os.getenv("AWS_REGION", "us-east-1")
        self.metrics_file = _metrics_file()
        self.alarms_file = os.path.join(_get_log_dir(), "rate_limit_alarms.json")
        logger.info(
            "Local metrics recorder initialized (metrics: {0})".format(
                self.metrics_file
            )
        )

    def _append_metric(self, record: Dict[str, Any]) -> None:
        with open(self.metrics_file, "a") as f:
            f.write(json.dumps(record) + "\n")

    async def put_rate_limit_metrics(
        self, user_id: str, tier: str, requests_count: int, violations: int
    ):
        """Record rate limiting metrics locally."""
        try:
            timestamp = datetime.now(timezone.utc).isoformat()
            namespace = "NeuroNews/RateLimiting"

            self._append_metric(
                {
                    "namespace": namespace,
                    "metric_name": "RequestCount",
                    "user_id": user_id,
                    "tier": tier,
                    "value": requests_count,
                    "unit": "Count",
                    "timestamp": timestamp,
                }
            )
            self._append_metric(
                {
                    "namespace": namespace,
                    "metric_name": "RateLimitViolations",
                    "user_id": user_id,
                    "tier": tier,
                    "value": violations,
                    "unit": "Count",
                    "timestamp": timestamp,
                }
            )

            logger.debug("Recorded rate-limit metrics for user {0}".format(user_id))

        except Exception as e:
            logger.error("Failed to record rate-limit metrics: {0}".format(e))

    async def create_rate_limit_alarms(self):
        """Create (persist) local alarm definitions for rate-limit violations."""
        try:
            alarm = {
                "AlarmName": "NeuroNews-HighRateLimitViolations",
                "ComparisonOperator": "GreaterThanThreshold",
                "EvaluationPeriods": 2,
                "MetricName": "RateLimitViolations",
                "Namespace": "NeuroNews/RateLimiting",
                "Period": 300,  # 5 minutes
                "Statistic": "Sum",
                "Threshold": 100.0,
                "ActionsEnabled": True,
                "AlarmDescription": "High rate limit violations detected",
                "Unit": "Count",
                "created": datetime.now(timezone.utc).isoformat(),
            }

            with open(self.alarms_file, "w") as f:
                json.dump({"alarms": [alarm]}, f, indent=2)

            logger.info("Created local rate-limiting alarms")

        except Exception as e:
            logger.error("Failed to create local rate-limiting alarms: {0}".format(e))


# Utility functions for integration (stable factory names)
def get_api_gateway_manager() -> LocalUsagePlanManager:
    """Get a configured local usage plan manager instance."""
    return LocalUsagePlanManager()


def get_cloudwatch_metrics() -> LocalMetricsRecorder:
    """Get a configured local metrics recorder instance."""
    return LocalMetricsRecorder()


async def setup_aws_rate_limiting() -> bool:
    """Set up local rate limiting infrastructure (usage plans + alarms)."""
    try:
        # Initialize managers
        api_manager = get_api_gateway_manager()
        metrics = get_cloudwatch_metrics()

        # Create usage plans
        plans = await api_manager.create_usage_plans()
        logger.info("Created usage plans: {0}".format(plans))

        # Set up monitoring
        await metrics.create_rate_limit_alarms()

        logger.info("Local rate limiting setup completed successfully")
        return True

    except Exception as e:
        logger.error("Failed to set up local rate limiting: {0}".format(e))
        return False
