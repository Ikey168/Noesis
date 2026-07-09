# MLflow MLOps Suite for NeuroNews

A comprehensive MLflow-based MLOps solution for NeuroNews, providing standardized experiment tracking, model lifecycle management, and deployment automation.

## 🚀 Components

### 🔬 Experiment Tracking (`tracking.py`)
Standardized MLflow tracking with automatic tagging and validation.

### 📦 Model Registry (`registry.py`) 
Complete model lifecycle management with versioning and stage transitions.

### 📊 Experiment Organization
Structured experiments with naming conventions and validation.

## Features

### Experiment Tracking
- 🏷️ **Automatic Standard Tags**: git SHA, branch, environment, hostname, code version
- 🌍 **Environment Detection**: Automatically detects dev/ci/prod environments
- 📊 **Context Manager**: Simple `mlrun()` context manager for experiment tracking
- ⚙️ **Configurable**: Environment variables for tracking URI and experiment names
- 🧪 **Well Tested**: Comprehensive unit and integration tests

### Model Registry
- 📝 **Model Registration**: Standardized model registration with metadata
- 🔄 **Stage Management**: Controlled transitions (None → Staging → Production → Archived)
- ✅ **Performance Gates**: Automatic validation before production deployment
- 📈 **Version Comparison**: Performance tracking across model versions
- 🗄️ **Automated Archival**: Cleanup of old model versions
- 🚀 **Deployment Management**: Centralized deployment status and URIs

## Quick Start

### Experiment Tracking

```python
from services.mlops.tracking import mlrun
import mlflow

# Basic experiment tracking
with mlrun("my-experiment") as run:
    mlflow.log_param("learning_rate", 0.01)
    mlflow.log_metric("accuracy", 0.95)
    mlflow.log_artifact("model.pkl")
```

### Model Registry

```python
from services.mlops.registry import NeuroNewsModelRegistry, ModelMetadata

# Initialize registry
registry = NeuroNewsModelRegistry()

# Register model with metadata
metadata = ModelMetadata(
    name="neuro_sentiment_classifier",
    description="News sentiment classification model",
    tags={"team": "nlp", "algorithm": "random_forest"},
    owner="ml-team",
    use_case="sentiment_analysis",
    performance_metrics={"accuracy": 0.89, "f1_score": 0.85},
    deployment_target="production"
)

# Register and promote model
version = registry.register_model(
    model_uri="runs:/abc123/model",
    name="neuro_sentiment_classifier",
    metadata=metadata
)

# Promote to production (with performance validation)
registry.transition_model_stage(
    name="neuro_sentiment_classifier",
    version=version.version,
    stage=ModelStage.PRODUCTION,
    check_performance_gates=True
)
```
    mlflow.log_param("learning_rate", 0.01)
    mlflow.log_metric("accuracy", 0.95)
    mlflow.log_artifact("model.pkl")
```

## Standard Tags Applied

The `mlrun` context manager automatically applies these standard tags:

| Tag | Description | Example |
|-----|-------------|---------|
| `git.sha` | Current git commit SHA | `abc123def456` |
| `git.branch` | Current git branch | `feature/sentiment` |
| `env` | Environment (dev/ci/prod) | `dev` |
| `hostname` | Machine hostname | `ml-server-01` |
| `code_version` | Short git SHA or timestamp | `abc123de` |
| `pipeline` | Pipeline name (if set) | `sentiment-analyzer` |

## Environment Configuration

Configure MLflow through environment variables:

```bash
# MLflow server URL
export MLFLOW_TRACKING_URI="http://localhost:5001"

# Default experiment name
export MLFLOW_EXPERIMENT="news-sentiment"

# Pipeline identifier (optional)
export NEURONEWS_PIPELINE="data-preprocessing"
```

Or use the helper function:

```python
from services.mlops.tracking import setup_mlflow_env

setup_mlflow_env("http://localhost:5001", "my-experiment")
```

## Usage Examples

### Basic Experiment Tracking

```python
from services.mlops.tracking import mlrun
import mlflow

with mlrun("sentiment-training", experiment="news-nlp") as run:
    # Log hyperparameters
    mlflow.log_param("model_type", "bert-base-uncased")
    mlflow.log_param("learning_rate", 2e-5)
    mlflow.log_param("batch_size", 32)
    
    # Log metrics during training
    for epoch in range(3):
        accuracy = train_epoch()  # Your training code
        mlflow.log_metric("accuracy", accuracy, step=epoch)
    
    # Log final model
    mlflow.log_artifact("model.pkl")
```

### Custom Tags

```python
custom_tags = {
    "model_version": "v2.1.0",
    "data_source": "twitter",
    "experiment_type": "hyperparameter_tuning"
}

with mlrun("bert-tuning", tags=custom_tags) as run:
    mlflow.log_param("learning_rate", 1e-4)
    mlflow.log_metric("f1_score", 0.89)
```

### Pipeline Context

```python
import os

# Set pipeline context
os.environ["NEURONEWS_PIPELINE"] = "data-preprocessing"

with mlrun("data-cleaning") as run:
    mlflow.log_param("input_format", "json")
    mlflow.log_param("output_format", "parquet")
    mlflow.log_metric("records_processed", 10000)
    
# Pipeline tag will be automatically added
```

## Environment Detection

The helper automatically detects the environment:

- **CI**: Detected when `CI`, `GITHUB_ACTIONS`, `GITLAB_CI`, `JENKINS_URL`, or `TRAVIS` environment variables are present
- **Production**: Detected when `ENV=production` or `ENVIRONMENT=prod`
- **Development**: Default fallback for local development

## Testing

Run the tests:

```bash
# Unit tests only
pytest tests/mlops -m "not integration"

# All tests (requires MLflow server)
make mlflow-up  # Start MLflow server
pytest tests/mlops

# Quick test
pytest -q tests/mlops
```

## Integration with NeuroNews Pipelines

The tracking helper integrates seamlessly with NeuroNews ML pipelines:

### Sentiment Analysis Pipeline

```python
from services.mlops.tracking import mlrun
import mlflow

def train_sentiment_model(data_path, model_config):
    with mlrun("sentiment-bert-training", experiment="news-sentiment") as run:
        # Log data and model configuration
        mlflow.log_param("data_path", data_path)
        mlflow.log_params(model_config)
        
        # Training code here...
        model = train_model(data_path, model_config)
        
        # Log results
        mlflow.log_metric("final_accuracy", model.accuracy)
        mlflow.log_artifact(model.save_path)
        
        return model
```

### Data Processing Pipeline

```python
import os
from services.mlops.tracking import mlrun
import mlflow

def process_news_data(input_path, output_path):
    os.environ["NEURONEWS_PIPELINE"] = "data-preprocessing"
    
    with mlrun("news-data-processing") as run:
        mlflow.log_param("input_path", input_path)
        mlflow.log_param("output_path", output_path)
        
        # Processing logic...
        result = process_data(input_path, output_path)
        
        mlflow.log_metric("records_processed", result.record_count)
        mlflow.log_metric("processing_time_seconds", result.duration)
        
        return result
```

## API Reference

### `mlrun(name, experiment=None, tags=None)`

Context manager for MLflow runs with standardized tagging.

**Parameters:**
- `name` (str): Name for the MLflow run
- `experiment` (str, optional): Experiment name (overrides environment)
- `tags` (dict, optional): Additional custom tags

**Returns:**
- MLflow active run object

### `setup_mlflow_env(tracking_uri, experiment)`

Helper to setup MLflow environment variables.

**Parameters:**
- `tracking_uri` (str): MLflow tracking server URI
- `experiment` (str): Default experiment name

### `get_current_run_info()`

Get information about the current MLflow run context.

**Returns:**
- Dictionary with current run information

## Error Handling

The tracking helper is designed to be robust:

- **Offline Development**: Works without MLflow server (logs warnings)
- **Git Unavailable**: Falls back to timestamp-based versioning
- **Network Issues**: Gracefully handles MLflow server connectivity issues

## Requirements

- `mlflow >= 2.0.0`
- `python >= 3.8`

## Contributing

When adding new features:

1. Add unit tests to `tests/mlops/test_tracking.py`
2. Update this documentation
3. Ensure all tests pass: `pytest tests/mlops`

## Troubleshooting

### MLflow Server Not Available

```
Warning: Could not setup MLflow experiment 'test': Connection refused
```

**Solution**: Start the MLflow server:
```bash
make mlflow-up
```

### Permission Denied for Artifacts

```
Warning: MLflow run failed: [Errno 13] Permission denied: '/mlflow'
```

**Solution**: Check MLflow server artifact storage configuration in `docker/mlflow/docker-compose.mlflow.yml`.

### Git Information Not Available

```
git.sha: unknown
git.branch: unknown
```

**Solution**: Ensure you're running in a git repository, or the warnings can be ignored for non-git environments.

## Model Registry

The model registry provides comprehensive model lifecycle management. See [Model Registry Documentation](../../docs/subsystems/mlops/model-registry.md) for detailed usage.

### Standard Models

| Model Name | Description | Performance Gates |
|------------|-------------|------------------|
| `neuro_sentiment_classifier` | News sentiment classification | accuracy > 0.85, f1_score > 0.80 |
| `neuro_embeddings_encoder` | Document embedding generation | similarity_score > 0.75, recall@10 > 0.70 |
| `neuro_rag_retriever` | RAG retrieval and ranking | relevance_score > 0.80, answer_quality > 0.75 |
| `neuro_topic_extractor` | Topic and keyword extraction | - |
| `neuro_summarizer` | Article summarization | - |
| `neuro_clustering_engine` | Article clustering | - |

### Helper Functions

```python
from services.mlops.registry import (
    register_model_from_run,
    promote_to_production,
    get_production_model_uri
)

# Register model from run
version = register_model_from_run("run_id", "model_name")

# Promote to production
promote_to_production("model_name", "1")

# Get production URI for inference
uri = get_production_model_uri("model_name")
model = mlflow.pyfunc.load_model(uri)
```

## Documentation

- **Experiment Tracking**: [experiments.md](../../docs/subsystems/mlops/experiments.md)
- **Model Registry**: [model_registry.md](../../docs/subsystems/mlops/model-registry.md)
- **MLflow Setup**: [docker/mlflow/README.md](../../docker/mlflow/README.md)
