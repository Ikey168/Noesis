"""Exercise TaskFlow callables and provider initialization on Airflow 3."""

import importlib.util
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pytest

pytest.importorskip('airflow.sdk')

DAGS = Path(__file__).resolve().parents[2] / 'airflow/dags'


def load_dag_module(name):
    spec = importlib.util.spec_from_file_location(name, DAGS / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='module')
def news_dag():
    return load_dag_module('news_pipeline').news_pipeline_dag


def test_clean_reports_corpus_checkpoint(news_dag):
    conn = Mock()
    conn.execute.return_value.fetchone.return_value = (7,)
    with patch('src.database.local_analytics_connector.get_shared_connection', return_value=conn):
        result = news_dag.get_task('clean').python_callable({'totals': {'inserted': 2}})
    assert result == {'documents_in_warehouse': 7}
    conn.execute.assert_called_once_with('SELECT COUNT(*) FROM documents')


def test_publish_preserves_analytics_and_argument_results(news_dag):
    conn = Mock()
    conn.execute.return_value.fetchall.side_effect = [[('rss', 7)], [('neutral', 5)]]
    batches = {'conflicts': {'processed': 2}}
    with patch('src.database.local_analytics_connector.get_shared_connection', return_value=conn):
        result = news_dag.get_task('publish').python_callable(
            {'enriched': 5}, batches, ds='2026-09-05'
        )
    assert result == {
        'date': '2026-09-05', 'documents_by_source_type': {'rss': 7},
        'documents_by_sentiment': {'neutral': 5}, 'enriched_this_run': 5,
        'argument_mining': batches,
    }


def test_openlineage_provider_client_initializes_without_network():
    module = load_dag_module('test_openlineage_integration')
    assert module.test_openlineage_import() == 'OpenLineage integration test passed'


def test_example_pipeline_keeps_xcom_context():
    module = load_dag_module('example_pipeline')
    ti = Mock()
    ti.xcom_pull.return_value = {'records': 4}
    assert module.example_transform(task_instance=ti) == {'status': 'transformed', 'records': 4}
    ti.xcom_pull.assert_called_once_with(task_ids='extract')


def test_example_dag_executes_through_airflow_task_api():
    module = load_dag_module('example_pipeline')
    run = module.dag.test()
    assert run.state == 'success'


def test_mlflow_callbacks_use_airflow_3_run_fields(news_dag, monkeypatch):
    import mlflow_callbacks as callbacks

    mlflow = MagicMock()
    mlflow.start_run.return_value.__enter__.return_value.info.run_id = 'tracked-run'
    monkeypatch.setattr(callbacks, 'mlflow', mlflow)
    tracker = callbacks.AirflowMLflowCallbacks()
    tracker.mlflow_enabled = True
    monkeypatch.setattr(tracker, '_get_git_info', lambda: {})
    run = SimpleNamespace(
        run_id='scheduled-test', dag_id=news_dag.dag_id,
        logical_date=datetime(2026, 9, 5, tzinfo=timezone.utc),
        start_date=datetime.now(tz=timezone.utc),
    )
    context = {'dag': news_dag, 'dag_run': run}
    tracker.on_dag_start(context)
    assert tracker.parent_runs == {'scheduled-test': 'tracked-run'}
    tags = mlflow.set_tags.call_args.args[0]
    assert tags['execution_date'] == '2026-09-05T00:00:00+00:00'
    mlflow.log_params.assert_called_once()
    tracker.on_dag_success(context)
    assert not tracker.parent_runs
    metrics = mlflow.log_metrics.call_args.args[0]
    assert metrics['task_count'] == 5
    assert metrics['duration_seconds'] >= 0
