"""
FastAPI Middleware for Prometheus Metrics Collection
Issue #236: Observability: Prometheus counters + logs

This middleware automatically collects metrics for all RAG API requests,
including timing, success/error rates, and request characteristics.
"""

import time
import logging
from typing import Callable, Optional
from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as StarletteResponse

# Import our metrics collector
from services.obs.metrics import metrics_collector

# Configure logging
logger = logging.getLogger(__name__)

class RAGMetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware to collect Prometheus metrics for RAG API endpoints.
    
    This middleware automatically tracks:
    - Request counts by endpoint and status
    - Request latency distributions
    - Error rates and types
    - RAG-specific metrics when available
    """
    
    def __init__(self, app: FastAPI, track_all_endpoints: bool = False):
        """
        Initialize the metrics middleware.
        
        Args:
            app: FastAPI application instance
            track_all_endpoints: Whether to track all endpoints or just RAG ones
        """
        super().__init__(app)
        self.track_all_endpoints = track_all_endpoints
        logger.info(f"RAG Metrics Middleware initialized (track_all={track_all_endpoints})")
    
    async def dispatch(self, request: Request, call_next: Callable) -> StarletteResponse:
        """
        Process each request and collect metrics.
        
        Args:
            request: Incoming HTTP request
            call_next: Next middleware/handler in chain
            
        Returns:
            Response with metrics collected
        """
        start_time = time.time()
        
        # Extract request information
        method = request.method
        path = request.url.path
        
        # Determine if this is a RAG endpoint
        is_rag_endpoint = self._is_rag_endpoint(path)
        
        # Skip non-RAG endpoints unless tracking all
        if not is_rag_endpoint and not self.track_all_endpoints:
            return await call_next(request)
        
        # Extract RAG-specific parameters from request
        rag_params = await self._extract_rag_params(request)
        
        try:
            # Process the request
            response = await call_next(request)
            
            # Calculate latency
            latency_ms = (time.time() - start_time) * 1000
            
            # Record metrics for RAG endpoints
            if is_rag_endpoint:
                await self._record_rag_metrics(
                    request, response, latency_ms, rag_params
                )
            
            # Add metrics headers for debugging
            if hasattr(response, 'headers'):
                response.headers["X-Request-Latency-Ms"] = str(round(latency_ms, 2))
                if is_rag_endpoint:
                    response.headers["X-RAG-Tracked"] = "true"
            
            return response
            
        except Exception as e:
            # Calculate latency for errors too
            latency_ms = (time.time() - start_time) * 1000
            
            # Record error metrics
            if is_rag_endpoint:
                await self._record_rag_error(request, e, latency_ms, rag_params)
            
            # Re-raise the exception
            raise e
    
    def _is_rag_endpoint(self, path: str) -> bool:
        """
        Determine if an endpoint is RAG-related.
        
        Args:
            path: Request path
            
        Returns:
            True if this is a RAG endpoint
        """
        rag_paths = [
            "/ask",
            "/search", 
            "/retrieve",
            "/answer",
            "/rag"
        ]
        
        return any(rag_path in path for rag_path in rag_paths)
    
    async def _extract_rag_params(self, request: Request) -> dict:
        """
        Extract RAG-specific parameters from the request.
        
        Args:
            request: HTTP request
            
        Returns:
            Dictionary of RAG parameters
        """
        params = {
            "provider": "unknown",
            "lang": "en", 
            "has_rerank": False,
            "k": 10
        }
        
        try:
            # Try to get parameters from query string
            query_params = request.query_params
            
            if "provider" in query_params:
                params["provider"] = query_params["provider"]
            
            if "lang" in query_params:
                params["lang"] = query_params["lang"]
                
            if "rerank" in query_params:
                params["has_rerank"] = query_params["rerank"].lower() in ["true", "1", "yes"]
                
            if "k" in query_params:
                try:
                    params["k"] = int(query_params["k"])
                except ValueError:
                    pass
            
            # For POST requests, try to get from body
            if request.method == "POST":
                # Note: We can't easily read the body here without consuming it
                # In practice, you'd want to extract this from the actual handler
                pass
                
        except Exception as e:
            logger.debug(f"Could not extract RAG params: {e}")
        
        return params
    
    async def _record_rag_metrics(
        self, 
        request: Request, 
        response: Response, 
        latency_ms: float,
        rag_params: dict
    ):
        """
        Record metrics for successful RAG requests.
        
        Args:
            request: HTTP request
            response: HTTP response  
            latency_ms: Request latency in milliseconds
            rag_params: Extracted RAG parameters
        """
        try:
            provider = rag_params.get("provider", "unknown")
            lang = rag_params.get("lang", "en")
            has_rerank = rag_params.get("has_rerank", False)
            k = rag_params.get("k", 10)
            
            # Determine status
            status = "success" if 200 <= response.status_code < 400 else "error"
            
            # Record query metrics
            metrics_collector.record_query(provider, lang, has_rerank, status)
            
            # For successful requests, record additional metrics
            if status == "success":
                # Record answer if this was an answer endpoint
                if "/ask" in request.url.path or "/answer" in request.url.path:
                    metrics_collector.record_answer(provider, lang, has_rerank)
                    
                    # Record answer latency
                    metrics_collector.record_answer_latency(latency_ms, provider, lang)
                
                # Record retrieval metrics if this was a search/retrieve endpoint
                if "/search" in request.url.path or "/retrieve" in request.url.path:
                    metrics_collector.record_retrieval_latency(latency_ms, provider, k)
                    metrics_collector.record_k_documents(k, provider, "api_request")
            
            logger.debug(f"Recorded RAG metrics for {request.url.path}: {latency_ms}ms")
            
        except Exception as e:
            logger.error(f"Failed to record RAG metrics: {e}")
    
    async def _record_rag_error(
        self,
        request: Request,
        error: Exception,
        latency_ms: float, 
        rag_params: dict
    ):
        """
        Record metrics for failed RAG requests.
        
        Args:
            request: HTTP request
            error: Exception that occurred
            latency_ms: Request latency before failure
            rag_params: Extracted RAG parameters
        """
        try:
            provider = rag_params.get("provider", "unknown")
            lang = rag_params.get("lang", "en")
            has_rerank = rag_params.get("has_rerank", False)
            
            # Record failed query
            metrics_collector.record_query(provider, lang, has_rerank, "error")
            
            # Record specific error
            error_type = type(error).__name__.lower()
            component = self._determine_component_from_path(request.url.path)
            metrics_collector.record_error(provider, error_type, component)
            
            logger.debug(f"Recorded RAG error for {request.url.path}: {error_type}")
            
        except Exception as e:
            logger.error(f"Failed to record RAG error metrics: {e}")
    
    def _determine_component_from_path(self, path: str) -> str:
        """
        Determine the RAG component from the request path.
        
        Args:
            path: Request path
            
        Returns:
            Component name (retrieval, answer, etc.)
        """
        if "/ask" in path or "/answer" in path:
            return "answer"
        elif "/search" in path or "/retrieve" in path:
            return "retrieval"
        elif "/rerank" in path:
            return "rerank"
        else:
            return "api"

def add_metrics_middleware(app: FastAPI, track_all_endpoints: bool = False):
    """
    Add RAG metrics middleware to a FastAPI application.
    
    Args:
        app: FastAPI application
        track_all_endpoints: Whether to track all endpoints or just RAG ones
    """
    middleware = RAGMetricsMiddleware(app, track_all_endpoints)
    app.add_middleware(RAGMetricsMiddleware, track_all_endpoints=track_all_endpoints)
    logger.info("RAG metrics middleware added to FastAPI app")

def create_metrics_endpoint(app: FastAPI, path: str = "/metrics"):
    """
    Add a /metrics endpoint to expose Prometheus metrics.
    
    Args:
        app: FastAPI application
        path: Path for the metrics endpoint
    """
    from services.obs.metrics import get_metrics, get_metrics_content_type
    
    @app.get(path)
    async def metrics():
        """Expose Prometheus metrics."""
        metrics_data = get_metrics()
        return Response(
            content=metrics_data,
            media_type=get_metrics_content_type()
        )
    
    logger.info(f"Metrics endpoint created at {path}")

# =============================================================================
# Integration Helpers
# =============================================================================

def setup_rag_observability(app: FastAPI, metrics_path: str = "/metrics"):
    """
    Set up complete RAG observability for a FastAPI app.
    
    Args:
        app: FastAPI application
        metrics_path: Path for the metrics endpoint
    """
    # Add metrics middleware
    add_metrics_middleware(app, track_all_endpoints=False)
    
    # Add metrics endpoint
    create_metrics_endpoint(app, metrics_path)
    
    logger.info("RAG observability setup complete")

# =============================================================================
# Context Managers for Manual Tracking
# =============================================================================

class track_rag_request:
    """Context manager for manual RAG request tracking."""
    
    def __init__(self, provider: str, operation: str, lang: str = "en", has_rerank: bool = False):
        self.provider = provider
        self.operation = operation
        self.lang = lang
        self.has_rerank = has_rerank
        self.start_time = None
    
    def __enter__(self):
        self.start_time = time.time()
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        latency_ms = (time.time() - self.start_time) * 1000
        
        if exc_type is None:
            # Success
            metrics_collector.record_query(self.provider, self.lang, self.has_rerank, "success")
            
            if self.operation == "answer":
                metrics_collector.record_answer(self.provider, self.lang, self.has_rerank)
                metrics_collector.record_answer_latency(latency_ms, self.provider, self.lang)
        else:
            # Error
            metrics_collector.record_query(self.provider, self.lang, self.has_rerank, "error")
            error_type = exc_type.__name__.lower()
            metrics_collector.record_error(self.provider, error_type, self.operation)

# =============================================================================
# Demo Functions
# =============================================================================

def demo_middleware():
    """Demonstrate the metrics middleware."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    
    # Create test app
    app = FastAPI()
    setup_rag_observability(app)
    
    @app.get("/ask")
    async def ask_endpoint(query: str, provider: str = "openai", k: int = 10):
        # Simulate processing time
        import asyncio
        await asyncio.sleep(0.1)
        return {"answer": f"Response to: {query}", "provider": provider}
    
    @app.get("/search")
    async def search_endpoint(query: str, k: int = 10):
        import asyncio
        await asyncio.sleep(0.05)
        return {"documents": [f"doc_{i}" for i in range(k)]}
    
    # Test the endpoints
    client = TestClient(app)
    
    print("Testing RAG endpoints with metrics collection...")
    
    # Test successful requests
    response = client.get("/ask?query=test&provider=openai&k=5")
    print(f"Ask endpoint: {response.status_code}")
    
    response = client.get("/search?query=test&k=10")
    print(f"Search endpoint: {response.status_code}")
    
    # Test metrics endpoint
    response = client.get("/metrics")
    print(f"Metrics endpoint: {response.status_code}")
    print("Metrics preview:")
    print(response.text[:500] + "...")

if __name__ == "__main__":
    demo_middleware()
