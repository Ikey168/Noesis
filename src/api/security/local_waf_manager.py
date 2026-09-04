"""Local Web Application Firewall (WAF) for the NeuroNews API.

A fully in-process replacement for the former AWS WAF integration. It performs
request inspection locally (SQL injection / XSS detection, geofencing and
rate-limit configuration) and persists security events, metrics and the
"web ACL" configuration to local files under ``NOESIS_LOG_DIR`` (default
``./logs``). No AWS account, boto3 or network access is required.
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from src.config.env import resolve_env

logger = logging.getLogger(__name__)


def _log_dir() -> str:
    """Return the local directory used to persist WAF state."""
    path = resolve_env("LOG_DIR", "./logs") or "./logs"
    os.makedirs(path, exist_ok=True)
    return path


class ThreatType(Enum):
    """Types of security threats."""

    SQL_INJECTION = "sql_injection"
    XSS_ATTACK = "xss_attack"
    RATE_LIMIT_EXCEEDED = "rate_limit_exceeded"
    GEO_BLOCKED = "geo_blocked"
    MALICIOUS_IP = "malicious_ip"
    BOT_TRAFFIC = "bot_traffic"
    DDOS_ATTEMPT = "ddos_attempt"


class ActionType(Enum):
    """WAF action types."""

    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    COUNT = "COUNT"
    CAPTCHA = "CAPTCHA"


@dataclass
class WAFRule:
    """WAF rule configuration."""

    name: str
    priority: int
    action: ActionType
    rule_type: str
    description: str
    enabled: bool = True
    metric_name: Optional[str] = None


@dataclass
class SecurityEvent:
    """Security event data structure."""

    timestamp: datetime
    threat_type: ThreatType
    source_ip: str
    user_agent: str
    request_path: str
    action_taken: ActionType
    details: Dict[str, Any]
    severity: str = "medium"


class LocalWAFManager:
    """In-process Web Application Firewall manager (local, no AWS)."""

    # Metric names tracked by the local WAF (mirror the former CloudWatch metrics).
    METRIC_NAMES = [
        "SQLInjectionBlocked",
        "XSSBlocked",
        "GeoBlocked",
        "RateLimitExceeded",
        "KnownBadInputsBlocked",
        "CoreRuleSetBlocked",
        "BotControlBlocked",
    ]

    def __init__(self):
        """Initialize the local WAF manager."""
        self.region = os.getenv("AWS_REGION", "local")
        self.web_acl_name = os.getenv("WAF_WEB_ACL_NAME", "NeuroNewsAPIProtection")
        self.api_gateway_arn = os.getenv("API_GATEWAY_ARN", "")

        # Allowed countries for geofencing
        self.allowed_countries = os.getenv(
            "WAF_ALLOWED_COUNTRIES", "US,CA,GB,AU,DE,FR,JP"
        ).split(",")

        # Rate limiting configuration (requests per 5 minutes)
        self.rate_limit_requests = int(os.getenv("WAF_RATE_LIMIT", "2000"))

        # Local persistence
        self.events_file = os.path.join(_log_dir(), "waf_events.jsonl")
        self.web_acl_file = os.path.join(_log_dir(), "waf_web_acl.json")
        self.dashboard_file = os.path.join(_log_dir(), "waf_dashboard.json")

        # In-memory state
        self.web_acl_arn: Optional[str] = None
        self.security_events: List[SecurityEvent] = []
        self.metrics: Dict[str, int] = {name: 0 for name in self.METRIC_NAMES}

        logger.info("Local WAF manager initialized (region=%s)", self.region)

    # ------------------------------------------------------------------
    # Web ACL configuration (persisted locally)
    # ------------------------------------------------------------------
    def create_web_acl(self) -> bool:
        """Create the local Web ACL configuration and persist it to disk."""
        try:
            config = {
                "name": self.web_acl_name,
                "region": self.region,
                "default_action": "ALLOW",
                "rules": self._get_waf_rules(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            with open(self.web_acl_file, "w") as f:
                json.dump(config, f, indent=2)

            self.web_acl_arn = "local:waf:{0}:{1}".format(
                self.region, self.web_acl_name
            )
            logger.info("Created local Web ACL: %s", self.web_acl_name)
            return True
        except OSError as e:
            logger.error("Failed to create local Web ACL: %s", e)
            return False

    def _get_waf_rules(self) -> List[Dict[str, Any]]:
        """Define the local WAF rule set as plain configuration dictionaries."""
        rules: List[Dict[str, Any]] = [
            {
                "name": "SQLInjectionProtection",
                "priority": 1,
                "type": "sqli",
                "action": "BLOCK",
                "metric_name": "SQLInjectionBlocked",
            },
            {
                "name": "XSSProtection",
                "priority": 2,
                "type": "xss",
                "action": "BLOCK",
                "metric_name": "XSSBlocked",
            },
        ]

        if self.allowed_countries:
            rules.append(
                {
                    "name": "GeofencingRule",
                    "priority": 3,
                    "type": "geo",
                    "action": "BLOCK",
                    "allowed_countries": self.allowed_countries,
                    "metric_name": "GeoBlocked",
                }
            )

        rules.append(
            {
                "name": "RateLimitingRule",
                "priority": 4,
                "type": "rate_based",
                "action": "BLOCK",
                "limit": self.rate_limit_requests,
                "metric_name": "RateLimitExceeded",
            }
        )
        return rules

    def _get_existing_web_acl(self) -> bool:
        """Load an existing local Web ACL configuration if present."""
        try:
            if os.path.exists(self.web_acl_file):
                with open(self.web_acl_file) as f:
                    config = json.load(f)
                if config.get("name") == self.web_acl_name:
                    self.web_acl_arn = "local:waf:{0}:{1}".format(
                        self.region, self.web_acl_name
                    )
                    return True
            logger.warning("Local Web ACL %s not found", self.web_acl_name)
            return False
        except (OSError, ValueError) as e:
            logger.error("Error reading local Web ACL: %s", e)
            return False

    def associate_with_api_gateway(self, api_gateway_arn: str = None) -> bool:
        """Associate the Web ACL with an API target (local no-op)."""
        target_arn = api_gateway_arn or self.api_gateway_arn
        if not self.web_acl_arn and not self.create_web_acl():
            return False
        logger.info("Associated local Web ACL with target: %s", target_arn or "local")
        return True

    # ------------------------------------------------------------------
    # Event recording, metrics and blocked-request reporting
    # ------------------------------------------------------------------
    def record_event(self, event: SecurityEvent) -> None:
        """Record a security event in memory and append it to the local log."""
        self.security_events.append(event)

        metric = {
            ThreatType.SQL_INJECTION: "SQLInjectionBlocked",
            ThreatType.XSS_ATTACK: "XSSBlocked",
            ThreatType.GEO_BLOCKED: "GeoBlocked",
            ThreatType.RATE_LIMIT_EXCEEDED: "RateLimitExceeded",
            ThreatType.MALICIOUS_IP: "KnownBadInputsBlocked",
            ThreatType.BOT_TRAFFIC: "BotControlBlocked",
        }.get(event.threat_type)
        if metric:
            self.metrics[metric] = self.metrics.get(metric, 0) + 1

        record = {
            "timestamp": event.timestamp.isoformat(),
            "threat_type": event.threat_type.value,
            "source_ip": event.source_ip,
            "user_agent": event.user_agent,
            "request_path": event.request_path,
            "action_taken": event.action_taken.value,
            "severity": event.severity,
            "details": event.details,
        }
        try:
            with open(self.events_file, "a") as f:
                f.write(json.dumps(record) + "\n")
        except OSError as e:
            logger.warning("Failed to persist WAF event: %s", e)

    def get_security_metrics(self) -> Dict[str, Any]:
        """Return locally-tracked WAF security metrics."""
        now = datetime.now(timezone.utc)
        metrics = {
            name: {"blocked_requests": self.metrics.get(name, 0), "datapoints": 1}
            for name in self.METRIC_NAMES
        }
        return {
            "timestamp": now.isoformat(),
            "web_acl_name": self.web_acl_name,
            "metrics": metrics,
            "time_range": {"start": now.isoformat(), "end": now.isoformat()},
        }

    def get_blocked_requests(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return recent blocked requests from the local security event log."""
        blocked = [
            e
            for e in self.security_events
            if e.action_taken in (ActionType.BLOCK, ActionType.CAPTCHA)
        ]
        blocked = blocked[-limit:]
        return [
            {
                "timestamp": e.timestamp.isoformat(),
                "client_ip": e.source_ip,
                "country": e.details.get("country", "unknown"),
                "uri": e.request_path,
                "method": e.details.get("method", "unknown"),
                "headers": e.details.get("headers", []),
                "action": e.action_taken.value,
                "weight": e.details.get("weight", 1),
            }
            for e in reversed(blocked)
        ]

    def create_security_dashboard(self) -> bool:
        """Write a local security dashboard definition to disk."""
        try:
            dashboard = {
                "name": "NeuroNews-WAF-Security-{0}".format(self.region),
                "web_acl_name": self.web_acl_name,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "widgets": [
                    {"title": "WAF Requests Overview", "metrics": self.METRIC_NAMES},
                    {
                        "title": "Attack Types Blocked",
                        "metrics": ["SQLInjectionBlocked", "XSSBlocked"],
                    },
                    {
                        "title": "Geo & Rate Limiting",
                        "metrics": ["GeoBlocked", "RateLimitExceeded"],
                    },
                ],
            }
            with open(self.dashboard_file, "w") as f:
                json.dump(dashboard, f, indent=2)
            logger.info("Created local security dashboard: %s", self.dashboard_file)
            return True
        except OSError as e:
            logger.error("Error creating local security dashboard: %s", e)
            return False

    def setup_logging(self) -> bool:
        """Set up local WAF logging (ensures the events file is writable)."""
        try:
            with open(self.events_file, "a"):
                pass
            logger.info("Local WAF logging configured: %s", self.events_file)
            return True
        except OSError as e:
            logger.error("Error setting up local WAF logging: %s", e)
            return False

    def _get_account_id(self) -> str:
        """Return a synthetic local account id."""
        return os.getenv("LOCAL_ACCOUNT_ID", "000000000000")

    def health_check(self) -> Dict[str, Any]:
        """Check the local WAF system health."""
        web_acl_ok = os.path.exists(self.web_acl_file) or self.web_acl_arn is not None
        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "components": {
                "waf_engine": "operational",
                "event_log": "operational",
                "metrics": "operational",
                "web_acl": "operational" if web_acl_ok else "not_configured",
            },
            "overall_status": "healthy",
        }

    # ------------------------------------------------------------------
    # Threat detection (pure, in-process)
    # ------------------------------------------------------------------
    def detect_sql_injection(self, content: str) -> bool:
        """Detect SQL injection patterns in content."""
        return self._detect_sql_injection(content)

    def detect_xss(self, content: str) -> bool:
        """Detect XSS patterns in content."""
        return self._detect_xss(content)

    def _detect_sql_injection(self, content: str) -> bool:
        """Return True when content contains common SQL injection patterns."""
        if not content:
            return False
        import re

        lowered = content.lower()
        patterns = [
            r"'\s*or\s+\d+\s*=\s*\d+",
            r"'\s*and\s+\d+\s*=\s*\d+",
            r"\bunion\b\s+\bselect\b",
            r"\bdrop\s+table\b",
            r"\binsert\s+into\b",
            r"\bdelete\s+from\b",
            r"--",
            r"#$",
            r";\s*drop\b",
            r"\bexec(\s|\+)+(s|x)p\w+",
        ]
        return any(re.search(p, lowered) for p in patterns)

    def _detect_xss(self, content: str) -> bool:
        """Return True when content contains common XSS patterns."""
        if not content:
            return False
        import re

        lowered = content.lower()
        patterns = [
            r"<\s*script",
            r"</\s*script",
            r"javascript:",
            r"\bon\w+\s*=",
            r"<\s*img[^>]*\bon\w+",
            r"<\s*svg[^>]*\bon\w+",
            r"<\s*iframe",
            r"document\.(cookie|location)",
        ]
        return any(re.search(p, lowered) for p in patterns)


# Global local WAF manager instance
waf_manager = LocalWAFManager()
