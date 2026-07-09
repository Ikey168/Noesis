# Redshift ETL Implementation - Issue #22

## Overview

This document describes the comprehensive ETL (Extract, Transform, Load) implementation for storing processed news articles in AWS Redshift, addressing all requirements of Issue #22:

- ✅ **Define news_articles schema in Redshift**
- ✅ **Implement ETL process (src/database/redshift_loader.py)**
- ✅ **Convert raw JSON articles to structured format before ingestion**
- ✅ **Enable batch uploads for efficiency**

## Architecture

The Redshift ETL system consists of several integrated components:

### Core Components

1. **RedshiftETLProcessor** - Main ETL engine with batch processing capabilities
2. **ArticleRecord** - Structured data model for Redshift storage
3. **Schema Management** - Automated schema initialization and updates
4. **Scrapy Integration** - Seamless pipeline integration for automated storage
5. **Analytics Support** - Optional analytics-optimized data storage

### Data Flow

```
Raw Articles → Data Validation Pipeline → RedshiftETLProcessor → AWS Redshift
                                      ↓
                              Batch Processing & Error Handling
```

## Schema Design

### Primary Table: `news_articles`

```sql
CREATE TABLE news_articles (
    -- Primary identifiers
    id VARCHAR(255) DISTKEY PRIMARY KEY,
    url VARCHAR(1000) NOT NULL,
    
    -- Core article content
    title VARCHAR(1000) NOT NULL,
    content VARCHAR(65535) NOT NULL,
    source VARCHAR(255) NOT NULL,
    
    -- Publishing metadata
    published_date TIMESTAMP SORTKEY,
    scraped_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Validation metadata
    validation_score DECIMAL(5,2),
    content_quality VARCHAR(20), -- 'high', 'medium', 'low'
    source_credibility VARCHAR(20), -- 'trusted', 'reliable', 'questionable', 'unreliable', 'banned'
    validation_flags SUPER, -- JSON array of validation issues
    validated_at TIMESTAMP,
    
    -- Content metrics
    word_count INTEGER,
    content_length INTEGER,
    
    -- Optional fields
    author VARCHAR(255),
    category VARCHAR(100),
    
    -- NLP analysis results (for future use)
    sentiment_score DECIMAL(3,2),
    sentiment_label VARCHAR(20),
    entities SUPER, -- JSON array of extracted entities
    keywords SUPER  -- JSON array of keywords
)
DISTSTYLE KEY
COMPOUND SORTKEY (published_date, source_credibility, content_quality);
```

### Optimizations

- **Distribution Key**: `id` for even data distribution
- **Sort Keys**: `published_date`, `source_credibility`, `content_quality` for query optimization
- **Staging Table**: `news_articles_staging` for atomic batch operations
- **Views**: Pre-built views for common queries and high-quality articles

## Implementation Details

### ArticleRecord Class

Structured representation of articles for Redshift storage:

```python
@dataclass
class ArticleRecord:
    id: str
    url: str
    title: str
    content: str
    source: str
    published_date: Optional[datetime] = None
    validation_score: Optional[float] = None
    content_quality: Optional[str] = None
    source_credibility: Optional[str] = None
    # ... additional fields
    
    @classmethod
    def from_validated_article(cls, validated_data: Dict) -> 'ArticleRecord':
        """Create from validation pipeline output."""
```

**Features:**
- Automatic ID generation using content hash
- Flexible datetime parsing for multiple formats
- Integration with validation pipeline output
- Automatic word count and content length calculation

### RedshiftETLProcessor Class

Main ETL engine with comprehensive functionality:

```python
class RedshiftETLProcessor:
    def __init__(self, host, database='dev', user='admin', 
                 password=None, batch_size=1000):
    
    # Core operations
    def load_single_article(self, article) -> bool
    def batch_load_articles(self, articles, use_staging=True) -> Dict
    def process_validation_pipeline_output(self, results) -> Dict
    
    # Schema management
    def initialize_schema(self, schema_file=None) -> None
    
    # Analytics and monitoring
    def get_article_stats(self) -> Dict
```

**Key Features:**
- **Batch Processing**: Efficient bulk uploads with configurable batch sizes
- **Staging Tables**: Atomic batch operations for data consistency
- **Error Handling**: Comprehensive error recovery and reporting
- **Duplicate Detection**: Automatic duplicate prevention
- **Performance Monitoring**: Detailed statistics and timing metrics

### Batch Processing Strategy

#### Standard Batch Processing
```python
def batch_load_articles(self, articles: List, use_staging: bool = True):
    # Process articles in batches for memory efficiency
    for i in range(0, len(articles), self.batch_size):
        batch = articles[i:i + self.batch_size]
        self._process_batch(batch, use_staging)
```

#### Staging Table Strategy
1. **Clear staging table**: `TRUNCATE news_articles_staging`
2. **Bulk insert**: Load entire batch into staging
3. **Merge operation**: Insert non-duplicates from staging to main table
4. **Atomic commit**: Ensure data consistency

**Benefits:**
- Atomic batch operations
- Automatic duplicate handling
- Better performance for large batches
- Reduced lock contention

## Integration Points

### Data Validation Pipeline Integration

Seamless integration with the existing validation pipeline:

```python
# Process validation results directly
validation_results = validation_pipeline.process_articles(raw_articles)
valid_articles = [r.cleaned_data for r in validation_results if r]

# Load into Redshift
etl_processor = RedshiftETLProcessor(host=redshift_host)
stats = etl_processor.process_validation_pipeline_output(valid_articles)
```

### Scrapy Pipeline Integration

Automatic storage during scraping workflow:

```python
# settings.py
ITEM_PIPELINES = {
    'src.scraper.enhanced_pipelines.EnhancedValidationPipeline': 200,
    'src.scraper.redshift_pipelines.RedshiftStoragePipeline': 900,
}

# Redshift configuration
REDSHIFT_HOST = 'your-cluster.redshift.amazonaws.com'
REDSHIFT_BATCH_SIZE = 100
REDSHIFT_AUTO_COMMIT = True
```

**Pipeline Features:**
- Automatic batch processing during scraping
- Graceful error handling without stopping spiders
- Comprehensive statistics logging
- Optional analytics pipeline for denormalized data

## Configuration

### Environment Variables

Required configuration for production use:

```bash
# Required
export REDSHIFT_HOST=your-cluster.redshift.amazonaws.com
export REDSHIFT_PASSWORD=your-password

# Optional (with defaults)
export REDSHIFT_DATABASE=dev
export REDSHIFT_USER=admin
export REDSHIFT_BATCH_SIZE=1000
```

### Scrapy Settings

```python
# Redshift connection
REDSHIFT_HOST = 'your-cluster.redshift.amazonaws.com'
REDSHIFT_DATABASE = 'neuronews'
REDSHIFT_USER = 'etl_user'
REDSHIFT_PASSWORD = 'secure-password'

# Batch processing
REDSHIFT_BATCH_SIZE = 100
REDSHIFT_AUTO_COMMIT = True
REDSHIFT_DROP_ON_ERROR = False

# Analytics (optional)
REDSHIFT_ANALYTICS_TABLE = 'article_analytics'
```

## Usage Examples

### Standalone ETL Processing

```python
from src.database.redshift_loader import RedshiftETLProcessor

# Initialize processor
with RedshiftETLProcessor(host='your-cluster.redshift.amazonaws.com') as processor:
    # Initialize schema
    processor.initialize_schema()
    
    # Load articles
    stats = processor.batch_load_articles(validated_articles)
    
    print(f"Loaded {stats['loaded_count']}/{stats['total_articles']} articles")
    print(f"Success rate: {stats['success_rate']:.1f}%")
```

### Integration with Validation Pipeline

```python
from src.database.data_validation_pipeline import DataValidationPipeline
from src.database.redshift_loader import RedshiftETLProcessor

# Process articles through validation
config = SourceReputationConfig.from_file('config/validation_settings.json')
validator = DataValidationPipeline(config)

validated_articles = []
for article in raw_articles:
    result = validator.process_article(article)
    if result:
        validated_articles.append(result.cleaned_data)

# Store in Redshift
with RedshiftETLProcessor(host=redshift_host) as etl:
    stats = etl.process_validation_pipeline_output(validated_articles)
```

### Analytics Queries

Pre-built views for common analytics:

```sql
-- High-quality articles from trusted sources
SELECT * FROM high_quality_articles 
WHERE published_date >= CURRENT_DATE - 7;

-- Article statistics by source and quality
SELECT * FROM article_statistics 
WHERE publish_date >= CURRENT_DATE - 30
ORDER BY article_count DESC;

-- Custom analytics query
SELECT 
    source,
    content_quality,
    AVG(validation_score) as avg_score,
    COUNT(*) as article_count
FROM news_articles
WHERE scraped_at >= CURRENT_DATE - 7
GROUP BY source, content_quality
ORDER BY avg_score DESC;
```

## Performance Optimization

### Batch Size Tuning

Optimal batch sizes for different scenarios:

- **High-throughput scraping**: 500-1000 articles per batch
- **Real-time processing**: 50-100 articles per batch  
- **Large data migrations**: 1000-5000 articles per batch

### Query Optimization

The schema is optimized for common query patterns:

1. **Time-based queries**: `published_date` sort key
2. **Quality filtering**: `source_credibility`, `content_quality` sort keys
3. **Source analysis**: Distribution by `id` for even data distribution

### Memory Management

- Streaming batch processing prevents memory overflow
- Automatic batch size adjustment based on available memory
- Connection pooling for high-concurrency scenarios

## Monitoring and Logging

### Statistics Tracking

Comprehensive statistics for monitoring ETL performance:

```python
stats = processor.get_article_stats()
{
    'total_articles': 15247,
    'recent_articles': 342,
    'avg_validation_score': {'average': 78.5, 'minimum': 45.0, 'maximum': 95.2},
    'by_source_credibility': [
        {'source_credibility': 'trusted', 'count': 8532},
        {'source_credibility': 'reliable', 'count': 4201}
    ],
    'top_sources': [
        {'source': 'reuters.com', 'count': 2841},
        {'source': 'bbc.com', 'count': 2156}
    ]
}
```

### Error Handling

Multi-layered error handling ensures data integrity:

1. **Connection errors**: Automatic retry with exponential backoff
2. **Batch errors**: Individual article retry with error isolation
3. **Schema errors**: Graceful degradation with detailed logging
4. **Duplicate errors**: Silent handling with statistics tracking

### Logging Integration

Structured logging for production monitoring:

```python
logger.info(f"Batch processed: {loaded}/{total} loaded, {failed} failed")
logger.error(f"ETL processing failed: {error}", extra={'batch_id': batch_id})
```

## Testing

### Integration Tests

Comprehensive test suite covering:

- Article record creation and validation
- Batch processing scenarios
- Error handling and recovery
- Schema management
- Performance benchmarking

```bash
# Run ETL integration tests
python -m pytest tests/integration/test_redshift_etl.py -v

# Run with coverage
python -m pytest tests/integration/test_redshift_etl.py --cov=src.database.redshift_loader
```

### Demo Script

Interactive demonstration of all features:

```bash
# Run with mock data (no Redshift connection required)
python demo_redshift_etl.py --mock

# Run with real Redshift (requires configuration)
python demo_redshift_etl.py --batch-size 50
```

## Security Considerations

### Access Control

- Use dedicated ETL user with minimal required permissions
- Implement connection encryption (SSL/TLS)
- Store credentials securely using environment variables or AWS Secrets Manager

### Data Validation

- Input sanitization for all article fields
- SQL injection prevention through parameterized queries
- Content length limits to prevent memory attacks

### Audit Trail

- Comprehensive logging of all ETL operations
- Timestamps for all data modifications
- Error tracking and alerting

## Future Enhancements

### 1. Incremental Loading
- Delta detection for updating existing articles
- Change data capture (CDC) for real-time updates
- Efficient handling of article modifications

### 2. Data Partitioning
- Date-based partitioning for improved query performance
- Automated partition management
- Historical data archiving strategies

### 3. Advanced Analytics
- Pre-computed aggregation tables
- Real-time analytics dashboards
- Machine learning feature engineering

### 4. Multi-Region Support
- Cross-region replication for disaster recovery
- Geo-distributed ETL processing
- Regional data compliance handling

## Summary

The Redshift ETL implementation successfully addresses all Issue #22 requirements:

✅ **Schema Definition**: Comprehensive `news_articles` schema with optimization for analytics queries  
✅ **ETL Process**: Full-featured `RedshiftETLProcessor` with batch processing and error handling  
✅ **Data Transformation**: Seamless conversion from validation pipeline output to structured format  
✅ **Batch Efficiency**: High-performance batch uploads with staging tables and memory optimization  
✅ **Integration**: Complete Scrapy pipeline integration for automated workflow  
✅ **Monitoring**: Comprehensive statistics, logging, and error tracking  

The system is production-ready and provides a robust foundation for storing and analyzing news articles at scale in AWS Redshift.

## Files Created/Modified

### New Files
- `src/database/redshift_schema.sql` - Database schema definition
- `src/database/redshift_loader.py` - Enhanced ETL processor (major update)
- `src/scraper/redshift_pipelines.py` - Scrapy integration pipelines
- `tests/integration/test_redshift_etl.py` - Comprehensive integration tests
- `demo_redshift_etl.py` - Interactive demonstration script
- `REDSHIFT_ETL_IMPLEMENTATION.md` - This documentation

### Integration Points
- Compatible with existing `src/database/data_validation_pipeline.py`
- Integrates with `src/scraper/enhanced_pipelines.py`
- Uses configuration from `config/validation_settings.json`
- Extends existing Scrapy pipeline architecture
