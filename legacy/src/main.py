"""
Main entry point for NeuroNews application.
"""

import argparse
import os

from src.scraper.run import load_aws_config, run_spider


def main():
    """Main function."""
    print("NeuroNews application starting...")

    parser = argparse.ArgumentParser(description="NeuroNews Application")
    parser.add_argument(
        "--scrape", "-s", action="store_true", help="Run the news scraper"
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Output file path for scraped data (default: data/news_articles.json)",
        default="data/news_articles.json",
    )
    parser.add_argument(
        "--playwright",
        "-p",
        action="store_true",
        help="Use Playwright for JavaScript-heavy pages",
    )
    parser.add_argument(
        "--env",
        help="Environment (dev, staging, prod) for loading AWS config",
        default="dev",
    )

    # AWS region for local emulators
    parser.add_argument(
        "--aws-region", help="AWS region for local emulators (default: us-east-1)", default="us-east-1"
    )
    parser.add_argument("--aws-profile", help="AWS profile name")

    # CloudWatch logging options
    cloudwatch_group = parser.add_argument_group("CloudWatch Logging Options")
    cloudwatch_group.add_argument(
        "--cloudwatch", "-c", action="store_true", help="Log to AWS CloudWatch"
    )
    cloudwatch_group.add_argument(
        "--cloudwatch-log-group",
        help="CloudWatch log group name (default: NeuroNews-Scraper)",
        default="NeuroNews-Scraper",
    )
    cloudwatch_group.add_argument(
        "--cloudwatch-log-stream-prefix",
        help="CloudWatch log stream prefix (default: scraper)",
        default="scraper",
    )
    cloudwatch_group.add_argument(
        "--cloudwatch-log-level",
        help="CloudWatch log level (default: INFO)",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        default="INFO",
    )

    args = parser.parse_args()

    if args.scrape:
        print("Running news scraper...")

        # Load AWS configuration from file
        aws_config = load_aws_config(args.env)

        # Set AWS profile if provided in config or as argument
        aws_profile = args.aws_profile
        if aws_profile is None and "aws_profile" in aws_config:
            aws_profile = aws_config["aws_profile"]

        if aws_profile:
            os.environ["AWS_PROFILE"] = aws_profile
            print("Using AWS profile: {0}".format(aws_profile))

        # Override S3 settings from config if not provided as arguments.
        # The --s3/--s3-bucket/--s3-prefix CLI flags were removed in the offline
        # migration; S3 is now opt-in via the AWS config file only. Use getattr
        # so any leftover attribute access degrades to the former defaults.
        s3_storage = getattr(args, "s3", False)
        if not s3_storage and aws_config.get("s3_storage", {}).get("enabled", False):
            s3_storage = True

        s3_bucket = getattr(args, "s3_bucket", None)
        if s3_storage and s3_bucket is None and "s3_storage" in aws_config:
            s3_bucket = aws_config["s3_storage"].get("bucket")

        s3_prefix = getattr(args, "s3_prefix", "news_articles")
        if s3_storage and s3_prefix == "news_articles" and "s3_storage" in aws_config:
            s3_prefix = aws_config["s3_storage"].get("prefix", s3_prefix)

        # Override CloudWatch settings from config if not provided as arguments
        cloudwatch_logging = args.cloudwatch
        if not cloudwatch_logging and aws_config.get("cloudwatch_logging", {}).get(
            "enabled", False
        ):
            cloudwatch_logging = True

        cloudwatch_log_group = args.cloudwatch_log_group
        if (
            cloudwatch_logging
            and cloudwatch_log_group == "NeuroNews-Scraper"
            and "cloudwatch_logging" in aws_config
        ):
            cloudwatch_log_group = aws_config["cloudwatch_logging"].get(
                "log_group", cloudwatch_log_group
            )

        cloudwatch_log_stream_prefix = args.cloudwatch_log_stream_prefix
        if (
            cloudwatch_logging
            and cloudwatch_log_stream_prefix == "scraper"
            and "cloudwatch_logging" in aws_config
        ):
            cloudwatch_log_stream_prefix = aws_config["cloudwatch_logging"].get(
                "log_stream_prefix", cloudwatch_log_stream_prefix
            )

        cloudwatch_log_level = args.cloudwatch_log_level
        if (
            cloudwatch_logging
            and cloudwatch_log_level == "INFO"
            and "cloudwatch_logging" in aws_config
        ):
            cloudwatch_log_level = aws_config["cloudwatch_logging"].get(
                "log_level", cloudwatch_log_level
            )


        if args.playwright:
            print("Using Playwright for JavaScript-heavy pages")
        if s3_storage:
            print("Storing articles in S3 bucket: {0}".format(s3_bucket))
            print("S3 prefix: {0}".format(s3_prefix))
        if cloudwatch_logging:
            print(
                "Logging to CloudWatch: {0}/{1}-*".format(
                    cloudwatch_log_group, cloudwatch_log_stream_prefix
                )
            )
            print("Log level: {0}".format(cloudwatch_log_level))

        run_spider(
            output_file=args.output,
            use_playwright=args.playwright,
            s3_storage=s3_storage,
            aws_access_key_id=getattr(args, "aws_key_id", None),
            aws_secret_access_key=getattr(args, "aws_secret_key", None),
            s3_bucket=s3_bucket,
            s3_prefix=s3_prefix,
            cloudwatch_logging=cloudwatch_logging,
            aws_region=args.aws_region,
            cloudwatch_log_group=cloudwatch_log_group,
            cloudwatch_log_stream_prefix=cloudwatch_log_stream_prefix,
            cloudwatch_log_level=cloudwatch_log_level,
            aws_profile=aws_profile,
            env=args.env,
        )
        print("Scraping completed. Data saved to {0}".format(args.output))
        if s3_storage:
            print("Articles also stored in S3 bucket: {0}".format(s3_bucket))
        if cloudwatch_logging:
            print("Logs available in CloudWatch: {0}".format(cloudwatch_log_group))
    else:
        print("No action specified. Use --scrape to run the news scraper.")


if __name__ == "__main__":
    main()
