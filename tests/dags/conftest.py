"""DAG checks use an isolated Airflow database, not the application Postgres DB."""

import os
import sys
from pathlib import Path

import pytest

AIRFLOW_DIR = Path(__file__).resolve().parents[2] / 'airflow'
sys.path.insert(0, str(AIRFLOW_DIR / 'plugins'))
os.environ.setdefault('AIRFLOW__CORE__DAGS_FOLDER', str(AIRFLOW_DIR / 'dags'))


@pytest.fixture(scope='session', autouse=True)
def setup_test_env():
    """Override the repository's external application-database fixture."""
    yield
