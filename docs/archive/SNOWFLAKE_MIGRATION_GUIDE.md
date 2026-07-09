# Snowflake Schema Migration Guide

## Overview
This document outlines the migration of the NeuroNews articles schema from Amazon Redshift to Snowflake, highlighting key changes and optimizations made for Snowflake's architecture.

## Key Changes Made

### 1. Data Type Conversions

| Redshift | Snowflake | Notes |
|----------|-----------|-------|
| `SUPER` | `VARIANT` | Snowflake's native JSON data type |
| `DECIMAL(x,y)` | `NUMBER(x,y)` | Snowflake's numeric type |
| `TIMESTAMP` | `TIMESTAMP_NTZ` | Non-timezone aware timestamps |
| `TEXT` | `TEXT` | Compatible, no change needed |
| `BIGINT IDENTITY(1,1)` | `NUMBER AUTOINCREMENT` | Snowflake's auto-increment |

### 2. Removed Redshift-Specific Features

#### Distribution and Sort Keys
- **Removed**: `DISTKEY`, `DISTSTYLE KEY`, `DISTSTYLE EVEN`
- **Replaced with**: `CLUSTER BY` clauses for better data organization
- **Reason**: Snowflake uses automatic clustering and micro-partitions

#### Compound Sort Keys
- **Removed**: `COMPOUND SORTKEY (column1, column2, ...)`
- **Replaced with**: `CLUSTER BY (column1, column2, ...)` 
- **Reason**: Snowflake's clustering is more flexible and automatic

### 3. Snowflake Optimizations Added

#### Clustering Keys
```sql
-- Example: news_articles table
CLUSTER BY (published_date, source_credibility, content_quality)
```

#### Search Optimization
```sql
-- Added for better equality searches
ALTER TABLE news_articles ADD SEARCH OPTIMIZATION ON EQUALITY(source, content_quality, source_credibility);
```

#### Automatic Clustering
```sql
-- Enable automatic maintenance of clustering
ALTER TABLE news_articles RESUME RECLUSTER;
```

### 4. Function and Syntax Updates

#### String Aggregation
- **Redshift**: `STRING_AGG(column, delimiter)`
- **Snowflake**: `LISTAGG(column, delimiter)`

#### Current Timestamp
- **Redshift**: `CURRENT_TIMESTAMP`
- **Snowflake**: `CURRENT_TIMESTAMP()`

#### Date Functions
- **Redshift**: `DATEADD(unit, value, date)`
- **Snowflake**: `DATEADD(unit, value, date)` (compatible)

### 5. Schema Organization

#### Database Context
```sql
USE SCHEMA NEURONEWS;
```
Added explicit schema usage for better organization.

#### Foreign Key Constraints
Enhanced referential integrity with explicit foreign key definitions:
```sql
FOREIGN KEY (article_id) REFERENCES news_articles(id)
```

## Performance Optimizations

### 1. Clustering Strategy
- **Primary clustering**: Time-based (published_date, created_at)
- **Secondary clustering**: Categorical data (source_credibility, content_quality)
- **Benefit**: Improved query performance for time-series and filtered queries

### 2. VARIANT Usage
- Efficient storage and querying of JSON data
- Better compression than text-based JSON storage
- Native JSON path querying support

### 3. Search Optimization
- Added on frequently filtered columns
- Improves point lookups and equality filters
- Particularly beneficial for source and category filtering

## Migration Steps

### 1. Schema Creation
```bash
# Run the Snowflake schema creation script
snowsql -f src/database/snowflake_schema.sql
```

### 2. Data Migration
```sql
-- Example ETL from Redshift to Snowflake
INSERT INTO snowflake_news_articles 
SELECT 
    id, url, title, content, source,
    published_date::TIMESTAMP_NTZ,
    scraped_at::TIMESTAMP_NTZ,
    validation_score::NUMBER(5,2),
    content_quality, source_credibility,
    PARSE_JSON(validation_flags), -- Convert to VARIANT
    validated_at::TIMESTAMP_NTZ,
    word_count, content_length, author, category,
    sentiment_score::NUMBER(3,2), sentiment_label,
    PARSE_JSON(entities), -- Convert to VARIANT
    PARSE_JSON(keywords), -- Convert to VARIANT
    PARSE_JSON(topics), -- Convert to VARIANT
    PARSE_JSON(dominant_topic), -- Convert to VARIANT
    extraction_method,
    extraction_processed_at::TIMESTAMP_NTZ,
    extraction_processing_time::NUMBER(10,3)
FROM redshift_news_articles;
```

### 3. View Recreation
All views have been updated with Snowflake-compatible syntax and are included in the schema file.

### 4. Application Updates
- Update connection strings to point to Snowflake
- Verify queries work with new data types (especially VARIANT fields)
- Test time zone handling with TIMESTAMP_NTZ

## Compatibility Notes

### Analytics Queries
- Most SELECT queries remain compatible
- JSON field access may need updates to use Snowflake's JSON path syntax
- Aggregation functions updated (STRING_AGG → LISTAGG)

### ETL Processes
- Update data loading processes to use Snowflake's COPY INTO commands
- Leverage Snowflake's automatic data type inference for staging
- Consider using Snowflake's streams and tasks for incremental loading

### Monitoring and Maintenance
- Snowflake handles most optimization automatically
- Monitor clustering effectiveness through system views
- Use Snowflake's query profiler for performance tuning

## Benefits of Migration

1. **Auto-scaling**: Snowflake automatically scales compute and storage
2. **Zero-maintenance clustering**: Automatic clustering without manual tuning
3. **Better JSON support**: Native VARIANT type for complex data
4. **Improved concurrency**: Better handling of concurrent workloads
5. **Cost optimization**: Pay-per-use model with automatic suspension

## Testing Checklist

- [ ] Schema creation completes without errors
- [ ] All tables created with correct structure
- [ ] Views compile and return expected results  
- [ ] Foreign key constraints work properly
- [ ] Clustering keys are applied correctly
- [ ] Search optimization is enabled
- [ ] Sample data loads and queries correctly
- [ ] JSON fields (VARIANT) work as expected
- [ ] Performance meets or exceeds Redshift benchmarks
