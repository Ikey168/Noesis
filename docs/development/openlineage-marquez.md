# OpenLineage + Marquez Data Lineage Integration

## Issue #296: Lineage for Spark ↔ Iceberg jobs (OpenLineage + Marquez)

This implementation provides comprehensive data lineage tracking for the NeuroNews Spark pipeline using OpenLineage and Marquez.

## Overview

Data lineage tracking enables:
- **Data Governance**: Track data flow from source to destination
- **Impact Analysis**: Understand downstream effects of data changes
- **Debugging**: Trace data issues to their source
- **Compliance**: Meet regulatory requirements for data provenance

## Architecture

```
┌─────────┐    ┌──────────────┐    ┌─────────────┐    ┌─────────────┐
│  Kafka  │───▶│ Spark Jobs   │───▶│  Iceberg    │───▶│  Marquez    │
│ Topics  │    │ (w/OpenLineage)│    │  Tables     │    │   UI        │
└─────────┘    └──────────────┘    └─────────────┘    └─────────────┘
                      │                                      ▲
                      │                                      │
                      └──────── Lineage Events ─────────────┘
```

## Components

### 1. OpenLineage Configuration (`jobs/spark/openlineage_config.py`)

Provides reusable configuration for enabling OpenLineage in Spark jobs:

```python
spark = create_spark_session_with_lineage(
    app_name="MySparkJob",
    additional_configs={
        "spark.sql.adaptive.enabled": "true"
    }
)

add_lineage_metadata(spark, job_type="batch", source_system="kafka", target_system="iceberg")
```

**Key Configurations:**
- `spark.extraListeners=io.openlineage.spark.agent.OpenLineageSparkListener`
- `spark.openlineage.transport.type=http`
- `spark.openlineage.transport.url=http://marquez:5000`
- `spark.openlineage.namespace=neuronews`

### 2. Enhanced Spark Jobs

#### Batch Job with Lineage (`jobs/spark/batch_write_raw_with_lineage.py`)
```bash
python jobs/spark/batch_write_raw_with_lineage.py
```

Features:
- Automatic lineage collection from file reads to Iceberg writes
- Custom metadata injection for enhanced tracking
- Validation of OpenLineage configuration

#### Streaming Job with Lineage (`jobs/spark/stream_write_raw_with_lineage.py`)
```bash
python jobs/spark/stream_write_raw_with_lineage.py
```

Features:
- Kafka source lineage tracking
- Batch-level lineage events in foreachBatch
- Graceful shutdown with lineage cleanup

### 3. Marquez Backend Services

#### Docker Compose Setup (`docker-compose.lineage.yml`)
```bash
# Start Marquez services
docker-compose -f docker-compose.lineage.yml up -d

# Check services
docker-compose -f docker-compose.lineage.yml ps
```

**Services:**
- **Marquez API**: Data lineage backend (port 5000)
- **Marquez Web**: Lineage visualization UI (port 3000)
- **PostgreSQL**: Metadata storage (port 5432)

#### Configuration (`marquez/config/marquez.yml`)
- Database connection to PostgreSQL
- OpenLineage API endpoint configuration
- CORS settings for web UI access

## Usage Examples

### Starting Lineage Infrastructure

```bash
# 1. Start Marquez services
docker-compose -f docker-compose.lineage.yml up -d

# 2. Wait for services to be ready
curl http://localhost:5000/api/v1/health

# 3. Create namespace
curl -X PUT http://localhost:5000/api/v1/namespaces/neuronews \
  -H "Content-Type: application/json" \
  -d '{"name": "neuronews", "description": "NeuroNews pipeline"}'
```

### Running Jobs with Lineage

```bash
# Batch job with lineage tracking
export MARQUEZ_URL="http://localhost:5000"
export SCRAPED_DATA_PATH="data/scraped/latest/*.csv"
python jobs/spark/batch_write_raw_with_lineage.py

# Streaming job with lineage tracking  
export KAFKA_TOPIC="articles.raw.v1"
export ICEBERG_TABLE="demo.news.articles_raw"
python jobs/spark/stream_write_raw_with_lineage.py
```

### Viewing Lineage in Marquez UI

1. **Open Web UI**: http://localhost:3000
2. **Select Namespace**: Choose "neuronews" from dropdown
3. **Browse Jobs**: View batch and streaming jobs
4. **Explore Lineage**: Click on jobs to see data flow graphs
5. **View Datasets**: See input/output datasets and their relationships

## API Examples

### Check Available Jobs
```bash
curl http://localhost:5000/api/v1/namespaces/neuronews/jobs | jq
```

### Get Job Details
```bash
curl http://localhost:5000/api/v1/namespaces/neuronews/jobs/BatchWriteRawWithLineage | jq
```

### View Dataset Lineage
```bash
curl http://localhost:5000/api/v1/namespaces/neuronews/datasets/demo.news.articles_raw | jq
```

### List Job Runs
```bash
curl http://localhost:5000/api/v1/namespaces/neuronews/jobs/BatchWriteRawWithLineage/runs | jq
```

## Lineage Data Structure

### Job Metadata
```json
{
  "name": "BatchWriteRawWithLineage",
  "type": "BATCH",
  "description": "Batch job with OpenLineage tracking",
  "location": "jobs/spark/batch_write_raw_with_lineage.py",
  "context": {
    "team": "data-engineering",
    "project": "neuronews",
    "environment": "development"
  }
}
```

### Dataset Metadata
```json
{
  "name": "demo.news.articles_raw",
  "type": "TABLE", 
  "physicalName": "demo.news.articles_raw",
  "sourceName": "iceberg",
  "fields": [
    {"name": "id", "type": "STRING"},
    {"name": "published_at", "type": "TIMESTAMP"},
    {"name": "title", "type": "STRING"}
  ]
}
```

### Run Metadata
```json
{
  "runId": "01234567-89ab-cdef-0123-456789abcdef",
  "createdAt": "2024-01-01T10:00:00Z",
  "updatedAt": "2024-01-01T10:05:00Z",
  "nominalStartTime": "2024-01-01T10:00:00Z",
  "nominalEndTime": "2024-01-01T10:05:00Z",
  "state": "COMPLETED",
  "inputDatasets": ["file:///data/scraped/latest/articles.csv"],
  "outputDatasets": ["iceberg://demo.news.articles_raw"]
}
```

## Monitoring and Alerting

### Health Checks
```bash
# Marquez API health
curl http://localhost:5000/api/v1/health

# Database connectivity
curl http://localhost:5000/api/v1/namespaces

# Recent job runs
curl http://localhost:5000/api/v1/jobs/runs
```

### Key Metrics
- **Job Success Rate**: Percentage of successful job runs
- **Lineage Coverage**: Percentage of jobs with lineage data
- **Data Freshness**: Time since last dataset update
- **Dataset Count**: Number of tracked datasets

### Alerting Rules
```yaml
# Example alerting configuration
alerts:
  - name: lineage_job_failure
    condition: job_run.state == 'FAILED'
    notification: slack_data_team
    
  - name: missing_lineage_data  
    condition: job_without_lineage_percentage > 20
    notification: email_data_governance
    
  - name: marquez_service_down
    condition: marquez_health_check_failed
    notification: page_oncall
```

## Troubleshooting

### Common Issues

#### 1. OpenLineage Listener Not Loading
```
ERROR: Cannot find OpenLineageSparkListener class
```
**Solution**: Ensure OpenLineage JAR is in Spark classpath:
```bash
export SPARK_CLASSPATH="/path/to/openlineage-spark-*.jar:$SPARK_CLASSPATH"
```

#### 2. Cannot Connect to Marquez
```
ERROR: Connection refused to http://marquez:5000
```
**Solution**: Check Marquez service status:
```bash
docker-compose -f docker-compose.lineage.yml ps
curl http://localhost:5000/api/v1/health
```

#### 3. No Lineage Data Appearing
**Checklist:**
- [ ] OpenLineage listener configured in Spark
- [ ] Marquez URL accessible from Spark driver
- [ ] Namespace exists in Marquez
- [ ] Job actually reading/writing data

#### 4. Lineage Events Missing
```python
# Add debug logging
spark.conf.set("spark.openlineage.debugFacet", "true")
spark.conf.set("log4j.logger.io.openlineage", "DEBUG")
```

## Performance Considerations

### OpenLineage Overhead
- **CPU**: 1-3% additional overhead for lineage collection
- **Memory**: ~50MB additional memory for OpenLineage agent
- **Network**: Minimal - async HTTP requests to Marquez

### Optimization Tips
1. **Batch Lineage Events**: Use buffering for high-frequency jobs
2. **Selective Tracking**: Disable lineage for non-critical jobs
3. **Async Transport**: Use async HTTP transport (default)
4. **Resource Sizing**: Size Marquez backend appropriately

## Security

### API Security
```yaml
# Production Marquez configuration
marquez:
  auth:
    type: "oauth2"
    provider: "okta"
    
  cors:
    allowedOrigins: ["https://lineage.company.com"]
```

### Network Security
- **TLS**: Enable HTTPS for Marquez API
- **Firewall**: Restrict access to Marquez ports
- **VPC**: Deploy in private network with secure access

## Demo Script

Run the complete demonstration:
```bash
python demo_openlineage_marquez.py
```

This will:
1. Start Marquez services
2. Setup NeuroNews namespace  
3. Run batch job with lineage
4. Run streaming job demo
5. Validate lineage data in Marquez
6. Show UI access information

## Integration with Existing Jobs

To add lineage to existing Spark jobs:

1. **Import OpenLineage Config**:
   ```python
   from jobs.spark.openlineage_config import create_spark_session_with_lineage
   ```

2. **Replace Spark Session Creation**:
   ```python
   # Old way
   spark = SparkSession.builder.appName("MyJob").getOrCreate()
   
   # New way with lineage
   spark = create_spark_session_with_lineage("MyJob")
   ```

3. **Add Metadata** (optional):
   ```python
   add_lineage_metadata(spark, job_type="batch", source_system="s3", target_system="iceberg")
   ```

## Future Enhancements

- **Cost Tracking**: Add cost metadata to lineage events
- **Data Quality**: Integrate data quality metrics
- **Column-Level Lineage**: Track field-level transformations
- **Custom Facets**: Add business-specific metadata
- **Real-time Alerting**: Stream lineage events to alerting systems

---

*This implementation provides comprehensive data lineage tracking for the NeuroNews pipeline, enabling better data governance, debugging, and compliance.*
