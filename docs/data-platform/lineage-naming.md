# Lineage Dataset Naming Convention

**Issue #193** - Standardized dataset URI scheme for clean OpenLineage graphs in NeuroNews.

## Overview

This document defines the standardized dataset naming convention for NeuroNews data assets to ensure consistent lineage tracking across all data processing pipelines.

## Dataset URI Scheme

### Base Pattern
```
{protocol}://{base_path}/{layer}/{entity}/{partition}/part-{sequence}.{format}
```

### Components

| Component | Description | Example |
|-----------|-------------|---------|
| `protocol` | Storage protocol | `file`, `s3`, `gcs`, `azure` |
| `base_path` | Base storage location | `data`, `neuronews-prod-bucket` |
| `layer` | Data processing tier | `raw`, `bronze`, `silver`, `gold` |
| `entity` | Business entity/dataset | `news_articles`, `sentiment_analysis` |
| `partition` | Time-based partition | `yyyy=2025/mm=08/dd=23` |
| `sequence` | File sequence number | `001`, `002`, `003` |
| `format` | File format | `json`, `parquet`, `csv` |

### Current Implementation (Local Development)
```
file://data/{layer}/{entity}/{partition}/part-{sequence}.{format}
```

### Future Implementation (Production)
```
s3://neuronews-{env}-bucket/{layer}/{entity}/{partition}/part-{sequence}.{format}
```

## Data Layers

### 1. Raw Layer (`raw/`)
**Purpose**: Unprocessed data as ingested from sources
**Format**: Typically JSON for flexibility
**Retention**: Long-term (regulatory compliance)

**Examples**:
```
# News articles from web scraping
file://data/raw/news_articles/yyyy=2025/mm=08/dd=23/part-001.json

# Social media posts
file://data/raw/social_posts/yyyy=2025/mm=08/dd=23/part-001.json
```

**Schema**:
- Preserve original structure
- Add metadata: `source`, `scraped_at`, `scraper_version`
- Include data quality indicators

### 2. Bronze Layer (`bronze/`)
**Purpose**: Cleaned and standardized data
**Format**: Parquet for performance
**Retention**: Medium-term

**Examples**:
```
# Cleaned and validated news articles
file://data/bronze/clean_articles/yyyy=2025/mm=08/dd=23/part-001.parquet

# Standardized article metadata
file://data/bronze/article_metadata/yyyy=2025/mm=08/dd=23/part-001.parquet
```

**Schema**:
- Standardized field names and types
- Data validation applied
- Deduplication completed
- Basic quality metrics

### 3. Silver Layer (`silver/`)
**Purpose**: Enriched data with business logic applied
**Format**: Parquet for analytics
**Retention**: Medium-term

**Examples**:
```
# NLP-processed articles with sentiment, entities, keywords
file://data/silver/nlp_processed/yyyy=2025/mm=08/dd=23/part-001.parquet

# Sentiment analysis results
file://data/silver/sentiment_analysis/yyyy=2025/mm=08/dd=23/part-001.parquet
```

**Schema**:
- Business rules applied
- Enrichments added (NLP, ML predictions)
- Calculated fields
- Aggregations prepared

### 4. Gold Layer (`gold/`)
**Purpose**: Business-ready datasets for analytics and reporting
**Format**: CSV/Parquet for consumption
**Retention**: Long-term (business value)

**Examples**:
```
# Daily news summary for business intelligence
file://data/gold/daily_summary/yyyy=2025/mm=08/dd=23/part-001.csv

# Trending topics analysis
file://data/gold/trending_topics/yyyy=2025/mm=08/dd=23/part-001.csv
```

**Schema**:
- Denormalized for performance
- Business-friendly naming
- Ready for visualization tools
- Optimized for specific use cases

## Partitioning Strategy

### Time-Based Partitioning
Use Hive-style partitioning for efficient querying:
```
yyyy=2025/mm=08/dd=23/
```

### Benefits
- Efficient partition pruning
- Easy data lifecycle management
- Standard across data ecosystems
- Compatible with Spark, Hive, Presto

### Alternative Partitioning (Future)
```
# For high-volume datasets
yyyy=2025/mm=08/dd=23/hh=14/

# For multi-source datasets
yyyy=2025/mm=08/dd=23/source=reuters/

# For multi-region deployments
yyyy=2025/mm=08/dd=23/region=eu-central-1/
```

## File Naming Convention

### Sequence Numbers
- Use zero-padded 3-digit sequence: `001`, `002`, `003`
- Enables proper lexicographic sorting
- Supports up to 999 files per partition

### Format Extensions
- `.json` - Raw, semi-structured data
- `.parquet` - Structured, high-performance analytics
- `.csv` - Human-readable, external consumption
- `.avro` - Schema evolution support (future)

## OpenLineage Integration

### Dataset Registration
Every dataset URI is automatically registered with OpenLineage metadata:

```python
{
    "name": "file://data/silver/sentiment_analysis/yyyy=2025/mm=08/dd=23/part-001.parquet",
    "namespace": "neuro_news_dev",
    "facets": {
        "schema": {...},
        "dataSource": {...},
        "columnLineage": {...}
    }
}
```

### Lineage Tracking
Complete data lineage from raw to gold:
```
raw/news_articles → bronze/clean_articles → silver/nlp_processed → gold/daily_summary
```

## Environment-Specific Naming

### Development Environment
```
file://data/{layer}/{entity}/{partition}/part-{sequence}.{format}
```

### Staging Environment
```
s3://neuronews-staging-bucket/{layer}/{entity}/{partition}/part-{sequence}.{format}
```

### Production Environment
```
s3://neuronews-prod-bucket/{layer}/{entity}/{partition}/part-{sequence}.{format}
```

## Migration Strategy

### Phase 1: Local Development (Current)
- Use file:// protocol for local development
- Implement naming convention in news_pipeline DAG
- Validate with demo datasets

### Phase 2: Cloud Storage
- Migrate to S3/GCS/Azure storage
- Update URIs to use cloud protocols
- Maintain same logical structure

### Phase 3: Advanced Features
- Multi-region replication
- Data versioning
- Schema registry integration

## Best Practices

### Naming Guidelines
1. **Consistency**: Always use the standard pattern
2. **Descriptive**: Entity names should be clear and business-meaningful
3. **Future-proof**: Consider evolution and scaling needs
4. **Tool-friendly**: Compatible with common data tools

### Implementation Guidelines
1. **Use Helper Functions**: Always use `lineage_utils.py` helpers
2. **Validate URIs**: Check format before creating datasets
3. **Document Changes**: Update this document when patterns evolve
4. **Test Thoroughly**: Validate lineage tracking in development

### Performance Considerations
1. **Partition Size**: Target 100-500MB per file
2. **Sequence Limits**: Monitor file count per partition
3. **Format Choice**: Use Parquet for analytics, JSON for flexibility
4. **Compression**: Enable appropriate compression (snappy, gzip)

## Examples by Use Case

### News Pipeline Dataset Flow
```
1. Raw Scraping:
   file://data/raw/news_articles/yyyy=2025/mm=08/dd=23/part-001.json
   
2. Data Cleaning:
   file://data/bronze/clean_articles/yyyy=2025/mm=08/dd=23/part-001.parquet
   
3. NLP Processing:
   file://data/silver/sentiment_analysis/yyyy=2025/mm=08/dd=23/part-001.parquet
   file://data/silver/entity_extraction/yyyy=2025/mm=08/dd=23/part-001.parquet
   
4. Business Analytics:
   file://data/gold/daily_summary/yyyy=2025/mm=08/dd=23/part-001.csv
   file://data/gold/trending_topics/yyyy=2025/mm=08/dd=23/part-001.csv
```

### Social Media Pipeline Dataset Flow
```
1. Raw Collection:
   file://data/raw/social_posts/yyyy=2025/mm=08/dd=23/part-001.json
   
2. Content Processing:
   file://data/bronze/processed_posts/yyyy=2025/mm=08/dd=23/part-001.parquet
   
3. Engagement Analysis:
   file://data/silver/engagement_metrics/yyyy=2025/mm=08/dd=23/part-001.parquet
   
4. Trend Analysis:
   file://data/gold/social_trends/yyyy=2025/mm=08/dd=23/part-001.csv
```

## Compliance and Governance

### Data Retention
- **Raw**: 7 years (regulatory compliance)
- **Bronze**: 2 years (operational needs)
- **Silver**: 1 year (analytics history)
- **Gold**: 3 years (business intelligence)

### Access Control
- Layer-based permissions
- Role-based access (data-engineer, analyst, business-user)
- Audit logging for sensitive datasets

### Data Quality
- Schema validation at ingestion
- Quality metrics tracked per dataset
- Automated data profiling

## Troubleshooting

### Common Issues
1. **Inconsistent Naming**: Use helper functions to avoid errors
2. **Missing Partitions**: Ensure date formatting is correct
3. **Lineage Gaps**: Verify all datasets use standard URIs
4. **Performance Issues**: Check partition size and file count

### Validation Tools
```bash
# Check dataset naming compliance
make validate-dataset-naming

# Verify lineage tracking
make test-lineage-tracking

# Performance analysis
make analyze-dataset-performance
```

## Related Documentation
- [OpenLineage + Marquez implementation](../development/openlineage-marquez.md)
