#!/usr/bin/env python3
"""
Setup script for NeuroNews scraper with AWS credentials for dev environment.

This script:
1. Sets up AWS credentials for the scraper
2. Creates a configuration file for the dev environment
3. Provides commands to run the scraper with AWS integration
"""
import argparse
import configparser
import json
import os
import subprocess
from pathlib import Path

# Default values
DEFAULT_REGION = "us-east-1"
DEFAULT_S3_BUCKET = "neuronews-raw-articles-dev"
DEFAULT_S3_PREFIX = "news_articles"
DEFAULT_CLOUDWATCH_LOG_GROUP = "NeuroNews-Scraper"
DEFAULT_CLOUDWATCH_LOG_STREAM_PREFIX = "scraper"
DEFAULT_CLOUDWATCH_LOG_LEVEL = "INFO"


def setup_aws_credentials(profile_name, access_key, secret_key, region):
    """Set up AWS credentials in ~/.aws/credentials and ~/.aws/config."""
    # Create ~/.aws directory if it doesn't exist
    aws_dir = Path.home() / ".aws"'
    aws_dir.mkdir(exist_ok=True)

    # Set up credentials file
    credentials_path = aws_dir / "credentials"
    credentials = configparser.ConfigParser()

    if credentials_path.exists():
        credentials.read(credentials_path)

    if profile_name not in credentials:
        credentials[profile_name] = {}

    credentials[profile_name]["aws_access_key_id"] = access_key
    credentials[profile_name]["aws_secret_access_key"] = secret_key

    with open(credentials_path, "w") as f:
        credentials.write(f)

    # Set up config file
    config_path = aws_dir / "config"
    config = configparser.ConfigParser()

    if config_path.exists():
        config.read(config_path)

    profile_section = (
        f"profile {profile_name}" if profile_name != "default" else "default"
    )

    if profile_section not in config:
        config[profile_section] = {}

    config[profile_section]["region"] = region

    with open(config_path, "w") as f:
        config.write(f)

    print(f"AWS credentials set up for profile '{profile_name}'")
    return True


def create_dev_config(
    profile_name,
    s3_bucket,
    s3_prefix,
    cloudwatch_log_group,
    cloudwatch_log_stream_prefix,
    cloudwatch_log_level,
):
    """Create a configuration file for the dev environment."""
    # Create config directory if it doesn't exist
    config_dir = Path("config")'
    config_dir.mkdir(exist_ok=True)

    # Create dev config file
    dev_config = {
        "aws_profile": profile_name,
        "s3_storage": {"enabled": True, "bucket": s3_bucket, "prefix": s3_prefix},
        "cloudwatch_logging": {
            "enabled": True,
            "log_group": cloudwatch_log_group,
            "log_stream_prefix": cloudwatch_log_stream_prefix,
            "log_level": cloudwatch_log_level,
        },
    }

    dev_config_path = config_dir / "dev_aws.json"
    with open(dev_config_path, "w") as f:
        json.dump(dev_config, f, indent=2)

    print(f"Dev environment configuration created at {dev_config_path}")
    return dev_config_path


def check_aws_resources(profile_name, s3_bucket, cloudwatch_log_group):
    """Check if the AWS resources exist and are accessible."""
    # Set AWS_PROFILE environment variable
    os.environ["AWS_PROFILE"] = profile_name

    # Check S3 bucket
    try:
        result = subprocess.run(
            ["aws", "s3api", "head-bucket", "--bucket", s3_bucket],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            print(f"S3 bucket '{s3_bucket}' exists and is accessible")
        else:
            print(
                f"S3 bucket '{s3_bucket}' does not exist or is not accessible")
            print(f"Error: {result.stderr}")

            # Try to create the bucket
            create_bucket = input(
                f"Do you want to create the S3 bucket '{s3_bucket}'? (y/n): "
            )
            if create_bucket.lower() == "y":
                region_param = [
                    "--region",
                    os.environ.get("AWS_DEFAULT_REGION", DEFAULT_REGION),
                ]
                result = subprocess.run(
                    ["aws", "s3api", "create-bucket", "--bucket", s3_bucket]
                    + region_param,
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    print(f"S3 bucket '{s3_bucket}' created successfully")
                else:
                    print(f"Failed to create S3 bucket '{s3_bucket}'")
                    print(f"Error: {result.stderr}")
                    return False
    except Exception as e:
        print(f"Error checking S3 bucket: {e}")
        return False

    # Check CloudWatch log group
    try:
        result = subprocess.run(
            [
                "aws",
                "logs",
                "describe-log-groups",
                "--log-group-name-prefix",
                cloudwatch_log_group,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            log_groups = json.loads(result.stdout).get("logGroups", [])
            log_group_exists = any(
                lg["logGroupName"] == cloudwatch_log_group for lg in log_groups
            )

            if log_group_exists:
                print(
                    f"CloudWatch log group '{cloudwatch_log_group}' exists and is accessible"
                )
            else:
                print(
                    f"CloudWatch log group '{cloudwatch_log_group}' does not exist")

                # Try to create the log group
                create_log_group = input(
                    f"Do you want to create the CloudWatch log group '{cloudwatch_log_group}'? (y/n): ")
                if create_log_group.lower() == "y":
                    result = subprocess.run(
                        [
                            "aws",
                            "logs",
                            "create-log-group",
                            "--log-group-name",
                            cloudwatch_log_group,
                        ],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode == 0:
                        print(
                            f"CloudWatch log group '{cloudwatch_log_group}' created successfully"
                        )
                    else:
                        print(
                            f"Failed to create CloudWatch log group '{cloudwatch_log_group}'"
                        )
                        print(f"Error: {result.stderr}")
                        return False
        else:
            print(f"Error checking CloudWatch log group: {result.stderr}")
            return False
    except Exception as e:
        print(f"Error checking CloudWatch log group: {e}")
        return False

    return True


def generate_run_commands(
    profile_name,
    s3_bucket,
    s3_prefix,
    cloudwatch_log_group,
    cloudwatch_log_stream_prefix,
    cloudwatch_log_level,
):
    """Generate commands to run the scraper with AWS integration."""
    # Command to run the scraper directly
    direct_cmd = (
        f"AWS_PROFILE={profile_name} python -m src.scraper.run "
        f"--s3 --s3-bucket {s3_bucket} --s3-prefix {s3_prefix} "
        f"--cloudwatch --cloudwatch-log-group {cloudwatch_log_group} "
        f"--cloudwatch-log-stream-prefix {cloudwatch_log_stream_prefix} "
        f"--cloudwatch-log-level {cloudwatch_log_level}"
    )

    # Command to run the scraper from the main application
    main_cmd = (
        f"AWS_PROFILE={profile_name} python legacy/src/main.py --scrape "
        f"--s3 --s3-bucket {s3_bucket} --s3-prefix {s3_prefix} "
        f"--cloudwatch --cloudwatch-log-group {cloudwatch_log_group} "
        f"--cloudwatch-log-stream-prefix {cloudwatch_log_stream_prefix} "
        f"--cloudwatch-log-level {cloudwatch_log_level}"
    )

    # Python code to run the scraper as a library
    library_code = """
# Python code to run the scraper as a library
import os
os.environ["AWS_PROFILE"] = "{profile_name}"

from src.scraper import scrape_news

articles = scrape_news(
    s3_storage=True,
    s3_bucket="{s3_bucket}",
    s3_prefix="{s3_prefix}",
    cloudwatch_logging=True,
    cloudwatch_log_group="{cloudwatch_log_group}",
    cloudwatch_log_stream_prefix="{cloudwatch_log_stream_prefix}",
    cloudwatch_log_level="{cloudwatch_log_level}"
)
"""

    return {"direct": direct_cmd, "main": main_cmd, "library": library_code}


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Setup AWS credentials and dev environment for NeuroNews scraper"
    )

    # AWS credentials options
    parser.add_argument("--profile", default="neuronews-dev",
                        help="AWS profile name")
    parser.add_argument("--access-key", required=True,
                        help="AWS access key ID")
    parser.add_argument("--secret-key", required=True,
                        help="AWS secret access key")
    parser.add_argument("--region", default=DEFAULT_REGION, help="AWS region")

    # S3 options
    parser.add_argument(
        "--s3-bucket", default=DEFAULT_S3_BUCKET, help="S3 bucket name")
    parser.add_argument(
        "--s3-prefix", default=DEFAULT_S3_PREFIX, help="S3 key prefix")

    # CloudWatch options
    parser.add_argument(
        "--cloudwatch-log-group",
        default=DEFAULT_CLOUDWATCH_LOG_GROUP,
        help="CloudWatch log group name",
    )
    parser.add_argument(
        "--cloudwatch-log-stream-prefix",
        default=DEFAULT_CLOUDWATCH_LOG_STREAM_PREFIX,
        help="CloudWatch log stream prefix",
    )
    parser.add_argument(
        "--cloudwatch-log-level",
        default=DEFAULT_CLOUDWATCH_LOG_LEVEL,
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="CloudWatch log level",
    )

    args = parser.parse_args()

    # Set up AWS credentials
    if not setup_aws_credentials(
        args.profile, args.access_key, args.secret_key, args.region
    ):
        print("Failed to set up AWS credentials")
        return

    # Create dev environment configuration
    dev_config_path = create_dev_config(
        args.profile,
        args.s3_bucket,
        args.s3_prefix,
        args.cloudwatch_log_group,
        args.cloudwatch_log_stream_prefix,
        args.cloudwatch_log_level,
    )

    # Check AWS resources
    if not check_aws_resources(args.profile, args.s3_bucket, args.cloudwatch_log_group):
        print("Failed to check AWS resources")
        return

    # Generate run commands
    commands = generate_run_commands(
        args.profile,
        args.s3_bucket,
        args.s3_prefix,
        args.cloudwatch_log_group,
        args.cloudwatch_log_stream_prefix,
        args.cloudwatch_log_level,
    )

    # Print run commands
    print(
        ""
You can now run the scraper with AWS integration using the following commands:""
    )
    print("
1. Run the scraper directly:")
    print(f"   {commands['direct']}")
    print("
2. Run the scraper from the main application:")
    print(f"   {commands['main']}")
    print(""
3. Run the scraper as a library:")
    print(commands["library"])"

    print(""
AWS setup completed successfully!")"


if __name__ == "__main__":
    main()
