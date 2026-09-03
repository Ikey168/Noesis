# IAM role for Lambda execution
resource "aws_iam_role" "lambda_execution_role" {
  name = "neuronews-lambda-execution-${var.environment}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17",
    Statement = [{
      Action = "sts:AssumeRole",
      Effect = "Allow",
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "lambda_basic" {
  name = "basic-lambda-policy"
  role = aws_iam_role.lambda_execution_role.id

  policy = jsonencode({
    Version = "2012-10-17",
    Statement = [
      {
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ],
        Effect = "Allow",
        Resource = "arn:aws:logs:*:*:*"
      },
      {
        Action = ["s3:*"],
        Effect = "Allow",
        Resource = [
          module.s3.lambda_code_bucket_arn,
          "${module.s3.lambda_code_bucket_arn}/*",
          module.s3.raw_articles_bucket_arn,
          "${module.s3.raw_articles_bucket_arn}/*"
        ]
      },
      {
        Action = [
          "cloudwatch:PutMetricData",
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:ListMetrics"
        ],
        Effect = "Allow",
        Resource = "*"
      },
      {
        Action = [
          "events:PutEvents"
        ],
        Effect = "Allow",
        Resource = "*"
      }
    ]
  })
}
# Lambda Function Configuration for NeuroNews

# Create Lambda function for article processing
resource "aws_lambda_function" "article_processor" {
  function_name = "${var.lambda_function_prefix}-article-processor-${var.environment}"
  description   = "Processes news articles and stores them in S3"
  handler       = "article_processor.lambda_handler"
  runtime       = var.lambda_runtime
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory_size
  s3_bucket     = module.s3.lambda_code_bucket_name
  s3_key        = "lambda-functions/article_processor.zip"
  role          = aws_iam_role.lambda_execution_role.arn

  environment {
    variables = {
      S3_BUCKET = module.s3.raw_articles_bucket_name
    }
  }

  tags = merge(
    var.tags,
    {
      Name        = "Article Processor Lambda Function"
      Environment = var.environment
    }
  )
}

resource "aws_lambda_function" "article_notifier" {
  function_name = "${var.lambda_function_prefix}-article-notifier-${var.environment}"
  description   = "Sends notifications when new articles are available"
  handler       = "article_notifier.lambda_handler"
  runtime       = var.lambda_runtime
  timeout       = var.lambda_timeout
  memory_size   = var.lambda_memory_size
  s3_bucket     = module.s3.lambda_code_bucket_name
  s3_key        = "lambda-functions/article_notifier.zip"
  role          = aws_iam_role.lambda_execution_role.arn

  tags = merge(
    var.tags,
    {
      Name        = "Article Notifier Lambda Function"
      Environment = var.environment
    }
  )
}

# News Scraper Lambda Function for Automated Scraping
resource "aws_lambda_function" "news_scraper" {
  function_name = "${var.lambda_function_prefix}-news-scraper-${var.environment}"
  description   = "Automated news scraping with multi-source support"
  handler       = "news_scraper.lambda_handler"
  runtime       = var.lambda_runtime
  timeout       = var.lambda_scraper_timeout
  memory_size   = var.lambda_scraper_memory_size
  s3_bucket     = module.s3.lambda_code_bucket_name
  s3_key        = "lambda-functions/news_scraper.zip"
  role          = aws_iam_role.lambda_execution_role.arn

  environment {
    variables = {
      S3_BUCKET = module.s3.raw_articles_bucket_name
      S3_PREFIX = "lambda-scraped-articles"
      CLOUDWATCH_LOG_GROUP = "/aws/lambda/${var.lambda_function_prefix}-news-scraper-${var.environment}"
      CLOUDWATCH_NAMESPACE = "NeuroNews/Lambda/Scraper"
      AWS_REGION = var.aws_region
      ENVIRONMENT = var.environment
      S3_STORAGE_ENABLED = "true"
      CLOUDWATCH_LOGGING_ENABLED = "true"
      MONITORING_ENABLED = "true"
    }
  }

  tags = merge(
    var.tags,
    {
      Name        = "News Scraper Lambda Function"
      Environment = var.environment
      Service     = "scraper"
    }
  )
}

resource "aws_lambda_permission" "allow_s3_invoke_article_processor" {
  statement_id  = "AllowExecutionFromS3"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.article_processor.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = module.s3.raw_articles_bucket_arn
}

# EventBridge (CloudWatch Events) rule for scheduled scraping
resource "aws_cloudwatch_event_rule" "news_scraper_schedule" {
  name                = "${var.lambda_function_prefix}-scraper-schedule-${var.environment}"
  description         = "Trigger news scraper Lambda function on schedule"
  schedule_expression = var.scraper_schedule_expression

  tags = merge(
    var.tags,
    {
      Name        = "News Scraper Schedule"
      Environment = var.environment
      Service     = "scraper"
    }
  )
}

# EventBridge target for news scraper Lambda
resource "aws_cloudwatch_event_target" "news_scraper_target" {
  rule      = aws_cloudwatch_event_rule.news_scraper_schedule.name
  target_id = "NewsScraperLambdaTarget"
  arn       = aws_lambda_function.news_scraper.arn

  input = jsonencode({
    sources = var.scraper_sources
    max_articles_per_source = var.scraper_max_articles_per_source
    scraper_config = {
      concurrent_requests = var.scraper_concurrent_requests
      timeout = var.scraper_timeout
    }
  })
}

# Permission for EventBridge to invoke the news scraper Lambda
resource "aws_lambda_permission" "allow_eventbridge_invoke_news_scraper" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.news_scraper.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.news_scraper_schedule.arn
}

# CloudWatch Log Groups
resource "aws_cloudwatch_log_group" "article_processor_logs" {
  name              = "/aws/lambda/${aws_lambda_function.article_processor.function_name}"
  retention_in_days = var.lambda_log_retention_days
}

resource "aws_cloudwatch_log_group" "knowledge_graph_generator_logs" {
  name              = "/aws/lambda/${aws_lambda_function.knowledge_graph_generator.function_name}"
  retention_in_days = var.lambda_log_retention_days
}

resource "aws_cloudwatch_log_group" "article_notifier_logs" {
  name              = "/aws/lambda/${aws_lambda_function.article_notifier.function_name}"
  retention_in_days = var.lambda_log_retention_days
}

resource "aws_cloudwatch_log_group" "news_scraper_logs" {
  name              = "/aws/lambda/${aws_lambda_function.news_scraper.function_name}"
  retention_in_days = var.lambda_log_retention_days

  tags = merge(
    var.tags,
    {
      Name        = "News Scraper Lambda Logs"
      Environment = var.environment
      Service     = "scraper"
    }
  )
}
