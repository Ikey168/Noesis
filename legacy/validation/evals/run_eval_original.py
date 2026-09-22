"""
RAG Evaluation with MLflow Logging
Issue #223: Wire existing RAG eval harness to MLflow

This module provides comprehensive evaluation of the RAG system with MLflow tracking
for key metrics including Recall@k, nDCG@k, MRR, and Answer_F1.
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

import numpy as np
import pandas as pd
from sklearn.metrics import f1_score

# Add src to path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

try:
    from services.mlops.tracking import mlrun
    from legacy.services.rag.answer import RAGAnswerService
    from services.embeddings.provider import EmbeddingsProvider
except ImportError as e:
    print(f"Import error: {e}")
    print("Please ensure you're running from the project root directory")
    sys.exit(1)

# Configure logging
logger = logging.getLogger(__name__)


class RAGEvaluator:
    """
    RAG Evaluation Framework with MLflow logging.
    
    Features:
    - Recall@k, nDCG@k, MRR, Answer_F1 metrics calculation
    - Per-query results tracking with CSV artifact logging
    - Configuration parameter logging (fusion weights, k, reranker)
    - Comprehensive MLflow experiment tracking
    """

    def __init__(
        self,
        rag_service: Optional[RAGAnswerService] = None,
        k_values: List[int] = None,
        fusion_weights: Dict[str, float] = None,
        reranker_enabled: bool = True,
    ):
        """Initialize RAG evaluator with configuration."""
        self.rag_service = rag_service or RAGAnswerService()
        self.k_values = k_values or [1, 3, 5, 10]
        self.fusion_weights = fusion_weights or {
            "semantic": 0.6,
            "keyword": 0.4,
        }
        self.reranker_enabled = reranker_enabled
        
        # Evaluation results storage
        self.per_query_results = []
        self.overall_metrics = {}
        
        logger.info(f"RAG Evaluator initialized with k_values={self.k_values}")

    async def evaluate_dataset(
        self,
        dataset: List[Dict[str, Any]],
        experiment_name: str = "rag_evaluation",
        run_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate RAG system on a dataset with MLflow tracking.

        Args:
            dataset: List of evaluation examples with format:
                     [{"question": str, "ground_truth": List[str], "relevant_docs": List[str]}]
            experiment_name: MLflow experiment name
            run_name: Optional run name for MLflow

        Returns:
            Dictionary containing overall metrics and per-query results
        """
        if not run_name:
            run_name = f"rag_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        logger.info(f"Starting RAG evaluation on {len(dataset)} queries")

        with mlrun(
            name=run_name,
            experiment=experiment_name,
            tags={
                "description": f"RAG evaluation on {len(dataset)} queries",
                "pipeline": "rag_evaluation",
                "data_version": "v1.0",
                "env": "dev",
            }
        ):
            import mlflow
            
            # Log configuration parameters
            await self._log_config_params(mlflow)
            
            overall_start = time.time()
            
            # Reset evaluation state
            self.per_query_results = []
            self.overall_metrics = {}
            
            try:
                # Process each query in the dataset
                for i, example in enumerate(dataset):
                    logger.info(f"Processing query {i+1}/{len(dataset)}: {example['question'][:50]}...")
                    
                    query_result = await self._evaluate_single_query(example)
                    self.per_query_results.append(query_result)
                
                # Calculate overall metrics
                await self._calculate_overall_metrics()
                
                total_time = time.time() - overall_start
                
                # Log metrics to MLflow
                await self._log_metrics_to_mlflow(mlflow, total_time)
                
                # Log per-query results as CSV artifact
                await self._log_csv_artifact(mlflow)
                
                logger.info(f"Evaluation completed in {total_time:.2f}s")
                
                return {
                    "overall_metrics": self.overall_metrics,
                    "per_query_results": self.per_query_results,
                    "dataset_size": len(dataset),
                    "total_time": total_time,
                }

            except Exception as e:
                logger.error(f"Evaluation failed: {e}")
                mlflow.log_metric("evaluation_failed", 1)
                raise

    async def _log_config_params(self, mlflow) -> None:
        """Log configuration parameters to MLflow."""
        # Log k values
        mlflow.log_param("k_values", ",".join(map(str, self.k_values)))
        mlflow.log_param("max_k", max(self.k_values))
        
        # Log fusion weights
        for weight_type, weight_value in self.fusion_weights.items():
            mlflow.log_param(f"fusion_weight_{weight_type}", weight_value)
        
        # Log reranker configuration
        mlflow.log_param("reranker_enabled", self.reranker_enabled)
        
        # Log RAG service configuration
        mlflow.log_param("rag_default_k", self.rag_service.default_k)
        mlflow.log_param("rag_fusion_enabled", self.rag_service.fusion_enabled)
        mlflow.log_param("rag_answer_provider", self.rag_service.answer_provider)

    async def _evaluate_single_query(self, example: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate a single query and return detailed results."""
        question = example["question"]
        ground_truth_answer = example.get("ground_truth", "")
        relevant_doc_ids = example.get("relevant_docs", [])
        
        query_start = time.time()
        
        # Get RAG response directly without MLflow tracking to avoid nested runs
        max_k = max(self.k_values)
        
        # Reset metrics for this run
        self.rag_service._reset_metrics()
        
        # Direct call to document retrieval and answer generation without MLflow
        retrieved_docs = await self.rag_service._retrieve_documents(
            question, max_k, None, self.rag_service.fusion_enabled
        )
        
        if self.reranker_enabled and retrieved_docs:
            retrieved_docs = await self.rag_service._rerank_documents(question, retrieved_docs)
        
        answer_result = await self.rag_service._generate_answer(
            question, retrieved_docs, self.rag_service.answer_provider
        )
        
        citations = await self.rag_service._extract_citations(
            answer_result["answer"], retrieved_docs
        )
        
        query_time = time.time() - query_start
        
        # Extract retrieved document IDs (simulated from response metadata)
        retrieved_doc_ids = self._extract_retrieved_docs_from_list(retrieved_docs)
        generated_answer = answer_result["answer"]
        
        # Calculate metrics for different k values
        recall_metrics = {}
        ndcg_metrics = {}
        
        for k in self.k_values:
            # Recall@k
            recall_at_k = self._calculate_recall_at_k(retrieved_doc_ids[:k], relevant_doc_ids)
            recall_metrics[f"recall_at_{k}"] = recall_at_k
            
            # nDCG@k  
            ndcg_at_k = self._calculate_ndcg_at_k(retrieved_doc_ids[:k], relevant_doc_ids)
            ndcg_metrics[f"ndcg_at_{k}"] = ndcg_at_k
        
        # MRR (Mean Reciprocal Rank)
        mrr = self._calculate_mrr(retrieved_doc_ids, relevant_doc_ids)
        
        # Answer F1 score
        answer_f1 = self._calculate_answer_f1(generated_answer, ground_truth_answer)
        
        # Prepare per-query result
        query_result = {
            "question": question,
            "generated_answer": generated_answer,
            "ground_truth": ground_truth_answer,
            "retrieved_doc_count": len(retrieved_docs),
            "relevant_doc_count": len(relevant_doc_ids),
            "query_time_ms": query_time * 1000,
            "mrr": mrr,
            "answer_f1": answer_f1,
            **recall_metrics,
            **ndcg_metrics,
        }
        
        return query_result

    def _extract_retrieved_docs_from_list(self, retrieved_docs: List[Dict[str, Any]]) -> List[str]:
        """Extract retrieved document IDs from document list."""
        # In a real implementation, this would extract actual document IDs
        # For demo purposes, generate simulated document IDs based on retrieved docs
        return [doc.get("id", f"doc_{i}") for i, doc in enumerate(retrieved_docs)]

    def _extract_retrieved_docs(self, response: Dict[str, Any]) -> List[str]:
        """Extract retrieved document IDs from RAG response."""
        # In a real implementation, this would extract actual document IDs
        # For demo purposes, generate simulated document IDs
        doc_count = response["metadata"].get("documents_retrieved", 5)
        return [f"doc_{i}" for i in range(doc_count)]

    def _calculate_recall_at_k(self, retrieved_docs: List[str], relevant_docs: List[str]) -> float:
        """Calculate Recall@k metric."""
        if not relevant_docs:
            return 0.0
        
        retrieved_set = set(retrieved_docs)
        relevant_set = set(relevant_docs)
        
        intersection = retrieved_set.intersection(relevant_set)
        return len(intersection) / len(relevant_set)

    def _calculate_ndcg_at_k(self, retrieved_docs: List[str], relevant_docs: List[str]) -> float:
        """Calculate nDCG@k (Normalized Discounted Cumulative Gain) metric."""
        if not relevant_docs:
            return 0.0
        
        # Calculate DCG
        dcg = 0.0
        for i, doc_id in enumerate(retrieved_docs):
            if doc_id in relevant_docs:
                # Binary relevance: 1 if relevant, 0 if not
                relevance = 1.0
                dcg += relevance / np.log2(i + 2)  # i+2 because log2(1) = 0
        
        # Calculate IDCG (ideal DCG)
        idcg = 0.0
        for i in range(min(len(relevant_docs), len(retrieved_docs))):
            idcg += 1.0 / np.log2(i + 2)
        
        if idcg == 0:
            return 0.0
        
        return dcg / idcg

    def _calculate_mrr(self, retrieved_docs: List[str], relevant_docs: List[str]) -> float:
        """Calculate Mean Reciprocal Rank (MRR) metric."""
        if not relevant_docs:
            return 0.0
        
        for i, doc_id in enumerate(retrieved_docs):
            if doc_id in relevant_docs:
                return 1.0 / (i + 1)
        
        return 0.0

    def _calculate_answer_f1(self, generated_answer: str, ground_truth: str) -> float:
        """Calculate F1 score between generated and ground truth answers."""
        if not generated_answer or not ground_truth:
            return 0.0
        
        # Simple token-based F1 calculation
        generated_tokens = set(generated_answer.lower().split())
        ground_truth_tokens = set(ground_truth.lower().split())
        
        if not generated_tokens and not ground_truth_tokens:
            return 1.0
        if not generated_tokens or not ground_truth_tokens:
            return 0.0
        
        intersection = generated_tokens.intersection(ground_truth_tokens)
        
        precision = len(intersection) / len(generated_tokens)
        recall = len(intersection) / len(ground_truth_tokens)
        
        if precision + recall == 0:
            return 0.0
        
        return 2 * (precision * recall) / (precision + recall)

    async def _calculate_overall_metrics(self) -> None:
        """Calculate overall metrics from per-query results."""
        if not self.per_query_results:
            return
        
        num_queries = len(self.per_query_results)
        
        # Calculate average metrics
        self.overall_metrics = {
            "num_queries": num_queries,
            "avg_query_time_ms": np.mean([r["query_time_ms"] for r in self.per_query_results]),
            "avg_retrieved_docs": np.mean([r["retrieved_doc_count"] for r in self.per_query_results]),
            "avg_mrr": np.mean([r["mrr"] for r in self.per_query_results]),
            "avg_answer_f1": np.mean([r["answer_f1"] for r in self.per_query_results]),
        }
        
        # Calculate average recall and nDCG for each k
        for k in self.k_values:
            recall_key = f"recall_at_{k}"
            ndcg_key = f"ndcg_at_{k}"
            
            self.overall_metrics[f"avg_{recall_key}"] = np.mean([
                r[recall_key] for r in self.per_query_results
            ])
            
            self.overall_metrics[f"avg_{ndcg_key}"] = np.mean([
                r[ndcg_key] for r in self.per_query_results
            ])

    async def _log_metrics_to_mlflow(self, mlflow, total_time: float) -> None:
        """Log overall metrics to MLflow."""
        # Log evaluation metadata
        mlflow.log_metric("total_evaluation_time_s", total_time)
        mlflow.log_metric("queries_evaluated", self.overall_metrics["num_queries"])
        
        # Log performance metrics
        mlflow.log_metric("avg_query_time_ms", self.overall_metrics["avg_query_time_ms"])
        mlflow.log_metric("avg_retrieved_docs", self.overall_metrics["avg_retrieved_docs"])
        
        # Log retrieval metrics
        mlflow.log_metric("avg_mrr", self.overall_metrics["avg_mrr"])
        
        # Log answer quality metrics
        mlflow.log_metric("avg_answer_f1", self.overall_metrics["avg_answer_f1"])
        
        # Log recall and nDCG metrics for each k
        for k in self.k_values:
            mlflow.log_metric(f"avg_recall_at_{k}", self.overall_metrics[f"avg_recall_at_{k}"])
            mlflow.log_metric(f"avg_ndcg_at_{k}", self.overall_metrics[f"avg_ndcg_at_{k}"])

    async def _log_csv_artifact(self, mlflow) -> None:
        """Log per-query results as CSV artifact to MLflow."""
        try:
            # Create temporary CSV file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
                if self.per_query_results:
                    # Get field names from first result
                    fieldnames = list(self.per_query_results[0].keys())
                    
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(self.per_query_results)
                    
                    csv_path = f.name
            
            # Try to log CSV as MLflow artifact
            mlflow.log_artifact(csv_path, "evaluation_results")
            logger.info(f"Logged per-query results CSV with {len(self.per_query_results)} rows")
            
        except Exception as e:
            logger.warning(f"Could not log CSV artifact: {e}")
            logger.info(f"Per-query results ready for logging ({len(self.per_query_results)} queries)")
            
        finally:
            # Clean up temporary file
            try:
                os.unlink(csv_path)
            except:
                pass


def create_sample_dataset() -> List[Dict[str, Any]]:
    """Create a sample evaluation dataset for testing."""
    return [
        {
            "question": "What is artificial intelligence?",
            "ground_truth": "Artificial intelligence (AI) is the simulation of human intelligence in machines that are programmed to think and learn.",
            "relevant_docs": ["doc_0", "doc_1", "doc_7"],
        },
        {
            "question": "How does machine learning work?",
            "ground_truth": "Machine learning is a subset of AI that enables computers to learn and improve from experience without being explicitly programmed.",
            "relevant_docs": ["doc_2", "doc_4", "doc_8"],
        },
        {
            "question": "What are neural networks?",
            "ground_truth": "Neural networks are computing systems inspired by biological neural networks that can recognize patterns and make decisions.",
            "relevant_docs": ["doc_3", "doc_5", "doc_9"],
        },
        {
            "question": "What is deep learning?",
            "ground_truth": "Deep learning is a subset of machine learning that uses neural networks with multiple layers to analyze data.",
            "relevant_docs": ["doc_1", "doc_6", "doc_8"],
        },
        {
            "question": "How is AI used in healthcare?",
            "ground_truth": "AI in healthcare includes medical imaging analysis, drug discovery, personalized treatment recommendations, and diagnostic assistance.",
            "relevant_docs": ["doc_4", "doc_7", "doc_9"],
        },
    ]


def create_tiny_dataset() -> List[Dict[str, Any]]:
    """Create a tiny evaluation dataset for CI testing."""
    return [
        {
            "question": "What is AI?",
            "ground_truth": "Artificial intelligence is machine simulation of human intelligence.",
            "relevant_docs": ["doc_0", "doc_1"],
        },
        {
            "question": "What is ML?",
            "ground_truth": "Machine learning enables computers to learn from data.",
            "relevant_docs": ["doc_2", "doc_3"],
        },
    ]


async def main():
    """Run RAG evaluation demo."""
    parser = argparse.ArgumentParser(description="Run RAG evaluation")
    parser.add_argument("--tiny", action="store_true", help="Run tiny evaluation for CI")
    args = parser.parse_args()
    
    if args.tiny:
        logger.info("Starting tiny RAG evaluation for CI")
        dataset = create_tiny_dataset()
        experiment_name = "rag_evaluation_ci"
        run_name = f"ci_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    else:
        logger.info("Starting RAG evaluation demo")
        dataset = create_sample_dataset()
        experiment_name = "rag_evaluation_demo"
        run_name = f"demo_eval_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    # Create evaluator with appropriate settings for the context
    if args.tiny:
        # Simpler configuration for CI
        evaluator = RAGEvaluator(
            k_values=[1, 3],
            fusion_weights={"semantic": 0.7, "keyword": 0.3},
            reranker_enabled=True,
        )
    else:
        # Full configuration for demo
        evaluator = RAGEvaluator(
            k_values=[1, 3, 5],
            fusion_weights={"semantic": 0.7, "keyword": 0.3},
            reranker_enabled=True,
        )
    
    # Run evaluation
    results = await evaluator.evaluate_dataset(
        dataset=dataset,
        experiment_name=experiment_name,
        run_name=run_name,
    )
    
    # Print results
    print("\n" + "="*50)
    if args.tiny:
        print("TINY RAG EVALUATION RESULTS (CI)")
    else:
        print("RAG EVALUATION RESULTS")
    print("="*50)
    
    print(f"\nDataset size: {results['dataset_size']} queries")
    print(f"Evaluation time: {results['total_time']:.2f}s")
    
    print("\nOverall Metrics:")
    for metric, value in results['overall_metrics'].items():
        if isinstance(value, float):
            print(f"  {metric}: {value:.4f}")
        else:
            print(f"  {metric}: {value}")
    
    print(f"\nPer-query results logged to MLflow")
    print(f"Check MLflow UI for detailed results and CSV artifacts")
    
    if args.tiny:
        logger.info("Tiny RAG evaluation for CI completed")
    else:
        logger.info("RAG evaluation demo completed")


if __name__ == "__main__":
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    asyncio.run(main())
