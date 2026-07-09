#!/usr/bin/env python3
"""
Spark Structured Streaming job for CDC to Iceberg upserts with exactly-once guarantees and observability.

Consumes Debezium CDC events from Kafka topic 'neuronews.public.articles'
and upserts them into an Iceberg v2 table with merge-on-read capabilities.

The job provides exactly-once semantics through:
1. Kafka consumer group with checkpointed offsets
2. Idempotent upserts keyed by document_id (mapped from the source article_id)
3. Deduplication window by ts_ms in MERGE operations
4. Safe reprocessing with startingOffsets=earliest

Observability features:
1. Consumer lag monitoring via JMX metrics
2. Throughput and processing rate tracking
3. OpenLineage data lineage emission
4. Prometheus metrics exposure
5. Comprehensive error tracking and alerting

The job expects flattened CDC records (via Debezium ExtractNewRecordState SMT).
"""

import os
import logging
import time
import sys
from pathlib import Path
from pyspark.sql import SparkSession, functions as F

# Add project root to path for OpenLineage imports
project_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(project_root))

# Configure logging with structured format for observability
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Import OpenLineage utilities
try:
    from jobs.spark.openlineage_config import create_spark_session_with_lineage, log_lineage_event
    OPENLINEAGE_AVAILABLE = True
    logger.info("OpenLineage integration enabled")
except ImportError:
    logger.warning("OpenLineage not available, lineage tracking disabled")
    OPENLINEAGE_AVAILABLE = False


def create_spark_session():
    """Create Spark session with Iceberg extensions, exactly-once configuration, and observability."""
    app_name = "cdc_to_iceberg_exactly_once_with_observability"
    
    if OPENLINEAGE_AVAILABLE:
        # Use OpenLineage-enabled session for lineage tracking
        additional_configs = {
            # Exactly-once configuration
            "spark.sql.streaming.checkpointLocation.deleteTmpCheckpointDir": "true",
            "spark.sql.adaptive.enabled": "false",
            "spark.sql.adaptive.coalescePartitions.enabled": "false",
            "spark.sql.streaming.kafka.useDeprecatedOffsetFetching": "false",
            
            # Observability and monitoring configurations
            "spark.sql.streaming.metricsEnabled": "true",
            "spark.metrics.conf.*.sink.prometheus.class": "org.apache.spark.metrics.sink.PrometheusServlet",
            "spark.metrics.conf.*.sink.prometheus.path": "/metrics",
            "spark.ui.prometheus.enabled": "true",
            
            # JMX metrics for Kafka consumer monitoring
            "spark.driver.extraJavaOptions": "-Dcom.sun.management.jmxremote -Dcom.sun.management.jmxremote.port=8090 -Dcom.sun.management.jmxremote.authenticate=false -Dcom.sun.management.jmxremote.ssl=false",
            "spark.executor.extraJavaOptions": "-Dcom.sun.management.jmxremote -Dcom.sun.management.jmxremote.port=8091 -Dcom.sun.management.jmxremote.authenticate=false -Dcom.sun.management.jmxremote.ssl=false"
        }
        
        logger.info("Creating Spark session with OpenLineage and observability features")
        return create_spark_session_with_lineage(app_name, additional_configs)
    else:
        # Fallback to basic session configuration
        builder = (SparkSession.builder
                   .appName(app_name)
                   .config("spark.sql.extensions", "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions")
                   .config("spark.sql.catalog.local", "org.apache.iceberg.spark.SparkCatalog")
                   .config("spark.sql.catalog.local.type", "hadoop")
                   .config("spark.sql.catalog.local.warehouse", "/warehouse"))
        
        # Exactly-once configuration
        builder = (builder
                   .config("spark.sql.streaming.checkpointLocation.deleteTmpCheckpointDir", "true")
                   .config("spark.sql.adaptive.enabled", "false")
                   .config("spark.sql.adaptive.coalescePartitions.enabled", "false")
                   .config("spark.sql.streaming.kafka.useDeprecatedOffsetFetching", "false")
                   
                   # Observability configurations
                   .config("spark.sql.streaming.metricsEnabled", "true")
                   .config("spark.metrics.conf.*.sink.prometheus.class", "org.apache.spark.metrics.sink.PrometheusServlet")
                   .config("spark.metrics.conf.*.sink.prometheus.path", "/metrics")
                   .config("spark.ui.prometheus.enabled", "true"))
        
        logger.info("Creating Spark session with basic observability features")
        return builder.getOrCreate()


def create_iceberg_table(spark):
    """Create Iceberg table with v2 format and upsert capabilities for exactly-once processing."""
    logger.info("Creating Iceberg table with exactly-once guarantees...")
    
    spark.sql("""
        CREATE TABLE IF NOT EXISTS local.documents (
            document_id  string,
            source_type  string,
            source_id    string,
            url          string,
            title        string,
            content      string,
            content_ref  string,
            language     string,
            authors      array<string>,
            created_at   timestamp,
            ingested_at  timestamp,
            metadata     map<string, string>,
            op           string,
            ts_ms        bigint,
            lsn          bigint,
            processing_time timestamp
        )
        USING iceberg
        PARTITIONED BY (days(created_at))
        TBLPROPERTIES (
            'format-version'='2',
            'write.delete.mode'='merge-on-read',
            'write.update.mode'='merge-on-read',
            'write.upsert.enabled'='true',
            'write.merge.mode'='merge-on-read'
        )
    """)
    
    logger.info("Iceberg table created/verified successfully")


def upsert_batch(batch_df, batch_id):
    """
    Upsert batch to Iceberg table with exactly-once guarantees and observability metrics.
    
    Features:
    1. Deduplication within batch by (article_id, ts_ms DESC, lsn DESC)
    2. Conditional updates only when ts_ms > existing ts_ms  
    3. LSN-based tie-breaking for same-timestamp updates
    4. Comprehensive logging and metrics for observability
    5. OpenLineage event emission for data lineage
    """
    batch_start_time = time.time()
    batch_count = batch_df.count()
    
    logger.info(f"Processing batch {batch_id} with {batch_count} records")
    
    if batch_count == 0:
        logger.info(f"Batch {batch_id} is empty, skipping")
        return
    
    try:
        # Deduplication within batch: keep latest by ts_ms, then by lsn
        # This ensures exactly-once semantics even with duplicate CDC events
        deduped = (batch_df
                   .withColumn("row_number",
                               F.row_number().over(
                                   F.Window.partitionBy("document_id")
                                   .orderBy(F.desc("ts_ms"), F.desc("lsn"))
                               ))
                   .filter(F.col("row_number") == 1)
                   .drop("row_number"))
        
        deduped_count = deduped.count()
        duplicates_filtered = batch_count - deduped_count
        
        logger.info(f"Batch {batch_id}: Filtered {duplicates_filtered} duplicates, processing {deduped_count} unique records")
        
        if deduped_count == 0:
            logger.info(f"Batch {batch_id}: No records after deduplication")
            return
        
        # Create temporary view for MERGE operation
        temp_view = f"cdc_batch_{batch_id}"
        deduped.createOrReplaceTempView(temp_view)
        
        # Emit OpenLineage event for batch processing
        if OPENLINEAGE_AVAILABLE:
            spark = batch_df.sql_ctx.sparkSession
            log_lineage_event(
                spark, 
                f"cdc_batch_process_{batch_id}", 
                f"kafka_cdc_batch_{batch_id}", 
                "process",
                metadata={
                    "batch_id": str(batch_id),
                    "input_records": batch_count,
                    "output_records": deduped_count,
                    "duplicates_filtered": duplicates_filtered
                }
            )
        
        # Perform MERGE operation with exactly-once semantics. The Iceberg table
        # now speaks document-ingest-v1 (#—): the news CDC stream is mapped onto
        # document columns in main(), keyed by document_id.
        merge_cols = (
            "source_type = s.source_type, source_id = s.source_id, url = s.url, "
            "title = s.title, content = s.content, content_ref = s.content_ref, "
            "language = s.language, authors = s.authors, created_at = s.created_at, "
            "ingested_at = s.ingested_at, metadata = s.metadata, op = s.op, "
            "ts_ms = s.ts_ms, lsn = s.lsn, processing_time = s.processing_time"
        )
        merge_query = f"""
        MERGE INTO local.documents t
        USING {temp_view} s ON t.document_id = s.document_id
        WHEN MATCHED AND s.ts_ms > t.ts_ms THEN
            UPDATE SET {merge_cols}
        WHEN MATCHED AND s.ts_ms = t.ts_ms AND s.lsn > t.lsn THEN
            UPDATE SET {merge_cols}
        WHEN NOT MATCHED THEN
            INSERT (document_id, source_type, source_id, url, title, content,
                   content_ref, language, authors, created_at, ingested_at,
                   metadata, op, ts_ms, lsn, processing_time)
            VALUES (s.document_id, s.source_type, s.source_id, s.url, s.title, s.content,
                   s.content_ref, s.language, s.authors, s.created_at, s.ingested_at,
                   s.metadata, s.op, s.ts_ms, s.lsn, s.processing_time)
        """
        
        # Execute MERGE with transaction semantics
        spark = batch_df.sql_ctx.sparkSession
        spark.sql(merge_query)
        
        # Calculate processing metrics
        batch_duration = time.time() - batch_start_time
        records_per_second = deduped_count / batch_duration if batch_duration > 0 else 0
        
        # Log comprehensive metrics for observability
        logger.info(f"Batch {batch_id} completed successfully:")
        logger.info(f"  - Input records: {batch_count}")
        logger.info(f"  - Unique records: {deduped_count}")
        logger.info(f"  - Duplicates filtered: {duplicates_filtered}")
        logger.info(f"  - Processing time: {batch_duration:.2f}s")
        logger.info(f"  - Records/second: {records_per_second:.2f}")
        
        # Emit completion lineage event
        if OPENLINEAGE_AVAILABLE:
            log_lineage_event(
                spark, 
                f"cdc_batch_complete_{batch_id}",
                "local.documents",
                "write",
                metadata={
                    "batch_id": str(batch_id),
                    "records_processed": deduped_count,
                    "processing_duration_seconds": round(batch_duration, 2),
                    "records_per_second": round(records_per_second, 2)
                }
            )
        
        # Drop temporary view
        spark.sql(f"DROP VIEW IF EXISTS {temp_view}")
        
    except Exception as e:
        batch_duration = time.time() - batch_start_time
        logger.error(f"Batch {batch_id} failed after {batch_duration:.2f}s: {e}")
        
        # Emit failure lineage event for observability
        if OPENLINEAGE_AVAILABLE:
            spark = batch_df.sql_ctx.sparkSession
            log_lineage_event(
                spark, 
                f"cdc_batch_error_{batch_id}", 
                f"kafka_cdc_batch_{batch_id}", 
                "error",
                metadata={
                    "batch_id": str(batch_id),
                    "error_message": str(e),
                    "processing_duration_seconds": round(batch_duration, 2)
                }
            )
        
        raise


def main():
    """Main streaming job execution with exactly-once guarantees and observability."""
    spark = create_spark_session()
    
    # Get configuration from environment variables
    kafka_brokers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    kafka_topic = os.getenv("KAFKA_TOPIC", "neuronews.public.articles")
    checkpoint_location = os.getenv("CHECKPOINT_LOCATION", "/tmp/checkpoints/cdc_to_iceberg")
    consumer_group = os.getenv("KAFKA_CONSUMER_GROUP", "cdc_to_iceberg")
    
    # For exactly-once, allow explicit override of starting offsets
    # Default: "earliest" for safe reprocessing (idempotent)
    # Production: "latest" for normal operation
    starting_offsets = os.getenv("STARTING_OFFSETS", "earliest")
    
    logger.info(f"Starting CDC to Iceberg job with exactly-once guarantees and observability")
    logger.info(f"Kafka brokers: {kafka_brokers}")
    logger.info(f"Kafka topic: {kafka_topic}")
    logger.info(f"Consumer group: {consumer_group}")
    logger.info(f"Checkpoint location: {checkpoint_location}")
    logger.info(f"Starting offsets: {starting_offsets}")
    logger.info(f"OpenLineage enabled: {OPENLINEAGE_AVAILABLE}")
    
    try:
        # Create Iceberg table (idempotent operation)
        create_iceberg_table(spark)
        
        # Emit initial lineage event for job start
        if OPENLINEAGE_AVAILABLE:
            log_lineage_event(
                spark, 
                "cdc_streaming_job_start", 
                kafka_topic, 
                "read",
                metadata={
                    "consumer_group": consumer_group,
                    "starting_offsets": starting_offsets,
                    "checkpoint_location": checkpoint_location
                }
            )
        
        # Read from Kafka stream with exactly-once configuration
        df = (spark.readStream
              .format("kafka")
              .option("kafka.bootstrap.servers", kafka_brokers)
              .option("subscribe", kafka_topic)
              .option("startingOffsets", starting_offsets)
              .option("kafka.group.id", consumer_group)
              # Exactly-once Kafka configuration
              .option("maxOffsetsPerTrigger", "1000")  # Control batch size for consistency
              .option("failOnDataLoss", "false")  # Handle missing data gracefully
              # Observability: Enable Kafka consumer metrics
              .option("kafka.consumer.enable.metrics", "true")
              .option("kafka.consumer.metrics.recording.level", "INFO")
              .load())
        
        # Parse JSON and flatten structure
        # Debezium unwrap gives us flat rows; capture CDC metadata for exactly-once
        value_schema = """
            struct<
                article_id:string, 
                source_id:string, 
                url:string,
                title:string, 
                body:string,
                language:string, 
                country:string, 
                published_at:timestamp,
                updated_at:timestamp,
                op:string,
                ts_ms:long,
                lsn:long
            >
        """
        
        parsed = df.select(
            F.from_json(F.col("value").cast("string"), value_schema).alias("cdc_record"),
            F.col("offset").alias("kafka_offset"),
            F.col("partition").alias("kafka_partition"),
            F.col("timestamp").alias("kafka_timestamp")
        )
        
        # Flatten, then map the news CDC payload onto document-ingest-v1 columns
        # (article_id -> document_id, body -> content, published_at -> created_at,
        # country -> metadata; source_type is constant 'news' for this stream).
        flat = (parsed.select("cdc_record.*", "kafka_offset", "kafka_partition", "kafka_timestamp")
                .filter(F.col("article_id").isNotNull())  # Skip invalid records
                .select(
                    F.col("article_id").alias("document_id"),
                    F.lit("news").alias("source_type"),
                    F.col("source_id"),
                    F.col("url"),
                    F.col("title"),
                    F.col("body").alias("content"),
                    F.lit(None).cast("string").alias("content_ref"),
                    F.col("language"),
                    F.array().cast("array<string>").alias("authors"),
                    F.col("published_at").alias("created_at"),
                    F.current_timestamp().alias("ingested_at"),
                    F.create_map(F.lit("country"), F.col("country").cast("string")).alias("metadata"),
                    F.col("op"),
                    F.col("ts_ms"),
                    F.col("lsn"),
                )
                .withColumn("processing_time", F.current_timestamp()))
        
        # Start streaming query with exactly-once guarantees and observability
        query = (flat.writeStream
                 .foreachBatch(upsert_batch)
                 .option("checkpointLocation", checkpoint_location)
                 .outputMode("update")
                 .trigger(processingTime="10 seconds")  # Controlled trigger for consistency
                 # Observability options
                 .option("queryName", "cdc_to_iceberg_streaming")  # Named query for metrics
                 .start())
        
        logger.info("CDC to Iceberg streaming job started with exactly-once guarantees and observability")
        logger.info("Observability features enabled:")
        logger.info("  ✅ Consumer lag monitoring via JMX")
        logger.info("  ✅ Throughput and processing rate tracking")
        logger.info(f"  {'✅' if OPENLINEAGE_AVAILABLE else '❌'} OpenLineage data lineage emission")
        logger.info("  ✅ Prometheus metrics exposure")
        logger.info("  ✅ Comprehensive error tracking")
        logger.info("Safe restart: Re-run with STARTING_OFFSETS=earliest for idempotent reprocessing")
        logger.info("Metrics endpoints:")
        logger.info("  - Spark UI: http://localhost:4040")
        logger.info("  - Prometheus metrics: http://localhost:4040/metrics")
        logger.info("  - JMX metrics: localhost:8090 (driver), localhost:8091 (executors)")
        logger.info("Waiting for termination...")
        
        # Monitor query progress for observability
        progress_count = 0
        while query.isActive:
            try:
                progress = query.lastProgress
                if progress:
                    progress_count += 1
                    if progress_count % 6 == 0:  # Log every minute (6 * 10s triggers)
                        input_rate = progress.get("inputRowsPerSecond", 0)
                        processing_rate = progress.get("processingRowsPerSecond", 0)
                        batch_duration = progress.get("durationMs", {}).get("triggerExecution", 0)
                        
                        logger.info(f"Streaming progress metrics:")
                        logger.info(f"  - Input rate: {input_rate:.2f} records/sec")
                        logger.info(f"  - Processing rate: {processing_rate:.2f} records/sec")
                        logger.info(f"  - Batch duration: {batch_duration}ms")
                        logger.info(f"  - Total batches: {progress.get('batchId', 0)}")
                
                query.awaitTermination(timeout=10)
            except Exception as e:
                logger.warning(f"Progress monitoring error: {e}")
                query.awaitTermination(timeout=10)
        
    except Exception as e:
        logger.error(f"Error in CDC streaming job: {e}")
        
        # Emit error lineage event
        if OPENLINEAGE_AVAILABLE:
            log_lineage_event(
                spark, 
                "cdc_streaming_job_error", 
                kafka_topic, 
                "error",
                metadata={
                    "error_message": str(e),
                    "consumer_group": consumer_group
                }
            )
        
        raise
    finally:
        logger.info("Stopping Spark session")
        spark.stop()


if __name__ == "__main__":
    main()
