"""
MLflow Tracking Helper for NeuroNews.

This module provides a standardized way to track ML experiments with MLflow,
automatically setting common tags, parameters, and metadata according to
the NeuroNews experiment structure and naming conventions.

Implements Issue #220: Experiment structure & naming conventions
"""

import os
import socket
import subprocess
from contextlib import contextmanager
from typing import Dict, Optional, Any, List
import mlflow
import mlflow.tracking
from datetime import datetime
import warnings

from src.config.env import resolve_env


# Standard experiment names (Issue #220)
STANDARD_EXPERIMENTS = {
    "neuro_news_indexing": "Document embedding generation, vector storage, and retrieval optimization",
    "neuro_news_ask": "Question-answering pipeline performance and accuracy tracking", 
    "research_prototypes": "Experimental features, research ideas, and proof-of-concept implementations"
}

# Required tags for all runs (Issue #220)
REQUIRED_TAGS = ["git.sha", "env", "pipeline", "data_version"]

# Valid environment values
VALID_ENVIRONMENTS = ["dev", "staging", "prod"]


def validate_experiment_name(experiment_name: str) -> bool:
    """
    Validate that experiment name follows naming conventions.
    
    Args:
        experiment_name: Name of the experiment to validate
        
    Returns:
        True if valid, False otherwise
    """
    if experiment_name in STANDARD_EXPERIMENTS:
        return True
    
    # Allow additional experiments but warn
    warnings.warn(
        f"Experiment '{experiment_name}' is not in standard experiments: {list(STANDARD_EXPERIMENTS.keys())}. "
        "Consider using a standard experiment or updating the documentation.",
        UserWarning
    )
    return True


def validate_required_tags(tags: Dict[str, Any]) -> List[str]:
    """
    Validate that all required tags are present.
    
    Args:
        tags: Dictionary of tags to validate
        
    Returns:
        List of missing required tags
    """
    missing_tags = []
    for required_tag in REQUIRED_TAGS:
        if required_tag not in tags or not tags[required_tag]:
            missing_tags.append(required_tag)
    
    return missing_tags


def validate_environment(env: str) -> bool:
    """
    Validate environment value.
    
    Args:
        env: Environment string to validate
        
    Returns:
        True if valid, False otherwise
    """
    return env in VALID_ENVIRONMENTS


def generate_run_name(component: str, descriptor: Optional[str] = None) -> str:
    """
    Generate standardized run name following convention: {component}_{timestamp}_{optional_descriptor}
    
    Args:
        component: Component name (e.g., 'embeddings_indexer', 'rag_answerer')
        descriptor: Optional descriptor for the run
        
    Returns:
        Formatted run name
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    if descriptor:
        return f"{component}_{timestamp}_{descriptor}"
    else:
        return f"{component}_{timestamp}"


def _get_git_info() -> Dict[str, str]:
    """Get current git information."""
    git_info = {}
    
    try:
        # Get git commit SHA
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        git_info["sha"] = result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        git_info["sha"] = "unknown"
    
    try:
        # Get git branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True
        )
        git_info["branch"] = result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        git_info["branch"] = "unknown"
    
    return git_info


def _get_environment() -> str:
    """Determine the current environment (dev/staging/prod)."""
    # Check explicit environment variable first
    env = resolve_env("ENV")
    if env and validate_environment(env):
        return env
    
    # Check common CI environment variables
    if any(env_var in os.environ for env_var in [
        "CI", "CONTINUOUS_INTEGRATION", "GITHUB_ACTIONS", 
        "GITLAB_CI", "JENKINS_URL", "TRAVIS"
    ]):
        return "ci"
    
    # Check for production indicators
    if os.environ.get("ENV") == "production" or os.environ.get("ENVIRONMENT") == "prod":
        return "prod"
    
    # Default to development
    return "dev"


def _get_code_version() -> str:
    """Get a code version identifier."""
    git_info = _get_git_info()
    if git_info["sha"] != "unknown":
        return git_info["sha"][:8]  # Short SHA
    
    # Fallback to timestamp if no git
    import time
    return f"local-{int(time.time())}"


def _setup_mlflow_tracking():
    """Setup MLflow tracking URI and experiment from environment variables."""
    # Set tracking URI from environment or default to local
    tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001")
    mlflow.set_tracking_uri(tracking_uri)
    
    # Set experiment from environment or create default
    experiment_name = os.environ.get("MLFLOW_EXPERIMENT", "neuronews-default")
    
    try:
        # Try to get existing experiment
        experiment = mlflow.get_experiment_by_name(experiment_name)
        if experiment is None:
            # Create experiment if it doesn't exist
            experiment_id = mlflow.create_experiment(experiment_name)
        else:
            experiment_id = experiment.experiment_id
        
        mlflow.set_experiment(experiment_name)
        return experiment_id
    except Exception as e:
        # If MLflow server is not available, log warning but continue
        print(f"Warning: Could not setup MLflow experiment '{experiment_name}': {e}")
        return None


@contextmanager
def mlrun(name: str, experiment: Optional[str] = None, tags: Optional[Dict[str, Any]] = None, 
          validate_tags: bool = True):
    """
    Context manager for MLflow runs with standardized tagging and validation.
    
    Implements Issue #220 experiment structure and naming conventions.
    
    Args:
        name: Name for the MLflow run (should follow: {component}_{timestamp}_{optional_descriptor})
        experiment: Optional experiment name (should be one of the standard experiments)
        tags: Additional custom tags to add
        validate_tags: Whether to validate required tags (default: True)
    
    Yields:
        MLflow active run object
    
    Raises:
        ValueError: If required tags are missing or invalid values provided
    
    Example:
        with mlrun(
            name="embeddings_indexer_20250825_143021",
            experiment="neuro_news_indexing",
            tags={
                "data_version": "v1.2.3",
                "notes": "Baseline embedding generation run",
                "model_type": "transformer",
                "task_type": "embedding"
            }
        ) as run:
            mlflow.log_param("model_name", "sentence-transformers/all-MiniLM-L6-v2")
            mlflow.log_metric("accuracy", 0.95)
    """
    # Setup MLflow tracking
    _setup_mlflow_tracking()
    
    # Validate experiment name if provided
    if experiment:
        validate_experiment_name(experiment)
        try:
            mlflow.set_experiment(experiment)
        except Exception as e:
            print(f"Warning: Could not set experiment '{experiment}': {e}")
    
    # Gather standard tags and metadata
    git_info = _get_git_info()
    env = _get_environment()
    
    # Build standard tags (required by Issue #220)
    standard_tags = {
        "git.sha": git_info["sha"],
        "git.branch": git_info["branch"], 
        "env": env,
        "hostname": socket.gethostname(),
        "code_version": _get_code_version(),
    }
    
    # Add pipeline tag if specified in environment
    pipeline = resolve_env("PIPELINE")
    if pipeline:
        standard_tags["pipeline"] = pipeline
    
    # Add data_version from environment if not in custom tags
    data_version = resolve_env("DATA_VERSION")
    if data_version and (not tags or "data_version" not in tags):
        standard_tags["data_version"] = data_version
    
    # Merge with custom tags
    final_tags = standard_tags.copy()
    if tags:
        final_tags.update(tags)
    
    # Validate required tags if requested
    if validate_tags:
        missing_tags = validate_required_tags(final_tags)
        if missing_tags:
            missing_list = ", ".join(missing_tags)
            raise ValueError(
                f"Missing required tags: {missing_list}. "
                f"Required tags: {REQUIRED_TAGS}. "
                f"Set missing tags in the tags parameter or environment variables "
                f"(NOESIS_PIPELINE, NOESIS_DATA_VERSION)."
            )
        
        # Validate environment value
        if not validate_environment(final_tags["env"]):
            raise ValueError(
                f"Invalid environment '{final_tags['env']}'. "
                f"Valid environments: {VALID_ENVIRONMENTS}. "
                f"Set NOESIS_ENV environment variable."
            )
    
    # Start MLflow run
    try:
        with mlflow.start_run(run_name=name, tags=final_tags) as run:
            # Log standard parameters
            mlflow.log_param("run_name", name)
            mlflow.log_param("git_sha", git_info["sha"])
            mlflow.log_param("git_branch", git_info["branch"])
            mlflow.log_param("environment", env)
            mlflow.log_param("hostname", socket.gethostname())
            
            # Log experiment info if available
            if experiment:
                mlflow.log_param("experiment_name", experiment)
                if experiment in STANDARD_EXPERIMENTS:
                    mlflow.log_param("experiment_description", STANDARD_EXPERIMENTS[experiment])

            yield run
    except Exception as e:
        print(f"Warning: MLflow run failed: {e}")
        # Create a mock run object for offline development
        class MockRun:
            def __init__(self):
                self.info = type('RunInfo', (), {
                    'run_id': 'mock-run-id',
                    'experiment_id': 'mock-experiment-id',
                    'status': 'RUNNING'
                })()
        
        yield MockRun()


def setup_mlflow_env(tracking_uri: str = "http://localhost:5001", experiment: str = "neuronews-default"):
    """
    Helper function to setup MLflow environment variables.
    
    Args:
        tracking_uri: MLflow tracking server URI
        experiment: Default experiment name
    """
    os.environ["MLFLOW_TRACKING_URI"] = tracking_uri
    os.environ["MLFLOW_EXPERIMENT"] = experiment
    
    print(f"MLflow environment setup:")
    print(f"  Tracking URI: {tracking_uri}")
    print(f"  Default Experiment: {experiment}")


def get_current_run_info() -> Dict[str, Any]:
    """
    Get information about the current MLflow run context.
    
    Returns:
        Dictionary with current run information
    """
    try:
        active_run = mlflow.active_run()
        if active_run:
            return {
                "run_id": active_run.info.run_id,
                "experiment_id": active_run.info.experiment_id,
                "status": active_run.info.status,
                "tracking_uri": mlflow.get_tracking_uri(),
            }
        else:
            return {"status": "no_active_run"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
