"""
Noesis daily data pipeline DAG.

Runs the real ingestion engine on a schedule (was a mock demo, #922):

- **scrape** → the health-aware :class:`HarvestScheduler` harvests every
  registered connector through ``harvest_run`` into the unified ``documents``
  corpus (retry + drift detection + adaptive back-off);
- **clean** → a corpus checkpoint (validation/dedup already happen at write time
  in ``DocumentStore``);
- **nlp** → enrichment plus incremental claim/frame extraction; the scan ledger
  guarantees stance/drift jobs see newly ingested documents;
- **analyze** → the argument-mining batches (conflict detection, stance
  aggregation, follow-through position tracking, fact-check) run under DAG
  orchestration via each scheduler's ``run_once``, instead of out-of-band
  APScheduler; a failing batch is recorded, not fatal;
- **publish** → an analytics summary over the real corpus.

Task bodies import the engine lazily, so the DAG imports (for dag-check) without
the `src` package on the path; the tasks need it at execution time.
"""

from datetime import datetime, timedelta
from pathlib import Path
import json
import pandas as pd
import yaml
from typing import Any, Dict, List, Optional

from airflow.decorators import dag, task
from airflow.models import Variable
from airflow.utils.dates import days_ago

# Import NeuroNews lineage utilities (Issue #193)
import sys
sys.path.append("/opt/airflow/plugins")
from lineage_utils import LineageHelper, build_uri

# Import MLflow callbacks for experiment tracking (Issue #225)
from mlflow_callbacks import configure_dag_for_mlflow, configure_task_for_mlflow


# Default arguments for all tasks
default_args = {
    'owner': 'neuronews',
    'depends_on_past': False,
    'start_date': datetime(2025, 8, 1),
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 2,
    'retry_delay': timedelta(minutes=5),
    'max_active_runs': 1,
}


def load_io_paths() -> Dict[str, Any]:
    """Load dataset paths configuration from YAML file."""
    yaml_path = Path("/opt/airflow/include/io_paths.yml")
    with open(yaml_path, 'r') as f:
        return yaml.safe_load(f)


@dag(
    dag_id='news_pipeline',
    default_args=default_args,
    description='NeuroNews data pipeline: scrape → clean → nlp → analyze → publish',
    schedule_interval='0 8 * * *',  # Daily at 08:00 Europe/Berlin
    start_date=datetime(2025, 8, 1),
    catchup=False,
    tags=['neuronews', 'data-pipeline', 'openlineage', 'mlflow:neuro_news_indexing'],
    max_active_runs=1,
    doc_md=__doc__,
)
def news_pipeline():
    """
    NeuroNews daily data pipeline.
    
    Pipeline stages:
    1. Scrape: Collect news articles from various sources
    2. Clean: Standardize, deduplicate, and validate data
    3. NLP: Extract insights, sentiment, entities, keywords
    4. Publish: Create business-ready datasets for analytics
    """
    
    @task
    def scrape(**context) -> Dict[str, Any]:
        """Harvest real documents into the warehouse via the health-aware scheduler.

        Runs every registered connector through ``harvest_run`` (retry + drift
        detection + adaptive scheduling) into the unified ``documents`` corpus.
        Replaces the former mock article generator.
        """
        from src.config.env import resolve_env
        from src.database.local_analytics_connector import get_shared_connection
        from src.ingestion.document_store import DocumentStore
        from src.ingestion.scheduler import HarvestScheduler
        from src.ingestion.source_health import SourceHealthTracker

        conn = get_shared_connection()
        health_path = resolve_env(
            "HEALTH_PATH", "/opt/airflow/data/source_health.json"
        )
        scheduler = HarvestScheduler(DocumentStore(conn), SourceHealthTracker(health_path))
        result = scheduler.run_once()
        totals = result["totals"]
        print(f"✅ Harvested: inserted={totals['inserted']} "
              f"duplicate={totals['duplicate']} skipped={totals['skipped']}")
        return {"totals": totals}

    @task
    def clean(scrape_result: Dict[str, Any], **context) -> Dict[str, Any]:
        """Validation/dedup is done at write time by DocumentStore.

        SLA: 15 minutes (Issue #190). This is a corpus checkpoint — it reports the
        number of documents in the warehouse after the harvest.
        """
        from src.database.local_analytics_connector import get_shared_connection

        conn = get_shared_connection()
        total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        print(f"✅ Documents in warehouse: {total} "
              f"(inserted this run: {scrape_result['totals']['inserted']})")
        return {"documents_in_warehouse": total}

    @task
    def nlp(clean_result: Dict[str, Any], **context) -> Dict[str, Any]:
        """Enrich un-enriched documents (sentiment + topics) into document_enrichments.

        Replaces the former mock NLP; delegates to the gate-tested
        ``src.ingestion.enrich`` pass (pluggable analyzer, lexicon default).
        """
        from src.database.local_analytics_connector import get_shared_connection
        from src.ingestion.enrich import enrich_documents
        from src.ingestion.argument_mining import mine_unprocessed_documents

        conn = get_shared_connection()
        enriched = enrich_documents(conn)
        mined = mine_unprocessed_documents(conn)
        print(f"✅ Enriched {enriched} documents into document_enrichments")
        print(f"✅ Argument-mined {mined['processed']} documents; pending={mined['documents_pending']}")
        return {"enriched": enriched, "argument_mining": mined}

    @task
    def analyze_arguments(nlp_result: Dict[str, Any], **context) -> Dict[str, Any]:
        """Run the argument-mining batches on the enriched corpus under DAG
        orchestration (retry + monitoring) instead of out-of-band APScheduler.

        Each batch delegates to its scheduler's ``run_once`` — conflict
        detection, stance aggregation, follow-through position tracking, and
        fact-check. A batch that fails is recorded and does not abort the others;
        the task returns per-batch summary counts. Imports are lazy so the DAG
        imports (for dag-check) without the argument-mining deps on the path.
        """
        import importlib

        batches = [
            ("conflicts", "src.argument_mining.conflict_scheduler"),
            ("stance", "src.argument_mining.stance_aggregator"),
            ("positions", "src.argument_mining.followthrough_scheduler"),
            ("factcheck", "src.argument_mining.factcheck_scheduler"),
        ]
        summary: Dict[str, Any] = {}
        for name, module_path in batches:
            try:
                result = importlib.import_module(module_path).run_once()
                summary[name] = result if result is not None else "ok"
                print(f"✅ argument-mining[{name}]: {summary[name]}")
            except Exception as exc:  # one batch never aborts the others
                summary[name] = {"error": str(exc)}
                print(f"⚠️ argument-mining[{name}] failed: {exc}")
        return summary

    @task
    def publish(nlp_result: Dict[str, Any], analyze_result: Optional[Dict[str, Any]] = None, **context) -> Dict[str, Any]:
        """Business-ready analytics summary over the real corpus.

        Aggregates counts by source_type and the sentiment distribution from
        the documents + document_enrichments tables, plus the argument-mining
        batch outcomes.
        """
        from src.database.local_analytics_connector import get_shared_connection

        conn = get_shared_connection()
        by_source_type = dict(
            conn.execute(
                "SELECT source_type, COUNT(*) FROM documents GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )
        by_sentiment = dict(
            conn.execute(
                "SELECT COALESCE(sentiment_label, 'unenriched'), COUNT(*) "
                "FROM document_enrichments GROUP BY 1 ORDER BY 1"
            ).fetchall()
        )
        summary = {
            "date": context.get("ds"),
            "documents_by_source_type": by_source_type,
            "documents_by_sentiment": by_sentiment,
            "enriched_this_run": nlp_result["enriched"],
            "argument_mining": analyze_result,
        }
        print(f"📊 Published corpus summary: {summary}")
        return summary

    # Define task dependencies using TaskFlow API
    scrape_result = scrape()
    clean_result = clean(scrape_result)
    nlp_result = nlp(clean_result)
    analyze_result = analyze_arguments(nlp_result)
    publish_result = publish(nlp_result, analyze_result)


# Create the DAG instance
news_pipeline_dag = news_pipeline()

# Configure MLflow integration (Issue #225)
news_pipeline_dag = configure_dag_for_mlflow(news_pipeline_dag)

# Configure SLA for the clean task (Issue #190)
# Set SLA to 15 minutes to demonstrate SLA monitoring
if hasattr(news_pipeline_dag, 'get_task') and news_pipeline_dag.get_task('clean', None):
    clean_task = news_pipeline_dag.get_task('clean')
    clean_task.sla = timedelta(minutes=15)
    
    # Add SLA miss callback for logging
    def sla_miss_callback(dag, task_list, blocking_task_list, slas, blocking_tis):
        """
        Callback function for SLA misses.
        Logs SLA violations for monitoring and alerting.
        """
        from airflow.utils.log.logging_mixin import LoggingMixin
        logger = LoggingMixin().log
        
        for sla in slas:
            logger.warning(
                f"🚨 SLA MISS: Task '{sla.task_id}' in DAG '{sla.dag_id}' "
                f"missed SLA. Expected by: {sla.execution_date + sla.sla}, "
                f"Actual completion: Not completed yet"
            )
            
        for blocking_ti in blocking_tis:
            logger.warning(
                f"⏳ BLOCKING: Task '{blocking_ti.task_id}' is blocking SLA compliance"
            )
    
    # Apply SLA miss callback to the DAG
    news_pipeline_dag.sla_miss_callback = sla_miss_callback
