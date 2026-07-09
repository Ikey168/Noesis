#!/usr/bin/env python3
"""
Example usage of the NeuroNews monitoring and error handling system.
Demonstrates CloudWatch logging, DynamoDB failure tracking, SNS alerting, and retry logic.
"""

from scraper.sns_alert_manager import SNSAlertManager
from scraper.enhanced_retry_manager import EnhancedRetryManager, RetryConfig
from scraper.dynamodb_failure_manager import DynamoDBFailureManager
from scraper.cloudwatch_logger import (CloudWatchLogger, ScrapingMetrics,
                                       ScrapingStatus)
from scraper.async_scraper_engine import AsyncNewsScraperEngine, NewsSource
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from typing import List

sys.path.append("/workspaces/NeuroNews/src")


# Configure logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


class MonitoringDemo:
    """Demonstration of the complete monitoring and error handling system."""

    def __init__(self):
        # AWS Configuration (update with your actual values)
        self.aws_region = "us-east-1"
        self.cloudwatch_namespace = "NeuroNews/Scraper"
        self.dynamodb_table = "neuronews-failed-urls"
        self.sns_topic_arn = "arn:aws:sns:us-east-1:YOUR_ACCOUNT:neuronews-alerts"

        # Initialize monitoring components
        self.cloudwatch_logger = None
        self.failure_manager = None
        self.alert_manager = None
        self.retry_manager = None

    async def initialize_monitoring(self):
        """Initialize all monitoring components."""
        logger.info("Initializing monitoring components...")

        try:
            # CloudWatch Logger
            self.cloudwatch_logger = CloudWatchLogger(
                region_name=self.aws_region, namespace=self.cloudwatch_namespace
            )
            logger.info(" CloudWatch logger initialized")

            # DynamoDB Failure Manager
            self.failure_manager = DynamoDBFailureManager(
                table_name=self.dynamodb_table, region_name=self.aws_region
            )
            logger.info(" DynamoDB failure manager initialized")

            # SNS Alert Manager (optional - requires topic ARN)
            if self.sns_topic_arn and "YOUR_ACCOUNT" not in self.sns_topic_arn:
                self.alert_manager = SNSAlertManager(
                    topic_arn=self.sns_topic_arn, region_name=self.aws_region
                )
                logger.info(" SNS alert manager initialized")
            else:
                logger.warning(
                    "⚠️ SNS alerts disabled - update topic ARN in demo")

            # Enhanced Retry Manager
            retry_config = RetryConfig(
                max_retries=3, base_delay=2.0, max_delay=60.0)

            self.retry_manager = EnhancedRetryManager(
                cloudwatch_logger=self.cloudwatch_logger,
                failure_manager=self.failure_manager,
                alert_manager=self.alert_manager,
                retry_config=retry_config,
            )
            logger.info(" Enhanced retry manager initialized")

        except Exception as e:
            logger.error("❌ Error initializing monitoring: {0}".format(e))
            raise

    async def demo_successful_scraping(self):
        """Demonstrate successful scraping with monitoring."""
        logger.info(""
 Demo: Successful Scraping Monitoring")"

        # Simulate successful scraping
        metrics=ScrapingMetrics(
            url="https://news.ycombinator.com",
            status=ScrapingStatus.SUCCESS,
            timestamp=time.time(),
            duration_ms=2500,
            articles_scraped=25,
            retry_count=0,
            proxy_used="proxy1.example.com:8080",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            response_code=200,
        )

        if self.cloudwatch_logger:
            await self.cloudwatch_logger.log_scraping_attempt(metrics)
            logger.info(
                " Logged successful scraping: {0} articles in {1}ms".format(
                    metrics.articles_scraped, metrics.duration_ms)
            )


    async def demo_failure_handling(self):
        """Demonstrate failure handling and retry logic."""
        logger.info(""
🔄 Demo: Failure Handling and Retry Logic")"

        url="https://difficult-site.example.com"

        # Record initial failure
        if self.failure_manager:
            failed_url=await self.failure_manager.record_failure(
                url=url,
                failure_reason="timeout",
                error_details="Connection timeout after 30 seconds",
                proxy_used="proxy2.example.com:8080",
                response_code=408,
            )
            logger.info(
                " Recorded failure: {0} (attempt {1})".format(
                    failed_url.url, failed_url.retry_count)
            )

        # Log failure metrics
        if self.cloudwatch_logger:
            failure_metrics=ScrapingMetrics(
                url=url,
                status=ScrapingStatus.TIMEOUT,
                timestamp=time.time(),
                duration_ms=30000,
                articles_scraped=0,
                retry_count=1,
                error_message="Connection timeout",
                response_code=408,
            )
            await self.cloudwatch_logger.log_scraping_attempt(failure_metrics)
            logger.info(" Logged failure metrics to CloudWatch")

        # Send alert for multiple failures
        if self.alert_manager:
            await self.alert_manager.alert_scraper_failure(
                url=url,
                failure_reason="timeout",
                retry_count=3,
                error_details="Consistent timeouts across multiple attempts",
            )
            logger.info(" Sent failure alert via SNS")


    async def demo_retry_logic(self):
        """Demonstrate intelligent retry logic."""
        logger.info(""
🔁 Demo: Intelligent Retry Logic")"

        if not self.retry_manager:
            logger.warning("Retry manager not available")
            return

        attempt_count=0

        async def simulated_scraping():
            """Simulate a scraping function that fails initially then succeeds."""
            nonlocal attempt_count
            attempt_count += 1

            if attempt_count < 3:
                logger.info("Attempt {0}: Simulating failure...".format(attempt_count))
                raise Exception("Simulated failure {0}".format(attempt_count))

            logger.info("Attempt {0}: Success!".format(attempt_count))
            return {"articles": 15, "status": "success"}

        try:
            result=await self.retry_manager.retry_with_backoff(
                simulated_scraping,
                url="https://retry-demo.example.com",
                context={"proxy_used": "proxy3.example.com:8080"},
            )
            logger.info(" Retry logic successful: {0}".format(result))
        except Exception as e:
            logger.error("❌ Retry logic failed: {0}".format(e))


    async def demo_monitoring_analytics(self):
        """Demonstrate monitoring analytics and statistics."""
        logger.info(""
 Demo: Monitoring Analytics")"

        if self.failure_manager:
            # Get failure statistics
            stats=await self.failure_manager.get_failure_statistics(hours=24)
            logger.info(" Failure Statistics (24h):")
            logger.info(f"   Total failures: {stats.get('total_failures', 0)}")
            logger.info(f"   Permanent failures: {stats.get('permanent_failures', 0)}")
            logger.info(f"   Pending retries: {stats.get('pending_retries', 0)}")

            failure_reasons=stats.get(f"ailure_reasons", {})
            if failure_reasons:
                logger.info("   Failure reasons:")
                for reason, count in failure_reasons.items():
                    logger.info("     {0}: {1}".format(reason, count))

        if self.cloudwatch_logger:
            # Get success rate
            success_rate=await self.cloudwatch_logger.get_success_rate(hours=24)
            logger.info(" Success Rate (24h): {0}%".format(success_rate: .1f))

            # Get failure count
            failure_count=await self.cloudwatch_logger.get_failure_count(hours=1)
            logger.info("❌ Recent Failures (1h): {0}".format(failure_count))

        if self.retry_manager:
            # Get retry statistics
            retry_stats=await self.retry_manager.get_retry_statistics()
            logger.info(f"🔄 Active Retries: {retry_stats.get('active_retries', 0)}")

            circuit_breakers=retry_stats.get("circuit_breakers", {})
            if circuit_breakers:
                logger.info("🔌 Circuit Breaker Status:")
                for domain, status in circuit_breakers.items():
                    logger.info(
                        f"   {domain}: {status['state']} (failures: {status[f'ailure_count']})"
                    )


    async def demo_alerting_scenarios(self):
        """Demonstrate various alerting scenarios."""
        logger.info(""
🚨 Demo: Alerting Scenarios")"

        if not self.alert_manager:
            logger.warning("Alert manager not available")
            return

        # High failure rate alert
        await self.alert_manager.alert_high_failure_rate(
            failure_rate=75.5, time_period=1, failed_count=151, total_count=200
        )
        logger.info(" Sent high failure rate alert")

        # CAPTCHA blocking alert
        await self.alert_manager.alert_captcha_blocking(
            url="https://protected-site.example.com", captcha_count=15, time_period=2
        )
        logger.info(" Sent CAPTCHA blocking alert")

        # Performance degradation alert
        await self.alert_manager.alert_performance_degradation(
            avg_response_time=45000, threshold=10000, time_period=1
        )
        logger.info(" Sent performance degradation alert")


    async def demo_scraper_integration(self):
        """Demonstrate integration with AsyncNewsScraperEngine."""
        logger.info(""
🔧 Demo: Scraper Integration")"

        # Configure scraper with monitoring
        scraper=AsyncNewsScraperEngine(
            max_concurrent=5,
            enable_monitoring=True,
            cloudwatch_region=self.aws_region,
            cloudwatch_namespace=self.cloudwatch_namespace,
            dynamodb_table=self.dynamodb_table,
            sns_topic_arn=(
                self.sns_topic_arn if "YOUR_ACCOUNT" not in self.sns_topic_arn else None
            ),
        )

        # Example news sources
        sources=[
            NewsSource(
                name="Hacker News",
                base_url="https://news.ycombinator.com",
                article_selectors={
                    "title": ".titlelink",
                    "url": ".titlelink",
                    "content": ".comment",
                },
                link_patterns=["https://news.ycombinator.com/item*"],
                rate_limit=1.0,
            )
        ]

        try:
            await scraper.start()
            logger.info(" Scraper with monitoring started successfully")

            # The scraper will automatically use monitoring for all operations
            logger.info("🔄 Scraper is ready with full monitoring capabilities")

        except Exception as e:
            logger.error("❌ Error starting scraper: {0}".format(e))
        finally:
            await scraper.close()
            logger.info(" Scraper closed")


    async def run_complete_demo(self):
        """Run the complete monitoring system demonstration."""
        logger.info(" Starting NeuroNews Monitoring System Demo")
        logger.info("=" * 60)

        try:
            # Initialize monitoring
            await self.initialize_monitoring()

            # Run all demo scenarios
            await self.demo_successful_scraping()
            await asyncio.sleep(1)  # Brief pause between demos

            await self.demo_failure_handling()
            await asyncio.sleep(1)

            await self.demo_retry_logic()
            await asyncio.sleep(1)

            await self.demo_monitoring_analytics()
            await asyncio.sleep(1)

            await self.demo_alerting_scenarios()
            await asyncio.sleep(1)

            await self.demo_scraper_integration()

            logger.info(""
 Demo completed successfully!")
            logger.info("=" * 60)"

        except Exception as e:
            logger.error("❌ Demo failed: {0}".format(e))
            raise


    async def cleanup(self):
        """Clean up demo resources."""
        logger.info("🧹 Cleaning up demo resources...")

        if self.cloudwatch_logger:
            await self.cloudwatch_logger.flush_metrics()

        if self.failure_manager:
            # Note: In production, you might want to keep failure records
            # await self.failure_manager.cleanup_old_failures(days=1)
            pass

        logger.info(" Cleanup completed")


async def main():
    """Main demo function."""
    demo = MonitoringDemo()

    try:
        await demo.run_complete_demo()
    except Exception as e:
        logger.error("Demo error: {0}".format(e))
    finally:
        await demo.cleanup()


if __name__ == "__main__":
    """Run the monitoring system demo."""
    print(" NeuroNews Monitoring & Error Handling System Demo")
    print("This demo showcases the complete monitoring capabilities.")
    print(""
⚠️  Note: Some features require AWS credentials and resources.")
    print("Update the AWS configuration in the script before running.
")"

    # Run the demo
    asyncio.run(main())
