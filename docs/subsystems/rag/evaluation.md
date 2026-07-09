# RAG Evaluation Framework

This document describes the evaluation framework for the NeuroNews RAG (Retrieval-Augmented Generation) system, as implemented in Issue #235.

## Overview

The evaluation framework provides comprehensive assessment of RAG system performance using standardized metrics and configuration comparison capabilities. It supports both individual configuration evaluation and comparative analysis across multiple settings.

## Evaluation Dataset

### Format
The evaluation dataset is stored in `evals/qa_dev.jsonl` in JSON Lines format, with each line containing:

```json
{
  "query": "What are the latest developments in artificial intelligence?",
  "answers": ["Recent AI developments include large language models..."],
  "must_have_terms": ["artificial intelligence", "AI", "developments"],
  "date_filter": "2024-01-01"
}
```

### Fields Description
- **query**: The question to be answered by the RAG system
- **answers**: List of acceptable ground truth answers
- **must_have_terms**: Terms that should appear in relevant retrieved documents
- **date_filter**: Optional date filter for document retrieval

### Dataset Size
The current dataset contains 50 carefully curated questions covering:
- Technology and AI developments
- Climate and environmental issues
- Economic and financial trends
- Healthcare and biotechnology
- Policy and regulatory changes
- Scientific and research advances

## Metrics

### Retrieval Metrics

#### Recall@k
Measures the proportion of relevant documents retrieved in the top-k results.
- **Formula**: `relevant_retrieved / total_relevant`
- **Range**: [0, 1]
- **Higher is better**

#### nDCG@k (Normalized Discounted Cumulative Gain)
Evaluates ranking quality considering both relevance and position.
- **Formula**: `DCG@k / IDCG@k`
- **Range**: [0, 1]
- **Higher is better**

#### MRR (Mean Reciprocal Rank)
Measures the average reciprocal rank of the first relevant document.
- **Formula**: `1 / rank_of_first_relevant`
- **Range**: [0, 1]
- **Higher is better**

#### Precision@k
Proportion of retrieved documents that are relevant.
- **Formula**: `relevant_retrieved / k`
- **Range**: [0, 1]
- **Higher is better**

#### Must-Have Terms Coverage
Proportion of required terms found in retrieved documents.
- **Formula**: `found_terms / total_must_have_terms`
- **Range**: [0, 1]
- **Higher is better**

### Answer Quality Metrics

#### Exact Match
Binary indicator of perfect answer match (case-insensitive).
- **Range**: {0, 1}
- **Higher is better**

#### Partial Match
Binary indicator if any ground truth appears as substring in prediction.
- **Range**: {0, 1}
- **Higher is better**

#### Token F1
Token-level F1 score between prediction and best matching ground truth.
- **Formula**: `2 * precision * recall / (precision + recall)`
- **Range**: [0, 1]
- **Higher is better**

### Performance Metrics

#### Query Time
Average time to process a single query (milliseconds).
- **Lower is better** (efficiency measure)

#### Citation Count
Average number of citations returned per query.
- **Context-dependent** (completeness vs. conciseness)

## Configuration Options

### Predefined Configurations

1. **baseline**: Default configuration with balanced settings
   - k=5, fusion=True, rerank=True, semantic_weight=0.7

2. **no_rerank**: Baseline without reranking
   - k=5, fusion=True, rerank=False, semantic_weight=0.7

3. **no_fusion**: Baseline without query fusion
   - k=5, fusion=False, rerank=True, semantic_weight=0.7

4. **high_k**: Higher retrieval count
   - k=10, fusion=True, rerank=True, semantic_weight=0.7

5. **semantic_heavy**: Emphasizes semantic search
   - k=5, fusion=True, rerank=True, semantic_weight=0.9

### Custom Configuration Parameters

- **k**: Number of documents to retrieve (1-20)
- **fusion**: Enable/disable query fusion (vector + lexical search)
- **rerank**: Enable/disable cross-encoder reranking
- **provider**: LLM provider (openai, anthropic, local)
- **semantic_weight**: Weight for semantic search in fusion (0.0-1.0)

## Usage

### Basic Evaluation

Run evaluation with default configuration:
```bash
python evals/run_eval.py --config baseline
```

### Configuration Comparison

Compare multiple predefined configurations:
```bash
python evals/run_eval.py --compare
```

### Custom Configuration

Use custom parameters:
```bash
python evals/run_eval.py --custom --k 10 --fusion true --rerank false --semantic-weight 0.8
```

### Output Options

Save results to specific CSV file:
```bash
python evals/run_eval.py --config baseline --output results.csv
```

Sample dataset for quick testing:
```bash
python evals/run_eval.py --config baseline --sample 10
```

## Output Format

### Console Output
The framework prints formatted metrics tables showing:
- Retrieval metrics (Recall@k, nDCG@k, MRR, Precision@k)
- Answer quality metrics (Exact match, Partial match, Token F1)
- Performance metrics (Query time, Citation count, Success rate)

### CSV Output
Detailed per-query results saved as CSV with columns:
- query_idx, query, predicted_answer, ground_truth_answers
- All metric values per query
- Configuration parameters
- Timing information

### MLflow Logging
When MLflow is available, the framework logs:
- Configuration parameters
- Overall metrics
- Individual query results
- CSV artifacts
- Experiment metadata

## Interpretation Guidelines

### Retrieval Quality
- **Recall@k > 0.6**: Good document retrieval
- **nDCG@k > 0.7**: Well-ranked results
- **MRR > 0.5**: Relevant docs appear early

### Answer Quality
- **Token F1 > 0.4**: Reasonable answer overlap
- **Partial Match > 0.7**: Good content coverage
- **Exact Match**: Rare but indicates perfect answers

### Configuration Insights
- **High fusion weight**: Better for semantic queries
- **Reranking**: Improves precision, adds latency
- **Higher k**: More comprehensive but slower

## Troubleshooting

### Common Issues

1. **Import Errors**: Ensure all dependencies are installed and you're running from project root
2. **Dataset Not Found**: Check that `evals/qa_dev.jsonl` exists
3. **MLflow Errors**: MLflow logging is optional; evaluation continues without it
4. **Memory Issues**: Use `--sample` flag to limit dataset size

### Performance Considerations

- Evaluation time scales linearly with dataset size
- Reranking adds ~50ms per query
- Large k values increase retrieval time
- MLflow logging adds minimal overhead

## Extension Points

### Adding New Metrics
Extend `_calculate_retrieval_metrics()` and `_calculate_answer_metrics()` methods in the `EnhancedRAGEvaluator` class.

### Custom Configurations
Add new entries to the `create_predefined_configs()` function.

### Dataset Expansion
Add new examples to `evals/qa_dev.jsonl` following the established format.

### Integration with CI/CD
Use `--sample` flag for quick validation in continuous integration pipelines.

## References

- Issue #235: Evals: small QA set + metrics (R@k, nDCG, MRR)
- Issue #233: Answering pipeline + citations (FastAPI /ask)
- MLflow Documentation: https://mlflow.org/docs/latest/index.html
- Information Retrieval Metrics: https://en.wikipedia.org/wiki/Evaluation_measures_(information_retrieval)
