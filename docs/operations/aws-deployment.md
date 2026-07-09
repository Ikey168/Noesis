# AWS Deployment Guide for NeuroNews Monitoring System

## Overview

This guide provides step-by-step instructions for deploying the NeuroNews monitoring and error handling system on AWS. It includes infrastructure setup, IAM permissions, service configuration, and validation procedures.

## Prerequisites

- AWS CLI installed and configured

- AWS account with appropriate permissions

- Python 3.8+ environment

- Terraform (optional, for infrastructure as code)

## Step 1: IAM Role and Permissions Setup

### 1.1 Create IAM Policy for NeuroNews Monitoring

Create a policy document `neuronews-monitoring-policy.json`:

```json

{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "CloudWatchMetricsAndLogs",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:PutMetricData",
                "cloudwatch:GetMetricStatistics",
                "cloudwatch:ListMetrics",
                "cloudwatch:PutMetricAlarm",
                "cloudwatch:DeleteAlarms",
                "cloudwatch:DescribeAlarms",
                "logs:CreateLogGroup",
                "logs:CreateLogStream",
                "logs:PutLogEvents",
                "logs:DescribeLogGroups",
                "logs:DescribeLogStreams"
            ],
            "Resource": [
                "arn:aws:cloudwatch:*:*:metric/NeuroNews/*",
                "arn:aws:logs:*:*:log-group:/aws/lambda/neuronews-scraper*"
            ]
        },
        {
            "Sid": "DynamoDBAccess",
            "Effect": "Allow",
            "Action": [
                "dynamodb:CreateTable",
                "dynamodb:DescribeTable",
                "dynamodb:GetItem",
                "dynamodb:PutItem",
                "dynamodb:UpdateItem",
                "dynamodb:DeleteItem",
                "dynamodb:Scan",
                "dynamodb:Query",
                "dynamodb:BatchGetItem",
                "dynamodb:BatchWriteItem"
            ],
            "Resource": [
                "arn:aws:dynamodb:*:*:table/neuronews-failed-urls",
                "arn:aws:dynamodb:*:*:table/neuronews-failed-urls/index/*"
            ]
        },
        {
            "Sid": "SNSPublish",
            "Effect": "Allow",
            "Action": [
                "sns:Publish",
                "sns:CreateTopic",
                "sns:GetTopicAttributes",
                "sns:ListSubscriptionsByTopic"
            ],
            "Resource": "arn:aws:sns:*:*:neuronews-alerts*"
        }
    ]
}

```text

### 1.2 Create IAM Role

```bash

# Create the IAM policy

aws iam create-policy \
    --policy-name NeuroNewsMonitoringPolicy \
    --policy-document file://neuronews-monitoring-policy.json \
    --description "Policy for NeuroNews monitoring and error handling"

# Create trust policy for EC2/Lambda execution

cat > trust-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "Service": [
                    "ec2.amazonaws.com",
                    "lambda.amazonaws.com"
                ]
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
EOF

# Create IAM role

aws iam create-role \
    --role-name NeuroNewsMonitoringRole \
    --assume-role-policy-document file://trust-policy.json \
    --description "Role for NeuroNews monitoring components"

# Attach the policy to the role

aws iam attach-role-policy \
    --role-name NeuroNewsMonitoringRole \
    --policy-arn arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):policy/NeuroNewsMonitoringPolicy

# Attach basic execution role for Lambda (if using Lambda)

aws iam attach-role-policy \
    --role-name NeuroNewsMonitoringRole \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

```text

## Step 2: CloudWatch Setup

### 2.1 Create Log Group

```bash

# Create CloudWatch log group

aws logs create-log-group \
    --log-group-name /aws/lambda/neuronews-scraper \
    --region us-east-1

# Set retention policy (optional)

aws logs put-retention-policy \
    --log-group-name /aws/lambda/neuronews-scraper \
    --retention-in-days 30

```text

### 2.2 Create CloudWatch Alarms

```bash

# High failure rate alarm

aws cloudwatch put-metric-alarm \
    --alarm-name "NeuroNews-High-Failure-Rate" \
    --alarm-description "Alert when scraper failure rate exceeds 50%" \
    --actions-enabled \
    --alarm-actions "arn:aws:sns:us-east-1:$(aws sts get-caller-identity --query Account --output text):neuronews-alerts" \
    --metric-name "SuccessRate" \
    --namespace "NeuroNews/Scraper" \
    --statistic "Average" \
    --dimensions "Source=AsyncScraper" \
    --period 300 \
    --unit "Percent" \
    --evaluation-periods 2 \
    --threshold 50.0 \
    --comparison-operator "LessThanThreshold" \
    --treat-missing-data "breaching"

# Performance degradation alarm

aws cloudwatch put-metric-alarm \
    --alarm-name "NeuroNews-Slow-Response" \
    --alarm-description "Alert when average response time exceeds 10 seconds" \
    --actions-enabled \
    --alarm-actions "arn:aws:sns:us-east-1:$(aws sts get-caller-identity --query Account --output text):neuronews-alerts" \
    --metric-name "AverageDuration" \
    --namespace "NeuroNews/Scraper" \
    --statistic "Average" \
    --dimensions "Source=AsyncScraper" \
    --period 300 \
    --unit "Milliseconds" \
    --evaluation-periods 3 \
    --threshold 10000.0 \
    --comparison-operator "GreaterThanThreshold" \
    --treat-missing-data "notBreaching"

# High retry count alarm

aws cloudwatch put-metric-alarm \
    --alarm-name "NeuroNews-High-Retry-Count" \
    --alarm-description "Alert when retry count is consistently high" \
    --actions-enabled \
    --alarm-actions "arn:aws:sns:us-east-1:$(aws sts get-caller-identity --query Account --output text):neuronews-alerts" \
    --metric-name "RetryCount" \
    --namespace "NeuroNews/Scraper" \
    --statistic "Average" \
    --dimensions "Source=AsyncScraper" \
    --period 600 \
    --unit "Count" \
    --evaluation-periods 2 \
    --threshold 3.0 \
    --comparison-operator "GreaterThanThreshold" \
    --treat-missing-data "notBreaching"

```text

## Step 3: DynamoDB Setup

### 3.1 Create DynamoDB Table

```bash

# Create the main table for failed URLs

aws dynamodb create-table \
    --table-name neuronews-failed-urls \
    --attribute-definitions \
        AttributeName=url,AttributeType=S \
        AttributeName=next_retry_time,AttributeType=N \
        AttributeName=failure_reason,AttributeType=S \
    --key-schema \
        AttributeName=url,KeyType=HASH \
    --global-secondary-indexes \
        'IndexName=retry-time-index,KeySchema=[{AttributeName=next_retry_time,KeyType=HASH}],Projection={ProjectionType=ALL},ProvisionedThroughput={ReadCapacityUnits=5,WriteCapacityUnits=5}' \
        'IndexName=failure-reason-index,KeySchema=[{AttributeName=failure_reason,KeyType=HASH}],Projection={ProjectionType=ALL},ProvisionedThroughput={ReadCapacityUnits=5,WriteCapacityUnits=5}' \
    --provisioned-throughput ReadCapacityUnits=10,WriteCapacityUnits=10 \
    --region us-east-1

# Wait for table to be created

aws dynamodb wait table-exists --table-name neuronews-failed-urls

# Enable point-in-time recovery (recommended)

aws dynamodb update-continuous-backups \
    --table-name neuronews-failed-urls \
    --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true

```text

### 3.2 Configure Auto Scaling (Optional)

```bash

# Register scalable target for read capacity

aws application-autoscaling register-scalable-target \
    --service-namespace dynamodb \
    --resource-id "table/neuronews-failed-urls" \
    --scalable-dimension "dynamodb:table:ReadCapacityUnits" \
    --min-capacity 5 \
    --max-capacity 40

# Register scalable target for write capacity

aws application-autoscaling register-scalable-target \
    --service-namespace dynamodb \
    --resource-id "table/neuronews-failed-urls" \
    --scalable-dimension "dynamodb:table:WriteCapacityUnits" \
    --min-capacity 5 \
    --max-capacity 40

# Create scaling policy for read capacity

aws application-autoscaling put-scaling-policy \
    --service-namespace dynamodb \
    --resource-id "table/neuronews-failed-urls" \
    --scalable-dimension "dynamodb:table:ReadCapacityUnits" \
    --policy-name "ReadCapacityScalingPolicy" \
    --policy-type "TargetTrackingScaling" \
    --target-tracking-scaling-policy-configuration \
    'TargetValue=70.0,PredefinedMetricSpecification={PredefinedMetricType=DynamoDBReadCapacityUtilization}'

# Create scaling policy for write capacity

aws application-autoscaling put-scaling-policy \
    --service-namespace dynamodb \
    --resource-id "table/neuronews-failed-urls" \
    --scalable-dimension "dynamodb:table:WriteCapacityUnits" \
    --policy-name "WriteCapacityScalingPolicy" \
    --policy-type "TargetTrackingScaling" \
    --target-tracking-scaling-policy-configuration \
    'TargetValue=70.0,PredefinedMetricSpecification={PredefinedMetricType=DynamoDBWriteCapacityUtilization}'

```text

## Step 4: SNS Setup

### 4.1 Create SNS Topic and Subscriptions

```bash

# Create SNS topic

aws sns create-topic \
    --name neuronews-alerts \
    --region us-east-1

# Get topic ARN

TOPIC_ARN=$(aws sns get-topic-attributes \
    --topic-arn arn:aws:sns:us-east-1:$(aws sts get-caller-identity --query Account --output text):neuronews-alerts \
    --query 'Attributes.TopicArn' --output text)

# Subscribe email for critical alerts

aws sns subscribe \
    --topic-arn $TOPIC_ARN \
    --protocol email \
    --notification-endpoint "your-alert-email@example.com"

# Subscribe SMS for critical alerts (optional)

aws sns subscribe \
    --topic-arn $TOPIC_ARN \
    --protocol sms \
    --notification-endpoint "+1234567890"

# Create topic for different severity levels

aws sns create-topic --name neuronews-alerts-critical
aws sns create-topic --name neuronews-alerts-warning
aws sns create-topic --name neuronews-alerts-info

```text

### 4.2 Configure SNS Message Filtering (Optional)

```bash

# Set filter policy for critical alerts only

aws sns set-subscription-attributes \
    --subscription-arn "arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts:subscription-id" \
    --attribute-name FilterPolicy \
    --attribute-value '{"severity":["CRITICAL","ERROR"]}'

```text

## Step 5: Environment Configuration

### 5.1 Create Environment Variables File

Create `.env` file for local development:

```bash

# AWS Configuration

AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1

# CloudWatch Configuration

CLOUDWATCH_NAMESPACE=NeuroNews/Scraper
CLOUDWATCH_LOG_GROUP=/aws/lambda/neuronews-scraper

# DynamoDB Configuration

DYNAMODB_TABLE_NAME=neuronews-failed-urls
DYNAMODB_REGION=us-east-1

# SNS Configuration

SNS_TOPIC_ARN=arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts
SNS_REGION=us-east-1

# Monitoring Configuration

MONITORING_ENABLED=true
RETRY_MAX_ATTEMPTS=5
CIRCUIT_BREAKER_FAILURE_THRESHOLD=10
ALERT_RATE_LIMIT_WINDOW=300

```text

### 5.2 Update Configuration Files

Update `src/scraper/config_monitoring.json`:

```json

{
  "monitoring": {
    "enabled": true,
    "cloudwatch": {
      "region_name": "us-east-1",
      "namespace": "NeuroNews/Scraper",
      "log_group": "/aws/lambda/neuronews-scraper",
      "log_stream_prefix": "scraper-",
      "batch_size": 20,
      "flush_interval": 60
    },
    "dynamodb": {
      "table_name": "neuronews-failed-urls",
      "region_name": "us-east-1",
      "retry_strategy": "exponential_backoff",
      "max_retries": 5,
      "cleanup_interval_hours": 24,
      "permanent_failure_threshold": 10
    },
    "sns": {
      "topic_arn": "arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts",
      "region_name": "us-east-1",
      "rate_limit_window": 300,
      "max_alerts_per_window": 10,
      "severity_topics": {
        "CRITICAL": "arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts-critical",
        "ERROR": "arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts",
        "WARNING": "arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts-warning",
        "INFO": "arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts-info"
      }
    },
    "retry": {
      "max_retries": 5,
      "base_delay": 2.0,
      "max_delay": 300.0,
      "exponential_base": 2.0,
      "jitter": true,
      "circuit_breaker_failure_threshold": 10,
      "circuit_breaker_recovery_timeout": 60,
      "permanent_failure_codes": [401, 403, 404, 410],
      "retry_status_codes": [429, 500, 502, 503, 504, 520, 521, 522, 523, 524]
    }
  }
}

```text

## Step 6: CloudWatch Dashboard

### 6.1 Create Dashboard JSON

Create `cloudwatch-dashboard.json`:

```json

{
    "widgets": [
        {
            "type": "metric",
            "x": 0,
            "y": 0,
            "width": 12,
            "height": 6,
            "properties": {
                "metrics": [
                    [ "NeuroNews/Scraper", "SuccessRate", "Source", "AsyncScraper" ],
                    [ ".", "ScrapingAttempts", ".", "." ],
                    [ ".", "ArticlesScraped", ".", "." ]
                ],
                "view": "timeSeries",
                "stacked": false,
                "region": "us-east-1",
                "title": "Scraper Performance Overview",
                "period": 300,
                "stat": "Average"
            }
        },
        {
            "type": "metric",
            "x": 12,
            "y": 0,
            "width": 12,
            "height": 6,
            "properties": {
                "metrics": [
                    [ "NeuroNews/Scraper", "AverageDuration", "Source", "AsyncScraper" ],
                    [ ".", "RetryCount", ".", "." ]
                ],
                "view": "timeSeries",
                "stacked": false,
                "region": "us-east-1",
                "title": "Response Time and Retries",
                "period": 300,
                "stat": "Average"
            }
        },
        {
            "type": "metric",
            "x": 0,
            "y": 6,
            "width": 24,
            "height": 6,
            "properties": {
                "metrics": [
                    [ "NeuroNews/Scraper", "CaptchaEncounters", "Source", "AsyncScraper" ],
                    [ ".", "IPBlocks", ".", "." ]
                ],
                "view": "timeSeries",
                "stacked": false,
                "region": "us-east-1",
                "title": "Anti-Detection Metrics",
                "period": 300,
                "stat": "Sum"
            }
        }
    ]
}

```text

### 6.2 Create Dashboard

```bash

# Create CloudWatch dashboard

aws cloudwatch put-dashboard \
    --dashboard-name "NeuroNews-Scraper-Monitoring" \
    --dashboard-body file://cloudwatch-dashboard.json

```text

## Step 7: Testing and Validation

### 7.1 Test CloudWatch Integration

```python

# test_cloudwatch_deployment.py

import asyncio
import boto3
from src.scraper.cloudwatch_logger import CloudWatchLogger, ScrapingMetrics, ScrapingStatus

async def test_cloudwatch():
    logger = CloudWatchLogger(region_name='us-east-1')

    # Test metric logging

    metrics = ScrapingMetrics(
        url="https://test.example.com",
        status=ScrapingStatus.SUCCESS,
        duration=1.5,
        articles_count=5,
        retry_count=0
    )

    await logger.log_scraping_metrics(metrics)
    print("CloudWatch metrics logged successfully")

    # Test alarm creation

    await logger.create_alarm(
        alarm_name="test-alarm",
        metric_name="SuccessRate",
        threshold=50.0,
        comparison_operator="LessThanThreshold"
    )
    print("CloudWatch alarm created successfully")

if __name__ == "__main__":
    asyncio.run(test_cloudwatch())

```text

### 7.2 Test DynamoDB Integration

```python

# test_dynamodb_deployment.py

import asyncio
from src.scraper.dynamodb_failure_manager import DynamoDBFailureManager

async def test_dynamodb():
    manager = DynamoDBFailureManager(table_name='neuronews-failed-urls')

    # Test table creation and access

    await manager.ensure_table_exists()
    print("DynamoDB table verified")

    # Test failure recording

    await manager.record_failure(
        url="https://test.example.com",
        failure_reason="Connection timeout",
        error_details="Request timed out after 30 seconds"
    )
    print("Failure recorded successfully")

    # Test retrieval

    failed_urls = await manager.get_urls_ready_for_retry(limit=10)
    print(f"Retrieved {len(failed_urls)} failed URLs")

if __name__ == "__main__":
    asyncio.run(test_dynamodb())

```text

### 7.3 Test SNS Integration

```python

# test_sns_deployment.py

import asyncio
from src.scraper.sns_alert_manager import SNSAlertManager

async def test_sns():
    manager = SNSAlertManager(topic_arn='arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts')

    # Test alert sending

    await manager.alert_scraper_failure(
        url="https://test.example.com",
        error_message="Test alert",
        retry_count=1,
        max_retries=5
    )
    print("Test alert sent successfully")

if __name__ == "__main__":
    asyncio.run(test_sns())

```text

### 7.4 Run Validation Scripts

```bash

# Install dependencies

pip install boto3 aiohttp

# Run tests

python test_cloudwatch_deployment.py
python test_dynamodb_deployment.py
python test_sns_deployment.py

# Test full integration

python demo_monitoring.py

```text

## Step 8: Production Deployment

### 8.1 EC2 Deployment

```bash

# Create EC2 instance with IAM role

aws ec2 run-instances \
    --image-id ami-0abcdef1234567890 \
    --count 1 \
    --instance-type t3.medium \
    --key-name your-key-pair \
    --security-group-ids sg-0123456789abcdef0 \
    --subnet-id subnet-0123456789abcdef0 \
    --iam-instance-profile Name=NeuroNewsMonitoringRole \
    --user-data file://user-data.sh

# user-data.sh content:

#!/bin/bash
yum update -y
yum install -y python3 python3-pip git
pip3 install --upgrade pip
git clone https://github.com/your-repo/neuronews.git
cd neuronews
pip3 install -r requirements.txt
python3 -m src.scraper.async_scraper_runner

```text

### 8.2 Lambda Deployment

```bash

# Create deployment package

zip -r neuronews-monitoring.zip src/ requirements.txt

# Create Lambda function

aws lambda create-function \
    --function-name neuronews-scraper \
    --runtime python3.9 \
    --role arn:aws:iam::ACCOUNT:role/NeuroNewsMonitoringRole \
    --handler src.scraper.lambda_handler.handler \
    --zip-file fileb://neuronews-monitoring.zip \
    --timeout 900 \
    --memory-size 512 \
    --environment Variables='{
        "MONITORING_ENABLED":"true",
        "CLOUDWATCH_NAMESPACE":"NeuroNews/Scraper",
        "DYNAMODB_TABLE_NAME":"neuronews-failed-urls",
        "SNS_TOPIC_ARN":"arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts"
    }'

# Create EventBridge rule for scheduled execution

aws events put-rule \
    --name neuronews-scraper-schedule \
    --schedule-expression "rate(1 hour)" \
    --description "Run NeuroNews scraper every hour"

# Add Lambda permission for EventBridge

aws lambda add-permission \
    --function-name neuronews-scraper \
    --statement-id neuronews-scraper-schedule \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn arn:aws:events:us-east-1:ACCOUNT:rule/neuronews-scraper-schedule

# Create EventBridge target

aws events put-targets \
    --rule neuronews-scraper-schedule \
    --targets "Id"="1","Arn"="arn:aws:lambda:us-east-1:ACCOUNT:function:neuronews-scraper"

```text

## Step 9: Monitoring and Maintenance

### 9.1 Cost Monitoring

```bash

# Create billing alert

aws cloudwatch put-metric-alarm \
    --alarm-name "NeuroNews-Billing-Alert" \
    --alarm-description "Alert when estimated charges exceed $50" \
    --metric-name EstimatedCharges \
    --namespace AWS/Billing \
    --statistic Maximum \
    --period 86400 \
    --threshold 50.0 \
    --comparison-operator GreaterThanThreshold \
    --dimensions Name=Currency,Value=USD \
    --evaluation-periods 1 \
    --alarm-actions arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts

```text

### 9.2 Regular Maintenance Tasks

Create a maintenance script `maintenance.py`:

```python

import asyncio
from src.scraper.dynamodb_failure_manager import DynamoDBFailureManager
from src.scraper.cloudwatch_logger import CloudWatchLogger

async def daily_maintenance():
    """Run daily maintenance tasks."""

    # Clean up old failure records

    failure_manager = DynamoDBFailureManager(table_name='neuronews-failed-urls')
    deleted_count = await failure_manager.cleanup_old_failures(hours=168)  # 7 days

    print(f"Cleaned up {deleted_count} old failure records")

    # Generate daily statistics

    stats = await failure_manager.get_failure_statistics(hours=24)
    print(f"Daily stats: {stats}")

    # Log maintenance metrics

    cloudwatch = CloudWatchLogger(region_name='us-east-1')
    # Log maintenance completion metric


if __name__ == "__main__":
    asyncio.run(daily_maintenance())

```text

### 9.3 Health Check Endpoint

Create `health_check.py`:

```python

from fastapi import FastAPI
import asyncio
from src.scraper.cloudwatch_logger import CloudWatchLogger
from src.scraper.dynamodb_failure_manager import DynamoDBFailureManager
from src.scraper.sns_alert_manager import SNSAlertManager

app = FastAPI()

@app.get("/health")
async def health_check():
    """Health check endpoint for monitoring system."""
    try:
        # Test CloudWatch connection

        cloudwatch = CloudWatchLogger(region_name='us-east-1')

        # Test DynamoDB connection

        failure_manager = DynamoDBFailureManager(table_name='neuronews-failed-urls')
        await failure_manager.ensure_table_exists()

        # Test SNS connection

        sns = SNSAlertManager(topic_arn='arn:aws:sns:us-east-1:ACCOUNT:neuronews-alerts')

        return {"status": "healthy", "components": ["cloudwatch", "dynamodb", "sns"]}

    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}

@app.get("/metrics")
async def get_metrics():
    """Get current system metrics."""
    failure_manager = DynamoDBFailureManager(table_name='neuronews-failed-urls')
    stats = await failure_manager.get_failure_statistics(hours=1)
    return stats

```text

## Troubleshooting

### Common Issues and Solutions

1. **CloudWatch Permission Denied**

   - Verify IAM role has CloudWatch permissions

   - Check resource ARNs in policy match your resources

   - Ensure region consistency across all components

2. **DynamoDB Table Not Found**

   - Run table creation command manually

   - Check table name consistency in configuration

   - Verify region settings

3. **SNS Message Not Received**

   - Confirm subscription and email verification

   - Check SNS topic permissions

   - Verify message filtering policies

4. **High AWS Costs**

   - Review CloudWatch custom metrics usage

   - Optimize DynamoDB read/write capacity

   - Implement cost monitoring alerts

5. **Performance Issues**

   - Monitor DynamoDB throttling

   - Optimize batch sizes for CloudWatch metrics

   - Review retry configuration parameters

This deployment guide provides a complete setup for the NeuroNews monitoring system on AWS, ensuring reliable operation and comprehensive monitoring capabilities.
