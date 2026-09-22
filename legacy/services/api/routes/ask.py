"""
Ask API Routes with MLflow Tracking
Issue #218: Instrument /ask pipeline with MLflow tracking

This module implements the /ask API endpoint with comprehensive MLflow tracking
and configurable sampling rate for logging requests.
"""

import asyncio
import json
import logging
import os
import random
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Depends
from pydantic import BaseModel, Field

# Import validation utilities
try:
    from legacy.services.api.validation import ValidatedAskRequest, ValidatedAskResponse, get_schema_validator
except ImportError:
    # Fallback if validation module not available
    ValidatedAskRequest = None
    ValidatedAskResponse = None
    get_schema_validator = None

# Shared query cache (avoid circular import with legacy.services.api.main)
try:
    from legacy.services.api.cache import get_query_cache

    query_cache = get_query_cache()
except ImportError:
    query_cache = None

# Add src to path for imports
sys.path.append(os.path.join(os.path.dirname(__file__), "../../.."))

try:
    from services.mlops.tracking import mlrun
    from legacy.services.rag.answer import RAGAnswerService
    from services.embeddings.provider import EmbeddingProvider
except ImportError as e:  # degrade gracefully; endpoints fail at request time
    logging.getLogger(__name__).warning(f"RAG dependencies unavailable: {e}")
    mlrun = None
    RAGAnswerService = None
    EmbeddingProvider = None

# Configure logging
logger = logging.getLogger(__name__)

# Router setup
router = APIRouter(prefix="/ask", tags=["question-answering"])

# Configuration
ASK_LOG_SAMPLE_RATE = float(os.getenv("ASK_LOG_SAMPLE", "0.2"))  # 20% default sampling
logger.info(f"Ask API MLflow sampling rate: {ASK_LOG_SAMPLE_RATE}")


class AskRequest(BaseModel):
    """Request model for the ask endpoint with JSON Schema validation."""
    question: str = Field(..., min_length=3, max_length=500, description="The question to answer")
    k: Optional[int] = Field(5, ge=1, le=20, description="Number of documents to retrieve")
    filters: Optional[Dict[str, Any]] = Field(None, description="Filters to apply during retrieval")
    rerank_on: Optional[bool] = Field(True, description="Whether to enable reranking")
    fusion: Optional[bool] = Field(True, description="Whether to enable query fusion")
    provider: Optional[str] = Field("openai", description="Answer provider to use")

    def __init__(self, **data):
        # Validate against JSON Schema if validator is available
        if get_schema_validator:
            try:
                validator = get_schema_validator()
                validator.validate_and_raise("ask-request-v1", data)
            except Exception as e:
                logger.warning(f"JSON Schema validation failed: {e}")
        
        # Continue with Pydantic validation
        super().__init__(**data)


class AskResponse(BaseModel):
    """Response model for the ask endpoint."""
    question: str = Field(..., description="The original question")
    answer: str = Field(..., description="The generated answer")
    citations: List[Dict[str, Any]] = Field(..., description="List of citations with sources")
    metadata: Dict[str, Any] = Field(..., description="Response metadata and timing")
    request_id: str = Field(..., description="Unique request identifier")
    tracked_in_mlflow: bool = Field(..., description="Whether this request was logged to MLflow")


class CitationModel(BaseModel):
    """Model for citation information."""
    citation_id: int
    title: str
    source: str
    url: str
    relevance_score: float
    published_date: str
    excerpt: str
    citation_strength: float


# Global service instances (would typically be dependency-injected)
_rag_service: Optional[RAGAnswerService] = None
_embeddings_provider: Optional[EmbeddingProvider] = None


def get_rag_service() -> RAGAnswerService:
    """Get or create RAG service instance."""
    global _rag_service, _embeddings_provider
    
    if _rag_service is None:
        if _embeddings_provider is None:
            _embeddings_provider = EmbeddingProvider(
                model_name="all-MiniLM-L6-v2",
                batch_size=16,
            )
        
        _rag_service = RAGAnswerService(
            embeddings_provider=_embeddings_provider,
            default_k=5,
            rerank_enabled=True,
            fusion_enabled=True,
            answer_provider="openai"
        )
        
        logger.info("RAG service initialized")
    
    return _rag_service


def should_log_to_mlflow() -> bool:
    """Determine if this request should be logged to MLflow based on sampling rate."""
    return random.random() < ASK_LOG_SAMPLE_RATE


@router.post("/", response_model=AskResponse)
async def ask_question(
    request: AskRequest,
    rag_service: RAGAnswerService = Depends(get_rag_service)
) -> AskResponse:
    """
    Answer a question using RAG with optional MLflow tracking.
    
    This endpoint processes questions through a retrieval-augmented generation
    pipeline with comprehensive tracking and citation support.
    """
    # Generate unique request ID
    request_id = f"ask_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}"
    
    # Determine if we should log to MLflow
    should_track = should_log_to_mlflow()
    
    logger.info(
        f"Processing ask request {request_id}: '{request.question[:50]}...' "
        f"(MLflow tracking: {'enabled' if should_track else 'disabled'})"
    )

    # Try cache lookup
    cache_result = None
    if query_cache:
        cache_result = query_cache.get(
            request.question,
            request.filters or {},
            request.provider or "openai",
            request.k or 5,
            request.rerank_on or False
        )
        if cache_result:
            logger.info(f"Cache hit for request {request_id}")
            cache_result["request_id"] = request_id
            cache_result["tracked_in_mlflow"] = False
            return AskResponse(**cache_result)
    
    try:
        if should_track:
            # Full MLflow tracking
            response = await rag_service.answer_question(
                question=request.question,
                k=request.k,
                filters=request.filters,
                rerank_on=request.rerank_on,
                fusion=request.fusion,
                provider=request.provider,
                experiment_name="ask_api_production",
                run_name=request_id
            )
            tracked_in_mlflow = True
            
        else:
            # No MLflow tracking - direct processing
            response = await _answer_question_without_tracking(
                rag_service, request, request_id
            )
            tracked_in_mlflow = False

        # Prepare API response
        api_response = AskResponse(
            question=response["question"],
            answer=response["answer"],
            citations=response["citations"],
            metadata=response["metadata"],
            request_id=request_id,
            tracked_in_mlflow=tracked_in_mlflow
        )

        # Store result in cache
        if query_cache and not should_track:
            query_cache.set(
                request.question,
                request.filters or {},
                request.provider or "openai",
                request.k or 5,
                request.rerank_on or False,
                api_response.dict()
            )

        logger.info(
            f"Request {request_id} completed in {response['metadata']['total_time_ms']:.1f}ms: "
            f"{len(response['citations'])} citations, MLflow: {tracked_in_mlflow}"
        )

        return api_response

    except Exception as e:
        logger.error(f"Request {request_id} failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process question: {str(e)}"
        )


async def _answer_question_without_tracking(
    rag_service: RAGAnswerService,
    request: AskRequest,
    request_id: str
) -> Dict[str, Any]:
    """Answer question without MLflow tracking for non-sampled requests."""
    import time
    
    overall_start = time.time()
    
    # Reset service metrics
    rag_service._reset_metrics()
    
    # Step 1: Retrieve documents
    retrieval_start = time.time()
    retrieved_docs = await rag_service._retrieve_documents(
        request.question, 
        request.k or 5, 
        request.filters, 
        request.fusion or True
    )
    retrieval_time = (time.time() - retrieval_start) * 1000
    
    # Step 2: Rerank if enabled
    rerank_time = 0
    if request.rerank_on and retrieved_docs:
        rerank_start = time.time()
        retrieved_docs = await rag_service._rerank_documents(
            request.question, retrieved_docs
        )
        rerank_time = (time.time() - rerank_start) * 1000
    
    # Step 3: Generate answer
    answer_start = time.time()
    answer_result = await rag_service._generate_answer(
        request.question, retrieved_docs, request.provider or "openai"
    )
    answer_time = (time.time() - answer_start) * 1000
    
    # Step 4: Extract citations
    citations = await rag_service._extract_citations(
        answer_result["answer"], retrieved_docs
    )
    
    # Prepare response
    total_time = (time.time() - overall_start) * 1000
    
    response = {
        "question": request.question,
        "answer": answer_result["answer"],
        "citations": citations,
        "metadata": {
            "retrieval_time_ms": retrieval_time,
            "answer_time_ms": answer_time,
            "rerank_time_ms": rerank_time,
            "total_time_ms": total_time,
            "documents_retrieved": len(retrieved_docs),
            "provider_used": request.provider or "openai",
            "rerank_enabled": request.rerank_on or False,
            "fusion_enabled": request.fusion or False,
            "request_id": request_id,
            "sampling_status": "not_tracked",
        },
    }
    
    return response


@router.get("/health")
async def ask_health_check():
    """Health check endpoint for the ask API."""
    try:
        # Check if service can be instantiated
        service = get_rag_service()
        
        return {
            "status": "healthy",
            "service": "ask-api",
            "mlflow_sampling_rate": ASK_LOG_SAMPLE_RATE,
            "embedding_model": service.embeddings_provider.model_name,
            "default_provider": service.answer_provider,
            "timestamp": datetime.now().isoformat(),
        }
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=f"Ask API unhealthy: {str(e)}"
        )


@router.get("/config")
async def get_ask_config():
    """Get current ask API configuration."""
    try:
        service = get_rag_service()
        
        return {
            "sampling": {
                "mlflow_sample_rate": ASK_LOG_SAMPLE_RATE,
                "sample_rate_source": "ASK_LOG_SAMPLE environment variable",
            },
            "retrieval": {
                "default_k": service.default_k,
                "rerank_enabled": service.rerank_enabled,
                "fusion_enabled": service.fusion_enabled,
                "embedding_model": service.embeddings_provider.model_name,
                "embedding_dimension": service.embeddings_provider.embedding_dimension,
            },
            "generation": {
                "default_provider": service.answer_provider,
                "available_providers": ["openai", "anthropic", "local"],
            },
            "mlflow": {
                "experiment_name": "ask_api_production",
                "tracking_enabled": True,
                "artifacts_logged": ["citations.json", "trace.json", "qa_summary.json"],
            },
            "api": {
                "max_question_length": 500,
                "max_k_value": 20,
                "endpoint_version": "1.0.0",
            }
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get config: {str(e)}"
        )


class BatchAskRequest(BaseModel):
    """Request model for batch ask endpoint."""
    questions: List[str] = Field(..., min_length=1, max_length=10, description="List of questions to answer")
    k: Optional[int] = Field(5, ge=1, le=20, description="Number of documents to retrieve")
    filters: Optional[Dict[str, Any]] = Field(None, description="Filters to apply during retrieval")
    rerank_on: Optional[bool] = Field(True, description="Whether to enable reranking")
    fusion: Optional[bool] = Field(True, description="Whether to enable query fusion")
    provider: Optional[str] = Field("openai", description="Answer provider to use")


@router.post("/batch")
async def ask_batch_questions(
    request: BatchAskRequest,
    rag_service: RAGAnswerService = Depends(get_rag_service)
) -> Dict[str, Any]:
    """
    Process multiple questions in batch with MLflow tracking.
    
    Useful for processing multiple related questions efficiently.
    Each question in the batch follows the same sampling rules.
    """
    batch_id = f"batch_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{random.randint(1000, 9999)}"
    
    logger.info(f"Processing batch request {batch_id} with {len(request.questions)} questions")
    
    try:
        batch_start = time.time()
        results = []
        tracked_count = 0
        
        for i, question in enumerate(request.questions):
            should_track = should_log_to_mlflow()
            
            ask_request = AskRequest(
                question=question,
                k=request.k,
                filters=request.filters,
                rerank_on=request.rerank_on,
                fusion=request.fusion,
                provider=request.provider
            )
            
            if should_track:
                response = await rag_service.answer_question(
                    question=question,
                    k=request.k,
                    filters=request.filters,
                    rerank_on=request.rerank_on,
                    fusion=request.fusion,
                    provider=request.provider,
                    experiment_name="ask_api_batch",
                    run_name=f"{batch_id}_q{i+1}"
                )
                tracked_count += 1
            else:
                response = await _answer_question_without_tracking(
                    rag_service, ask_request, f"{batch_id}_q{i+1}"
                )
            
            response["tracked_in_mlflow"] = should_track
            results.append(response)
        
        batch_time = (time.time() - batch_start) * 1000
        
        return {
            "batch_id": batch_id,
            "total_questions": len(request.questions),
            "results": results,
            "batch_metadata": {
                "total_time_ms": batch_time,
                "avg_time_per_question": batch_time / len(request.questions),
                "questions_tracked": tracked_count,
                "tracking_rate": tracked_count / len(request.questions),
                "sampling_rate_configured": ASK_LOG_SAMPLE_RATE,
            }
        }
        
    except Exception as e:
        logger.error(f"Batch request {batch_id} failed: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to process batch questions: {str(e)}"
        )


# Module-level function for testing
async def demo_ask_api():
    """Demo function to test the ask API functionality."""
    service = get_rag_service()
    
    # Test single question
    request = AskRequest(
        question="What are the benefits of renewable energy?",
        k=5,
        filters={"category": "environment"},
        rerank_on=True,
        fusion=True,
        provider="openai"
    )
    
    response = await ask_question(request, service)
    
    print("Demo Ask API Response:")
    print(f"Question: {response.question}")
    print(f"Answer: {response.answer[:200]}...")
    print(f"Citations: {len(response.citations)}")
    print(f"Tracked in MLflow: {response.tracked_in_mlflow}")
    print(f"Total time: {response.metadata['total_time_ms']:.1f}ms")


if __name__ == "__main__":
    import asyncio
    logging.basicConfig(level=logging.INFO)
    asyncio.run(demo_ask_api())
