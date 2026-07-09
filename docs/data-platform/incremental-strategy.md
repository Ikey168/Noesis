# Incremental Strategy for fact_news

## Overview
The `fact_news` model has been configured with an incremental materialization strategy to optimize performance and reduce processing time for large datasets.

## Configuration
- **Materialization**: `incremental`
- **Unique Key**: `article_id`
- **On Schema Change**: `append_new_columns`
- **Incremental Strategy**: Date-based partitioning by `published_at_utc`

## Commands

### Full Load (Initial Run)
Run this command for the first time or when you need to rebuild the entire table:

```bash
# Full load - processes all historical data
dbt run --select fact_news --full-refresh
```

### Incremental Run (Default)
Run this command for regular updates - only processes new/updated articles:

```bash
# Incremental run - only processes new data since last run
dbt run --select fact_news
```

### Specific Date Range
To process data from a specific date:

```bash
# Run with variables to override date filters
dbt run --select fact_news --vars '{"start_date": "2024-08-01"}'
```

## Incremental Logic
The incremental filter processes articles that meet either condition:
1. **New articles**: `published_at_utc::date > MAX(published_at_utc::date)` from existing data
2. **Updated articles**: `updated_at_utc > MAX(updated_at_utc)` from existing data

## Macro Usage
The incremental filtering logic is encapsulated in the `incremental_filter` macro:

```sql
{{ incremental_filter('n.published_at_utc', 'n.updated_at_utc') }}
```

## Performance Benefits
- **Faster processing**: Only new/updated data is processed on incremental runs
- **Reduced resource usage**: Lower CPU and memory consumption
- **Partitioned storage**: Data is partitioned by date for efficient querying
- **Upsert capability**: Handles both inserts and updates seamlessly

## Testing
Verify the incremental behavior:

1. **First run (full load)**:
   ```bash
   dbt run --select fact_news --full-refresh
   ```

2. **Second run (incremental)**:
   ```bash
   dbt run --select fact_news
   ```
   
   Should process only new rows since the last run.

## Monitoring
Check the incremental performance:

```sql
-- Check row counts after each run
SELECT COUNT(*) as total_rows FROM {{ ref('fact_news') }};

-- Check latest processed date
SELECT MAX(published_at_utc::date) as latest_date FROM {{ ref('fact_news') }};
```
