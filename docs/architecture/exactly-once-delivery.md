# Exactly-Once Delivery Design: Kafka → Iceberg

## Issue #294: Exactly-once design & failure tests (Kafka → Iceberg)

This document explains the exactly-once delivery design for the Kafka → Spark → Iceberg streaming pipeline, ensuring no data loss or duplication even during failures.

## Design Overview

Exactly-once delivery in streaming systems requires three components:
1. **Replayable Source** (Kafka)
2. **Idempotent Sink** (Iceberg with MERGE INTO)
3. **Checkpointing** (Spark Structured Streaming)

## 1. Replayable Source: Kafka

Kafka provides durable, replayable message delivery:

- **Topic Partitions**: Messages are stored across partitions with offsets
- **Offset Tracking**: Each message has a unique (partition, offset) identifier
- **Retention**: Messages retained for configurable duration (default 7 days)
- **Consumer Groups**: Track last processed offset per partition

```python
# Kafka source configuration for exactly-once
kafka_stream = spark.readStream \
    .format("kafka") \
    .option("kafka.bootstrap.servers", "kafka:9092") \
    .option("subscribe", "articles.raw.v1") \
    .option("startingOffsets", "earliest") \
    .option("failOnDataLoss", "false") \
    .load()
```

**Replayability**: If a job fails, it can restart from the last committed offset and reprocess messages without data loss.

## 2. Idempotent Sink: Iceberg with MERGE INTO

Iceberg provides transactional, idempotent writes:

- **ACID Transactions**: Each write is atomic and isolated
- **Primary Key Constraints**: Natural deduplication via MERGE INTO
- **Snapshot Isolation**: Consistent reads during concurrent writes
- **Time Travel**: Ability to query historical versions

```sql
-- Idempotent upsert pattern
MERGE INTO demo.news.articles_raw t
USING updates s
ON t.id = s.id  -- Primary key deduplication
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
```

**Idempotency**: Re-processing the same message results in the same final state, preventing duplicates.

## 3. Checkpointing: Spark Structured Streaming

Spark Structured Streaming provides reliable checkpointing:

- **Offset Tracking**: Stores Kafka offsets in checkpoint directory
- **State Management**: Maintains aggregation state across restarts
- **Write-Ahead Log**: Records planned operations before execution
- **Recovery**: Automatically restarts from last successful checkpoint

```python
# Checkpointing configuration
query = parsed_stream.writeStream \
    .format("iceberg") \
    .outputMode("append") \
    .option("checkpointLocation", "/chk/articles_raw") \
    .option("path", "demo.news.articles_raw") \
    .trigger(processingTime='30 seconds') \
    .start()
```

**Fault Tolerance**: If the job crashes, it resumes from the last checkpoint without reprocessing committed data.

## Exactly-Once Guarantee

The combination provides end-to-end exactly-once semantics:

1. **Source Offsets**: Checkpointing tracks Kafka offsets
2. **Sink Idempotency**: MERGE INTO prevents duplicates
3. **Atomic Commits**: Both checkpoint and data write succeed together

### Failure Scenarios

| Failure Point | Recovery Mechanism | Guarantee |
|---------------|-------------------|-----------|
| Kafka Broker Down | Kafka replication, consumer restart | No data loss |
| Spark Job Crash | Checkpoint recovery, offset replay | No loss/duplication |
| Iceberg Write Fail | Transaction rollback, retry | Consistency maintained |
| Network Partition | Automatic retry, timeout handling | Eventually consistent |

## Implementation Components

### Stream Processing Job (`jobs/spark/stream_write_raw.py`)

```python
def write_to_iceberg(parsed_stream):
    """Write stream to Iceberg with exactly-once guarantees."""
    return parsed_stream.writeStream \
        .format("iceberg") \
        .outputMode("append") \
        .option("checkpointLocation", CHECKPOINT_LOCATION) \
        .option("path", ICEBERG_TABLE) \
        .trigger(processingTime='30 seconds') \
        .start()
```

### Idempotent Upsert Job (`jobs/spark/upsert_enriched.py`)

```python
def upsert_to_enriched_table(spark, enriched_df):
    """Idempotent upsert using MERGE INTO."""
    merge_sql = f"""
    MERGE INTO {ENRICHED_TABLE} t
    USING tmp_updates s
    ON t.id = s.id
    WHEN MATCHED THEN UPDATE SET *
    WHEN NOT MATCHED THEN INSERT *
    """
    spark.sql(merge_sql)
```

### Failure Testing (`tests/integration/test_exactly_once_kafka_iceberg.py`)

- **Mid-batch Failure**: Kill job during processing, verify recovery
- **Duplicate Prevention**: Send same message twice, verify single record
- **Offset Consistency**: Verify checkpoint/data write atomicity
- **Count Stability**: Verify record counts remain stable across restarts

## Configuration Best Practices

### Kafka Producer
```properties
# Exactly-once semantics
enable.idempotence=true
acks=all
retries=Integer.MAX_VALUE
max.in.flight.requests.per.connection=5
```

### Spark Streaming
```python
spark.conf.set("spark.sql.adaptive.enabled", "true")
spark.conf.set("spark.sql.adaptive.coalescePartitions.enabled", "true")
spark.conf.set("spark.sql.streaming.minBatchesToRetain", "10")
```

### Iceberg Table Properties
```sql
ALTER TABLE demo.news.articles_raw 
SET TBLPROPERTIES (
    'write.format.default'='parquet',
    'write.parquet.compression-codec'='snappy',
    'commit.manifest.target-size-bytes'='134217728'
);
```

## Monitoring and Alerting

### Key Metrics
- **Kafka Consumer Lag**: Time behind latest message
- **Processing Rate**: Records per second throughput
- **Checkpoint Age**: Time since last successful checkpoint
- **Duplicate Rate**: Percentage of duplicate records detected
- **Error Rate**: Failed batch processing rate

### Alerts
- Consumer lag > 5 minutes
- Processing rate drops > 50%
- Checkpoint failures
- Error rate > 1%

## Testing Strategy

### Unit Tests
- Individual component functionality
- Error handling and retries
- Configuration validation

### Integration Tests
- End-to-end pipeline testing
- Failure injection and recovery
- Performance under load
- Data consistency validation

### Chaos Engineering
- Random job kills during processing
- Network partitions and timeouts
- Resource exhaustion scenarios
- Clock skew and time zone issues

## Benefits

1. **Data Integrity**: No loss or duplication of events
2. **Consistency**: Strong consistency guarantees across restarts
3. **Scalability**: Horizontal scaling with maintained guarantees
4. **Operability**: Simple recovery procedures
5. **Observability**: Clear metrics and debugging capabilities

## Limitations

1. **Latency**: Additional overhead from checkpointing
2. **Storage**: Checkpoint storage requirements
3. **Complexity**: More complex configuration and monitoring
4. **Performance**: Some throughput trade-offs for consistency

## Future Enhancements

- **Multi-table Transactions**: Coordinate writes across tables
- **Schema Evolution**: Handle schema changes gracefully
- **Backpressure**: Dynamic scaling based on processing lag
- **Cost Optimization**: Checkpoint pruning and optimization
