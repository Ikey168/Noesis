"""
RAG Answer Service with MLflow Tracking
Issue #218: Instrument /ask pipeline with MLflow tracking

This module provides question answering capabilities using retrieval-augmented
generation with comprehensive MLflow tracking of retrieval and answer metrics.
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

# Add src to path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), "../.."))

try:
    from services.mlops.tracking import mlrun
    from services.embeddings.provider import EmbeddingProvider
    from jobs.rag.indexer import RAGIndexer
except ImportError as e:  # pragma: no cover
    # Re-raise rather than sys.exit so importing this module (e.g. during test
    # collection) fails gracefully instead of terminating the interpreter.
    raise ImportError(
        f"{e}. Ensure you're running from the project root with RAG "
        "dependencies installed."
    ) from e

# Configure logging
logger = logging.getLogger(__name__)


class RAGAnswerService:
    """
    RAG Answer Service with MLflow tracking integration.
    
    Features:
    - Document retrieval with configurable parameters
    - Answer generation with multiple providers
    - Citation extraction and scoring
    - Comprehensive MLflow tracking
    - Performance monitoring and artifacts
    """

    def __init__(
        self,
        embeddings_provider: Optional[EmbeddingProvider] = None,
        default_k: int = 5,
        rerank_enabled: bool = True,
        fusion_enabled: bool = True,
        answer_provider: str = "openai",
    ):
        """
        Initialize RAG answer service.

        Args:
            embeddings_provider: Provider for generating query embeddings
            default_k: Default number of documents to retrieve
            rerank_enabled: Whether to enable reranking of results
            fusion_enabled: Whether to enable query fusion
            answer_provider: Provider for answer generation
        """
        self.embeddings_provider = embeddings_provider or EmbeddingProvider()
        self.default_k = default_k
        self.rerank_enabled = rerank_enabled
        self.fusion_enabled = fusion_enabled
        self.answer_provider = answer_provider

        # Metrics tracking
        self.metrics = {
            "retrieval_ms": 0.0,
            "answer_ms": 0.0,
            "k_used": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "answer_length": 0,
            "num_citations": 0,
            "rerank_time_ms": 0.0,
            "fusion_time_ms": 0.0,
        }

        # Artifacts storage
        self.artifacts = {
            "citations": [],
            "retrieval_trace": [],
            "query_processing": {},
        }

    async def answer_question(
        self,
        question: str,
        k: Optional[int] = None,
        filters: Optional[Dict[str, Any]] = None,
        rerank_on: bool = None,
        fusion: bool = None,
        provider: Optional[str] = None,
        experiment_name: str = "rag_question_answering",
        run_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Answer a question using RAG with MLflow tracking.

        Args:
            question: The question to answer
            k: Number of documents to retrieve
            filters: Filters to apply during retrieval
            rerank_on: Whether to enable reranking
            fusion: Whether to enable query fusion
            provider: Answer provider to use
            experiment_name: MLflow experiment name
            run_name: Optional run name for MLflow

        Returns:
            Answer response with citations and metadata
        """
        # Set defaults
        k = k or self.default_k
        rerank_on = rerank_on if rerank_on is not None else self.rerank_enabled
        fusion = fusion if fusion is not None else self.fusion_enabled
        provider = provider or self.answer_provider

        if not run_name:
            run_name = f"ask_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        with mlrun(
            name=run_name,
            experiment=experiment_name,
            tags={"description": f"RAG Q&A: {question[:100]}..."}
        ):
            # Log parameters
            import mlflow
            mlflow.log_param("k", k)
            mlflow.log_param("filters", json.dumps(filters) if filters else "none")
            mlflow.log_param("rerank_on", rerank_on)
            mlflow.log_param("fusion", fusion)
            mlflow.log_param("provider", provider)
            mlflow.log_param("question_length", len(question))

            # Reset metrics for this run
            self._reset_metrics()
            
            overall_start = time.time()
            
            try:
                # Step 1: Process query and retrieve documents
                logger.info(f"Processing question: {question[:100]}...")
                retrieval_start = time.time()
                
                retrieved_docs = await self._retrieve_documents(
                    question, k, filters, fusion
                )
                
                retrieval_time = (time.time() - retrieval_start) * 1000
                self.metrics["retrieval_ms"] = retrieval_time
                self.metrics["k_used"] = len(retrieved_docs)

                # Step 2: Rerank if enabled
                if rerank_on and retrieved_docs:
                    rerank_start = time.time()
                    retrieved_docs = await self._rerank_documents(question, retrieved_docs)
                    self.metrics["rerank_time_ms"] = (time.time() - rerank_start) * 1000

                # Step 3: Generate answer
                answer_start = time.time()
                answer_result = await self._generate_answer(
                    question, retrieved_docs, provider
                )
                answer_time = (time.time() - answer_start) * 1000
                self.metrics["answer_ms"] = answer_time

                # Step 4: Extract citations
                citations = await self._extract_citations(
                    answer_result["answer"], retrieved_docs
                )
                self.metrics["num_citations"] = len(citations)

                # Update final metrics
                self.metrics["tokens_in"] = answer_result.get("tokens_in", 0)
                self.metrics["tokens_out"] = answer_result.get("tokens_out", 0)
                self.metrics["answer_length"] = len(answer_result["answer"])

                # Prepare response
                response = {
                    "question": question,
                    "answer": answer_result["answer"],
                    "citations": citations,
                    "metadata": {
                        "retrieval_time_ms": retrieval_time,
                        "answer_time_ms": answer_time,
                        "total_time_ms": (time.time() - overall_start) * 1000,
                        "documents_retrieved": len(retrieved_docs),
                        "provider_used": provider,
                        "rerank_enabled": rerank_on,
                        "fusion_enabled": fusion,
                    },
                    "metrics": self.metrics,
                }

                # Log metrics to MLflow
                mlflow.log_metric("retrieval_ms", self.metrics["retrieval_ms"])
                mlflow.log_metric("answer_ms", self.metrics["answer_ms"])
                mlflow.log_metric("k_used", self.metrics["k_used"])
                mlflow.log_metric("tokens_in", self.metrics["tokens_in"])
                mlflow.log_metric("tokens_out", self.metrics["tokens_out"])
                mlflow.log_metric("answer_len", self.metrics["answer_length"])
                mlflow.log_metric("num_citations", self.metrics["num_citations"])
                
                if rerank_on:
                    mlflow.log_metric("rerank_time_ms", self.metrics["rerank_time_ms"])
                if fusion:
                    mlflow.log_metric("fusion_time_ms", self.metrics["fusion_time_ms"])

                # Log artifacts
                await self._log_artifacts(mlflow, citations, retrieved_docs, question)

                logger.info(
                    f"Question answered in {response['metadata']['total_time_ms']:.1f}ms: "
                    f"{len(retrieved_docs)} docs retrieved, {len(citations)} citations"
                )

                return response

            except Exception as e:
                logger.error(f"Failed to answer question: {e}")
                mlflow.log_metric("error", 1)
                raise

    async def _retrieve_documents(
        self, 
        question: str, 
        k: int, 
        filters: Optional[Dict], 
        fusion: bool
    ) -> List[Dict[str, Any]]:
        """Retrieve relevant documents for the question."""
        try:
            # Store query processing info
            self.artifacts["query_processing"] = {
                "original_query": question,
                "query_length": len(question),
                "filters_applied": filters is not None,
                "fusion_enabled": fusion,
            }

            # Simulate query fusion if enabled
            if fusion:
                fusion_start = time.time()
                expanded_queries = await self._expand_query(question)
                self.metrics["fusion_time_ms"] = (time.time() - fusion_start) * 1000
                self.artifacts["query_processing"]["expanded_queries"] = expanded_queries
            else:
                expanded_queries = [question]

            # Generate query embedding (no nested MLflow tracking)
            # Since we're already in an MLflow context, just generate embeddings directly
            query_texts = expanded_queries
            query_embeddings = []
            for text in query_texts:
                # Simulate embedding generation
                embedding = self.embeddings_provider.model.encode(
                    text, convert_to_numpy=True, show_progress_bar=False
                )
                query_embeddings.append(embedding)

            # Simulate document retrieval
            retrieved_docs = await self._perform_vector_search(
                query_embeddings[0], k, filters
            )

            # Store retrieval trace
            self.artifacts["retrieval_trace"] = [
                {
                    "doc_id": doc["id"],
                    "title": doc["title"][:100] + "..." if len(doc["title"]) > 100 else doc["title"],
                    "score": doc["score"],
                    "source": doc.get("source", "unknown"),
                    "rank": i + 1,
                }
                for i, doc in enumerate(retrieved_docs)
            ]

            return retrieved_docs

        except Exception as e:
            logger.error(f"Document retrieval failed: {e}")
            raise

    async def _expand_query(self, question: str) -> List[str]:
        """Expand query using fusion techniques."""
        # Simulate query expansion
        expanded = [
            question,
            f"What is {question.lower()}",
            f"Explain {question.lower()}",
        ]
        return expanded[:2]  # Limit for demo

    async def _perform_vector_search(
        self, 
        query_embedding: np.ndarray, 
        k: int, 
        filters: Optional[Dict]
    ) -> List[Dict[str, Any]]:
        """Perform vector similarity search."""
        # Simulate vector search with realistic results
        # Generate enough documents to satisfy filtering + k requirement
        total_docs_needed = max(k * 3, 15)  # Ensure we have enough after filtering
        
        sample_docs = [
            {
                "id": f"doc_{i+1:03d}",
                "title": f"Sample Document {i+1}",
                "content": f"This is sample content for document {i+1} that contains relevant information about the query topic. " * 3,
                "source": f"source_{(i % 3) + 1}.com",
                "score": 0.95 - (i * 0.05),  # Decreasing relevance scores
                "published_date": f"2024-{(i % 12) + 1:02d}-{((i * 3) % 28) + 1:02d}",
                "category": ["technology", "science", "business", "technology", "technology"][i % 5],  # More technology docs
            }
            for i in range(total_docs_needed)
        ]

        # Apply filters if provided
        if filters:
            filtered_docs = []
            for doc in sample_docs:
                include = True
                for filter_key, filter_value in filters.items():
                    if filter_key in doc and doc[filter_key] != filter_value:
                        include = False
                        break
                if include:
                    filtered_docs.append(doc)
            sample_docs = filtered_docs

        # Ensure we have enough documents after filtering
        if len(sample_docs) < k:
            # Generate additional documents if needed
            additional_docs = [
                {
                    "id": f"doc_{len(sample_docs) + i + 1:03d}",
                    "title": f"Additional Document {len(sample_docs) + i + 1}",
                    "content": f"This is additional content for document {len(sample_docs) + i + 1} that matches the applied filters. " * 3,
                    "source": f"source_{(i % 3) + 1}.com",
                    "score": 0.7 - (i * 0.05),
                    "published_date": f"2024-{((i + 6) % 12) + 1:02d}-{((i * 2) % 28) + 1:02d}",
                    "category": filters.get("category", "technology") if filters else "technology",
                }
                for i in range(k - len(sample_docs))
            ]
            sample_docs.extend(additional_docs)

        # Return top k documents
        return sample_docs[:k]

    async def _rerank_documents(
        self, question: str, documents: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Rerank documents based on relevance to question."""
        # Simulate reranking by slightly shuffling and adjusting scores
        reranked = documents.copy()
        
        # Simulate reranking logic
        for i, doc in enumerate(reranked):
            # Add small random adjustment to simulate reranking
            adjustment = np.random.uniform(-0.05, 0.05)
            doc["score"] = max(0.0, min(1.0, doc["score"] + adjustment))
            doc["rerank_score"] = doc["score"]

        # Sort by new scores
        reranked.sort(key=lambda x: x["score"], reverse=True)
        
        return reranked

    async def _generate_answer(
        self, question: str, documents: List[Dict[str, Any]], provider: str
    ) -> Dict[str, Any]:
        """Generate answer using the specified provider."""
        try:
            # Simulate different providers
            if provider == "openai":
                base_tokens_in = 100 + len(question.split()) * 2
                base_tokens_out = 150
            elif provider == "anthropic":
                base_tokens_in = 90 + len(question.split()) * 2
                base_tokens_out = 160
            else:
                base_tokens_in = 80 + len(question.split()) * 2
                base_tokens_out = 140

            # Add tokens for context documents
            context_tokens = sum(len(doc["content"].split()) for doc in documents[:3])
            total_tokens_in = base_tokens_in + context_tokens

            # Generate answer based on question type
            if "what" in question.lower():
                answer = f"Based on the retrieved documents, {question.lower()} refers to a comprehensive concept that involves multiple aspects. The analysis of relevant sources indicates that this topic encompasses several key components and has significant implications for the field."
            elif "how" in question.lower():
                answer = f"According to the available information, {question.lower()} can be accomplished through a systematic approach. The documents suggest a multi-step process that includes preparation, implementation, and evaluation phases."
            elif "why" in question.lower():
                answer = f"The documents provide evidence that {question.lower()} is primarily due to several interconnected factors. Research indicates that this phenomenon occurs as a result of specific conditions and influences."
            else:
                answer = f"The retrieved documents provide comprehensive information about your query. Based on the analysis of multiple sources, the topic involves several important considerations that are relevant to understanding the broader context."

            # Add citation markers
            answer += " [1][2][3]"

            return {
                "answer": answer,
                "tokens_in": total_tokens_in,
                "tokens_out": base_tokens_out,
                "provider": provider,
                "confidence": 0.85,
            }

        except Exception as e:
            logger.error(f"Answer generation failed: {e}")
            raise

    async def _extract_citations(
        self, answer: str, documents: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Extract and format citations from retrieved documents."""
        citations = []
        
        # Take top documents as citations (simulate citation extraction)
        for i, doc in enumerate(documents[:5]):  # Top 5 as potential citations
            citation = {
                "citation_id": i + 1,
                "title": doc["title"],
                "source": doc.get("source", "Unknown"),
                "url": f"https://{doc.get('source', 'example.com')}/article/{doc['id']}",
                "relevance_score": doc["score"],
                "published_date": doc.get("published_date", "Unknown"),
                "excerpt": doc["content"][:200] + "..." if len(doc["content"]) > 200 else doc["content"],
                "citation_strength": min(1.0, doc["score"] + 0.1),  # Boost for actual citations
            }
            citations.append(citation)

        # Sort by relevance score
        citations.sort(key=lambda x: x["relevance_score"], reverse=True)
        
        return citations

    async def _log_artifacts(
        self, 
        mlflow, 
        citations: List[Dict], 
        retrieved_docs: List[Dict], 
        question: str
    ):
        """Log artifacts to MLflow."""
        try:
            import tempfile
            with tempfile.TemporaryDirectory() as temp_dir:
                
                # 1. Citations JSON
                citations_file = os.path.join(temp_dir, "citations.json")
                citations_data = {
                    "total_citations": len(citations),
                    "citations": citations,
                    "citation_metadata": {
                        "avg_relevance_score": np.mean([c["relevance_score"] for c in citations]) if citations else 0,
                        "top_source": citations[0]["source"] if citations else None,
                        "date_range": {
                            "earliest": min([c["published_date"] for c in citations if c["published_date"] != "Unknown"], default="Unknown"),
                            "latest": max([c["published_date"] for c in citations if c["published_date"] != "Unknown"], default="Unknown"),
                        }
                    }
                }
                with open(citations_file, "w") as f:
                    json.dump(citations_data, f, indent=2)
                mlflow.log_artifact(citations_file, "citations")

                # 2. Optional trace JSON
                trace_file = os.path.join(temp_dir, "trace.json")
                trace_data = {
                    "question": question,
                    "retrieval_trace": self.artifacts["retrieval_trace"],
                    "query_processing": self.artifacts["query_processing"],
                    "pipeline_steps": [
                        {
                            "step": "query_processing",
                            "duration_ms": self.metrics.get("fusion_time_ms", 0),
                            "success": True,
                        },
                        {
                            "step": "document_retrieval",
                            "duration_ms": self.metrics["retrieval_ms"],
                            "docs_found": self.metrics["k_used"],
                            "success": True,
                        },
                        {
                            "step": "reranking",
                            "duration_ms": self.metrics.get("rerank_time_ms", 0),
                            "enabled": self.metrics.get("rerank_time_ms", 0) > 0,
                            "success": True,
                        },
                        {
                            "step": "answer_generation",
                            "duration_ms": self.metrics["answer_ms"],
                            "tokens_processed": self.metrics["tokens_in"] + self.metrics["tokens_out"],
                            "success": True,
                        },
                    ],
                    "performance_summary": {
                        "total_pipeline_time": sum([
                            self.metrics["retrieval_ms"],
                            self.metrics["answer_ms"],
                            self.metrics.get("rerank_time_ms", 0),
                            self.metrics.get("fusion_time_ms", 0),
                        ]),
                        "bottleneck_step": max([
                            ("retrieval", self.metrics["retrieval_ms"]),
                            ("answer", self.metrics["answer_ms"]),
                            ("rerank", self.metrics.get("rerank_time_ms", 0)),
                            ("fusion", self.metrics.get("fusion_time_ms", 0)),
                        ], key=lambda x: x[1])[0]
                    }
                }
                with open(trace_file, "w") as f:
                    json.dump(trace_data, f, indent=2)
                mlflow.log_artifact(trace_file, "traces")

                # 3. Query and answer summary
                summary_file = os.path.join(temp_dir, "qa_summary.json")
                summary_data = {
                    "question_analysis": {
                        "question": question,
                        "question_type": self._classify_question_type(question),
                        "question_length": len(question),
                        "complexity_score": min(1.0, len(question.split()) / 20),
                    },
                    "answer_analysis": {
                        "answer_length": self.metrics["answer_length"],
                        "citation_count": self.metrics["num_citations"],
                        "answer_to_question_ratio": self.metrics["answer_length"] / max(1, len(question)),
                        "tokens_efficiency": self.metrics["tokens_out"] / max(1, self.metrics["tokens_in"]),
                    },
                    "retrieval_effectiveness": {
                        "documents_retrieved": self.metrics["k_used"],
                        "avg_relevance_score": np.mean([doc["score"] for doc in retrieved_docs]) if retrieved_docs else 0,
                        "citation_rate": self.metrics["num_citations"] / max(1, self.metrics["k_used"]),
                    }
                }
                with open(summary_file, "w") as f:
                    json.dump(summary_data, f, indent=2)
                mlflow.log_artifact(summary_file, "analysis")

                logger.debug("RAG artifacts logged to MLflow")

        except Exception as e:
            logger.warning(f"Failed to log artifacts: {e}")

    def _classify_question_type(self, question: str) -> str:
        """Classify the type of question."""
        question_lower = question.lower()
        if question_lower.startswith(("what", "which")):
            return "factual"
        elif question_lower.startswith(("how", "why")):
            return "explanatory"
        elif question_lower.startswith(("when", "where")):
            return "temporal_spatial"
        elif question_lower.startswith(("who")):
            return "entity"
        elif "?" in question:
            return "general_question"
        else:
            return "statement_query"

    def _reset_metrics(self):
        """Reset metrics for a new run."""
        self.metrics = {
            "retrieval_ms": 0.0,
            "answer_ms": 0.0,
            "k_used": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "answer_length": 0,
            "num_citations": 0,
            "rerank_time_ms": 0.0,
            "fusion_time_ms": 0.0,
        }
        
        self.artifacts = {
            "citations": [],
            "retrieval_trace": [],
            "query_processing": {},
        }

    def get_metrics(self) -> Dict[str, Any]:
        """Get current metrics."""
        return self.metrics.copy()


async def main():
    """Demo usage of RAGAnswerService with MLflow tracking."""
    service = RAGAnswerService(
        default_k=5,
        rerank_enabled=True,
        fusion_enabled=True,
        answer_provider="openai"
    )

    # Sample questions
    sample_questions = [
        "What are the latest developments in artificial intelligence?",
        "How does climate change affect global weather patterns?",
        "Why is renewable energy important for the future?",
    ]

    for question in sample_questions:
        logger.info(f"Answering question: {question}")
        
        response = await service.answer_question(
            question=question,
            k=5,
            filters={"category": "technology"},
            experiment_name="rag_qa_demo",
            run_name=f"demo_{question[:20].replace(' ', '_')}"
        )

        print(f"Question: {response['question']}")
        print(f"Answer: {response['answer'][:200]}...")
        print(f"Citations: {len(response['citations'])}")
        print(f"Time: {response['metadata']['total_time_ms']:.1f}ms")
        print("-" * 80)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
