"""
Airflow ↔ MLflow Integration Plugin (Issue #225)

This module provides Airflow callbacks to integrate DAG and task execution
with MLflow tracking, creating parent runs for DAGs and child runs for tasks.

Features:
- DAG-level MLflow run tracking with standardized tags
- Task-level nested runs for detailed pipeline visibility
- Environment-based MLflow enabling/disabling 
- Integration with NeuroNews MLflow tracking conventions
- Proper error handling and resource cleanup
"""

import os
import logging
from contextlib import contextmanager
from typing import Optional, Dict, Any
from datetime import datetime

from airflow.sdk import DAG, Context

# MLflow imports with fallback handling
try:
    import mlflow
    import mlflow.tracking
    MLFLOW_AVAILABLE = True
except ImportError:
    MLFLOW_AVAILABLE = False
    logging.warning("MLflow not available - Airflow MLflow integration disabled")

# Import NeuroNews MLflow utilities if available
try:
    import sys
    sys.path.append("/opt/airflow/plugins")
    sys.path.append("/workspaces/NeuroNews")
    from services.mlops.tracking import MLflowHelper, STANDARD_EXPERIMENTS
    NEURONEWS_MLFLOW_AVAILABLE = True
except ImportError:
    NEURONEWS_MLFLOW_AVAILABLE = False
    logging.warning("NeuroNews MLflow utilities not available - using basic MLflow integration")


logger = logging.getLogger(__name__)


class AirflowMLflowCallbacks:
    """
    Airflow callbacks for MLflow integration.
    
    Provides DAG and task-level callbacks to track Airflow pipeline execution
    in MLflow with proper parent-child run relationships.
    """
    
    def __init__(self):
        """Initialize MLflow callbacks with environment configuration."""
        self.mlflow_enabled = self._is_mlflow_enabled()
        self.parent_runs: Dict[str, str] = {}  # dag_run_id -> mlflow_run_id
        
        if self.mlflow_enabled and MLFLOW_AVAILABLE:
            self._configure_mlflow()
    
    def _is_mlflow_enabled(self) -> bool:
        """Check if MLflow tracking is enabled via environment variables."""
        # Respect MLFLOW_DISABLED=true env to turn off in CI
        if os.getenv("MLFLOW_DISABLED", "").lower() == "true":
            logger.info("MLflow tracking disabled via MLFLOW_DISABLED environment variable")
            return False
        
        if not MLFLOW_AVAILABLE:
            logger.warning("MLflow not available - tracking disabled")
            return False
        
        return True
    
    def _configure_mlflow(self):
        """Configure MLflow tracking URI and settings."""
        # Use environment-specific tracking URI or default
        tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "file:./mlruns")
        mlflow.set_tracking_uri(tracking_uri)
        logger.info(f"MLflow tracking URI configured: {tracking_uri}")
    
    def _get_git_info(self) -> Dict[str, str]:
        """Get Git information for run tagging."""
        git_info = {}
        try:
            import subprocess
            
            # Get Git SHA
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                git_info["git.sha"] = result.stdout.strip()
            
            # Get Git branch
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                git_info["git.branch"] = result.stdout.strip()
        
        except Exception as e:
            logger.warning(f"Failed to get Git information: {e}")
        
        return git_info
    
    def on_dag_start(self, context: Context):
        """
        Callback triggered on DAG start.
        
        Creates a parent MLflow run for the entire DAG execution with
        tags: dag_id, run_id, execution_date.
        """
        if not self.mlflow_enabled:
            return
        
        dag_run = context.get("dag_run")
        dag = context.get("dag")
        
        if not dag_run or not dag:
            logger.warning("DAG or DAG run not available in context")
            return
        
        try:
            # Determine experiment name based on DAG tags or use default
            experiment_name = self._get_experiment_name(dag)
            
            # Set or create experiment
            try:
                mlflow.set_experiment(experiment_name)
            except Exception:
                # Create experiment if it doesn't exist
                mlflow.create_experiment(experiment_name)
                mlflow.set_experiment(experiment_name)
            
            # Start parent run for DAG
            run_name = f"dag_{dag.dag_id}_{dag_run.run_id}"
            
            with mlflow.start_run(run_name=run_name) as run:
                # Store run ID for child tasks
                self.parent_runs[dag_run.run_id] = run.info.run_id
                
                # Set standard tags
                tags = {
                    "dag_id": dag.dag_id,
                    "run_id": dag_run.run_id,
                    "execution_date": (dag_run.logical_date.isoformat() if dag_run.logical_date else ""),
                    "environment": os.getenv("AIRFLOW_ENV", "dev"),
                    "pipeline": "airflow",
                    "dag_description": dag.description or "",
                    "airflow_version": context.get("airflow", {}).get("version", "unknown"),
                    "start_time": datetime.now().isoformat()
                }
                
                # Add Git information
                tags.update(self._get_git_info())
                
                # Add DAG-specific tags
                if hasattr(dag, 'tags') and dag.tags:
                    tags["dag_tags"] = ",".join(dag.tags)
                
                mlflow.set_tags(tags)
                
                # Log DAG parameters
                params = {
                    "schedule_interval": str(dag.timetable),
                    "max_active_runs": dag.max_active_runs,
                    "catchup": dag.catchup,
                    "is_paused_upon_creation": dag.is_paused_upon_creation,
                    "task_count": len(dag.task_ids)
                }
                
                mlflow.log_params(params)
                
                logger.info(f"Started MLflow parent run for DAG {dag.dag_id}: {run.info.run_id}")
        
        except Exception as e:
            logger.error(f"Failed to start MLflow run for DAG {dag.dag_id}: {e}")
    
    def on_dag_success(self, context: Context):
        """Callback triggered on DAG success."""
        self._finalize_dag_run(context, "SUCCESS")
    
    def on_dag_failure(self, context: Context):
        """Callback triggered on DAG failure.""" 
        self._finalize_dag_run(context, "FAILED")
    
    def _finalize_dag_run(self, context: Context, status: str):
        """Finalize DAG MLflow run with status and metrics."""
        if not self.mlflow_enabled:
            return
        
        dag_run = context.get("dag_run")
        if not dag_run or dag_run.run_id not in self.parent_runs:
            return
        
        try:
            parent_run_id = self.parent_runs[dag_run.run_id]
            
            with mlflow.start_run(run_id=parent_run_id):
                # Log final metrics
                end_time = datetime.now(tz=dag_run.start_date.tzinfo)
                duration_seconds = (end_time - dag_run.start_date).total_seconds()
                
                mlflow.log_metrics({
                    "duration_seconds": duration_seconds,
                    "task_count": len(context["dag"].task_ids),
                    "end_time": end_time.timestamp()
                })
                
                # Update status tag
                mlflow.set_tag("status", status)
                mlflow.set_tag("end_time", end_time.isoformat())
                
                logger.info(f"Finalized MLflow run for DAG {dag_run.dag_id} with status {status}")
            
            # Clean up parent run reference
            del self.parent_runs[dag_run.run_id]
        
        except Exception as e:
            logger.error(f"Failed to finalize MLflow run for DAG: {e}")
    
    def on_task_start(self, context: Context):
        """
        Callback triggered on task start.
        
        Creates a child MLflow run nested under the DAG parent run.
        """
        if not self.mlflow_enabled:
            return
        
        task_instance = context.get("task_instance")
        dag_run = context.get("dag_run")
        
        if not task_instance or not dag_run:
            return
        
        parent_run_id = self.parent_runs.get(dag_run.run_id)
        if not parent_run_id:
            logger.warning(f"No parent run found for DAG {dag_run.dag_id}")
            return
        
        try:
            # Create nested run name
            run_name = f"task_{task_instance.task_id}_{task_instance.try_number}"
            
            # Start nested run
            nested_run = mlflow.start_run(
                run_name=run_name,
                nested=True
            )
            
            # Store nested run ID on task instance for later use
            if hasattr(task_instance, 'xcom_push'):
                task_instance.xcom_push(key="mlflow_run_id", value=nested_run.info.run_id)
            
            # Set task-specific tags
            tags = {
                "task_id": task_instance.task_id,
                "try_number": task_instance.try_number,
                "operator": task_instance.task.__class__.__name__,
                "parent_run_id": parent_run_id,
                "start_time": datetime.now().isoformat()
            }
            
            mlflow.set_tags(tags)
            
            # Log task parameters
            params = {
                "max_tries": task_instance.max_tries,
                "pool": task_instance.pool,
                "queue": task_instance.queue or "default"
            }
            
            mlflow.log_params(params)
            
            logger.info(f"Started MLflow nested run for task {task_instance.task_id}: {nested_run.info.run_id}")
        
        except Exception as e:
            logger.error(f"Failed to start MLflow run for task {task_instance.task_id}: {e}")
    
    def on_task_success(self, context: Context):
        """Callback triggered on task success."""
        self._finalize_task_run(context, "SUCCESS")
    
    def on_task_failure(self, context: Context):
        """Callback triggered on task failure."""
        self._finalize_task_run(context, "FAILED")
    
    def _finalize_task_run(self, context: Context, status: str):
        """Finalize task MLflow run with status and metrics."""
        if not self.mlflow_enabled:
            return
        
        task_instance = context.get("task_instance")
        if not task_instance:
            return
        
        try:
            # Get nested run ID from XCom
            nested_run_id = None
            if hasattr(task_instance, 'xcom_pull'):
                nested_run_id = task_instance.xcom_pull(key="mlflow_run_id")
            
            if nested_run_id:
                with mlflow.start_run(run_id=nested_run_id):
                    # Log task metrics
                    end_time = datetime.now(
                        tz=task_instance.start_date.tzinfo if task_instance.start_date else None
                    )
                    if task_instance.start_date:
                        duration_seconds = (end_time - task_instance.start_date).total_seconds()
                        mlflow.log_metric("duration_seconds", duration_seconds)
                    
                    # Update status
                    mlflow.set_tag("status", status)
                    mlflow.set_tag("end_time", end_time.isoformat())
                    
                    # Log any exception information on failure
                    if status == "FAILED" and hasattr(context, 'exception'):
                        exception = context.get('exception')
                        if exception:
                            mlflow.set_tag("error_message", str(exception))
                    
                    logger.info(f"Finalized MLflow run for task {task_instance.task_id} with status {status}")
            
            # End the nested run
            if nested_run_id:
                mlflow.end_run()
        
        except Exception as e:
            logger.error(f"Failed to finalize MLflow run for task {task_instance.task_id}: {e}")
    
    def _get_experiment_name(self, dag: DAG) -> str:
        """
        Determine MLflow experiment name based on DAG configuration.
        
        Args:
            dag: Airflow DAG object
            
        Returns:
            Experiment name to use
        """
        # Check if DAG has mlflow_experiment tag
        if hasattr(dag, 'tags') and dag.tags:
            for tag in dag.tags:
                if tag.startswith('mlflow:'):
                    return tag.replace('mlflow:', '')
        
        # Use DAG ID-based experiment name
        experiment_name = f"airflow_{dag.dag_id}"
        
        # If NeuroNews MLflow utilities are available, validate against standards
        if NEURONEWS_MLFLOW_AVAILABLE and hasattr(STANDARD_EXPERIMENTS, '__contains__'):
            if experiment_name not in STANDARD_EXPERIMENTS:
                # Use research_prototypes for non-standard experiments
                return "research_prototypes"
        
        return experiment_name


# Global instance for use in DAGs
mlflow_callbacks = AirflowMLflowCallbacks()


# Convenience functions for DAG configuration
def on_dag_start(context: Context):
    """Convenience function for DAG start callback."""
    mlflow_callbacks.on_dag_start(context)


def on_dag_success(context: Context):
    """Convenience function for DAG success callback."""
    mlflow_callbacks.on_dag_success(context)


def on_dag_failure(context: Context): 
    """Convenience function for DAG failure callback."""
    mlflow_callbacks.on_dag_failure(context)


def on_task_start(context: Context):
    """Convenience function for task start callback."""
    mlflow_callbacks.on_task_start(context)


def on_task_success(context: Context):
    """Convenience function for task success callback."""
    mlflow_callbacks.on_task_success(context)


def on_task_failure(context: Context):
    """Convenience function for task failure callback."""
    mlflow_callbacks.on_task_failure(context)


@contextmanager
def mlflow_dag_context(dag: DAG):
    """
    Context manager for DAG-level MLflow tracking.
    
    Usage:
        with mlflow_dag_context(dag):
            # DAG execution with MLflow tracking
            pass
    """
    if mlflow_callbacks.mlflow_enabled:
        try:
            # Could add pre-execution setup here if needed
            yield
        finally:
            # Could add cleanup here if needed
            pass
    else:
        yield


def configure_dag_for_mlflow(dag: DAG) -> DAG:
    """
    Configure a DAG with MLflow callbacks.
    
    Args:
        dag: Airflow DAG to configure
        
    Returns:
        Configured DAG with MLflow callbacks
    """
    if not mlflow_callbacks.mlflow_enabled:
        return dag
    
    # Add MLflow callbacks to DAG
    dag.on_success_callback = on_dag_success
    dag.on_failure_callback = on_dag_failure
    
    # Note: DAG start callback is not directly supported in Airflow
    # It would need to be added to individual tasks or operators
    
    return dag


def configure_task_for_mlflow(task, **kwargs):
    """
    Configure a task with MLflow callbacks.
    
    Args:
        task: Airflow task/operator to configure
        **kwargs: Additional configuration
        
    Returns:
        Configured task with MLflow callbacks
    """
    if not mlflow_callbacks.mlflow_enabled:
        return task
    
    # Add MLflow callbacks to task
    task.on_success_callback = on_task_success
    task.on_failure_callback = on_task_failure
    
    # Pre and post execute callbacks for task tracking
    original_pre_execute = getattr(task, 'pre_execute', None)
    original_post_execute = getattr(task, 'post_execute', None)
    
    def pre_execute_with_mlflow(context):
        on_task_start(context)
        if original_pre_execute:
            original_pre_execute(context)
    
    def post_execute_with_mlflow(context, result):
        if original_post_execute:
            original_post_execute(context, result)
        # Task success is handled by on_success_callback
    
    task.pre_execute = pre_execute_with_mlflow
    task.post_execute = post_execute_with_mlflow
    
    return task
