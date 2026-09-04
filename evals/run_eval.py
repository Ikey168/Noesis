"""
Enhanced RAG Evaluation with MLflow Logging and Configuration Comparison
Issue #235: Evals: small QA set + metrics (R@k, nDCG, MRR)

This module provides comprehensive evaluation of the RAG system with:
- Retrieval metrics: Recall@k, nDCG@k, MRR
- Answer quality metrics: exact/partial match, token F1
- Configuration comparison capabilities
- CSV output and MLflow logging
"""

import asyncio
import argparse
import csv
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.metrics import f1_score

# Add src to path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

try:
    from services.mlops.tracking import mlrun
except ImportError as e:
    print(f"Import error: {e}")
    print("Please ensure you're running from the project root directory")
    sys.exit(1)

# The heavy retrieval/answer stack (FastAPI ask route, RAG service) is imported
# lazily inside _evaluate_single_query so lightweight offline paths -- such as
# the --tiny CI smoke evaluation -- do not require those dependencies to be
# installed.

# Configure logging
logger = logging.getLogger(__name__)


class EvaluationConfig:
    """Configuration for RAG evaluation experiments."""
    
    def __init__(
        self,
        name: str,
        k: int = 5,
        fusion_enabled: bool = True,
        rerank_enabled: bool = True,
        provider: str = "openai",
        fusion_weights: Optional[Dict[str, float]] = None
    ):
        self.name = name
        self.k = k
        self.fusion_enabled = fusion_enabled
        self.rerank_enabled = rerank_enabled
        self.provider = provider
        self.fusion_weights = fusion_weights or {"semantic": 0.7, "keyword": 0.3}
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary for logging."""
        return {
            "name": self.name,
            "k": self.k,
            "fusion_enabled": self.fusion_enabled,
            "rerank_enabled": self.rerank_enabled,
            "provider": self.provider,
            "fusion_weights": self.fusion_weights
        }
    
    @classmethod
    def from_args(cls, args: argparse.Namespace) -> 'EvaluationConfig':
        """Create config from command line arguments."""
        return cls(
            name=args.config,
            k=args.k,
            fusion_enabled=args.fusion,
            rerank_enabled=args.rerank,
            provider=args.provider,
            fusion_weights={"semantic": args.semantic_weight, "keyword": 1.0 - args.semantic_weight}
        )


class EnhancedRAGEvaluator:
    """
    Enhanced RAG Evaluation Framework with comprehensive metrics and comparison capabilities.
    
    Features:
    - Retrieval metrics: Recall@k, nDCG@k, MRR
    - Answer quality metrics: exact match, partial match, token F1
    - Configuration comparison support
    - CSV output and MLflow logging
    - Support for JSONL evaluation datasets
    """

    def __init__(self, config: EvaluationConfig):
        """Initialize evaluator with configuration."""
        self.config = config
        self.results = []
        
        logger.info(f"Enhanced RAG Evaluator initialized with config: {config.name}")

    async def evaluate_dataset(
        self,
        dataset: List[Dict[str, Any]],
        experiment_name: str = "rag_evaluation_comparison",
        run_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate RAG system on a dataset with comprehensive metrics.

        Args:
            dataset: List of evaluation examples from qa_dev.jsonl
            experiment_name: MLflow experiment name
            run_name: Optional run name for MLflow

        Returns:
            Dictionary containing metrics and results
        """
        if not run_name:
            run_name = f"{self.config.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        logger.info(f"Starting evaluation: {self.config.name} on {len(dataset)} queries")

        try:
            with mlrun(
                name=run_name,
                experiment=experiment_name,
                tags={
                    "config": self.config.name,
                    "evaluation_type": "comprehensive_rag",
                    "dataset_size": len(dataset),
                    "issue": "235"
                }
            ):
                import mlflow
                
                # Log configuration parameters
                mlflow.log_params(self.config.to_dict())
                
                overall_start = time.time()
                self.results = []
                
                # Process each query
                for i, example in enumerate(dataset):
                    logger.info(f"Processing {i+1}/{len(dataset)}: {example['query'][:50]}...")
                    
                    result = await self._evaluate_single_query(example, i)
                    self.results.append(result)
                
                # Calculate overall metrics
                overall_metrics = self._calculate_overall_metrics()
                total_time = time.time() - overall_start
                
                # Log metrics to MLflow
                self._log_metrics_to_mlflow(mlflow, overall_metrics, total_time)
                
                # Save CSV output
                csv_path = self._save_csv_results()
                if csv_path:
                    mlflow.log_artifact(csv_path, "evaluation_results")
                
                logger.info(f"Evaluation completed in {total_time:.2f}s")
                
                return {
                    "config": self.config.to_dict(),
                    "overall_metrics": overall_metrics,
                    "results": self.results,
                    "dataset_size": len(dataset),
                    "total_time": total_time,
                    "csv_path": csv_path
                }

        except Exception as e:
            logger.error(f"Evaluation failed: {e}")
            raise

    async def _evaluate_single_query(self, example: Dict[str, Any], query_idx: int) -> Dict[str, Any]:
        """Evaluate a single query and return detailed results."""
        query_start = time.time()

        try:
            from services.api.routes.ask import (
                AskRequest,
                ask_question,
                get_rag_service,
            )

            # Create request based on configuration
            request = AskRequest(
                question=example["query"],
                k=self.config.k,
                filters=self._build_filters(example),
                rerank_on=self.config.rerank_enabled,
                fusion=self.config.fusion_enabled,
                provider=self.config.provider
            )
            
            # Get RAG service and process
            rag_service = get_rag_service()
            response = await ask_question(request, rag_service)
            
            query_time = (time.time() - query_start) * 1000
            
            # Calculate retrieval metrics
            retrieval_metrics = self._calculate_retrieval_metrics(
                response.citations, 
                example.get("must_have_terms", []),
                example.get("relevant_docs", [])
            )
            
            # Calculate answer quality metrics
            answer_metrics = self._calculate_answer_metrics(
                response.answer,
                example.get("answers", [])
            )
            
            # Combine results
            result = {
                "query_idx": query_idx,
                "query": example["query"],
                "predicted_answer": response.answer,
                "ground_truth_answers": example.get("answers", []),
                "num_citations": len(response.citations),
                "query_time_ms": query_time,
                "config_name": self.config.name,
                **retrieval_metrics,
                **answer_metrics,
                "metadata": response.metadata
            }
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to evaluate query {query_idx}: {e}")
            return {
                "query_idx": query_idx,
                "query": example["query"],
                "error": str(e),
                "config_name": self.config.name
            }

    def _build_filters(self, example: Dict[str, Any]) -> Dict[str, Any]:
        """Build filters from example data."""
        filters = {}
        if "date_filter" in example:
            filters["date_from"] = example["date_filter"]
        return filters

    def _calculate_retrieval_metrics(
        self, 
        citations: List[Dict[str, Any]], 
        must_have_terms: List[str],
        relevant_docs: List[str]
    ) -> Dict[str, float]:
        """Calculate retrieval-based metrics."""
        if not citations:
            return {
                "recall_at_k": 0.0,
                "ndcg_at_k": 0.0,
                "mrr": 0.0,
                "precision_at_k": 0.0,
                "has_must_terms": 0.0
            }
        
        # Extract citation text for analysis
        citation_texts = [
            f"{cit.get('title', '')} {cit.get('excerpt', '')}"
            for cit in citations
        ]
        
        # Check for must-have terms coverage
        has_must_terms = 0.0
        if must_have_terms:
            found_terms = 0
            for term in must_have_terms:
                if any(term.lower() in text.lower() for text in citation_texts):
                    found_terms += 1
            has_must_terms = found_terms / len(must_have_terms)
        
        # Calculate relevance-based metrics
        relevance_scores = [
            float(cit.get("relevance_score", 0.0)) 
            for cit in citations
        ]
        
        # Recall@k (simplified - based on relevance threshold)
        relevant_threshold = 0.7
        relevant_retrieved = sum(1 for score in relevance_scores if score >= relevant_threshold)
        recall_at_k = relevant_retrieved / max(len(relevant_docs), 1) if relevant_docs else 0.0
        
        # Precision@k
        precision_at_k = relevant_retrieved / len(citations) if citations else 0.0
        
        # nDCG@k (simplified calculation)
        dcg = sum(
            score / np.log2(i + 2) 
            for i, score in enumerate(relevance_scores)
        )
        ideal_scores = sorted(relevance_scores, reverse=True)
        idcg = sum(
            score / np.log2(i + 2)
            for i, score in enumerate(ideal_scores)
        )
        ndcg_at_k = dcg / idcg if idcg > 0 else 0.0
        
        # MRR (Mean Reciprocal Rank)
        mrr = 0.0
        for i, score in enumerate(relevance_scores):
            if score >= relevant_threshold:
                mrr = 1.0 / (i + 1)
                break
        
        return {
            "recall_at_k": recall_at_k,
            "ndcg_at_k": ndcg_at_k,
            "mrr": mrr,
            "precision_at_k": precision_at_k,
            "has_must_terms": has_must_terms
        }

    def _calculate_answer_metrics(
        self, 
        predicted: str, 
        ground_truth: List[str]
    ) -> Dict[str, float]:
        """Calculate answer quality metrics."""
        if not ground_truth:
            return {
                "exact_match": 0.0,
                "partial_match": 0.0,
                "token_f1": 0.0,
                "answer_length": len(predicted.split())
            }
        
        predicted_lower = predicted.lower()
        
        # Exact match (case-insensitive)
        exact_match = float(any(
            gt.lower() == predicted_lower 
            for gt in ground_truth
        ))
        
        # Partial match (any ground truth substring in prediction)
        partial_match = float(any(
            gt.lower() in predicted_lower 
            for gt in ground_truth
        ))
        
        # Token F1 (best match against any ground truth)
        token_f1 = 0.0
        predicted_tokens = set(predicted.lower().split())
        
        for gt in ground_truth:
            gt_tokens = set(gt.lower().split())
            if gt_tokens:
                precision = len(predicted_tokens & gt_tokens) / len(predicted_tokens) if predicted_tokens else 0
                recall = len(predicted_tokens & gt_tokens) / len(gt_tokens)
                f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0
                token_f1 = max(token_f1, f1)
        
        return {
            "exact_match": exact_match,
            "partial_match": partial_match,
            "token_f1": token_f1,
            "answer_length": len(predicted.split())
        }

    def _calculate_overall_metrics(self) -> Dict[str, float]:
        """Calculate overall metrics from all results."""
        if not self.results:
            return {}
        
        # Filter out error results
        valid_results = [r for r in self.results if "error" not in r]
        
        if not valid_results:
            return {"error": "No valid results"}
        
        metrics = {}
        
        # Aggregate retrieval metrics
        metrics["avg_recall_at_k"] = np.mean([r["recall_at_k"] for r in valid_results])
        metrics["avg_ndcg_at_k"] = np.mean([r["ndcg_at_k"] for r in valid_results])
        metrics["avg_mrr"] = np.mean([r["mrr"] for r in valid_results])
        metrics["avg_precision_at_k"] = np.mean([r["precision_at_k"] for r in valid_results])
        metrics["avg_has_must_terms"] = np.mean([r["has_must_terms"] for r in valid_results])
        
        # Aggregate answer metrics
        metrics["avg_exact_match"] = np.mean([r["exact_match"] for r in valid_results])
        metrics["avg_partial_match"] = np.mean([r["partial_match"] for r in valid_results])
        metrics["avg_token_f1"] = np.mean([r["token_f1"] for r in valid_results])
        metrics["avg_answer_length"] = np.mean([r["answer_length"] for r in valid_results])
        
        # Performance metrics
        metrics["avg_query_time_ms"] = np.mean([r["query_time_ms"] for r in valid_results])
        metrics["avg_num_citations"] = np.mean([r["num_citations"] for r in valid_results])
        
        # Summary stats
        metrics["total_queries"] = len(self.results)
        metrics["valid_queries"] = len(valid_results)
        metrics["error_rate"] = (len(self.results) - len(valid_results)) / len(self.results)
        
        return metrics

    def _log_metrics_to_mlflow(self, mlflow, metrics: Dict[str, float], total_time: float) -> None:
        """Log metrics to MLflow."""
        # Log evaluation metadata
        mlflow.log_metric("total_evaluation_time_s", total_time)
        
        # Log all calculated metrics
        for metric_name, value in metrics.items():
            mlflow.log_metric(metric_name, value)
        
        logger.info(f"Logged {len(metrics)} metrics to MLflow")

    def _save_csv_results(self) -> Optional[str]:
        """Save results to CSV file."""
        if not self.results:
            return None
        
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            csv_path = f"eval_results_{self.config.name}_{timestamp}.csv"
            
            # Convert results to DataFrame and save
            df = pd.DataFrame(self.results)
            df.to_csv(csv_path, index=False)
            
            logger.info(f"Results saved to {csv_path}")
            return csv_path
            
        except Exception as e:
            logger.error(f"Failed to save CSV: {e}")
            return None


def load_qa_dataset(file_path: str) -> List[Dict[str, Any]]:
    """Load QA dataset from JSONL file."""
    dataset = []
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                try:
                    example = json.loads(line.strip())
                    dataset.append(example)
                except json.JSONDecodeError as e:
                    logger.warning(f"Skipping invalid JSON on line {line_num}: {e}")
        
        logger.info(f"Loaded {len(dataset)} examples from {file_path}")
        return dataset
        
    except FileNotFoundError:
        logger.error(f"Dataset file not found: {file_path}")
        raise
    except Exception as e:
        logger.error(f"Error loading dataset: {e}")
        raise


def create_predefined_configs() -> Dict[str, EvaluationConfig]:
    """Create predefined evaluation configurations for comparison."""
    configs = {
        "baseline": EvaluationConfig(
            name="baseline",
            k=5,
            fusion_enabled=True,
            rerank_enabled=True,
            provider="openai"
        ),
        "no_rerank": EvaluationConfig(
            name="no_rerank",
            k=5,
            fusion_enabled=True,
            rerank_enabled=False,
            provider="openai"
        ),
        "no_fusion": EvaluationConfig(
            name="no_fusion",
            k=5,
            fusion_enabled=False,
            rerank_enabled=True,
            provider="openai"
        ),
        "high_k": EvaluationConfig(
            name="high_k",
            k=10,
            fusion_enabled=True,
            rerank_enabled=True,
            provider="openai"
        ),
        "semantic_heavy": EvaluationConfig(
            name="semantic_heavy",
            k=5,
            fusion_enabled=True,
            rerank_enabled=True,
            provider="openai",
            fusion_weights={"semantic": 0.9, "keyword": 0.1}
        )
    }
    return configs


async def run_evaluation(config: EvaluationConfig, dataset: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Run evaluation with given configuration."""
    evaluator = EnhancedRAGEvaluator(config)
    results = await evaluator.evaluate_dataset(dataset)
    return results


async def compare_configurations(
    configs: List[EvaluationConfig], 
    dataset: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Compare multiple configurations and generate comparison report."""
    logger.info(f"Comparing {len(configs)} configurations")
    
    all_results = {}
    
    for config in configs:
        logger.info(f"Running evaluation for config: {config.name}")
        results = await run_evaluation(config, dataset)
        all_results[config.name] = results
    
    # Create comparison table
    comparison_df = pd.DataFrame({
        config_name: result["overall_metrics"]
        for config_name, result in all_results.items()
    }).T
    
    # Save comparison CSV
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    comparison_csv = f"config_comparison_{timestamp}.csv"
    comparison_df.to_csv(comparison_csv)
    
    logger.info(f"Configuration comparison saved to {comparison_csv}")
    
    return {
        "individual_results": all_results,
        "comparison_table": comparison_df,
        "comparison_csv": comparison_csv
    }


def print_metrics_table(results: Dict[str, Any]) -> None:
    """Print formatted metrics table."""
    if "overall_metrics" in results:
        metrics = results["overall_metrics"]
        config_name = results.get("config", {}).get("name", "Unknown")
        
        print(f"\nResults for Configuration: {config_name}")
        print("=" * 60)
        
        # Retrieval metrics
        print("Retrieval Metrics:")
        print(f"  Recall@k:        {metrics.get('avg_recall_at_k', 0):.4f}")
        print(f"  nDCG@k:          {metrics.get('avg_ndcg_at_k', 0):.4f}")
        print(f"  MRR:             {metrics.get('avg_mrr', 0):.4f}")
        print(f"  Precision@k:     {metrics.get('avg_precision_at_k', 0):.4f}")
        print(f"  Must-have terms: {metrics.get('avg_has_must_terms', 0):.4f}")
        
        # Answer metrics
        print("\nAnswer Quality Metrics:")
        print(f"  Exact match:     {metrics.get('avg_exact_match', 0):.4f}")
        print(f"  Partial match:   {metrics.get('avg_partial_match', 0):.4f}")
        print(f"  Token F1:        {metrics.get('avg_token_f1', 0):.4f}")
        print(f"  Avg length:      {metrics.get('avg_answer_length', 0):.1f} tokens")
        
        # Performance metrics
        print("\nPerformance Metrics:")
        print(f"  Avg query time:  {metrics.get('avg_query_time_ms', 0):.1f} ms")
        print(f"  Avg citations:   {metrics.get('avg_num_citations', 0):.1f}")
        print(f"  Success rate:    {1 - metrics.get('error_rate', 0):.4f}")


def _run_tiny_smoke_eval() -> None:
    """Run a self-contained offline smoke evaluation and log it to MLflow.

    This exercises the full MLflow tracking pipeline (experiment, run, params,
    metrics, artifact) using a tiny canned QA set scored with offline
    token-overlap metrics. It requires no retrieval infrastructure or answer
    provider, which makes it suitable for CI where those services are absent.
    """
    import mlflow

    from src.config.env import resolve_env

    experiment = resolve_env("PIPELINE", "rag_evaluation_ci") or "rag_evaluation_ci"

    # Tiny canned QA set: (question, gold answer, predicted answer).
    examples = [
        {"query": "What is the capital of France?", "gold": "Paris", "pred": "Paris"},
        {"query": "How many sides does a triangle have?", "gold": "three", "pred": "three"},
    ]

    def _tokens(text: str) -> set:
        return set(text.lower().split())

    def _token_f1(pred: str, gold: str) -> float:
        pred_tokens, gold_tokens = _tokens(pred), _tokens(gold)
        if not pred_tokens or not gold_tokens:
            return 0.0
        true_positives = len(pred_tokens & gold_tokens)
        if true_positives == 0:
            return 0.0
        precision = true_positives / len(pred_tokens)
        recall = true_positives / len(gold_tokens)
        return 2 * precision * recall / (precision + recall)

    exact = [float(e["pred"].strip().lower() == e["gold"].strip().lower()) for e in examples]
    token_f1 = [_token_f1(e["pred"], e["gold"]) for e in examples]

    logger.info("Running tiny offline smoke evaluation (%d examples)", len(examples))

    # validate_tags=False: this is a CI smoke test, not a tracked experiment, so
    # it should not require the full Issue #220 tag set (pipeline / data_version)
    # or a non-"ci" environment to be configured.
    with mlrun(
        name="tiny_smoke",
        experiment=experiment,
        tags={"evaluation_type": "tiny_smoke", "mode": "ci", "issue": "235"},
        validate_tags=False,
    ):
        mlflow.log_param("mode", "tiny")
        mlflow.log_param("dataset_size", len(examples))
        mlflow.log_metric("avg_exact_match", sum(exact) / len(exact))
        mlflow.log_metric("avg_token_f1", sum(token_f1) / len(token_f1))

        summary = {
            "examples": examples,
            "exact_match": exact,
            "token_f1": token_f1,
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="tiny_eval_", delete=False
        ) as handle:
            json.dump(summary, handle, indent=2)
            artifact_path = handle.name
        mlflow.log_artifact(artifact_path, "tiny_eval")

    print("✅ Tiny smoke evaluation complete — MLflow run logged.")


async def main():
    """Main evaluation function with CLI support."""
    parser = argparse.ArgumentParser(description="RAG System Evaluation with Configuration Comparison")
    
    # Dataset options
    parser.add_argument("--dataset", type=str, default="evals/qa_dev.jsonl",
                       help="Path to evaluation dataset (JSONL format)")
    parser.add_argument("--sample", type=int, default=None,
                       help="Sample N examples from dataset for quick testing")
    
    # Configuration options
    parser.add_argument("--config", type=str, default="baseline",
                       help="Configuration name (baseline, no_rerank, no_fusion, high_k, semantic_heavy)")
    parser.add_argument("--compare", action="store_true",
                       help="Compare multiple predefined configurations")
    parser.add_argument("--custom", action="store_true",
                       help="Use custom configuration from CLI args")
    
    # Custom configuration parameters
    parser.add_argument("--k", type=int, default=5,
                       help="Number of documents to retrieve")
    parser.add_argument("--fusion", type=bool, default=True,
                       help="Enable query fusion")
    parser.add_argument("--rerank", type=bool, default=True,
                       help="Enable reranking")
    parser.add_argument("--provider", type=str, default="openai",
                       help="Answer provider (openai, anthropic, local)")
    parser.add_argument("--semantic-weight", type=float, default=0.7,
                       help="Semantic search weight in fusion (0.0-1.0)")
    
    # Output options
    parser.add_argument("--output", type=str, default=None,
                       help="Output CSV file path")
    parser.add_argument("--mlflow", action="store_true", default=True,
                       help="Enable MLflow logging")
    parser.add_argument("--tiny", action="store_true",
                       help="Run a tiny offline smoke evaluation (no retrieval "
                            "infra or answer provider) and log it to MLflow. "
                            "Used by CI to verify the tracking pipeline.")

    args = parser.parse_args()

    if args.tiny:
        _run_tiny_smoke_eval()
        return

    # Load dataset
    logger.info(f"Loading dataset from {args.dataset}")
    dataset = load_qa_dataset(args.dataset)
    
    if args.sample:
        dataset = dataset[:args.sample]
        logger.info(f"Using sample of {len(dataset)} examples")
    
    if args.compare:
        # Compare multiple configurations
        configs = list(create_predefined_configs().values())
        comparison_results = await compare_configurations(configs, dataset)
        
        print("\nConfiguration Comparison Results")
        print("=" * 60)
        print(comparison_results["comparison_table"])
        print(f"\nDetailed results saved to: {comparison_results['comparison_csv']}")
        
    else:
        # Single configuration evaluation
        if args.custom:
            config = EvaluationConfig.from_args(args)
        else:
            predefined_configs = create_predefined_configs()
            if args.config not in predefined_configs:
                logger.error(f"Unknown config: {args.config}. Available: {list(predefined_configs.keys())}")
                sys.exit(1)
            config = predefined_configs[args.config]
        
        # Run evaluation
        results = await run_evaluation(config, dataset)
        
        # Print results
        print_metrics_table(results)
        
        # Save CSV if specified
        if args.output:
            csv_path = results.get("csv_path")
            if csv_path and os.path.exists(csv_path):
                import shutil
                shutil.move(csv_path, args.output)
                print(f"\nResults saved to: {args.output}")
    
    logger.info("Evaluation completed successfully")


if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    asyncio.run(main())
