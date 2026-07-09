# Spark + Iceberg Integration

This document describes the Apache Spark and Apache Iceberg integration for the NeuroNews lakehouse architecture.

## Overview

The Spark + Iceberg setup enables:
- **SQL analytics** on large-scale news data
- **Time travel queries** for historical analysis
- **Schema evolution** for changing data requirements
- **ACID transactions** for data consistency
- **Data versioning** for reproducible analytics

## Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│   Spark SQL     │    │  Iceberg REST    │    │     MinIO       │
│   Applications  │───▶│    Catalog       │───▶│   (S3-compat)   │
│                 │    │                  │    │                 │
└─────────────────┘    └──────────────────┘    └─────────────────┘
         │                       │                       │
         │                       │                       │
         ▼                       ▼                       ▼
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│ Iceberg Tables  │    │   PostgreSQL     │    │   Data Files    │
│   (Metadata)    │    │   (Catalog DB)   │    │  (Parquet/ORC)  │
└─────────────────┘    └──────────────────┘    └─────────────────┘
```

## Quick Start

### 1. Start Required Services

```bash
# Start Iceberg REST catalog, MinIO, and PostgreSQL
cd spark/
docker-compose -f docker-compose.iceberg.yml up -d
```

### 2. Download Dependencies (Optional for Local)

```bash
# Download required JAR files
cd spark/
./download_jars.sh
```

### 3. Test the Setup

```bash
# Run the DoD test
cd spark/
./test_iceberg_setup.sh
```

### 4. Start Spark SQL

```bash
# Use the configuration from spark/conf/
spark-sql
```

## Configuration

The Spark configuration is located in `spark/conf/spark-defaults.conf` and includes:

### Iceberg Extensions
```properties
spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions
```

### Catalog Configuration
```properties
spark.sql.catalog.demo=org.apache.iceberg.spark.SparkCatalog
spark.sql.catalog.demo.type=rest
spark.sql.catalog.demo.uri=http://iceberg-rest:8181
spark.sql.catalog.demo.warehouse=s3a://warehouse
```

### S3A Configuration
```properties
fs.s3a.endpoint=http://minio:9000
fs.s3a.access.key=minio
fs.s3a.secret.key=minio123
fs.s3a.path.style.access=true
```

## Basic Operations

### Create Namespace

```sql
CREATE NAMESPACE IF NOT EXISTS demo.news;
```

### Create Table

```sql
CREATE TABLE demo.news.articles (
    id BIGINT,
    title STRING,
    content STRING,
    published_date DATE,
    source STRING,
    sentiment_score DOUBLE
) USING ICEBERG
PARTITIONED BY (published_date);
```

### Insert Data

```sql
INSERT INTO demo.news.articles VALUES 
    (1, 'Breaking News', 'Important story...', '2025-08-25', 'CNN', 0.7),
    (2, 'Tech Update', 'Latest in AI...', '2025-08-25', 'TechCrunch', 0.8);
```

### Query Data

```sql
-- Current data
SELECT * FROM demo.news.articles 
WHERE published_date >= '2025-08-20';

-- Time travel queries
SELECT * FROM demo.news.articles 
FOR SYSTEM_VERSION AS OF 1;

-- Table history
SELECT * FROM demo.news.articles.history;

-- Snapshots
SELECT * FROM demo.news.articles.snapshots;
```

### Schema Evolution

```sql
-- Add new column
ALTER TABLE demo.news.articles 
ADD COLUMN category STRING;

-- Update schema
ALTER TABLE demo.news.articles 
ALTER COLUMN sentiment_score TYPE FLOAT;
```

## Integration with NeuroNews Pipeline

### Data Ingestion

```python
from pyspark.sql import SparkSession

# Initialize Spark with Iceberg
spark = SparkSession.builder \
    .appName("NeuroNews-Ingestion") \
    .getOrCreate()

# Read news data
news_df = spark.read.json("data/news/*.json")

# Write to Iceberg table
news_df.write \
    .format("iceberg") \
    .mode("append") \
    .option("path", "s3a://warehouse/news/articles") \
    .partitionBy("published_date") \
    .saveAsTable("demo.news.articles")
```

### Analytics Queries

```sql
-- Daily article count by source
SELECT 
    published_date,
    source,
    COUNT(*) as article_count
FROM demo.news.articles 
WHERE published_date >= current_date() - 30
GROUP BY published_date, source
ORDER BY published_date DESC;

-- Sentiment analysis trends
SELECT 
    published_date,
    AVG(sentiment_score) as avg_sentiment,
    COUNT(*) as article_count
FROM demo.news.articles 
WHERE published_date >= current_date() - 7
GROUP BY published_date
ORDER BY published_date;

-- Top sources by volume
SELECT 
    source,
    COUNT(*) as total_articles,
    AVG(sentiment_score) as avg_sentiment
FROM demo.news.articles 
WHERE published_date >= current_date() - 30
GROUP BY source
ORDER BY total_articles DESC;
```

### Data Quality Checks

```sql
-- Check for duplicate articles
SELECT id, COUNT(*) 
FROM demo.news.articles 
GROUP BY id 
HAVING COUNT(*) > 1;

-- Data completeness
SELECT 
    COUNT(*) as total_articles,
    COUNT(title) as articles_with_title,
    COUNT(content) as articles_with_content,
    COUNT(sentiment_score) as articles_with_sentiment
FROM demo.news.articles 
WHERE published_date = current_date();
```

## Performance Optimization

### Partitioning Strategy

```sql
-- Partition by date for time-based queries
CREATE TABLE demo.news.articles_optimized (
    id BIGINT,
    title STRING,
    content STRING,
    published_date DATE,
    source STRING,
    sentiment_score DOUBLE
) USING ICEBERG
PARTITIONED BY (days(published_date));
```

### Clustering

```sql
-- Order data for better compression and query performance
ALTER TABLE demo.news.articles 
WRITE ORDERED BY (published_date, source);
```

### Compaction

```sql
-- Compact small files
CALL demo.system.rewrite_data_files(
    table => 'demo.news.articles'
);

-- Compact manifests
CALL demo.system.rewrite_manifests(
    table => 'demo.news.articles'
);
```

## Monitoring and Maintenance

### Table Statistics

```sql
-- Table size and file count
SELECT * FROM demo.news.articles.files;

-- Partition statistics
SELECT * FROM demo.news.articles.partitions;

-- Metadata information
DESCRIBE EXTENDED demo.news.articles;
```

### Cleanup Old Snapshots

```sql
-- Expire old snapshots (keep last 30 days)
CALL demo.system.expire_snapshots(
    table => 'demo.news.articles',
    older_than => timestamp('2025-07-25 00:00:00')
);

-- Remove orphaned files
CALL demo.system.remove_orphan_files(
    table => 'demo.news.articles'
);
```

## Integration with Other Components

### Airflow Integration

```python
from airflow import DAG
from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator

# Spark job in Airflow
spark_task = SparkSubmitOperator(
    task_id='process_news_data',
    application='/path/to/news_processing.py',
    conf={
        'spark.sql.extensions': 'org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions',
        'spark.sql.catalog.demo': 'org.apache.iceberg.spark.SparkCatalog',
        'spark.sql.catalog.demo.type': 'rest',
        'spark.sql.catalog.demo.uri': 'http://iceberg-rest:8181'
    }
)
```

### MLflow Integration

```python
import mlflow
from pyspark.sql import SparkSession

# Log Iceberg table as MLflow artifact
with mlflow.start_run():
    # Process data with Spark + Iceberg
    spark = SparkSession.builder.getOrCreate()
    
    # Log table metadata
    mlflow.log_param("table_name", "demo.news.articles")
    mlflow.log_param("partition_spec", "published_date")
    
    # Log data statistics
    stats = spark.sql("SELECT COUNT(*) as row_count FROM demo.news.articles").collect()[0]
    mlflow.log_metric("row_count", stats.row_count)
```

### dbt Integration

```sql
-- models/staging/stg_news_articles.sql
{{ config(
    materialized='incremental',
    file_format='iceberg',
    incremental_strategy='merge'
) }}

SELECT 
    id,
    title,
    content,
    published_date,
    source,
    sentiment_score,
    current_timestamp() as processed_at
FROM {{ source('iceberg', 'demo.news.articles') }}

{% if is_incremental() %}
WHERE published_date > (SELECT MAX(published_date) FROM {{ this }})
{% endif %}
```

## Troubleshooting

### Common Issues

1. **Catalog Connection Failed**
   ```bash
   # Check REST catalog service
   curl http://iceberg-rest:8181/v1/config
   ```

2. **S3A Authentication Errors**
   ```bash
   # Verify MinIO credentials and endpoint
   curl http://minio:9000/minio/health/live
   ```

3. **JAR Dependencies Missing**
   ```bash
   # Download required JARs
   cd spark/
   ./download_jars.sh
   ```

### Debug Commands

```bash
# Check Spark configuration
spark-sql -e "SET spark.sql.extensions;"

# List available catalogs
spark-sql -e "SHOW CATALOGS;"

# Test basic connectivity
spark-sql -e "SHOW NAMESPACES IN demo;"
```

## Security Considerations

### Authentication
- REST catalog uses basic authentication
- S3A credentials stored in configuration
- Consider using IAM roles in production

### Access Control
- Implement table-level permissions
- Use namespace-based access control
- Audit query access patterns

### Data Encryption
- Enable S3 server-side encryption
- Use TLS for REST catalog communication
- Encrypt sensitive columns at application level

## Migration from Existing Systems

### From Hive Tables

```sql
-- Create Iceberg table from Hive
CREATE TABLE demo.news.migrated_articles
USING ICEBERG
AS SELECT * FROM hive_catalog.news.articles;
```

### From Parquet Files

```python
# Read existing Parquet files
parquet_df = spark.read.parquet("s3a://existing-bucket/news-data/")

# Write to Iceberg
parquet_df.write \
    .format("iceberg") \
    .mode("overwrite") \
    .saveAsTable("demo.news.converted_articles")
```

## References

- [Apache Iceberg Documentation](https://iceberg.apache.org/)
- [Spark SQL Guide](https://spark.apache.org/docs/latest/sql-programming-guide.html)
- [Iceberg Spark Integration](https://iceberg.apache.org/docs/latest/spark-configuration/)
- [MinIO Documentation](https://min.io/docs/)

## Next Steps

1. Implement automated data quality checks
2. Set up performance monitoring
3. Create data catalog documentation
4. Implement access control policies
5. Set up automated backup and recovery procedures
