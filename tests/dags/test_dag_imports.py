"""Import and serialize every shipped DAG against the supported Airflow runtime."""

from datetime import timedelta
from pathlib import Path

import pytest

pytest.importorskip('airflow.sdk')
from airflow.dag_processing.dagbag import DagBag
from airflow.serialization.serialized_objects import DagSerialization


@pytest.fixture(scope='module')
def dag_bag():
    bag = DagBag(dag_folder=str(Path(__file__).resolve().parents[2] / 'airflow/dags'))
    assert not bag.import_errors, bag.import_errors
    return bag


def test_all_shipped_dags_import(dag_bag):
    assert set(dag_bag.dags) == {
        'news_pipeline', 'neuronews_example_pipeline', 'test_openlineage_integration',
        'iceberg_weekly_compaction', 'iceberg_daily_snapshot_expiration',
    }


def test_all_dags_serialize(dag_bag):
    # Parsing alone does not exercise the scheduler/API serialization boundary.
    for dag in dag_bag.dags.values():
        restored = DagSerialization.from_dict(DagSerialization.to_dict(dag))
        assert restored.dag_id == dag.dag_id
        assert set(restored.task_ids) == set(dag.task_ids)
        for task in restored.tasks:
            assert task.upstream_task_ids == dag.get_task(task.task_id).upstream_task_ids


def test_news_pipeline_structure_and_retries(dag_bag):
    dag = dag_bag.dags['news_pipeline']
    assert dag.owner == 'neuronews'
    assert dag.max_active_runs == 1
    expected_dependencies = {
        'scrape': set(), 'clean': {'scrape'}, 'nlp': {'clean'},
        'analyze_arguments': {'nlp'}, 'publish': {'nlp', 'analyze_arguments'},
    }
    assert set(dag.task_ids) == set(expected_dependencies)
    for task in dag.tasks:
        assert task.upstream_task_ids == expected_dependencies[task.task_id]
        assert task.retries == 2
        assert task.retry_delay == timedelta(minutes=5)


def test_news_pipeline_deadline_survives_serialization(dag_bag):
    dag = dag_bag.dags['news_pipeline']
    payload = DagSerialization.to_dict(dag)
    assert dag.deadline[0].interval == timedelta(minutes=15)
    # Assert the alert is present in the scheduler payload, not merely attached
    # as an unused Python attribute (which was possible with the removed SLA API).
    alert = payload['dag']['deadline'][0]
    assert alert['reference']['reference_type'] == 'DagRunLogicalDateDeadline'
    assert alert['interval']['__data__'] == 900.0
    assert alert['callback']['__data__']['path'] == 'deadline_callbacks.log_pipeline_deadline'
