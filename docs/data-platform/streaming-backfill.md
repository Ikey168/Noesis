# Streaming Backfill Playbook: Batch + Stream Duality

## Issue #295: Backfill & replay playbook (batch + stream duality)

This playbook provides step-by-step instructions for managing backfills and replays in the Kafka → Iceberg streaming pipeline, ensuring data consistency across batch and streaming modes.

## Overview

The NeuroNews pipeline supports **batch + stream duality**, allowing you to:
1. **Pause streaming** for maintenance or backfills
2. **Run batch backfills** for historical data or gap filling
3. **Resume streaming** seamlessly from the last checkpoint
4. **Handle late data** with watermarks and replay scenarios

## Architecture Components

### Streaming Pipeline
- **Source**: Kafka topics with offset tracking
- **Processing**: Spark Structured Streaming with checkpoints
- **Sink**: Iceberg tables with ACID transactions
- **Checkpoints**: `/chk/articles_raw`, `/chk/articles_enriched`

### Batch Pipeline
- **Source**: Kafka offset ranges or file-based data
- **Processing**: Spark batch jobs with idempotent writes
- **Sink**: Same Iceberg tables (MERGE INTO for consistency)
- **Time Windows**: Configurable date/time ranges

## 🔄 Playbook: Pause → Backfill → Resume

### Step 1: Pause the Streaming Job

First, gracefully stop the running streaming job to prevent conflicts during backfill.

```bash
# Find the streaming job process
ps aux | grep stream_write_raw

# Gracefully stop the streaming job (SIGTERM)
kill -TERM <PID>

# Or use Airflow/scheduler to stop
airflow dags pause streaming_raw_ingestion
airflow dags pause streaming_enrichment

# Verify the job has stopped
ps aux | grep stream_write_raw
```

**Alternative: Programmatic Stop**
```python
# In your streaming job code
query = spark.readStream...writeStream...start()

# Stop gracefully
query.stop()
```

### Step 2: Identify Backfill Requirements

Determine what data needs to be backfilled and the time window.

```bash
# Check last processed offset in checkpoint
ls -la /chk/articles_raw/offsets/
cat /chk/articles_raw/offsets/<latest-batch-id>

# Check Kafka topic lag
kafka-consumer-groups.sh --bootstrap-server kafka:9092 \
  --describe --group exactly-once-consumer

# Check data gaps in Iceberg table
```

```sql
-- Find data gaps by date
SELECT 
  DATE(published_at) as date,
  COUNT(*) as record_count
FROM demo.news.articles_raw 
WHERE published_at >= '2024-01-01'
GROUP BY DATE(published_at)
ORDER BY date;

-- Identify missing time windows
WITH date_series AS (
  SELECT DATE('2024-01-01') + INTERVAL seq DAY as date
  FROM (SELECT row_number() OVER () - 1 as seq FROM ... LIMIT 365)
),
actual_data AS (
  SELECT DATE(published_at) as date, COUNT(*) as count
  FROM demo.news.articles_raw
  GROUP BY DATE(published_at)
)
SELECT d.date, COALESCE(a.count, 0) as records
FROM date_series d
LEFT JOIN actual_data a ON d.date = a.date
WHERE COALESCE(a.count, 0) = 0;
```

### Step 3: Run Batch Backfill

Use batch jobs to fill data gaps while streaming is paused.

#### Option A: Kafka Offset-Based Backfill

```bash
# Backfill from specific Kafka offset range
python jobs/spark/batch_backfill_kafka.py \
  --topic articles.raw.v1 \
  --start-offset 1000 \
  --end-offset 5000 \
  --table demo.news.articles_raw

# Backfill by timestamp range
python jobs/spark/batch_backfill_kafka.py \
  --topic articles.raw.v1 \
  --start-timestamp "2024-01-01 00:00:00" \
  --end-timestamp "2024-01-02 00:00:00" \
  --table demo.news.articles_raw
```

#### Option B: File-Based Backfill

```bash
# Backfill from stored files (CSV, Parquet, JSON)
python jobs/spark/batch_write_raw.py \
  --input-path "s3://backup-bucket/articles/2024/01/01/*" \
  --table demo.news.articles_raw \
  --mode "append"
```

#### Option C: Database-Based Backfill

```bash
# Backfill from external database
python jobs/spark/batch_backfill_db.py \
  --source-table raw_articles_backup \
  --date-range "2024-01-01:2024-01-02" \
  --target-table demo.news.articles_raw
```

### Step 4: Verify Backfill Completion

Check that the backfill completed successfully and data is consistent.

```sql
-- Verify record counts match expected
SELECT 
  DATE(published_at) as date,
  COUNT(*) as record_count,
  COUNT(DISTINCT id) as unique_records
FROM demo.news.articles_raw 
WHERE published_at BETWEEN '2024-01-01' AND '2024-01-02'
GROUP BY DATE(published_at)
ORDER BY date;

-- Check for duplicates (should be 0)
SELECT id, COUNT(*) as duplicate_count
FROM demo.news.articles_raw
WHERE published_at BETWEEN '2024-01-01' AND '2024-01-02'
GROUP BY id
HAVING COUNT(*) > 1
LIMIT 10;

-- Verify data quality
SELECT 
  source,
  COUNT(*) as count,
  COUNT(CASE WHEN title IS NULL THEN 1 END) as null_titles,
  COUNT(CASE WHEN body IS NULL THEN 1 END) as null_bodies
FROM demo.news.articles_raw
WHERE published_at BETWEEN '2024-01-01' AND '2024-01-02'
GROUP BY source;
```

### Step 5: Resume Streaming

Restart the streaming job from the last checkpoint.

```bash
# Resume streaming job
python jobs/spark/stream_write_raw_exactly_once.py

# Or use Airflow/scheduler
airflow dags unpause streaming_raw_ingestion
airflow dags unpause streaming_enrichment

# Monitor startup
tail -f /var/log/spark/streaming_raw.log
```

**Checkpoint Verification**
```bash
# Verify checkpoint integrity
ls -la /chk/articles_raw/
cat /chk/articles_raw/metadata

# Check that streaming resumes from correct offset
# (should be after backfill data)
```

## 🕐 Watermarks & Late Data Handling

### Understanding Watermarks

Watermarks define how long to wait for late-arriving data before considering a time window complete.

```python
# Configure watermarks in streaming job
df.withWatermark("published_at", "10 minutes") \
  .groupBy(
    window(col("published_at"), "1 hour"),
    col("source")
  ) \
  .count()
```

### Late Data Scenarios

#### Scenario 1: Normal Late Arrival (within watermark)
```
Timeline: 10:00 -> 10:05 -> 10:15 (late data for 10:00 window)
Action: Automatically included in aggregation
```

#### Scenario 2: Very Late Arrival (beyond watermark)
```
Timeline: 10:00 -> 10:15 (watermark passed) -> 10:30 (very late data)
Action: Dropped by streaming, handle via backfill
```

### Late Data Recovery Strategy

```bash
# 1. Identify late data in source
kafka-console-consumer.sh --bootstrap-server kafka:9092 \
  --topic articles.raw.v1 \
  --from-beginning | \
  jq '.published_at' | \
  sort

# 2. Run targeted backfill for late data
python jobs/spark/batch_backfill_late_data.py \
  --late-data-window "2 hours" \
  --target-date "2024-01-01"

# 3. Verify late data integration
# (Check data consistency queries from Step 4)
```

## 🔄 Common Backfill Patterns

### Pattern 1: Daily Catch-Up Backfill

```bash
#!/bin/bash
# daily_catchup_backfill.sh

# Pause streaming
airflow dags pause streaming_raw_ingestion

# Backfill yesterday's data
YESTERDAY=$(date -d "yesterday" +%Y-%m-%d)

python jobs/spark/batch_backfill_kafka.py \
  --topic articles.raw.v1 \
  --start-timestamp "${YESTERDAY} 00:00:00" \
  --end-timestamp "${YESTERDAY} 23:59:59" \
  --table demo.news.articles_raw

# Resume streaming
airflow dags unpause streaming_raw_ingestion
```

### Pattern 2: Historical Migration Backfill

```bash
#!/bin/bash
# historical_migration_backfill.sh

START_DATE="2024-01-01"
END_DATE="2024-01-31"

# Pause streaming
airflow dags pause streaming_raw_ingestion

# Process date range in chunks
current_date="$START_DATE"
while [[ "$current_date" < "$END_DATE" ]]; do
  echo "Processing $current_date"
  
  python jobs/spark/batch_backfill_kafka.py \
    --topic articles.raw.v1 \
    --start-timestamp "${current_date} 00:00:00" \
    --end-timestamp "${current_date} 23:59:59" \
    --table demo.news.articles_raw
  
  # Move to next day
  current_date=$(date -d "$current_date + 1 day" +%Y-%m-%d)
done

# Resume streaming
airflow dags unpause streaming_raw_ingestion
```

### Pattern 3: Disaster Recovery Backfill

```bash
#!/bin/bash
# disaster_recovery_backfill.sh

# 1. Assess damage
echo "Checking data integrity..."
python jobs/spark/data_integrity_check.py \
  --table demo.news.articles_raw \
  --start-date "2024-01-01" \
  --end-date "2024-01-31"

# 2. Reset checkpoint (if corrupted)
rm -rf /chk/articles_raw/*

# 3. Full reprocessing from Kafka retention
python jobs/spark/batch_backfill_kafka.py \
  --topic articles.raw.v1 \
  --from-beginning \
  --table demo.news.articles_raw

# 4. Resume streaming from latest
python jobs/spark/stream_write_raw_exactly_once.py
```

## 📊 Monitoring & Validation

### Key Metrics to Monitor

```sql
-- Streaming lag monitoring
SELECT 
  MAX(processed_at) as last_processed,
  CURRENT_TIMESTAMP() - MAX(processed_at) as lag_minutes
FROM demo.news.articles_raw;

-- Backfill progress monitoring
SELECT 
  DATE(published_at) as date,
  COUNT(*) as records,
  MIN(processed_at) as first_processed,
  MAX(processed_at) as last_processed
FROM demo.news.articles_raw
WHERE processed_at >= CURRENT_TIMESTAMP() - INTERVAL 1 HOUR
GROUP BY DATE(published_at)
ORDER BY date;

-- Data quality monitoring
SELECT 
  source,
  COUNT(*) as total_records,
  COUNT(CASE WHEN title IS NOT NULL THEN 1 END) / COUNT(*) * 100 as title_completeness,
  COUNT(CASE WHEN body IS NOT NULL THEN 1 END) / COUNT(*) * 100 as body_completeness
FROM demo.news.articles_raw
WHERE processed_at >= CURRENT_TIMESTAMP() - INTERVAL 1 DAY
GROUP BY source;
```

### Alerting Conditions

```yaml
# alerts.yml
alerts:
  - name: streaming_lag_high
    condition: lag_minutes > 30
    action: notify_oncall
    
  - name: backfill_failed
    condition: backfill_exit_code != 0
    action: page_data_team
    
  - name: duplicate_data_detected
    condition: duplicate_count > 0
    action: notify_data_team
    
  - name: checkpoint_corruption
    condition: checkpoint_read_error
    action: page_oncall
```

## 🛠️ Troubleshooting Guide

### Issue: Checkpoint Corruption

```bash
# Symptoms: Streaming job fails to start
# Solution: Reset checkpoint and backfill

# 1. Backup corrupted checkpoint
mv /chk/articles_raw /chk/articles_raw.corrupted.$(date +%s)

# 2. Create new checkpoint directory
mkdir -p /chk/articles_raw

# 3. Run backfill from last known good state
python jobs/spark/batch_backfill_kafka.py \
  --topic articles.raw.v1 \
  --start-timestamp "$(cat /var/log/last_good_checkpoint_time)" \
  --table demo.news.articles_raw

# 4. Resume streaming
python jobs/spark/stream_write_raw_exactly_once.py
```

### Issue: Kafka Topic Retention Exceeded

```bash
# Symptoms: Data older than retention period
# Solution: Restore from backup storage

# 1. Check available backups
aws s3 ls s3://neuronews-backup/kafka-dumps/

# 2. Restore from backup
python jobs/spark/batch_restore_from_backup.py \
  --backup-path "s3://neuronews-backup/kafka-dumps/2024-01-01/" \
  --table demo.news.articles_raw

# 3. Resume streaming from current time
python jobs/spark/stream_write_raw_exactly_once.py
```

### Issue: Backfill Taking Too Long

```bash
# Symptoms: Backfill job running for hours
# Solution: Parallelize and optimize

# 1. Check resource utilization
spark-submit --status <application-id>

# 2. Increase parallelism
python jobs/spark/batch_backfill_kafka.py \
  --partitions 100 \
  --executor-cores 4 \
  --executor-memory "8g" \
  --topic articles.raw.v1 \
  --start-timestamp "2024-01-01 00:00:00" \
  --end-timestamp "2024-01-01 23:59:59"

# 3. Split into smaller time windows
./scripts/parallel_backfill.sh \
  --start-date "2024-01-01" \
  --end-date "2024-01-31" \
  --window-hours 1
```

## 📋 Pre-flight Checklist

Before running any backfill operation:

- [ ] **Streaming Status**: Verify streaming job is properly stopped
- [ ] **Checkpoint Backup**: Backup current checkpoint state
- [ ] **Resource Availability**: Ensure sufficient cluster resources
- [ ] **Data Validation**: Define validation queries for post-backfill
- [ ] **Rollback Plan**: Document rollback procedure if backfill fails
- [ ] **Monitoring Setup**: Configure alerts for backfill progress
- [ ] **Stakeholder Notice**: Notify team of planned backfill window

## 📚 Additional Resources

- [Apache Spark Structured Streaming Guide](https://spark.apache.org/docs/latest/structured-streaming-programming-guide.html)
- [Apache Iceberg Table Operations](https://iceberg.apache.org/docs/latest/spark-writes/)
- [Kafka Offset Management](https://kafka.apache.org/documentation/#offset_management)
- [Checkpoint Recovery Best Practices](https://spark.apache.org/docs/latest/structured-streaming-programming-guide.html#recovery-semantics-after-changes-in-a-streaming-query)

---

*This playbook is part of the NeuroNews streaming infrastructure documentation. For questions or updates, please refer to the data engineering team.*
