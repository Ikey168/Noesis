# Airflow runtime

The supported runtime is **Airflow 3.3.1 on Python 3.11**.

See the [migration and setup guide](../../docs/development/airflow-3-migration.md) for installation, required environment values, upgrade/rollback steps and verification.

Both Dockerfiles use the repository root as their build context and install the canonical `airflow/requirements.txt`.
