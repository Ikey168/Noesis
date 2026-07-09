# MLflow Experiment Structure & Naming Conventions

**Issue #220: Define experiments and tag standards for consistent, comparable runs**

This document establishes standardized experiment organization, naming conventions, and tagging strategies for the NeuroNews MLflow deployment to ensure runs are discoverable, comparable, and properly categorized.

---

## 📊 Standard Experiment Structure

### Core Production Experiments

#### 1. `neuro_news_indexing`
**Purpose**: Document embedding generation, vector storage, and retrieval optimization

**Use Cases**:
- Embedding model evaluation and selection
- Vector database performance testing  
- Indexing pipeline optimization
- Chunking strategy experiments

**Example Runs**:
```bash
# Embedding model comparison
mlflow run . --experiment-name=neuro_news_indexing \
  -P model_name=sentence-transformers/all-MiniLM-L6-v2 \
  -P chunk_size=512 \
  -P overlap=50

# Vector database performance test
mlflow run . --experiment-name=neuro_news_indexing \
  -P vector_db=pinecone \
  -P index_type=cosine \
  -P batch_size=100
```

#### 2. `neuro_news_ask`
**Purpose**: Question-answering pipeline performance and accuracy tracking

**Use Cases**:
- RAG model performance evaluation
- Query expansion experiments
- Reranking algorithm testing
- Response quality assessment

**Example Runs**:
```bash
# RAG pipeline optimization
mlflow run . --experiment-name=neuro_news_ask \
  -P retrieval_k=10 \
  -P rerank_enabled=true \
  -P fusion_method=reciprocal_rank

# Answer generation model comparison
mlflow run . --experiment-name=neuro_news_ask \
  -P llm_provider=openai \
  -P model=gpt-4-turbo \
  -P temperature=0.1
```

#### 3. `research_prototypes`
**Purpose**: Experimental features, research ideas, and proof-of-concept implementations

**Use Cases**:
- New algorithm prototyping
- Feature exploration
- A/B testing experimental components
- Research paper implementations

**Example Runs**:
```bash
# New clustering algorithm test
mlflow run . --experiment-name=research_prototypes \
  -P algorithm=dbscan_embedding_cluster \
  -P min_samples=5 \
  -P eps=0.3

# Experimental sentiment analysis
mlflow run . --experiment-name=research_prototypes \
  -P sentiment_model=cardiffnlp/twitter-roberta-base-sentiment-latest \
  -P aggregation_method=weighted_average
```

---

## 🏷️ Standardized Tagging System

### Required Tags (All Runs)

#### `git.sha` - Git Commit Hash
- **Purpose**: Link runs to specific code versions
- **Format**: Full 40-character SHA
- **Auto-generated**: Yes (via `mlrun()` context manager)
- **Example**: `git.sha: a1b2c3d4e5f6789012345678901234567890abcd`

#### `env` - Environment Identifier  
- **Purpose**: Distinguish development, staging, and production runs
- **Format**: `dev|staging|prod`
- **Auto-generated**: Yes (from `NEURONEWS_ENV`)
- **Example**: `env: dev`

#### `pipeline` - Pipeline Component
- **Purpose**: Identify which system component generated the run
- **Format**: Snake_case component name
- **Auto-generated**: Yes (from `NEURONEWS_PIPELINE`)
- **Examples**: 
  - `pipeline: embeddings_indexer`
  - `pipeline: rag_answerer`
  - `pipeline: sentiment_analyzer`

#### `data_version` - Dataset Version
- **Purpose**: Track which data version was used for training/evaluation
- **Format**: Semantic versioning or date-based
- **Auto-generated**: No (must be set manually)
- **Examples**:
  - `data_version: v1.2.3`
  - `data_version: 2025-08-25`
  - `data_version: news_corpus_q3_2025`

### Optional Tags (As Needed)

#### `notes` - Human-Readable Description
- **Purpose**: Context, hypothesis, or experimental details
- **Format**: Free text (keep concise)
- **Examples**:
  - `notes: Testing improved chunking strategy for better retrieval`
  - `notes: Baseline run before algorithm optimization`
  - `notes: Reproducing paper results for comparison`

#### `model_type` - Model Category
- **Purpose**: Group runs by model architecture
- **Examples**: `transformer`, `embedding`, `classification`, `regression`

#### `task_type` - ML Task Category  
- **Purpose**: Categorize by machine learning task
- **Examples**: `embedding`, `classification`, `regression`, `rag`, `sentiment`

#### `dataset_type` - Data Source Category
- **Purpose**: Identify data origin and characteristics
- **Examples**: `synthetic`, `production`, `benchmark`, `curated`

---

## 🎯 Naming Conventions

### Run Names
**Format**: `{component}_{timestamp}_{optional_descriptor}`

**Examples**:
```bash
embeddings_indexer_20250825_143021
rag_answerer_20250825_143045_baseline
sentiment_pipeline_20250825_143109_roberta_test
```

### Parameter Names
**Format**: Snake_case, descriptive, consistent across experiments

**Standard Parameters**:
- `model_name` - Model identifier
- `batch_size` - Processing batch size
- `learning_rate` - Training learning rate
- `max_tokens` - Token limit for text processing
- `chunk_size` - Text chunking size
- `overlap` - Chunking overlap size
- `temperature` - LLM sampling temperature
- `top_k` - Top-K retrieval/generation
- `threshold` - Decision threshold

### Metric Names
**Format**: Snake_case, include units where applicable

**Standard Metrics**:
- `accuracy` - Classification accuracy (0-1)
- `precision`, `recall`, `f1_score` - Classification metrics
- `bleu_score`, `rouge_score` - Text generation metrics
- `retrieval_ms` - Retrieval latency (milliseconds)
- `inference_ms` - Inference latency (milliseconds)
- `memory_mb` - Memory usage (megabytes)
- `throughput_qps` - Queries per second
- `embedding_dimension` - Vector dimensions
- `tokens_per_second` - Token generation rate

---

## 💻 CLI Usage Examples

### Setting Up a Run with Proper Tags

```bash
# Export environment variables for auto-tagging
export NEURONEWS_ENV=dev
export NEURONEWS_PIPELINE=embeddings_indexer

# Start MLflow tracking server (if not running)
mlflow server --host 0.0.0.0 --port 5001 --backend-store-uri sqlite:///mlflow.db

# Run with proper experiment and manual tags
python scripts/train_embeddings.py \
  --experiment-name=neuro_news_indexing \
  --data-version=v1.2.3 \
  --notes="Testing sentence-transformers with improved chunking"
```

### Using the mlrun() Context Manager

```python
from services.mlops.tracking import mlrun

# Automatic tagging with manual overrides
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
    # Your training/evaluation code here
    mlflow.log_param("model_name", "sentence-transformers/all-MiniLM-L6-v2")
    mlflow.log_param("chunk_size", 512)
    mlflow.log_metric("embedding_time_ms", 45.2)
    mlflow.log_metric("accuracy", 0.887)
```

### Querying Runs by Tags

```python
import mlflow

# Find all production indexing runs
runs = mlflow.search_runs(
    experiment_ids=["neuro_news_indexing"],
    filter_string="tags.env = 'prod' AND tags.pipeline = 'embeddings_indexer'"
)

# Find runs with specific data version
runs = mlflow.search_runs(
    filter_string="tags.data_version = 'v1.2.3'"
)

# Find recent research prototype runs
runs = mlflow.search_runs(
    experiment_ids=["research_prototypes"],
    filter_string="tags.env = 'dev'",
    order_by=["start_time DESC"],
    max_results=10
)
```

---

## 📸 MLflow UI Screenshots & Examples

### Experiment Organization View
```
📊 Experiments
├── neuro_news_indexing (47 runs)
│   ├── 🔄 Recent: embeddings_indexer_20250825_143021
│   ├── 📈 Best: sentence_transformers_optimized (accuracy: 0.912)
│   └── 🏷️  Tags: env=prod, pipeline=embeddings_indexer
│
├── neuro_news_ask (23 runs)  
│   ├── 🔄 Recent: rag_answerer_20250825_142845
│   ├── 📈 Best: gpt4_turbo_fusion (relevance: 0.889)
│   └── 🏷️  Tags: env=prod, pipeline=rag_answerer
│
└── research_prototypes (12 runs)
    ├── 🔄 Recent: clustering_experiment_20250825_141203
    ├── 🧪 Latest: dbscan_embedding_cluster
    └── 🏷️  Tags: env=dev, task_type=clustering
```

### Run Details Tag View
```
🏷️ Tags
Required:
  git.sha: a1b2c3d4e5f6789012345678901234567890abcd
  env: dev  
  pipeline: embeddings_indexer
  data_version: v1.2.3

Optional:
  notes: Testing improved chunking strategy for better retrieval
  model_type: transformer
  task_type: embedding
  dataset_type: production
```

### Parameter Comparison View
```
📊 Parameters Comparison (neuro_news_indexing)
Run ID          | model_name                           | chunk_size | overlap | accuracy
abc123def456    | sentence-transformers/all-MiniLM-L6  | 512       | 50      | 0.887
def456ghi789    | sentence-transformers/all-mpnet-base | 512       | 50      | 0.901  
ghi789jkl012    | sentence-transformers/all-MiniLM-L6  | 256       | 25      | 0.879
jkl012mno345    | text-embedding-ada-002               | 512       | 50      | 0.912
```

---

## ✅ Implementation Checklist

### Phase 1: Documentation & Standards
- [x] Create experiment structure documentation
- [x] Define required and optional tag schemas
- [x] Establish naming conventions
- [x] Document CLI usage examples

### Phase 2: Code Updates
- [ ] Update `services/mlops/tracking.py` with tag validation
- [ ] Add experiment creation helper functions
- [ ] Update existing pipelines to use standard experiments
- [ ] Add tag validation in CI/CD

### Phase 3: Migration & Adoption
- [ ] Migrate existing runs to new experiment structure
- [ ] Update team documentation and training
- [ ] Add experiment validation to pre-commit hooks
- [ ] Create monitoring for tag compliance

---

## 🔄 Migration Guide

### Moving Existing Runs

```python
import mlflow

# List experiments that need migration
old_experiments = ["default", "test_runs", "misc"]

for exp_name in old_experiments:
    experiment = mlflow.get_experiment_by_name(exp_name)
    if experiment:
        runs = mlflow.search_runs(experiment_ids=[experiment.experiment_id])
        
        for _, run in runs.iterrows():
            # Determine target experiment based on run characteristics
            if "embedding" in run.get("tags.estimator_name", ""):
                target_experiment = "neuro_news_indexing"
            elif "rag" in run.get("run_name", ""):
                target_experiment = "neuro_news_ask"  
            else:
                target_experiment = "research_prototypes"
            
            # Set proper experiment (requires MLflow Pro or custom script)
            print(f"Move run {run['run_id']} to {target_experiment}")
```

### Updating Existing Code

```python
# Before (non-standard)
with mlflow.start_run() as run:
    mlflow.log_param("model", "bert-base")
    mlflow.log_metric("acc", 0.85)

# After (standardized)
with mlrun(
    name="sentiment_analyzer_20250825_143500",
    experiment="neuro_news_ask",
    tags={
        "data_version": "v1.2.3",
        "notes": "Sentiment analysis baseline",
        "model_type": "transformer",
        "task_type": "classification"
    }
) as run:
    mlflow.log_param("model_name", "bert-base-uncased")
    mlflow.log_metric("accuracy", 0.85)
```

---

## 📚 Best Practices

### 1. Consistent Experimentation
- Always use standard experiment names
- Include all required tags
- Use descriptive run names with timestamps
- Document hypotheses in `notes` tag

### 2. Data Version Tracking
- Tag every run with `data_version`
- Use semantic versioning for datasets
- Document data changes in experiment descriptions

### 3. Reproducibility
- Ensure `git.sha` is always captured
- Log all hyperparameters
- Save model artifacts and signatures
- Document environment setup

### 4. Performance Monitoring
- Track consistent metrics across experiments
- Use standard metric names and units
- Log both training and validation metrics
- Monitor resource usage (memory, time)

### 5. Team Collaboration
- Use consistent naming across team members
- Review experiment organization quarterly
- Share findings through experiment descriptions
- Archive old experiments when appropriate

---

## 🆘 Troubleshooting

### Common Issues

**Issue**: Runs not appearing in correct experiment
```bash
# Solution: Verify experiment name is exact match
mlflow experiments list
```

**Issue**: Missing required tags
```python
# Solution: Use mlrun() context manager for auto-tagging
from services.mlops.tracking import mlrun
```

**Issue**: Inconsistent parameter names
```bash
# Solution: Follow snake_case convention
# ✅ Good: chunk_size, learning_rate, max_tokens
# ❌ Bad: chunkSize, lr, maxTokens
```

**Issue**: Cannot find historical runs
```python
# Solution: Search by tags instead of experiment names
runs = mlflow.search_runs(filter_string="tags.pipeline = 'embeddings_indexer'")
```

---

## 📞 Support & Contact

- **Documentation Issues**: Create GitHub issue with `documentation` label
- **MLflow Setup**: See `docker/mlflow/README.md`
- **Integration Questions**: Check `services/mlops/README.md`
- **Team Training**: Contact MLOps team for convention workshops

---

*Last Updated: August 25, 2025*  
*Version: 1.0.0*  
*Next Review: September 25, 2025*
