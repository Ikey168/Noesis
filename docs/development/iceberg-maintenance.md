# Iceberg Table Maintenance Implementation

## Issue #293: Table maintenance: compaction & snapshot expiration

This implementation provides automated Iceberg table maintenance through Airflow DAGs and standalone Spark jobs.

## Components

### 1. Airflow DAGs (`airflow/dags/iceberg_maintenance.py`)

**Weekly Compaction DAG** (`iceberg_weekly_compaction`):
- Schedule: Sundays at 2 AM (`0 2 * * 0`)
- Tasks:
  - `rewrite_data_files_enriched`: Consolidates small data files
  - `rewrite_manifests_enriched`: Consolidates manifest files
- Sequence: data files → manifests

**Daily Snapshot Expiration DAG** (`iceberg_daily_snapshot_expiration`):
- Schedule: Daily at 1 AM (`0 1 * * *`)
- Tasks:
  - `expire_snapshots_enriched`: Removes old snapshots, retains last 5

### 2. Spark Jobs

**Compaction Job** (`airflow/dags/spark_jobs/iceberg_compaction.py`):
```bash
# Data file compaction
python iceberg_compaction.py --table demo.news.articles_enriched --operation rewrite_data_files

# Manifest compaction  
python iceberg_compaction.py --table demo.news.articles_enriched --operation rewrite_manifests
```

**Snapshot Expiration Job** (`airflow/dags/spark_jobs/iceberg_snapshot_expiration.py`):
```bash
python iceberg_snapshot_expiration.py \
  --table demo.news.articles_enriched \
  --older_than "2024-01-01 00:00:00" \
  --retain_last 5
```

### 3. Demo Script (`demo_iceberg_maintenance.py`)

Runs all maintenance operations locally for testing:
```bash
python demo_iceberg_maintenance.py
```

## Iceberg Procedures Used

### Compaction
```sql
-- Consolidate small data files into larger ones
CALL demo.system.rewrite_data_files(table => 'demo.news.articles_enriched');

-- Consolidate manifest files for better query performance
CALL demo.system.rewrite_manifests(table => 'demo.news.articles_enriched');
```

### Snapshot Expiration
```sql
-- Expire old snapshots while retaining recent ones
CALL demo.system.expire_snapshots(
  table => 'demo.news.articles_enriched',
  older_than => TIMESTAMP '2024-01-01 00:00:00',
  retain_last => 5
);
```

## DoD Compliance

✅ **Before/after metrics show reduced small-file count and trimmed history**:
- Both jobs collect and display metrics before/after operations
- File count reduction shown for compaction operations
- Snapshot count reduction shown for expiration operations
- Metrics logged for monitoring and validation

## Benefits

1. **Performance**: Fewer small files = faster queries
2. **Storage**: Removed old snapshots reduce storage costs
3. **Reliability**: Automated scheduling prevents manual errors
4. **Monitoring**: Detailed metrics for each operation

## Configuration

- **Tables**: Currently maintains `demo.news.articles_enriched`
- **Retention**: Keeps last 5 snapshots
- **Schedule**: Weekly compaction, daily expiration
- **Connection**: Uses `spark_default` Airflow connection

## Future Extensions

- Add maintenance for `demo.news.articles_raw` table
- Configure retention policies per table
- Add alerting for failed maintenance operations
- Implement cost optimization based on table size
