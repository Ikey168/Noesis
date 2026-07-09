# NeuroNews Monitoring & Error Handling System Documentation

## Overview

The NeuroNews monitoring and error handling system provides comprehensive real-time monitoring, intelligent failure tracking, automated retry logic, and proactive alerting for the news scraper. This system ensures reliable scraping operations with visibility into performance metrics and automatic recovery from transient failures.

## Architecture Components

### 1. CloudWatch Logger (`cloudwatch_logger.py`)

**Purpose**: Logs scraper execution metrics and success/failure rates to AWS CloudWatch.

**Key Features**:

- Real-time metrics logging (success rates, response times, articles scraped)

- Batch metric processing for efficiency

- Custom CloudWatch alarms for monitoring thresholds

- Detailed log entries with structured data

- Automatic log group and stream management

**Metrics Tracked**:

- `ScrapingAttempts`: Success/failure count by URL and status

- `ScrapingDuration`: Response time metrics by URL

- `ArticlesScraped`: Number of articles successfully extracted

- `RetryCount`: Number of retry attempts per URL

- `CaptchaEncounters`: CAPTCHA detection frequency

- `IPBlocks`: IP blocking incidents

- `SuccessRate`: Overall success percentage

- `AverageDuration`: Average response time

### 2. DynamoDB Failure Manager (`dynamodb_failure_manager.py`)

**Purpose**: Stores failed URLs for intelligent retry attempts with exponential backoff.

**Key Features**:

- Persistent failure tracking across scraper restarts

- Multiple retry strategies (exponential, linear, fixed interval)

- Automatic retry scheduling based on failure patterns

- Permanent failure detection to avoid infinite retries

- Comprehensive failure statistics and analytics

- Automatic cleanup of old failure records

**Data Stored**:

- URL and failure reason

- First and last failure timestamps

- Retry count and maximum retry limits

- Next scheduled retry time

- Error details and HTTP response codes

- Proxy and user agent used during failure

- Permanent failure flags

### 3. SNS Alert Manager (`sns_alert_manager.py`)

**Purpose**: Sends intelligent alerts when scrapers fail multiple times or performance degrades.

**Key Features**:

- Multiple alert types (scraper failure, high failure rate, CAPTCHA blocking, etc.)

- Rate limiting to prevent alert spam

- Severity-based alerting (INFO, WARNING, ERROR, CRITICAL)

- Structured alert messages with metadata

- Automatic muting during high-frequency alert periods

- Alert filtering and routing capabilities

**Alert Types**:

- **Scraper Failure**: Individual URL failures with retry counts

- **High Failure Rate**: When success rate drops below threshold

- **CAPTCHA Blocking**: Frequent CAPTCHA encounters indicating detection

- **IP Blocking**: Multiple IP addresses blocked by target sites

- **Performance Degradation**: Response times exceeding thresholds

- **System Errors**: Internal component failures

### 4. Enhanced Retry Manager (`enhanced_retry_manager.py`)

**Purpose**: Implements intelligent retry logic with circuit breaker patterns and monitoring integration.

**Key Features**:

- Multiple retry strategies with configurable parameters

- Circuit breaker pattern to prevent cascading failures

- Integration with all monitoring components

- Intelligent delay calculation based on failure types

- Permanent failure detection and handling

- Context-aware retry decisions

**Retry Strategies**:

- **Exponential Backoff**: Increasing delays (2^n * base_delay)

- **Linear Backoff**: Linear increase in delays

- **Fixed Interval**: Consistent delay between retries

- **Jitter**: Random variation to prevent thundering herd

## Setup and Configuration

### 1. AWS Prerequisites

**CloudWatch**:

```bash

# Ensure your AWS credentials have CloudWatch permissions

aws configure set region us-east-1
aws logs create-log-group --log-group-name /aws/lambda/neuronews-scraper

```text

**DynamoDB**:

```bash

# The table will be created automatically, but you can pre-create it:

aws dynamodb create-table \
    --table-name neuronews-failed-urls \
    --attribute-definitions \
        AttributeName=url,AttributeType=S \
        AttributeName=next_retry_time,AttributeType=N \
    --key-schema \
        AttributeName=url,KeyType=HASH \
    --global-secondary-indexes \
        IndexName=retry-time-index,KeySchema=[{AttributeName=next_retry_time,KeyType=HASH}],Projection={ProjectionType=ALL} \
    --billing-mode PAY_PER_REQUEST

```text

**SNS**:

```bash

# Create SNS topic for alerts

aws sns create-topic --name neuronews-alerts
aws sns subscribe --topic-arn arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts \
    --protocol email --notification-endpoint your-email@example.com

```text

### 2. Configuration Files

**Monitoring Configuration** (`config_monitoring.json`):

```json

{
  "monitoring": {
    "enabled": true,
    "cloudwatch": {
      "region_name": "us-east-1",
      "namespace": "NeuroNews/Scraper",
      "log_group": "/aws/lambda/neuronews-scraper"
    },
    "dynamodb": {
      "table_name": "neuronews-failed-urls",
      "region_name": "us-east-1",
      "retry_strategy": "exponential_backoff",
      "max_retries": 5
    },
    "sns": {
      "topic_arn": "arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts",
      "region_name": "us-east-1"
    }
  }
}

```text

### 3. Integration with AsyncNewsScraperEngine

**Basic Usage**:

```python

from scraper.async_scraper_engine import AsyncNewsScraperEngine

# Initialize with monitoring enabled

scraper = AsyncNewsScraperEngine(
    max_concurrent=10,
    enable_monitoring=True,
    cloudwatch_region='us-east-1',
    cloudwatch_namespace='NeuroNews/Scraper',
    dynamodb_table='neuronews-failed-urls',
    sns_topic_arn='arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts'
)

await scraper.start()

# All scraping operations now include automatic monitoring

results = await scraper.scrape_url("https://example.com/news")
await scraper.close()

```text

**Advanced Configuration**:

```python

from scraper.enhanced_retry_manager import RetryConfig

# Custom retry configuration

retry_config = RetryConfig(
    max_retries=5,
    base_delay=2.0,
    max_delay=300.0,
    exponential_base=2.0,
    jitter=True,
    retry_on_status_codes=[429, 500, 502, 503, 504],
    permanent_failure_codes=[401, 403, 404, 410]
)

scraper = AsyncNewsScraperEngine(
    enable_monitoring=True,
    # ... other parameters

)

```text

## Usage Examples

### 1. Manual Monitoring Integration

```python

import asyncio
from scraper.cloudwatch_logger import CloudWatchLogger, ScrapingMetrics, ScrapingStatus
from scraper.dynamodb_failure_manager import DynamoDBFailureManager
from scraper.sns_alert_manager import SNSAlertManager
from scraper.enhanced_retry_manager import EnhancedRetryManager

async def example_monitoring():
    # Initialize components

    cloudwatch = CloudWatchLogger(region_name='us-east-1')
    failure_manager = DynamoDBFailureManager(table_name='neuronews-failed-urls')
    alert_manager = SNSAlertManager(topic_arn='arn:aws:sns:us-east-1:ACCOUNT:alerts')

    retry_manager = EnhancedRetryManager(
        cloudwatch_logger=cloudwatch,
        failure_manager=failure_manager,
        alert_manager=alert_manager
    )

    # Example scraping function with monitoring

    async def scrape_with_monitoring(url):
        async def scraping_logic():
            # Your scraping logic here

            return {"articles": 10, "success": True}

        # Automatic retry with monitoring

        return await retry_manager.retry_with_backoff(
            scraping_logic,
            url=url,
            context={"proxy_used": "proxy1.example.com:8080"}
        )

    # Execute with monitoring

    result = await scrape_with_monitoring("https://example.com")

```text

### 2. Failure Recovery Workflow

```python

async def process_failed_urls():
    """Process URLs that are ready for retry."""
    failure_manager = DynamoDBFailureManager(table_name='neuronews-failed-urls')

    # Get URLs ready for retry

    failed_urls = await failure_manager.get_urls_ready_for_retry(limit=50)

    for failed_url in failed_urls:
        try:
            # Attempt to scrape again

            result = await scrape_url_with_retry(failed_url.url)

            # Mark as successful

            await failure_manager.mark_success(failed_url.url)

        except Exception as e:
            # Record another failure

            await failure_manager.record_failure(
                url=failed_url.url,
                failure_reason=str(e),
                error_details=str(e)
            )

```text

### 3. Custom Alerting

```python

async def custom_monitoring_alerts():
    alert_manager = SNSAlertManager(topic_arn='arn:aws:sns:us-east-1:ACCOUNT:alerts')

    # Check system health

    failure_manager = DynamoDBFailureManager(table_name='neuronews-failed-urls')
    stats = await failure_manager.get_failure_statistics(hours=1)

    # Alert on high failure rate

    if stats['total_failures'] > 50:
        await alert_manager.alert_high_failure_rate(
            failure_rate=80.0,
            time_period=1,
            failed_count=stats['total_failures'],
            total_count=100
        )

    # Alert on CAPTCHA issues

    if 'captcha' in stats.get('failure_reasons', {}):
        await alert_manager.alert_captcha_blocking(
            url="https://problematic-site.com",
            captcha_count=stats['failure_reasons']['captcha'],
            time_period=1
        )

```text

## CloudWatch Dashboards and Alarms

### 1. Key Metrics Dashboard

Create a CloudWatch dashboard to monitor:

- Success Rate (%)

- Average Response Time (ms)

- Articles Scraped per Hour

- Retry Count Trends

- Error Rate by Source

### 2. Automated Alarms

**High Failure Rate Alarm**:

```bash

aws cloudwatch put-metric-alarm \
    --alarm-name "NeuroNews-High-Failure-Rate" \
    --alarm-description "Alert when scraper failure rate exceeds 50%" \
    --metric-name SuccessRate \
    --namespace NeuroNews/Scraper \
    --statistic Average \
    --period 300 \
    --threshold 50 \
    --comparison-operator LessThanThreshold \
    --evaluation-periods 2

```text

**Performance Degradation Alarm**:

```bash

aws cloudwatch put-metric-alarm \
    --alarm-name "NeuroNews-Slow-Response" \
    --alarm-description "Alert when average response time exceeds 10 seconds" \
    --metric-name AverageDuration \
    --namespace NeuroNews/Scraper \
    --statistic Average \
    --period 300 \
    --threshold 10000 \
    --comparison-operator GreaterThanThreshold \
    --evaluation-periods 3

```text

## Performance Optimization

### 1. Batch Processing

- CloudWatch metrics are batched to reduce API calls

- DynamoDB operations use batch writes when possible

- SNS alerts are rate-limited to prevent spam

### 2. Memory Management

- Automatic cleanup of old failure records

- Circuit breaker patterns prevent resource exhaustion

- Monitoring data structures use bounded collections

### 3. Cost Optimization

- Use CloudWatch custom metrics efficiently

- DynamoDB on-demand billing for variable workloads

- SNS message filtering to reduce costs

## Troubleshooting

### Common Issues

**1. CloudWatch Permissions**:

```bash

# Ensure IAM role has required permissions

{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "cloudwatch:PutMetricData",
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents"
            ],
            "Resource": "*"
        }
    ]
}

```text

**2. DynamoDB Access**:

```bash

# Required DynamoDB permissions

{
    "Effect": "Allow",
    "Action": [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:CreateTable",
        "dynamodb:DescribeTable"
    ],
    "Resource": "arn:aws:dynamodb:*:*:table/neuronews-failed-urls*"
}

```text

**3. SNS Publishing**:

```bash

# SNS permissions for alerting

{
    "Effect": "Allow",
    "Action": [
        "sns:Publish"
    ],
    "Resource": "arn:aws:sns:*:*:neuronews-alerts"
}

```text

### Debugging

**Enable Debug Logging**:

```python

import logging
logging.basicConfig(level=logging.DEBUG)

# Components will output detailed debug information

```text

**Monitor System Resources**:

```python

# Check monitoring system health

retry_manager = EnhancedRetryManager()
stats = await retry_manager.get_retry_statistics()
print(f"Active retries: {stats['active_retries']}")
print(f"Circuit breakers: {stats['circuit_breakers']}")

```text

## Best Practices

### 1. Monitoring Configuration

- Use appropriate retry limits to balance persistence and resource usage

- Set reasonable alert thresholds to avoid notification fatigue

- Configure circuit breakers to prevent cascading failures

- Monitor system resources to prevent memory leaks

### 2. Failure Handling

- Classify failures appropriately (temporary vs. permanent)

- Use exponential backoff for transient failures

- Implement jitter to prevent thundering herd problems

- Clean up old failure records regularly

### 3. Alerting Strategy

- Use severity levels to prioritize alerts

- Implement rate limiting to prevent alert storms

- Create escalation policies for critical alerts

- Test alert systems regularly

### 4. Performance Monitoring

- Track key metrics consistently

- Set up automated dashboards for visibility

- Monitor trends over time, not just point-in-time values

- Use statistical analysis to detect anomalies

## Security Considerations

### 1. Credential Management

- Use IAM roles instead of hardcoded credentials

- Implement least-privilege access policies

- Rotate credentials regularly

- Use AWS Secrets Manager for sensitive data

### 2. Data Privacy

- Avoid logging sensitive data in CloudWatch

- Implement data retention policies

- Use encryption for data at rest and in transit

- Comply with applicable privacy regulations

### 3. Network Security

- Use VPC endpoints for AWS service access

- Implement proper security groups and NACLs

- Monitor for unusual network patterns

- Use SSL/TLS for all communications

## Maintenance and Operations

### 1. Regular Tasks

- Review and update alert thresholds

- Clean up old monitoring data

- Analyze failure patterns and optimize retry logic

- Update monitoring dashboards based on operational needs

### 2. Capacity Planning

- Monitor CloudWatch and DynamoDB usage

- Plan for scaling during high-volume periods

- Optimize costs based on usage patterns

- Implement auto-scaling where appropriate

### 3. Disaster Recovery

- Back up DynamoDB failure tracking data

- Document recovery procedures

- Test monitoring system failover

- Maintain alternative alerting channels

This comprehensive monitoring and error handling system provides enterprise-grade reliability and observability for the NeuroNews scraper, ensuring consistent performance and proactive issue resolution.
