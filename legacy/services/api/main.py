"""
Main FastAPI application for NeuroNews services.
Issue #233: Answering pipeline + citations (FastAPI /ask)

This module creates the main FastAPI application and includes
the ask router for question answering with citations.
"""

import os
import sys
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from legacy.services.api.cache import QueryCache, get_query_cache
from legacy.services.api.middleware.ratelimit import SlidingWindowRateLimiter

# Add to path for imports
sys.path.append(os.path.dirname(__file__))

from legacy.services.api.routes.ask import router as ask_router

# Create FastAPI app
app = FastAPI(
    title="NeuroNews Services API",
    description="REST API for NeuroNews question answering with citations",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Configure CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Add rate limit middleware
app.add_middleware(SlidingWindowRateLimiter)

# Include the ask router
app.include_router(ask_router, prefix="/api")

# Initialize global query cache (can be injected or used in ask endpoint)
query_cache = get_query_cache()

@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "NeuroNews Services API",
        "version": "1.0.0",
        "endpoints": {
            "ask": "/api/ask/",
            "health": "/api/ask/health",
            "config": "/api/ask/config",
            "docs": "/docs"
        }
    }

@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "neuronews-services-api",
        "components": {
            "ask_api": "operational"
        }
    }

# Add GET endpoint for ask to support curl queries (Issue #233 DoD requirement)
@app.get("/api/ask")
async def ask_question_get(
    q: str = Query(..., description="The question to answer"),
    k: int = Query(5, ge=1, le=20, description="Number of documents to retrieve"),
    rerank: bool = Query(True, description="Whether to enable reranking"),
    fusion: bool = Query(True, description="Whether to enable query fusion"),
    provider: str = Query("openai", description="Answer provider to use")
):
    """
    GET endpoint for asking questions (curl-friendly for DoD testing).
    
    This mirrors the POST endpoint functionality but accepts query parameters
    for easier testing with curl as required by issue #233 DoD.
    """
    from legacy.services.api.routes.ask import ask_question, get_rag_service, AskRequest
    
    # Convert GET params to POST request model
    request = AskRequest(
        question=q,
        k=k,
        rerank_on=rerank,
        fusion=fusion,
        provider=provider
    )
    
    # Call the main ask function
    rag_service = get_rag_service()
    response = await ask_question(request, rag_service)
    
    return response

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
