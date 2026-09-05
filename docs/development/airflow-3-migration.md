# Airflow 3.3.1 security migration

The runtime now requires Airflow 3.3.1, addressing [CVE-2026-33264](https://github.com/advisories/GHSA-2943-9672-r45w) (unsafe serialized-DAG deserialization, fixed in 3.3.0). The obsolete Airflow/provider exceptions have been removed from `.trivyignore`; the security gate remains enabled.

## Runtime and DAG changes

`airflow/requirements.txt` is the canonical runtime manifest. The manifests under `docker/airflow` and `deploy/docker/airflow` include it. CI and both Dockerfiles install it with Apache's Python 3.11 constraints for 3.3.1. Application ingestion dependencies are installed separately with an explicit Airflow pin and `pip check`. The old broad manifest mixed incompatible Airflow 2 providers, obsolete `openlineage-airflow`, and unrelated application/model dependencies; DAG CI silently ignored installation failures. Runtime requirements now contain the providers used by the shipped DAGs/bootstrap and MLflow's tracking client. Install additional model or connector extras explicitly when deploying those workloads.

DAGs use `airflow.sdk`, standard-provider operators and `schedule`. OpenLineage uses the supported Airflow provider. Tests parse all five DAGs, round-trip their serialized form, verify news task dependencies/retries and run representative task callables/provider initialization.

Airflow 3 removed task SLAs. The old clean-task SLA callback is replaced by a **DAG deadline alert 15 minutes after the scheduled logical date**, logged by the triggerer. This monitors completion of the whole news pipeline and does not terminate a task. This is a change from the former clean-task-only SLA. See [Apache's deadline documentation](https://airflow.apache.org/docs/apache-airflow/3.3.1/howto/deadline-alerts.html).

## Existing installations

Follow [Apache's upgrade guide](https://airflow.apache.org/docs/apache-airflow/3.3.1/installation/upgrading_to_airflow3.html). Stop the existing Airflow processes and back up the metadata database and configuration before migration. Keep the existing Fernet key so stored connections remain decryptable. A rollback requires restoring the pre-migration database backup alongside the old image; do not run Airflow 2 against the migrated schema.

Both Compose entry points now build from the repository root and run an API server, scheduler, DAG processor and triggerer with LocalExecutor. The UI/API service is renamed from `airflow-webserver` to `airflow-apiserver`. Its health endpoint is `/api/v2/monitor/health`. Initialization uses `airflow db migrate` through `_AIRFLOW_DB_MIGRATE` and retains FAB user management.

Set these in a private Compose `.env` file before starting:

- `AIRFLOW_FERNET_KEY`: the existing key when upgrading; generate one for a fresh instance.
- `AIRFLOW_API_JWT_SECRET`: one strong random value shared by all Airflow services.
- `_AIRFLOW_WWW_USER_PASSWORD`: the initial administrator password for a fresh instance.
- `AIRFLOW_UID`: your local UID for bind-mounted directories, if needed.

From the repository root:

```sh
docker compose -f docker/airflow/docker-compose.airflow.yml build
docker compose -f docker/airflow/docker-compose.airflow.yml up airflow-init
docker compose -f docker/airflow/docker-compose.airflow.yml up -d
```

The alternative entry point is `deploy/docker/airflow/docker-compose.airflow.yml`. Use one entry point per installation. Existing Postgres/Marquez volumes are retained; no live installation is migrated by merging this code.

## Local verification

Use an isolated Python 3.11 environment:

```sh
pip install -r airflow/requirements.txt \
  --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.3.1/constraints-3.11.txt
pip install 'apache-airflow==3.3.1' '.[ingestion]' pytest pyarrow
pip check
export AIRFLOW_HOME="$(mktemp -d)"
export AIRFLOW__CORE__LOAD_EXAMPLES=False
export AIRFLOW__OPENLINEAGE__DISABLED=True
export MLFLOW_DISABLED=true
export PYTHONPATH="$PWD:$PWD/airflow/plugins"
airflow db migrate
python -m pytest tests/dags -q
```

Verified for this change: constrained installation plus `pip check`; fresh metadata migration; ten DAG tests with no skips, including serialization and task-API execution; both Compose configurations; a custom-image build and isolated example-DAG execution; and the repository Trivy high/critical gate with the Airflow exceptions removed.
