"""
Web scraping module for NeuroNews.

This module provides a simple interface to the Scrapy-based scraper.
It maintains backward compatibility with the original API.
"""

import json
import os
from pathlib import Path

from src.scraper.run import run_spider


def load_aws_config(env="dev"):
    """
    Load AWS configuration from config/dev_aws.json.

    Args:
        env (str): Environment name (dev, staging, prod).

    Returns:
        dict: AWS configuration.
    """
    config_path = Path("config/{0}_aws.json".format(env))
    if not config_path.exists():
        return {}

    with open(config_path, "r") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            print("Error: Could not parse {0} as JSON.".format(config_path))
            return {}


def scrape_news(
    output_file="data/news_articles.json",
    use_playwright=False,
    s3_storage=False,
    aws_access_key_id=None,
    aws_secret_access_key=None,
    s3_bucket=None,
    s3_prefix="news_articles",
    cloudwatch_logging=False,
    aws_region="us-east-1",
    cloudwatch_log_group="NeuroNews-Scraper",
    cloudwatch_log_stream_prefix="scraper",
    cloudwatch_log_level="INFO",
    aws_profile=None,
    env="dev",
):
    """
    Scrape news articles from configured sources.

    Args:
        output_file (str): Path to save the scraped articles.
        use_playwright (bool): Whether to use Playwright for JavaScript-heavy pages.
        s3_storage (bool): Whether to store articles in S3.
        aws_access_key_id (str, optional): AWS access key ID.
        aws_secret_access_key (str, optional): AWS secret access key.
        s3_bucket (str, optional): S3 bucket name.
        s3_prefix (str, optional): S3 key prefix.
        cloudwatch_logging (bool): Whether to log to CloudWatch.
        aws_region (str, optional): AWS region.
        cloudwatch_log_group (str, optional): CloudWatch log group name.
        cloudwatch_log_stream_prefix (str, optional): CloudWatch log stream prefix.
        cloudwatch_log_level (str, optional): CloudWatch log level.
        aws_profile (str, optional): AWS profile name.
        env (str): Environment name (dev, staging, prod).

    Returns:
        list: List of scraped news articles.
    """
    # Load AWS configuration from file if available
    aws_config = load_aws_config(env)

    # Set AWS profile if provided in config or as argument
    if aws_profile is None and "aws_profile" in aws_config:
        aws_profile = aws_config["aws_profile"]

    if aws_profile:
        os.environ["AWS_PROFILE"] = aws_profile
        print("Using AWS profile: {0}".format(aws_profile))

    # Override S3 settings from config if not provided as arguments
    if not s3_storage and aws_config.get("s3_storage", {}).get("enabled", False):
        s3_storage = True

    if s3_storage and s3_bucket is None and "s3_storage" in aws_config:
        s3_bucket = aws_config["s3_storage"].get("bucket")

    if s3_storage and s3_prefix == "news_articles" and "s3_storage" in aws_config:
        s3_prefix = aws_config["s3_storage"].get("prefix", s3_prefix)

    # Override CloudWatch settings from config if not provided as arguments
    if not cloudwatch_logging and aws_config.get("cloudwatch_logging", {}).get(
        "enabled", False
    ):
        cloudwatch_logging = True

    if (
        cloudwatch_logging
        and cloudwatch_log_group == "NeuroNews-Scraper"
        and "cloudwatch_logging" in aws_config
    ):
        cloudwatch_log_group = aws_config["cloudwatch_logging"].get(
            "log_group", cloudwatch_log_group
        )

    if (
        cloudwatch_logging
        and cloudwatch_log_stream_prefix == "scraper"
        and "cloudwatch_logging" in aws_config
    ):
        cloudwatch_log_stream_prefix = aws_config["cloudwatch_logging"].get(
            "log_stream_prefix", cloudwatch_log_stream_prefix
        )

    if (
        cloudwatch_logging
        and cloudwatch_log_level == "INFO"
        and "cloudwatch_logging" in aws_config
    ):
        cloudwatch_log_level = aws_config["cloudwatch_logging"].get(
            "log_level", cloudwatch_log_level
        )

    print("Scraping news using Scrapy-based scraper...")
    if use_playwright:
        print("Using Playwright for JavaScript-heavy pages")
    if s3_storage:
        print(f"Storing articles in local S3 bucket: {s3_bucket}")
    if cloudwatch_logging:
        print(
            "Logging to CloudWatch: {0}/{1}-*".format(
                cloudwatch_log_group, cloudwatch_log_stream_prefix
            )
        )

    # Run the spider and save results to the output file
    run_spider(
        output_file=output_file,
        use_playwright=use_playwright,
        s3_storage=s3_storage,
        aws_access_key_id=aws_access_key_id,
        aws_secret_access_key=aws_secret_access_key,
        s3_bucket=s3_bucket,
        s3_prefix=s3_prefix,
        cloudwatch_logging=cloudwatch_logging,
        aws_region=aws_region,
        cloudwatch_log_group=cloudwatch_log_group,
        cloudwatch_log_stream_prefix=cloudwatch_log_stream_prefix,
        cloudwatch_log_level=cloudwatch_log_level,
    )

    # Read the results from the output file
    if os.path.exists(output_file):
        with open(output_file, "r") as f:
            try:
                articles = json.load(f)
                return articles
            except json.JSONDecodeError:
                print("Error: Could not parse {0} as JSON.".format(output_file))
                return []
    else:
        print("Error: Output file {0} not found.".format(output_file))
        return []
