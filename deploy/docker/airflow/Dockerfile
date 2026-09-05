FROM apache/airflow:3.3.1-python3.11

# The build context is the repository root for both Compose entry points.
COPY --chown=airflow:root airflow/requirements.txt /opt/airflow/runtime-requirements.txt
RUN pip install --no-cache-dir -r /opt/airflow/runtime-requirements.txt \
    --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.3.1/constraints-3.11.txt \
    && pip check

COPY --chown=airflow:root . /opt/noesis
RUN pip install --no-cache-dir "apache-airflow==3.3.1" '/opt/noesis[ingestion]' \
    && pip check
ENV PYTHONPATH=/opt/noesis:/opt/airflow/plugins
COPY --chown=airflow:root airflow/dags /opt/airflow/dags
COPY --chown=airflow:root airflow/plugins /opt/airflow/plugins
