# Enrichment Upsert: Raw → Enriched via Iceberg MERGE INTO

This document explains the enrichment upsert pipeline for Issue #291, including sentiment analysis, entity extraction, and idempotent updates using Iceberg MERGE INTO operations.

## Overview

The enrichment pipeline transforms raw articles into enriched articles with additional computed fields like sentiment analysis and entity extraction. It uses Iceberg's MERGE INTO functionality to ensure idempotent updates.

## Architecture

```
demo.news.articles_raw
    ↓ (daily batch processing)
Enrichment Processing
    ↓ (sentiment + entities + keywords)
MERGE INTO demo.news.articles_enriched
    ↓ (idempotent upserts)
```

## Enrichment Features

### Sentiment Analysis
- **Sentiment Score**: Float value 0.0 (negative) to 1.0 (positive)
- **Sentiment Label**: "positive", "neutral", or "negative"
- **Method**: Rule-based analysis (upgradeable to ML models)

### Entity Extraction
- **Entities**: Array of identified entities (organizations, people, etc.)
- **Method**: Regex-based extraction (upgradeable to NER models)

### Keyword Extraction
- **Keywords**: Array of important terms from title and body
- **Method**: Simple text processing (upgradeable to TF-IDF)

### Versioning
- **Version**: Integer tracking update count for each article
- **Purpose**: Audit trail and change tracking

## Iceberg MERGE INTO Operation

### SQL Pattern
```sql
MERGE INTO demo.news.articles_enriched t
USING tmp_updates s
ON t.id = s.id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
```

### Idempotent Behavior
- **Existing Records**: Updated in place with incremented version
- **New Records**: Inserted with version = 1
- **No Duplicates**: Same ID cannot exist multiple times
- **Reprocessing Safe**: Re-running same day updates existing records

## Configuration

### Environment Variables
```bash
export RAW_TABLE="demo.news.articles_raw"
export ENRICHED_TABLE="demo.news.articles_enriched"
export PROCESS_DATE="2025-08-25"  # Optional, defaults to yesterday
```

### Table Schema

#### Enriched Table Schema
```sql
CREATE TABLE demo.news.articles_enriched (
    id STRING,
    published_at TIMESTAMP,
    title STRING,
    body STRING,
    source STRING,
    url STRING,
    sentiment_score FLOAT,
    sentiment_label STRING,
    entities ARRAY<STRING>,
    keywords ARRAY<STRING>,
    processed_at TIMESTAMP,
    version INT
) USING iceberg
PARTITIONED BY (days(published_at))
```

## Usage

### Daily Enrichment Job
```bash
# Process yesterday's articles (default)
python jobs/spark/upsert_enriched.py

# Process specific date
PROCESS_DATE="2025-08-25" python jobs/spark/upsert_enriched.py
```

### Monitoring Progress
```bash
# Check enriched table count
spark-sql -e "SELECT count(*) FROM demo.news.articles_enriched"

# Check for duplicates (should be 0)
spark-sql -e "SELECT id, count(*) as cnt FROM demo.news.articles_enriched GROUP BY id HAVING cnt > 1"

# Check version distribution
spark-sql -e "SELECT version, count(*) FROM demo.news.articles_enriched GROUP BY version ORDER BY version"
```

## Testing

### Idempotency Test
```bash
# Run automated idempotency test
python jobs/spark/test_upsert_idempotency.py
```

### Manual Validation
```bash
# Check specific article versions
spark-sql -e "SELECT id, version, processed_at FROM demo.news.articles_enriched WHERE id = 'article-123'"

# Verify no duplicates
spark-sql -e "SELECT count(DISTINCT id) as unique_ids, count(*) as total_rows FROM demo.news.articles_enriched"
```

## DoD Validation (Issue #291)

### Requirement
"Re-running for a day updates rows in place (no dupes)"

### Validation Steps
1. **First Run**: Process articles for a specific date
2. **Record State**: Count records and note versions
3. **Second Run**: Re-process same date
4. **Verify Results**:
   - Total record count unchanged
   - No duplicate IDs
   - Versions incremented for existing records
   - Processed timestamps updated

### Expected Behavior
```
Initial Run:  3 articles → 3 enriched records (version=1)
Second Run:   3 articles → 3 enriched records (version=2)
Result:       No duplicates, versions incremented
```

## Performance Considerations

### Partitioning
- **Strategy**: Daily partitions on `published_at`
- **Benefit**: Efficient date-based filtering and updates
- **Maintenance**: Automatic partition pruning

### Optimization
```python
# Enable merge optimization
.config("spark.sql.adaptive.enabled", "true")
.config("spark.sql.adaptive.coalescePartitions.enabled", "true")
```

### Batch Size
```python
# Process daily batches for optimal performance
.config("spark.sql.adaptive.advisoryPartitionSizeInBytes", "128MB")
```

## Error Handling

### Common Issues

#### Missing Raw Data
```bash
# Check if raw data exists for date
spark-sql -e "SELECT count(*) FROM demo.news.articles_raw WHERE date(published_at) = '2025-08-25'"
```

#### Schema Evolution
- Iceberg handles schema evolution automatically
- New columns can be added without breaking existing data

#### Duplicate Detection
```bash
# Find and investigate duplicates
spark-sql -e "SELECT id, count(*) as cnt FROM demo.news.articles_enriched GROUP BY id HAVING cnt > 1"
```

### Recovery Procedures

#### Reprocess Failed Date
```bash
# Delete enriched data for specific date and reprocess
spark-sql -e "DELETE FROM demo.news.articles_enriched WHERE date(published_at) = '2025-08-25'"
PROCESS_DATE="2025-08-25" python jobs/spark/upsert_enriched.py
```

#### Reset All Enriched Data
```bash
# Complete reset (use with caution)
spark-sql -e "DROP TABLE demo.news.articles_enriched"
python jobs/spark/upsert_enriched.py
```

## Future Enhancements

### ML Model Integration
- Replace rule-based sentiment with transformer models
- Use spaCy or Hugging Face for entity extraction
- Implement keyword extraction with TF-IDF

### Advanced Features
- Multi-language sentiment analysis
- Topic modeling and classification
- Summary generation
- Fact-checking integration

## References
- [Iceberg MERGE INTO](https://iceberg.apache.org/docs/latest/spark-writes/#merge-into)
- [Spark SQL](https://spark.apache.org/docs/latest/sql-ref-syntax-dml-merge-into.html)
- [Data Enrichment Patterns](https://iceberg.apache.org/docs/latest/evolution/)
