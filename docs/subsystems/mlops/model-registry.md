# MLflow Model Registry Implementation

**Issue #221**: Complete model lifecycle management with MLflow Model Registry for NeuroNews project.

## 📋 Overview

This implementation provides a comprehensive MLflow Model Registry system for managing the complete lifecycle of machine learning models in the NeuroNews project. It includes model registration, versioning, stage transitions, performance validation, and deployment management.

## 🎯 Features

### Core Functionality
- **Model Registration**: Register models with standardized metadata and tags
- **Version Management**: Automatic versioning with comprehensive tracking
- **Stage Transitions**: Controlled promotion through None → Staging → Production → Archived
- **Performance Gates**: Automated validation before production deployment
- **Deployment Management**: Centralized deployment status and URI management
- **Model Comparison**: Version-to-version performance comparison
- **Archival Management**: Automatic cleanup of old model versions

### NeuroNews-Specific Features
- **Standard Model Names**: Pre-defined model categories for consistency
- **Performance Gates**: Model-specific validation thresholds
- **Metadata Templates**: Standardized model metadata structure
- **Integration**: Seamless integration with existing MLflow tracking

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────┐
│                MLflow Model Registry                    │
├─────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐    │
│  │    None     │  │   Staging   │  │ Production  │    │
│  │             │→ │             │→ │             │    │
│  │ Development │  │  Validation │  │  Live Model │    │
│  └─────────────┘  └─────────────┘  └─────────────┘    │
│                          ↓                              │
│                 ┌─────────────┐                        │
│                 │  Archived   │                        │
│                 │             │                        │
│                 │ Old Versions│                        │
│                 └─────────────┘                        │
└─────────────────────────────────────────────────────────┘
                          ↑
┌─────────────────────────────────────────────────────────┐
│              Performance Gates                          │
├─────────────────────────────────────────────────────────┤
│ • Accuracy Thresholds                                   │
│ • F1 Score Requirements                                 │
│ • Similarity Metrics                                    │
│ • Custom Validation Rules                              │
└─────────────────────────────────────────────────────────┘
```

## 🚀 Quick Start

### Basic Model Registration

```python
from services.mlops.registry import NeuroNewsModelRegistry, ModelMetadata

# Initialize registry
registry = NeuroNewsModelRegistry()

# Define model metadata
metadata = ModelMetadata(
    name="neuro_sentiment_classifier",
    description="News sentiment classification model",
    tags={"team": "nlp", "algorithm": "random_forest"},
    owner="ml-team",
    use_case="sentiment_analysis",
    performance_metrics={"accuracy": 0.89, "f1_score": 0.85},
    deployment_target="production"
)

# Register model from MLflow run
model_version = registry.register_model(
    model_uri="runs:/abc123/model",
    name="neuro_sentiment_classifier", 
    metadata=metadata
)
```

### Stage Transitions

```python
# Move to staging
registry.transition_model_stage(
    name="neuro_sentiment_classifier",
    version="1",
    stage=ModelStage.STAGING,
    description="Moving to staging for validation"
)

# Promote to production (with performance gate validation)
registry.transition_model_stage(
    name="neuro_sentiment_classifier", 
    version="1",
    stage=ModelStage.PRODUCTION,
    check_performance_gates=True
)
```

### Helper Functions

```python
from services.mlops.registry import (
    register_model_from_run,
    promote_to_production,
    get_production_model_uri
)

# Register model from run ID
version = register_model_from_run(
    run_id="abc123",
    model_name="neuro_sentiment_classifier"
)

# Promote to production
promote_to_production("neuro_sentiment_classifier", "1")

# Get production model URI for inference
model_uri = get_production_model_uri("neuro_sentiment_classifier")
model = mlflow.pyfunc.load_model(model_uri)
```

## 📊 Standard Models

### Pre-defined Model Categories

| Model Name | Description | Performance Gates |
|------------|-------------|------------------|
| `neuro_sentiment_classifier` | News sentiment classification | accuracy > 0.85, f1_score > 0.80 |
| `neuro_embeddings_encoder` | Document embedding generation | similarity_score > 0.75, recall@10 > 0.70 |
| `neuro_rag_retriever` | RAG retrieval and ranking | relevance_score > 0.80, answer_quality > 0.75 |
| `neuro_topic_extractor` | Topic and keyword extraction | - |
| `neuro_summarizer` | Article summarization | - |
| `neuro_clustering_engine` | Article clustering | - |

### Performance Gates

Performance gates automatically validate model quality before production deployment:

```python
# Example: Sentiment classifier gates
gates = [
    ModelPerformanceGate("accuracy", 0.85, "greater"),
    ModelPerformanceGate("f1_score", 0.80, "greater")
]

# Gates are checked automatically during stage transitions
registry.transition_model_stage(
    name="neuro_sentiment_classifier",
    version="1", 
    stage=ModelStage.PRODUCTION,
    check_performance_gates=True  # Will validate against gates
)
```

## 🏷️ Model Metadata

### Required Metadata Structure

```python
@dataclass
class ModelMetadata:
    name: str                               # Model name (use standard names)
    description: str                        # Detailed description
    tags: Dict[str, str]                   # Custom tags
    owner: str                             # Team or individual owner
    use_case: str                          # Primary use case
    performance_metrics: Dict[str, float]  # Key performance metrics
    deployment_target: str                 # Target deployment environment
```

### Standard Tags

Recommended tags for consistent metadata:

```python
standard_tags = {
    "team": "nlp|search|research",         # Owning team
    "domain": "sentiment|embeddings|rag",  # Problem domain
    "algorithm": "random_forest|bert|lr",  # Core algorithm
    "status": "experimental|stable|deprecated",
    "framework": "sklearn|transformers|pytorch"
}
```

## 🔄 Model Lifecycle

### Stage Progression

1. **None (Default)**: Newly registered models
2. **Staging**: Models under validation and testing
3. **Production**: Live models serving predictions
4. **Archived**: Deprecated or replaced models

### Transition Requirements

```python
class TransitionRequirement(Enum):
    MANUAL_APPROVAL = "manual"      # Requires explicit approval
    AUTOMATIC = "automatic"         # No validation required
    PERFORMANCE_GATE = "performance" # Must pass performance gates
```

### Version Management

- **Automatic Versioning**: Sequential version numbers (1, 2, 3, ...)
- **Metadata Tracking**: Full lineage and performance history
- **Comparison Tools**: Version-to-version performance analysis
- **Archival**: Automatic cleanup of old versions

## 📈 Model Comparison

Compare performance between model versions:

```python
# Compare two versions
comparison = registry.compare_model_versions(
    name="neuro_sentiment_classifier",
    version1="1", 
    version2="2"
)

# Results include:
# - Absolute and relative metric changes
# - Creation timestamps
# - Stage information
# - Performance improvements
```

Example output:
```
📊 Comparison Results:
   Model: neuro_sentiment_classifier
   Version 1 (baseline):
     accuracy: 0.8500
     f1_score: 0.8200
   Version 2 (improved):
     accuracy: 0.8900
     f1_score: 0.8600
   Improvements:
     accuracy: +4.71% (+0.0400)
     f1_score: +4.88% (+0.0400)
```

## 🚀 Deployment Management

### Deployment Information

Get comprehensive deployment status:

```python
deployment_info = registry.get_model_deployment_info("neuro_sentiment_classifier")

# Returns:
{
    "model_name": "neuro_sentiment_classifier",
    "production": {
        "version": "2",
        "creation_date": "2025-08-25T10:30:00",
        "model_uri": "models:/neuro_sentiment_classifier/Production"
    },
    "staging": [...],
    "latest": {...}
}
```

### Production Model Loading

```python
# Get production model URI
production_uri = get_production_model_uri("neuro_sentiment_classifier")

# Load for inference
model = mlflow.pyfunc.load_model(production_uri)
predictions = model.predict(data)
```

## 🗄️ Model Archival

Automatic cleanup of old model versions:

```python
# Archive old versions (keep latest 3, exclude production)
archived = registry.archive_old_versions(
    name="neuro_sentiment_classifier",
    keep_latest_n=3,
    exclude_production=True
)

print(f"Archived versions: {archived}")
```

## 🧪 Testing & Validation

### Unit Tests

```bash
# Run model registry tests
pytest tests/mlops/test_registry.py

# Run with coverage
pytest tests/mlops/test_registry.py --cov=services.mlops.registry
```

### Integration Testing

```bash
# Full integration test with MLflow server
make mlflow-up
python demo_mlflow_model_registry.py
make mlflow-down
```

## 📋 API Reference

### Core Classes

#### `NeuroNewsModelRegistry`
Main registry interface for model lifecycle management.

**Methods:**
- `register_model()`: Register a new model version
- `transition_model_stage()`: Move model between stages
- `get_model_versions()`: List model versions by stage
- `get_production_model()`: Get current production version
- `compare_model_versions()`: Compare version performance
- `archive_old_versions()`: Clean up old versions

#### `ModelMetadata`
Standard metadata structure for model registration.

#### `ModelStage`
Enum defining model lifecycle stages.

#### `ModelPerformanceGate`
Performance validation rules for stage transitions.

### Helper Functions

```python
# Convenience functions for common tasks
register_model_from_run(run_id, model_name, metadata=None)
promote_to_production(model_name, version, check_performance=True)
get_production_model_uri(model_name)
list_all_models()
setup_model_registry(tracking_uri)
```

## 🔧 Configuration

### Environment Variables

```bash
# MLflow tracking server
export MLFLOW_TRACKING_URI="http://localhost:5001"

# Model registry specific settings
export NEURONEWS_MODEL_REGISTRY_ENABLED="true"
export NEURONEWS_PERFORMANCE_GATES_ENABLED="true"
```

### Registry Settings

```python
# Default performance gates
DEFAULT_PERFORMANCE_GATES = {
    "neuro_sentiment_classifier": [
        ModelPerformanceGate("accuracy", 0.85, "greater"),
        ModelPerformanceGate("f1_score", 0.80, "greater")
    ],
    # ... more models
}

# Standard model definitions
STANDARD_MODELS = {
    "neuro_sentiment_classifier": "News article sentiment classification model",
    # ... more models
}
```

## 🚨 Error Handling

### Common Issues

1. **Model Registration Fails**
   ```
   MlflowException: Model version failed registration
   ```
   **Solution**: Check MLflow server connectivity and model artifact availability

2. **Performance Gate Failures**
   ```
   ValueError: Model does not meet performance gates for Production
   ```
   **Solution**: Improve model performance or adjust gate thresholds

3. **Stage Transition Errors**
   ```
   MlflowException: Cannot transition from Production to Staging
   ```
   **Solution**: Use proper stage progression (Production → Archived)

### Best Practices

- **Always use standard model names** for consistency
- **Include comprehensive metadata** during registration
- **Test performance gates** before production deployment
- **Regular archival** of old model versions
- **Monitor deployment status** across environments

## 🔗 Integration

### With Existing MLflow Tracking

```python
from services.mlops.tracking import mlrun
from services.mlops.registry import register_model_from_run

# Train and register in one flow
with mlrun("sentiment_training", experiment="neuro_news_indexing") as run:
    # Training code...
    mlflow.sklearn.log_model(model, "model")
    
    # Register model after training
    version = register_model_from_run(
        run_id=run.info.run_id,
        model_name="neuro_sentiment_classifier"
    )
```

### With CI/CD Pipelines

```python
# Automated model promotion in CI/CD
def promote_model_if_ready(model_name: str, version: str):
    registry = NeuroNewsModelRegistry()
    
    try:
        # Attempt promotion with performance validation
        production_version = registry.transition_model_stage(
            name=model_name,
            version=version,
            stage=ModelStage.PRODUCTION,
            check_performance_gates=True
        )
        print(f"✅ Model promoted to production: {model_name} v{version}")
        return True
    except ValueError as e:
        print(f"❌ Model promotion failed: {e}")
        return False
```

## 📖 Examples

### Complete Registration Workflow

```python
from services.mlops.registry import NeuroNewsModelRegistry, ModelMetadata, ModelStage

# 1. Initialize registry
registry = NeuroNewsModelRegistry("http://localhost:5001")

# 2. Prepare metadata
metadata = ModelMetadata(
    name="neuro_sentiment_classifier",
    description="Production sentiment classification model",
    tags={
        "team": "nlp",
        "algorithm": "random_forest",
        "framework": "sklearn"
    },
    owner="nlp-team",
    use_case="news_sentiment_analysis", 
    performance_metrics={
        "accuracy": 0.89,
        "f1_score": 0.86,
        "precision": 0.88,
        "recall": 0.85
    },
    deployment_target="production"
)

# 3. Register model
version = registry.register_model(
    model_uri="runs:/abc123def456/model",
    name="neuro_sentiment_classifier",
    metadata=metadata
)

# 4. Stage progression
registry.transition_model_stage(
    name="neuro_sentiment_classifier",
    version=version.version,
    stage=ModelStage.STAGING,
    description="Initial staging deployment"
)

# 5. Production promotion (with validation)
registry.transition_model_stage(
    name="neuro_sentiment_classifier", 
    version=version.version,
    stage=ModelStage.PRODUCTION,
    description="Promoting after successful validation",
    check_performance_gates=True
)

# 6. Get production model for inference
production_uri = get_production_model_uri("neuro_sentiment_classifier")
model = mlflow.pyfunc.load_model(production_uri)
```

## 🎯 Benefits

### For Data Scientists
- **Simplified registration** with helper functions
- **Automatic validation** prevents bad models in production
- **Version comparison** helps track improvements
- **Standardized workflow** across teams

### For MLOps Engineers  
- **Centralized model management** across all environments
- **Automated archival** reduces storage costs
- **Performance monitoring** ensures quality
- **Deployment tracking** provides visibility

### For NeuroNews Platform
- **Consistent model naming** improves discoverability
- **Quality gates** ensure production reliability
- **Version control** enables safe deployments
- **Metadata tracking** supports compliance and auditing

---

**Documentation Version**: 1.0.0  
**Last Updated**: August 25, 2025  
**Issue**: #221 - MLflow Model Registry Implementation
