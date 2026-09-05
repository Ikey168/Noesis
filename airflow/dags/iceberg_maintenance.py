"""
Iceberg table maintenance DAG: compaction & snapshot expiration
Issue #293

This DAG implements table maintenance operations for Iceberg tables:
1. Weekly compaction (rewrite_data_files, rewrite_manifests)
2. Daily snapshot expiration (retain last 5 snapshots)

Tables maintained:
- demo.news.articles_enriched
- demo.news.articles_raw (future)
"""
from datetime import datetime, timedelta

from airflow.providers.apache.spark.operators.spark_submit import SparkSubmitOperator
from airflow.sdk import DAG

# DAG configuration
default_args = {
    'owner': 'data-team',
    'depends_on_past': False,
    'start_date': datetime(2024, 1, 1),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 2,
    'retry_delay': timedelta(minutes=5)
}

# Weekly compaction DAG
weekly_compaction_dag = DAG(
    'iceberg_weekly_compaction',
    default_args=default_args,
    description='Weekly Iceberg table compaction and manifest rewrite',
    schedule='0 2 * * 0',  # Sunday at 2 AM
    catchup=False,
    max_active_runs=1,
    tags=['iceberg', 'maintenance', 'compaction']
)

# Daily snapshot expiration DAG
daily_expiration_dag = DAG(
    'iceberg_daily_snapshot_expiration',
    default_args=default_args,
    description='Daily Iceberg snapshot expiration',
    schedule='0 1 * * *',  # Daily at 1 AM
    catchup=False,
    max_active_runs=1,
    tags=['iceberg', 'maintenance', 'snapshots']
)

# Weekly compaction tasks
rewrite_data_files_enriched = SparkSubmitOperator(
    task_id='rewrite_data_files_enriched',
    application='/opt/airflow/dags/spark_jobs/iceberg_compaction.py',
    application_args=[
        '--table', 'demo.news.articles_enriched',
        '--operation', 'rewrite_data_files'
    ],
    conn_id='spark_default',
    dag=weekly_compaction_dag
)

rewrite_manifests_enriched = SparkSubmitOperator(
    task_id='rewrite_manifests_enriched',
    application='/opt/airflow/dags/spark_jobs/iceberg_compaction.py',
    application_args=[
        '--table', 'demo.news.articles_enriched',
        '--operation', 'rewrite_manifests'
    ],
    conn_id='spark_default',
    dag=weekly_compaction_dag
)

# Set task dependencies for weekly compaction
rewrite_data_files_enriched >> rewrite_manifests_enriched

# Daily snapshot expiration task
expire_snapshots_enriched = SparkSubmitOperator(
    task_id='expire_snapshots_enriched',
    application='/opt/airflow/dags/spark_jobs/iceberg_snapshot_expiration.py',
    application_args=[
        '--table', 'demo.news.articles_enriched',
        '--older_than', '{{ ds }} 00:00:00',
        '--retain_last', '5'
    ],
    conn_id='spark_default',
    dag=daily_expiration_dag
)

# Optional: Add dependency to ensure enrichment pipeline completes before maintenance
# enrichment_sensor = ExternalTaskSensor(
#     task_id='wait_for_enrichment_pipeline',
#     external_dag_id='news_enrichment_pipeline',
#     external_task_id='upsert_enriched_articles',
#     timeout=3600,
#     dag=daily_expiration_dag
# )
# 
# enrichment_sensor >> expire_snapshots_enriched
