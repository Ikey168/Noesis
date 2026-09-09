"""Airflow 3 deadline callback, importable by the triggerer."""

import logging


async def log_pipeline_deadline(context):
    """Report a pipeline that has not completed within its scheduled deadline."""
    logging.getLogger(__name__).warning("Pipeline deadline missed: %s", context)
