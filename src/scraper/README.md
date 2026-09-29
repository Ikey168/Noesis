# Multi-Source News Scraper

This enhanced scraper collects news articles from 10+ major news sources with

custom parsing, data validation, and comprehensive metadata extraction.

## Supported News Sources

- **CNN** (`cnn`) - Politics, Business, Technology, Health, Sports

- **BBC** (`bbc`) - World news, Politics, Business, Technology, Science

- **TechCrunch** (`techcrunch`) - Startups, Apps, Gadgets, Venture Capital, AI

- **Ars Technica** (`arstechnica`) - Technology, Science, Gaming, Policy

- **Reuters** (`reuters`) - Business, World news, Markets, Politics

- **The Guardian** (`guardian`) - Politics, Environment, Science, Culture

- **The Verge** (`theverge`) - Technology, Science, Gaming, Policy

- **Wired** (`wired`) - Technology, Science, Business, Culture

- **NPR** (`npr`) - National, World, Politics, Science, Health

- **Generic News Spider** (`news`) - Fallback for other sources

## Features

### 🔍 Custom Parsers

Each news source has a specialized spider with custom HTML parsing logic
tailored to their specific website structure:

- Source-specific CSS selectors

- Date format parsing

- Author extraction

- Category classification

- Content quality assessment

### 📊 Rich Metadata Extraction

Each article includes comprehensive metadata:

- **Basic**: Title, URL, content, publication date, source, author, category

- **Enhanced**: Scraped timestamp, content length, word count, reading time,

  language

- **Quality**: Validation score, content quality rating, duplicate check status

### ✅ Data Validation

Comprehensive validation pipeline ensures data quality:

- Required field completeness

- URL format validation

- Date format verification

- Content length assessment

- Duplicate detection (URL and content-based)

- Overall accuracy scoring

### 📁 Organized Storage

Articles are stored in multiple formats:

- **Combined**: All articles in `data/all_articles.json`

- **Source-specific**: Individual files in `data/sources/[source]_articles.json`

- **S3 Integration**: Optional cloud storage with AWS S3

## Usage

### Command Line Interface

#### Run All Sources

```bash

python -m src.scraper.run --multi-source

```text

#### Run Specific Source

```bash

python -m src.scraper.run --spider cnn
python -m src.scraper.run --spider bbc
python -m src.scraper.run --spider techcrunch

```text

#### Run Multiple Sources

```bash

python -m src.scraper.run --multi-source --include cnn bbc reuters

```text

#### Exclude Sources

```bash

python -m src.scraper.run --multi-source --exclude techcrunch arstechnica

```text

#### With Data Validation

```bash

python -m src.scraper.run --multi-source --validate

```text

#### Generate Reports

```bash

python -m src.scraper.run --report

```text

#### List Available Sources

```bash

python -m src.scraper.run --list-sources

```text

### Direct Multi-Source Runner

```bash

# Run all spiders

python -m src.scraper.multi_source_runner --all

# Run specific spider

python -m src.scraper.multi_source_runner --spider cnn

# Generate report

python -m src.scraper.multi_source_runner --report

# List available spiders

python -m src.scraper.multi_source_runner --list

```text

### Data Validation

```bash

# Run validation on scraped data

python -m src.scraper.data_validator

```text

## Configuration

### Sources Configuration

Edit `config/settings.json` to modify the list of sources:

```json

{
  "scraping": {
    "sources": [
      "https://www.cnn.com",
      "https://www.bbc.com/news",
      "https://techcrunch.com",
      "..."
    ]
  }
}

```text

### Pipeline Configuration

The enhanced pipeline system includes:

```python

ITEM_PIPELINES = {
    'src.scraper.pipelines.ValidationPipeline': 100,
    'src.scraper.pipelines.DuplicateFilterPipeline': 200,
    'src.scraper.pipelines.EnhancedJsonWriterPipeline': 300,
    'src.scraper.pipelines.s3_pipeline.S3StoragePipeline': 400,
}

```text

## Data Structure

### Article Schema

```json

{
  "title": "Article title",
  "url": "https://example.com/article",
  "content": "Full article content...",
  "published_date": "2024-01-01T12:00:00Z",
  "source": "CNN",
  "author": "John Doe",
  "category": "Technology",
  "scraped_date": "2024-01-01T12:30:00Z",
  "content_length": 1500,
  "word_count": 250,
  "reading_time": 2,
  "language": "en",
  "validation_score": 85,
  "content_quality": "high",
  "duplicate_check": "unique"
}

```text

### Output Files

#### Combined Data

- `data/all_articles.json` - All articles from all sources

#### Source-Specific Data

- `data/sources/cnn_articles.json`

- `data/sources/bbc_articles.json`

- `data/sources/techcrunch_articles.json`

- `data/sources/...`

#### Reports

- `data/scraping_report.json` - Summary statistics

- `data/validation_report.json` - Data quality analysis

## Validation Metrics

The validation system provides detailed quality metrics:

### Accuracy Score Calculation

- **Field Completeness** (30 points): Required fields filled

- **URL Validity** (20 points): Valid URL format and domain consistency

- **Content Quality** (25 points): Adequate content length and word count

- **Date Validity** (15 points): Valid date format and recency

- **Uniqueness** (10 points): No duplicates detected

### Quality Ratings

- **High**: Validation score ≥ 80

- **Medium**: Validation score 60-79

- **Low**: Validation score < 60

### Report Example

```json

{
  "summary": {
    "total_sources_analyzed": 9,
    "total_articles_across_sources": 450,
    "average_accuracy_score": 82.5,
    "best_performing_source": "reuters",
    "worst_performing_source": "techcrunch"
  },
  "cnn": {
    "total_articles": 50,
    "accuracy_score": 85.2,
    "field_completeness": {...},
    "content_quality": {...}
  }
}

```text

## Development

### Adding New Sources

1. Create a new spider file in `src/scraper/spiders/`:

```python

class NewSourceSpider(scrapy.Spider):
    name = 'newsource'
    allowed_domains = ['newsource.com']
    # ... implementation

```text

2. Add to multi-source runner in `multi_source_runner.py`:

```python

from .spiders.newsource_spider import NewSourceSpider

self.spiders = {
    # existing spiders...

    'newsource': NewSourceSpider,
}

```text

3. Update configuration in `config/settings.json`

### Custom Validation Rules

Extend the `ValidationPipeline` in `pipelines.py` to add source-specific validation rules:

```python

def process_item(self, item, spider):
    # Custom validation logic

    if spider.name == 'customsource':
        # Source-specific validation

        pass
    return item

```text

## Testing

Run the validation tests to ensure data quality:

```bash

# Test all sources

python -m src.scraper.data_validator

# Test specific functionality

python -m pytest tests/test_scraper.py

```text

## Monitoring

### CloudWatch Integration

Enable CloudWatch logging for production monitoring:

```bash

python -m src.scraper.run --multi-source --cloudwatch

```text

### S3 Storage

Store articles in S3 for scalable data management:

```bash

python -m src.scraper.run --multi-source --s3 --s3-bucket my-bucket

```text

## Troubleshooting

### Common Issues

1. **Robot.txt Blocking**: Some sites may block scrapers

   - Solution: Respect robots.txt or use delays

2. **JavaScript-Heavy Sites**: Some content requires JavaScript

   - Solution: Use `--playwright` flag for JS rendering

3. **Rate Limiting**: Sites may limit request frequency

   - Solution: Adjust `DOWNLOAD_DELAY` in settings

4. **Changing HTML Structure**: Sites update their layouts

   - Solution: Update spider selectors regularly

### Debug Mode

Enable debug logging:

```bash

python -m src.scraper.run --multi-source --env dev

```text

This will provide detailed logging for troubleshooting parsing issues.

- `--aws-key-id`: AWS access key ID

- `--aws-secret-key`: AWS secret access key

- `--aws-region`: AWS region (default: us-east-1)

S3 Storage Options:

- `--s3`, `-s`: Store articles in Amazon S3

- `--s3-bucket`: S3 bucket name

- `--s3-prefix`: S3 key prefix (default: news_articles)

CloudWatch Logging Options:

- `--cloudwatch`, `-c`: Log to AWS CloudWatch

- `--cloudwatch-log-group`: CloudWatch log group name (default: NeuroNews-Scraper)

- `--cloudwatch-log-stream-prefix`: CloudWatch log stream prefix (default: scraper)

- `--cloudwatch-log-level`: CloudWatch log level (default: INFO)

Example with all AWS features:

```bash

python -m src.scraper.run \
  --aws-profile neuronews-dev \
  --s3 --s3-bucket neuronews-raw-articles-dev \
  --cloudwatch --cloudwatch-log-level DEBUG

```text

### From the Main Application

You can also run the scraper from the main application:

```bash

python -m src.scraper.run

```text

The main application supports all the same options as the command-line interface.

### As a Library

```python

from src.scraper import scrape_news

# Run the scraper and get the results

articles = scrape_news()

# Or specify a custom output file

articles = scrape_news('path/to/output.json')

# Use Playwright for JavaScript-heavy pages

articles = scrape_news('path/to/output.json', use_playwright=True)

# Use AWS profile

articles = scrape_news(
    aws_profile='neuronews-dev',
    env='dev'  # Load config from config/dev_aws.json

)

# Store articles in S3

articles = scrape_news(
    output_file='path/to/output.json',
    s3_storage=True,
    aws_access_key_id='YOUR_KEY_ID',
    aws_secret_access_key='YOUR_SECRET_KEY',
    s3_bucket='your-bucket-name',
    s3_prefix='news_articles'
)

# Log to CloudWatch

articles = scrape_news(
    cloudwatch_logging=True,
    aws_access_key_id='YOUR_KEY_ID',
    aws_secret_access_key='YOUR_SECRET_KEY',
    cloudwatch_log_group='NeuroNews-Scraper',
    cloudwatch_log_level='DEBUG'
)

# Use all AWS features

articles = scrape_news(
    output_file='path/to/output.json',
    use_playwright=True,
    s3_storage=True,
    cloudwatch_logging=True,
    aws_access_key_id='YOUR_KEY_ID',
    aws_secret_access_key='YOUR_SECRET_KEY',
    aws_region='us-west-2',
    s3_bucket='your-bucket-name',
    s3_prefix='news_articles',
    cloudwatch_log_group='NeuroNews-Scraper',
    cloudwatch_log_stream_prefix='scraper',
    cloudwatch_log_level='INFO'
)

```text

## Configuration

### Scraping Configuration

The scraper uses the settings defined in `config/settings.json`. The relevant settings are:

```json

{
  "scraping": {
    "interval_minutes": 60,
    "sources": [
      "https://example.com/news",
      "https://example.org/tech"
    ]
  }
}

```text

### AWS Configuration

For AWS services (S3 storage and CloudWatch logging), you can provide credentials in four ways:

1. AWS profile:

   ```bash

   --aws-profile neuronews-dev
   ```

2. Command-line arguments:

   ```bash

   --aws-key-id YOUR_KEY_ID --aws-secret-key YOUR_SECRET_KEY --aws-region us-west-2
   ```

3. Environment variables:

   ```bash

   export AWS_ACCESS_KEY_ID=YOUR_KEY_ID
   export AWS_SECRET_ACCESS_KEY=YOUR_SECRET_KEY
   export AWS_REGION=us-west-2
   export S3_BUCKET=your-bucket-name
   export CLOUDWATCH_LOG_GROUP=NeuroNews-Scraper
   ```

4. Configuration file:

   ```json

   {
     "aws_profile": "neuronews-dev",
     "s3_storage": {
       "enabled": true,
       "bucket": "neuronews-raw-articles-dev",
       "prefix": "news_articles"
     },
     "cloudwatch_logging": {
       "enabled": true,
       "log_group": "NeuroNews-Scraper",
       "log_stream_prefix": "scraper",
       "log_level": "INFO"
     }
   }
   ```

   You can create this file manually or use the `scripts/setup_aws_dev.py` script to generate it.

## S3 Storage Structure

Articles are stored in S3 with the following key structure:

```text

{prefix}/{source-domain}/{timestamp}_{title-slug}.json

```text

For example:

```text

news_articles/example-com/20250406120000_breaking-news-article.json

```text

Each article is stored as a JSON file with the full article content, and S3
object metadata contains key information like title, URL, source, publication
date, author, and category for easy querying.

## CloudWatch Logging

The scraper logs execution details to CloudWatch Logs with the following structure:

```text

{log-group}/{log-stream-prefix}-{spider-name}-{timestamp}

```text

For example:

```text

NeuroNews-Scraper/scraper-news-20250406120000

```text

Log entries include:

- Spider start/stop events

- Request/response details

- Item extraction information

- Error and warning messages

- Performance metrics

This provides comprehensive monitoring and debugging capabilities for the scraper, especially useful for automated deployments.

## Development Environments

The scraper supports multiple environments (dev, staging, prod) for AWS configuration:

- **Dev**: Used for local development and testing

  - Configuration file: `config/dev_aws.json`

  - Command-line option: `--env dev` (default)

- **Staging**: Used for pre-production testing

  - Configuration file: `config/staging_aws.json`

  - Command-line option: `--env staging`

- **Prod**: Used for production deployment

  - Configuration file: `config/prod_aws.json`

  - Command-line option: `--env prod`

Each environment can have different AWS credentials, S3 buckets, and CloudWatch log groups.

## Customization

To add support for specific news sites, you may need to customize the selectors in the spider classes:

- `NewsSpider` in `src/scraper/spiders/news_spider.py` for regular sites

- `PlaywrightNewsSpider` in `src/scraper/spiders/playwright_spider.py` for JavaScript-heavy sites

## Structure

- `__init__.py`: Package initialization

- `items.py`: Defines the data structure for scraped items

- `pipelines.py`: Processing pipelines for scraped items

- `settings.py`: Scrapy settings

- `run.py`: Command-line interface

- `spiders/`: Directory containing spider implementations

  - `__init__.py`: Package initialization

  - `news_spider.py`: Regular news spider implementation

  - `playwright_spider.py`: Playwright-based spider for JavaScript-heavy pages

- `pipelines/`: Directory containing pipeline implementations

  - `__init__.py`: Package initialization

  - `s3_pipeline.py`: Pipeline for storing articles in S3

- `logging/`: Directory containing logging implementations

  - `__init__.py`: Package initialization

  - `cloudwatch_handler.py`: Handler for logging to CloudWatch

- `extensions/`: Directory containing Scrapy extensions

  - `__init__.py`: Package initialization

  - `cloudwatch_logging.py`: Extension for CloudWatch logging

