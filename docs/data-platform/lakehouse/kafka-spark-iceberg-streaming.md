# Kafka → Spark → Iceberg Streaming Setup

This document explains the streaming pipeline setup for Issue #289 and #290, including checkpoint management, restart behavior, watermarking, and deduplication.

## Overview

The streaming job reads from Kafka topic `articles.raw.v1`, parses Avro messages, applies watermarking and deduplication for late data handling, and writes to Iceberg table `demo.news.articles_raw` with checkpointing to ensure exactly-once processing.

## Architecture

```
Kafka Topic: articles.raw.v1
    ↓ (Avro messages)
Spark Streaming
    ↓ (parsed articles)
Watermarking (2 hours) + Deduplication
    ↓ (clean, unique articles)
Iceberg Table: demo.news.articles_raw
    ↓ (checkpoint: /chk/articles_raw)
```

## Watermarking & Deduplication (Issue #290)

### Watermarking
- **Window**: 2-hour watermark for late data tolerance
- **Purpose**: Handles out-of-order events within acceptable latency
- **Behavior**: Events arriving more than 2 hours late are dropped

### Deduplication
- **Key**: Article ID (`id` field)
- **Strategy**: `dropDuplicates(["id"])` within watermark window
- **Result**: Only the latest version of each article survives

### Implementation
```python
clean = (df.withWatermark("published_at", "2 hours")
           .dropDuplicates(["id"]))
```

## Checkpoint Directory & Restart Behavior

### Checkpoint Location
- **Default**: `/chk/articles_raw`
- **Configurable**: Set `CHECKPOINT_LOCATION` environment variable
- **Purpose**: Tracks Kafka offsets and streaming state for exactly-once processing

### Restart Behavior
1. **First Start**: Reads from earliest available Kafka messages
2. **Restart**: Resumes from last committed checkpoint
3. **No Duplicates**: Checkpoint ensures idempotent processing
4. **Fault Tolerance**: Handles Spark driver failures gracefully

### Checkpoint Contents
- Kafka topic partitions and offsets
- Streaming query metadata
- State information for stateful operations
- Iceberg table commit history

## Configuration

### Environment Variables
```bash
export KAFKA_BOOTSTRAP_SERVERS="kafka:9092"
export KAFKA_TOPIC="articles.raw.v1"
export ICEBERG_TABLE="demo.news.articles_raw"
export CHECKPOINT_LOCATION="/chk/articles_raw"
```

### Required Dependencies
- Spark with Kafka connector
- Iceberg Spark runtime
- Avro libraries for message parsing

## Usage

### Start Streaming Job
```bash
python jobs/spark/stream_write_raw.py
```

### Stop Streaming Job
- **Graceful**: Press Ctrl+C
- **Force**: Kill process (checkpoint will handle restart)

### Monitor Progress
```bash
# Check Iceberg table
spark-sql -e "SELECT count(*) FROM demo.news.articles_raw"

# Check for duplicates
spark-sql -e "SELECT id, count(*) as cnt FROM demo.news.articles_raw GROUP BY id HAVING cnt > 1"

# Check checkpoint directory
ls -la /chk/articles_raw/
```

## Testing

### Unit Tests
```bash
# Run watermarking and deduplication unit tests
python tests/test_watermark_deduplication.py
```

### Integration Tests
```bash
# Run full integration test with out-of-order events
python jobs/spark/test_watermark_deduplication.py
```

### DoD Validation (Issue #290)
The integration test validates:
- ✅ No duplicates for keys within watermark window
- ✅ Late data beyond watermark is properly handled
- ✅ Only latest version of each article survives
- ✅ Backpressure remains acceptable during processing
ls -la /chk/articles_raw/
```

## DoD Validation

### Test Restart Behavior
1. Start streaming job
2. Send test messages to Kafka
3. Stop job (Ctrl+C)
4. Restart job
5. Verify no duplicates in Iceberg table

### Expected Behavior
- ✅ No duplicate records after restart
- ✅ Continuous append to Iceberg table
- ✅ Checkpoint directory maintains state
- ✅ Kafka offsets properly tracked

## Troubleshooting

### Common Issues

#### Checkpoint Directory Permissions
```bash
# Ensure write permissions
chmod -R 755 /chk/articles_raw
```

#### Kafka Connection Issues
```bash
# Verify Kafka is running
kafka-topics.sh --list --bootstrap-server kafka:9092
```

#### Iceberg Table Issues
```bash
# Verify table exists
spark-sql -e "SHOW TABLES IN demo.news"
```

### Recovery Scenarios

#### Corrupted Checkpoint
```bash
# Remove checkpoint to restart from beginning
rm -rf /chk/articles_raw
```

#### Schema Evolution
- Iceberg handles schema evolution automatically
- Checkpoint remains valid across schema changes

## Performance Tuning

### Batch Size
```python
.option("maxOffsetsPerTrigger", "10000")  # Limit records per batch
```

### Parallelism
```python
.config("spark.sql.streaming.numPartitions", "4")  # Increase parallelism
```

### Checkpoint Interval
```python
.trigger(processingTime="30 seconds")  # Control checkpoint frequency
```

## Monitoring

### Key Metrics
- **Input Rate**: Messages/second from Kafka
- **Processing Time**: Time per batch
- **Queue Size**: Backlog in Kafka
- **Table Size**: Iceberg table row count

### Logging
```python
# Enable detailed logging
spark.sparkContext.setLogLevel("INFO")
```

## References
- [Iceberg Streaming](https://iceberg.apache.org/docs/latest/spark-streaming/)
- [Spark Structured Streaming](https://spark.apache.org/docs/latest/structured-streaming-programming-guide.html)
- [Kafka Integration](https://spark.apache.org/docs/latest/structured-streaming-kafka-integration.html)
