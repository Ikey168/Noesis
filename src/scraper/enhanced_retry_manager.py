"""
Enhanced retry logic system with intelligent failure handling and monitoring integration.
Provides comprehensive retry strategies with CloudWatch logging and SNS alerting.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .captcha_solver import CaptchaRequired
from .cloudwatch_logger import CloudWatchLogger, ScrapingMetrics, ScrapingStatus
from .dynamodb_failure_manager import DynamoDBFailureManager
from .sns_alert_manager import SNSAlertManager


class RetryReason(Enum):
    """Reasons for retry attempts."""

    NETWORK_ERROR = "network_error"
    TIMEOUT = "timeout"
    HTTP_ERROR = "http_error"
    CAPTCHA_FAILED = "captcha_failed"
    CAPTCHA_REQUIRED = "captcha-required"
    PROXY_FAILED = "proxy_failed"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    UNKNOWN_ERROR = "unknown_error"


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 5
    base_delay: float = 1.0
    max_delay: float = 300.0
    exponential_base: float = 2.0
    jitter: bool = True
    retry_on_status_codes: List[int] = None
    permanent_failure_codes: List[int] = None

    def __post_init__(self):
        if self.retry_on_status_codes is None:
            self.retry_on_status_codes = [429, 500, 502, 503, 504]
        if self.permanent_failure_codes is None:
            self.permanent_failure_codes = [401, 403, 404, 410]


@dataclass
class RetryAttempt:
    """Information about a retry attempt."""

    attempt_number: int
    delay: float
    reason: RetryReason
    timestamp: float
    error: Optional[str] = None
    proxy_used: Optional[str] = None
    user_agent_used: Optional[str] = None


class EnhancedRetryManager:
    """Enhanced retry manager with monitoring and intelligent failure handling."""

    def __init__(
        self,
        cloudwatch_logger: Optional[CloudWatchLogger] = None,
        failure_manager: Optional[DynamoDBFailureManager] = None,
        alert_manager: Optional[SNSAlertManager] = None,
        retry_config: Optional[RetryConfig] = None,
    ):
        """
        Initialize enhanced retry manager.

        Args:
            cloudwatch_logger: CloudWatch logger instance
            failure_manager: DynamoDB failure manager instance
            alert_manager: SNS alert manager instance
            retry_config: Retry configuration
        """
        self.cloudwatch_logger = cloudwatch_logger
        self.failure_manager = failure_manager
        self.alert_manager = alert_manager
        self.retry_config = retry_config or RetryConfig()

        # Setup logging
        self.logger = logging.getLogger(__name__)

        # Retry tracking
        self.active_retries: Dict[str, List[RetryAttempt]] = {}
        # URLs skipped as ``captcha-required`` (#2858): never retried.
        self.skipped: dict[str, str] = {}
        self.circuit_breaker_state: Dict[str, Dict[str, Any]] = {}

        # Circuit breaker configuration
        self.circuit_breaker_failure_threshold = 5
        self.circuit_breaker_timeout = 300  # 5 minutes

    async def retry_with_backoff(
        self,
        func: Callable[..., Awaitable[Any]],
        *args,
        url: str = "",
        retry_config: Optional[RetryConfig] = None,
        context: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> Any:
        """
        Execute function with intelligent retry logic and monitoring.

        Args:
            func: Async function to retry
            *args: Positional arguments for the function
            url: URL being processed (for tracking)
            retry_config: Override retry configuration
            context: Additional context for monitoring
            **kwargs: Keyword arguments for the function

        Returns:
            Result of successful function execution

        Raises:
            Exception: If all retry attempts fail
        """
        config = retry_config or self.retry_config
        context = context or {}

        start_time = time.time()
        last_exception = None
        retry_attempts = []

        # Check circuit breaker
        if self._is_circuit_breaker_open(url):
            raise Exception("Circuit breaker is open for {0}".format(url))

        for attempt in range(config.max_retries + 1):
            attempt_start = time.time()

            try:
                # Execute function
                result = await func(*args, **kwargs)

                # Record success
                duration_ms = int((time.time() - start_time) * 1000)
                await self._record_success(url, duration_ms, attempt, context)

                # Remove from active retries
                if url in self.active_retries:
                    del self.active_retries[url]

                # Reset circuit breaker
                self._reset_circuit_breaker(url)

                return result

            except CaptchaRequired as e:
                # Typed skip outcome (#2858): no solver is configured, so a
                # retry cannot succeed. Record the skip (not a failure, and not
                # a circuit-breaker strike) and let the caller continue.
                await self._record_captcha_skip(url, e, attempt, context)
                self.active_retries.pop(url, None)
                raise

            except Exception as e:
                last_exception = e
                time.time() - attempt_start

                # Determine retry reason
                retry_reason = self._determine_retry_reason(e, context)

                # Check if this is a permanent failure
                if self._is_permanent_failure(e, context):
                    await self._record_permanent_failure(url, str(e), attempt, context)
                    raise e

                # Record retry attempt
                retry_attempt = RetryAttempt(
                    attempt_number=attempt,
                    delay=0.0,
                    reason=retry_reason,
                    timestamp=time.time(),
                    error=str(e),
                    proxy_used=context.get("proxy_used"),
                    user_agent_used=context.get("user_agent_used"),
                )
                retry_attempts.append(retry_attempt)

                # Track active retries
                if url not in self.active_retries:
                    self.active_retries[url] = []
                self.active_retries[url].append(retry_attempt)

                # Check if we should continue retrying
                if attempt >= config.max_retries:
                    # Final failure
                    duration_ms = int((time.time() - start_time) * 1000)
                    await self._record_final_failure(
                        url, str(e), attempt, duration_ms, context
                    )

                    # Update circuit breaker
                    self._record_circuit_breaker_failure(url)

                    raise e

                # Calculate delay
                delay = self._calculate_delay(attempt, config, retry_reason)
                retry_attempt.delay = delay

                # Log retry attempt
                self.logger.warning(
                    "Retry attempt {0}/{1} for {2} after {3:.2f}s delay. Reason: {4}".format(
                        attempt + 1, config.max_retries, url, delay, retry_reason.value
                    )
                )

                # Record retry metrics
                await self._record_retry_attempt(
                    url, attempt, retry_reason, delay, str(e), context
                )

                # Wait before retry
                await asyncio.sleep(delay)

        # This should never be reached, but just in case
        raise last_exception

    async def _record_captcha_skip(
        self, url: str, exc: CaptchaRequired, attempt: int, context: dict[str, Any]
    ):
        """Record a ``captcha-required, skipped`` outcome without retrying."""
        self.skipped[url] = RetryReason.CAPTCHA_REQUIRED.value
        self.logger.warning(
            "Skipping %s: %s, skipped (no retry)",
            url, RetryReason.CAPTCHA_REQUIRED.value,
        )
        if self.cloudwatch_logger:
            metrics = ScrapingMetrics(
                url=url,
                status=ScrapingStatus.CAPTCHA_BLOCKED,
                timestamp=time.time(),
                duration_ms=0,
                retry_count=attempt,
                error_message=f"{RetryReason.CAPTCHA_REQUIRED.value}, skipped",
                proxy_used=context.get("proxy_used"),
                user_agent=context.get("user_agent_used"),
                response_code=context.get("response_code"),
                captcha_encountered=True,
            )
            await self.cloudwatch_logger.log_scraping_attempt(metrics)

    def _determine_retry_reason(
        self, exception: Exception, context: Dict[str, Any]
    ) -> RetryReason:
        """Determine the reason for retry based on exception and context."""
        if isinstance(exception, CaptchaRequired):
            return RetryReason.CAPTCHA_REQUIRED

        error_str = str(exception).lower()

        if "timeout" in error_str:
            return RetryReason.TIMEOUT
        elif "captcha" in error_str:
            return RetryReason.CAPTCHA_FAILED
        elif "proxy" in error_str:
            return RetryReason.PROXY_FAILED
        elif "rate limit" in error_str or "too many requests" in error_str:
            return RetryReason.RATE_LIMITED
        elif hasattr(exception, "status_code"):
            status_code = getattr(exception, "status_code")
            if status_code >= 500:
                return RetryReason.SERVER_ERROR
            elif status_code in [429]:
                return RetryReason.RATE_LIMITED
            else:
                return RetryReason.HTTP_ERROR
        elif "network" in error_str or "connection" in error_str:
            return RetryReason.NETWORK_ERROR
        else:
            return RetryReason.UNKNOWN_ERROR

    def _is_permanent_failure(
        self, exception: Exception, context: Dict[str, Any]
    ) -> bool:
        """Determine if an exception represents a permanent failure."""
        if isinstance(exception, CaptchaRequired):
            return True
        if hasattr(exception, "status_code"):
            status_code = getattr(exception, "status_code")
            return status_code in self.retry_config.permanent_failure_codes

        error_str = str(exception).lower()
        permanent_keywords = ["forbidden", "unauthorized", "not found", "gone"]
        return any(keyword in error_str for keyword in permanent_keywords)

    def _calculate_delay(
        self, attempt: int, config: RetryConfig, reason: RetryReason
    ) -> float:
        """Calculate delay before next retry attempt."""
        # Base exponential backoff
        delay = min(
            config.base_delay * (config.exponential_base**attempt), config.max_delay
        )

        # Adjust delay based on retry reason
        if reason == RetryReason.RATE_LIMITED:
            delay *= 2  # Longer delay for rate limiting
        elif reason == RetryReason.CAPTCHA_FAILED:
            delay *= 3  # Much longer delay for CAPTCHA issues
        elif reason == RetryReason.PROXY_FAILED:
            delay *= 0.5  # Shorter delay for proxy issues

        # Add jitter to prevent thundering herd
        if config.jitter:
            jitter_range = delay * 0.1
            delay += random.uniform(-jitter_range, jitter_range)

        return max(delay, 0.1)  # Minimum 100ms delay

    async def _record_success(
        self, url: str, duration_ms: int, retry_count: int, context: Dict[str, Any]
    ):
        """Record successful execution."""
        # CloudWatch logging
        if self.cloudwatch_logger:
            metrics = ScrapingMetrics(
                url=url,
                status=ScrapingStatus.SUCCESS,
                timestamp=time.time(),
                duration_ms=duration_ms,
                retry_count=retry_count,
                articles_scraped=context.get("articles_scraped", 0),
                proxy_used=context.get("proxy_used"),
                user_agent=context.get("user_agent_used"),
                response_code=context.get("response_code"),
            )
            await self.cloudwatch_logger.log_scraping_attempt(metrics)

        # Remove from DynamoDB failure tracking
        if self.failure_manager:
            await self.failure_manager.mark_success(url)

        self.logger.info(
            "Successfully scraped {0} after {1} retries in {2}ms".format(
                url, retry_count, duration_ms
            )
        )

    async def _record_retry_attempt(
        self,
        url: str,
        attempt: int,
        reason: RetryReason,
        delay: float,
        error: str,
        context: Dict[str, Any],
    ):
        """Record retry attempt metrics."""
        if self.cloudwatch_logger:
            metrics = ScrapingMetrics(
                url=url,
                status=ScrapingStatus.FAILURE,
                timestamp=time.time(),
                duration_ms=0,
                retry_count=attempt,
                error_message="{0}: {1}".format(reason.value, error),
                proxy_used=context.get("proxy_used"),
                user_agent=context.get("user_agent_used"),
                response_code=context.get("response_code"),
            )
            await self.cloudwatch_logger.log_scraping_attempt(metrics)

    async def _record_final_failure(
        self,
        url: str,
        error: str,
        retry_count: int,
        duration_ms: int,
        context: Dict[str, Any],
    ):
        """Record final failure after all retries exhausted."""
        # CloudWatch logging
        if self.cloudwatch_logger:
            metrics = ScrapingMetrics(
                url=url,
                status=ScrapingStatus.FAILURE,
                timestamp=time.time(),
                duration_ms=duration_ms,
                retry_count=retry_count,
                error_message=error,
                proxy_used=context.get("proxy_used"),
                user_agent=context.get("user_agent_used"),
                response_code=context.get("response_code"),
            )
            await self.cloudwatch_logger.log_scraping_attempt(metrics)

        # DynamoDB failure tracking
        if self.failure_manager:
            await self.failure_manager.record_failure(
                url=url,
                failure_reason=error,
                error_details=error,
                proxy_used=context.get("proxy_used"),
                user_agent_used=context.get("user_agent_used"),
                response_code=context.get("response_code"),
            )

        # SNS alerting
        if self.alert_manager:
            await self.alert_manager.alert_scraper_failure(
                url=url,
                failure_reason=error,
                retry_count=retry_count,
                error_details=error,
            )

        self.logger.error(
            "Final failure for {0} after {1} retries: {2}".format(
                url, retry_count, error
            )
        )

    async def _record_permanent_failure(
        self, url: str, error: str, attempt: int, context: Dict[str, Any]
    ):
        """Record permanent failure that won't be retried."""
        # CloudWatch logging
        if self.cloudwatch_logger:
            metrics = ScrapingMetrics(
                url=url,
                status=ScrapingStatus.FAILURE,
                timestamp=time.time(),
                duration_ms=0,
                retry_count=attempt,
                error_message="PERMANENT: {0}".format(error),
                proxy_used=context.get("proxy_used"),
                user_agent=context.get("user_agent_used"),
                response_code=context.get("response_code"),
            )
            await self.cloudwatch_logger.log_scraping_attempt(metrics)

        # Mark as permanent failure in DynamoDB
        if self.failure_manager:
            await self.failure_manager.record_failure(
                url=url,
                failure_reason="PERMANENT: {0}".format(error),
                error_details=error,
                proxy_used=context.get("proxy_used"),
                user_agent_used=context.get("user_agent_used"),
                response_code=context.get("response_code"),
            )
            await self.failure_manager.mark_permanent_failure(url)

        self.logger.error("Permanent failure for {0}: {1}".format(url, error))

    # Circuit breaker implementation

    def _is_circuit_breaker_open(self, url: str) -> bool:
        """Check if circuit breaker is open for a URL."""
        domain = self._get_domain_from_url(url)
        if domain not in self.circuit_breaker_state:
            return False

        state = self.circuit_breaker_state[domain]
        current_time = time.time()

        if state["state"] == "open":
            if current_time > state["open_until"]:
                # Move to half-open state
                state["state"] = "half-open"
                return False
            return True

        return False

    def _record_circuit_breaker_failure(self, url: str):
        """Record a failure for circuit breaker tracking."""
        domain = self._get_domain_from_url(url)
        current_time = time.time()

        if domain not in self.circuit_breaker_state:
            self.circuit_breaker_state[domain] = {
                "failure_count": 0,
                "state": "closed",
                "open_until": 0,
            }

        state = self.circuit_breaker_state[domain]
        state["failure_count"] += 1

        if state["failure_count"] >= self.circuit_breaker_failure_threshold:
            state["state"] = "open"
            state["open_until"] = current_time + self.circuit_breaker_timeout
            self.logger.warning("Circuit breaker opened for {0}".format(domain))

    def _reset_circuit_breaker(self, url: str):
        """Reset circuit breaker after successful operation."""
        domain = self._get_domain_from_url(url)
        if domain in self.circuit_breaker_state:
            self.circuit_breaker_state[domain] = {
                "failure_count": 0,
                "state": "closed",
                "open_until": 0,
            }

    def _get_domain_from_url(self, url: str) -> str:
        """Extract domain from URL."""
        try:
            from urllib.parse import urlparse

            return urlparse(url).netloc
        except Exception:
            return "unknown"

    async def process_retry_queue(self, limit: int = 50) -> List[str]:
        """
        Process URLs that are ready for retry from DynamoDB.

        Args:
            limit: Maximum number of URLs to process

        Returns:
            List of URLs that were processed
        """
        if not self.failure_manager:
            return []

        try:
            # Get URLs ready for retry
            failed_urls = await self.failure_manager.get_urls_ready_for_retry(limit)
            processed_urls = []

            for failed_url in failed_urls:
                try:
                    # Check circuit breaker
                    if self._is_circuit_breaker_open(failed_url.url):
                        continue

                    # This would be called by the main scraper to retry the URL
                    # For now, just log that it's ready for retry
                    self.logger.info(
                        "URL ready for retry: {0} (attempt {1})".format(
                            failed_url.url, failed_url.retry_count + 1
                        )
                    )
                    processed_urls.append(failed_url.url)

                except Exception as e:
                    self.logger.error(
                        "Error processing retry for {0}: {1}".format(failed_url.url, e)
                    )

            return processed_urls

        except Exception as e:
            self.logger.error("Error processing retry queue: {0}".format(e))
            return []

    async def get_retry_statistics(self) -> Dict[str, Any]:
        """Get comprehensive retry statistics."""
        stats = {"active_retries": len(self.active_retries), "circuit_breakers": {}}

        # Circuit breaker stats
        for domain, state in self.circuit_breaker_state.items():
            stats["circuit_breakers"][domain] = {
                "state": state["state"],
                "failure_count": state["failure_count"],
                "open_until": state.get("open_until", 0),
            }

        # Get DynamoDB failure stats if available
        if self.failure_manager:
            try:
                failure_stats = await self.failure_manager.get_failure_statistics()
                stats["failure_stats"] = failure_stats
            except Exception as e:
                self.logger.error("Error getting failure statistics: {0}".format(e))

        return stats
