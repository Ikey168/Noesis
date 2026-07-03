"""
Main FastAPI application configuration - Refactored for 100% test coverage.
All imports moved to functions to make ImportError blocks testable.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Initialize feature flags - will be set by import functions
ERROR_HANDLERS_AVAILABLE = False
ENHANCED_KG_AVAILABLE = False
EVENT_TIMELINE_AVAILABLE = False
QUICKSIGHT_AVAILABLE = False
TOPIC_ROUTES_AVAILABLE = False
GRAPH_SEARCH_AVAILABLE = False
INFLUENCE_ANALYSIS_AVAILABLE = False
RATE_LIMITING_AVAILABLE = False
RBAC_AVAILABLE = False
API_KEY_MANAGEMENT_AVAILABLE = False
WAF_SECURITY_AVAILABLE = False
AUTH_AVAILABLE = False
SEARCH_AVAILABLE = False

# Store imported modules globally
_imported_modules = {}


def try_import_error_handlers():
    """Try to import error handlers (Issue #428)."""
    global ERROR_HANDLERS_AVAILABLE
    try:
        from src.api.error_handlers import configure_error_handlers
        _imported_modules['configure_error_handlers'] = configure_error_handlers
        ERROR_HANDLERS_AVAILABLE = True
        return True
    except ImportError:
        ERROR_HANDLERS_AVAILABLE = False
        return False


def try_import_enhanced_kg_routes():
    """Try to import enhanced knowledge graph routes (Issue #37)."""
    global ENHANCED_KG_AVAILABLE
    try:
        from src.api.routes import enhanced_kg_routes
        _imported_modules['enhanced_kg_routes'] = enhanced_kg_routes
        ENHANCED_KG_AVAILABLE = True
        return True
    except ImportError:
        ENHANCED_KG_AVAILABLE = False
        return False


def try_import_event_timeline_routes():
    """Try to import event timeline routes (Issue #38)."""
    global EVENT_TIMELINE_AVAILABLE
    try:
        from src.api.routes import event_timeline_routes
        _imported_modules['event_timeline_routes'] = event_timeline_routes
        EVENT_TIMELINE_AVAILABLE = True
        return True
    except ImportError:
        EVENT_TIMELINE_AVAILABLE = False
        return False


def try_import_quicksight_routes():
    """Try to import QuickSight routes (Issue #39)."""
    global QUICKSIGHT_AVAILABLE
    try:
        from src.api.routes import quicksight_routes
        _imported_modules['quicksight_routes'] = quicksight_routes
        QUICKSIGHT_AVAILABLE = True
        return True
    except ImportError:
        QUICKSIGHT_AVAILABLE = False
        return False


def try_import_topic_routes():
    """Try to import topic routes (Issue #40)."""
    global TOPIC_ROUTES_AVAILABLE
    try:
        from src.api.routes import topic_routes
        _imported_modules['topic_routes'] = topic_routes
        TOPIC_ROUTES_AVAILABLE = True
        return True
    except ImportError:
        TOPIC_ROUTES_AVAILABLE = False
        return False


def try_import_graph_search_routes():
    """Try to import graph search routes (Issue #41)."""
    global GRAPH_SEARCH_AVAILABLE
    try:
        from src.api.routes import graph_search_routes
        _imported_modules['graph_search_routes'] = graph_search_routes
        GRAPH_SEARCH_AVAILABLE = True
        return True
    except ImportError:
        GRAPH_SEARCH_AVAILABLE = False
        return False


def try_import_influence_routes():
    """Try to import influence analysis routes (Issue #42)."""
    global INFLUENCE_ANALYSIS_AVAILABLE
    try:
        from src.api.routes import influence_routes
        _imported_modules['influence_routes'] = influence_routes
        INFLUENCE_ANALYSIS_AVAILABLE = True
        return True
    except ImportError:
        INFLUENCE_ANALYSIS_AVAILABLE = False
        return False


def try_import_rate_limiting():
    """Try to import rate limiting components (Issue #59)."""
    global RATE_LIMITING_AVAILABLE
    try:
        from src.api.middleware.rate_limit_middleware import (
            RateLimitConfig,
            RateLimitMiddleware,
        )
        from src.api.routes import auth_routes, rate_limit_routes
        
        _imported_modules['RateLimitConfig'] = RateLimitConfig
        _imported_modules['RateLimitMiddleware'] = RateLimitMiddleware
        _imported_modules['auth_routes'] = auth_routes
        _imported_modules['rate_limit_routes'] = rate_limit_routes
        RATE_LIMITING_AVAILABLE = True
        return True
    except ImportError:
        RATE_LIMITING_AVAILABLE = False
        return False


def try_import_rbac():
    """Try to import RBAC components (Issue #60)."""
    global RBAC_AVAILABLE
    try:
        from src.api.rbac.rbac_middleware import (
            EnhancedRBACMiddleware,
            RBACMetricsMiddleware,
        )
        from src.api.routes import rbac_routes
        
        _imported_modules['EnhancedRBACMiddleware'] = EnhancedRBACMiddleware
        _imported_modules['RBACMetricsMiddleware'] = RBACMetricsMiddleware
        _imported_modules['rbac_routes'] = rbac_routes
        RBAC_AVAILABLE = True
        return True
    except ImportError:
        RBAC_AVAILABLE = False
        return False


def try_import_api_key_management():
    """Try to import API key management components (Issue #61)."""
    global API_KEY_MANAGEMENT_AVAILABLE
    try:
        from src.api.auth.api_key_middleware import (
            APIKeyAuthMiddleware,
            APIKeyMetricsMiddleware,
        )
        from src.api.routes import api_key_routes
        
        _imported_modules['APIKeyAuthMiddleware'] = APIKeyAuthMiddleware
        _imported_modules['APIKeyMetricsMiddleware'] = APIKeyMetricsMiddleware
        _imported_modules['api_key_routes'] = api_key_routes
        API_KEY_MANAGEMENT_AVAILABLE = True
        return True
    except ImportError:
        API_KEY_MANAGEMENT_AVAILABLE = False
        return False


def try_import_waf_security():
    """Try to import AWS WAF security components (Issue #65)."""
    global WAF_SECURITY_AVAILABLE
    try:
        from src.api.routes import waf_security_routes
        from src.api.security.waf_middleware import (
            WAFMetricsMiddleware,
            WAFSecurityMiddleware,
        )
        
        _imported_modules['waf_security_routes'] = waf_security_routes
        _imported_modules['WAFMetricsMiddleware'] = WAFMetricsMiddleware
        _imported_modules['WAFSecurityMiddleware'] = WAFSecurityMiddleware
        WAF_SECURITY_AVAILABLE = True
        return True
    except ImportError:
        WAF_SECURITY_AVAILABLE = False
        return False


def try_import_auth_routes():
    """Try to import standalone auth routes."""
    global AUTH_AVAILABLE
    try:
        from src.api.routes import auth_routes
        _imported_modules['auth_routes_standalone'] = auth_routes
        AUTH_AVAILABLE = True
        return True
    except ImportError:
        AUTH_AVAILABLE = False
        return False


def try_import_search_routes():
    """Try to import search routes."""
    global SEARCH_AVAILABLE
    try:
        from src.api.routes import search_routes
        _imported_modules['search_routes'] = search_routes
        SEARCH_AVAILABLE = True
        return True
    except ImportError:
        SEARCH_AVAILABLE = False
        return False


def try_import_core_routes():
    """Try to import core routes that are always needed."""
    global _imported_modules
    try:
        from src.api.routes import event_routes, graph_routes, news_routes, veracity_routes, knowledge_graph_routes
        _imported_modules['event_routes'] = event_routes
        _imported_modules['graph_routes'] = graph_routes
        _imported_modules['news_routes'] = news_routes
        _imported_modules['veracity_routes'] = veracity_routes
        _imported_modules['knowledge_graph_routes'] = knowledge_graph_routes
        return True
    except ImportError:
        return False


def check_all_imports():
    """Check all imports to set feature flags."""
    try_import_error_handlers()
    try_import_enhanced_kg_routes()
    try_import_event_timeline_routes()
    try_import_quicksight_routes()
    try_import_topic_routes()
    try_import_graph_search_routes()
    try_import_influence_routes()
    try_import_rate_limiting()
    try_import_rbac()
    try_import_api_key_management()
    try_import_waf_security()
    try_import_auth_routes()
    try_import_search_routes()


def create_app():
    """Create and configure the FastAPI application."""
    # Check all optional imports
    check_all_imports()
    
    # Try to import core routes
    core_routes_available = try_import_core_routes()
    
    # Create FastAPI app instance
    app = FastAPI(
        title="Noesis API",
        description=(
            "API for the Noesis knowledge engine: documents, knowledge graph, "
            "analytics and the generative canvas, with RBAC, rate limiting, API "
            "key management, and AWS WAF security"
        ),
        version="0.1.0",
    )
    
    return app


def configure_error_handlers_if_available(app):
    """Configure global error handlers if available (Issue #428)."""
    if ERROR_HANDLERS_AVAILABLE:
        configure_error_handlers = _imported_modules.get('configure_error_handlers')
        if configure_error_handlers:
            configure_error_handlers(app)
            return True
    return False


def add_waf_middleware_if_available(app):
    """Add WAF security middleware first for maximum protection (Issue #65)."""
    if WAF_SECURITY_AVAILABLE:
        waf_security_middleware = _imported_modules.get('WAFSecurityMiddleware')
        waf_metrics_middleware = _imported_modules.get('WAFMetricsMiddleware')
        if waf_security_middleware and waf_metrics_middleware:
            app.add_middleware(waf_security_middleware)
            app.add_middleware(waf_metrics_middleware)
            return True
    return False


def add_rate_limiting_middleware_if_available(app):
    """Add rate limiting middleware (Issue #59)."""
    if RATE_LIMITING_AVAILABLE:
        rate_limit_middleware = _imported_modules.get('RateLimitMiddleware')
        rate_limit_config = _imported_modules.get('RateLimitConfig')
        if rate_limit_middleware and rate_limit_config:
            app.add_middleware(rate_limit_middleware, config=rate_limit_config())
            return True
    return False


def add_api_key_middleware_if_available(app):
    """Add API key authentication middleware (Issue #61)."""
    if API_KEY_MANAGEMENT_AVAILABLE:
        api_key_auth_middleware = _imported_modules.get('APIKeyAuthMiddleware')
        api_key_metrics_middleware = _imported_modules.get('APIKeyMetricsMiddleware')
        if api_key_auth_middleware and api_key_metrics_middleware:
            app.add_middleware(api_key_auth_middleware)
            app.add_middleware(api_key_metrics_middleware)
            return True
    return False


def add_rbac_middleware_if_available(app):
    """Add RBAC middleware (Issue #60)."""
    if RBAC_AVAILABLE:
        enhanced_rbac_middleware = _imported_modules.get('EnhancedRBACMiddleware')
        rbac_metrics_middleware = _imported_modules.get('RBACMetricsMiddleware')
        if enhanced_rbac_middleware and rbac_metrics_middleware:
            app.add_middleware(enhanced_rbac_middleware)
            app.add_middleware(rbac_metrics_middleware)
            return True
    return False


def add_cors_middleware(app):
    """Add CORS configuration."""
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # In production, replace with specific origins
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return True


def include_core_routers(app):
    """Include core routers that are always available."""
    graph_routes = _imported_modules.get('graph_routes')
    knowledge_graph_routes = _imported_modules.get('knowledge_graph_routes')
    news_routes = _imported_modules.get('news_routes')
    event_routes = _imported_modules.get('event_routes')
    veracity_routes = _imported_modules.get('veracity_routes')
    
    if graph_routes:
        app.include_router(graph_routes.router)
    if knowledge_graph_routes:
        app.include_router(knowledge_graph_routes.router)
    if news_routes:
        app.include_router(news_routes.router)
    if event_routes:
        app.include_router(event_routes.router)
    if veracity_routes:
        app.include_router(veracity_routes.router)
    return True


def include_versioned_routers(app):
    """Include versioned routers."""
    # Include core routers with versioning
    graph_routes = _imported_modules.get('graph_routes')
    knowledge_graph_routes = _imported_modules.get('knowledge_graph_routes')
    news_routes = _imported_modules.get('news_routes')
    event_routes = _imported_modules.get('event_routes')
    veracity_routes = _imported_modules.get('veracity_routes')
    
    if graph_routes:
        app.include_router(graph_routes.router, prefix="/api/v1")
    if knowledge_graph_routes:
        app.include_router(knowledge_graph_routes.router, prefix="/api/v1")
    if news_routes:
        app.include_router(news_routes.router, prefix="/api/v1")
    if event_routes:
        app.include_router(event_routes.router, prefix="/api/v1")
    if veracity_routes:
        app.include_router(veracity_routes.router, prefix="/api/v1")
    return True


def include_optional_routers(app):
    """Include optional routers based on availability."""
    routers_included = 0
    
    # Include authentication routes (Issue #59)
    if RATE_LIMITING_AVAILABLE:
        auth_routes = _imported_modules.get('auth_routes')
        rate_limit_routes = _imported_modules.get('rate_limit_routes')
        if auth_routes and rate_limit_routes:
            app.include_router(auth_routes.router)
            app.include_router(rate_limit_routes.router)
            routers_included += 1

    # Include RBAC routes (Issue #60)
    if RBAC_AVAILABLE:
        rbac_routes = _imported_modules.get('rbac_routes')
        if rbac_routes:
            app.include_router(rbac_routes.router)
            routers_included += 1

    # Include API key management routes (Issue #61)
    if API_KEY_MANAGEMENT_AVAILABLE:
        api_key_routes = _imported_modules.get('api_key_routes')
        if api_key_routes:
            app.include_router(api_key_routes.router)
            routers_included += 1

    # Include AWS WAF security routes (Issue #65)
    if WAF_SECURITY_AVAILABLE:
        waf_security_routes = _imported_modules.get('waf_security_routes')
        if waf_security_routes:
            app.include_router(waf_security_routes.router)
            routers_included += 1

    # Include enhanced knowledge graph routes if available (Issue #37)
    if ENHANCED_KG_AVAILABLE:
        enhanced_kg_routes = _imported_modules.get('enhanced_kg_routes')
        if enhanced_kg_routes:
            app.include_router(enhanced_kg_routes.router)
            routers_included += 1

    # Include event timeline routes if available (Issue #38)
    if EVENT_TIMELINE_AVAILABLE:
        event_timeline_routes = _imported_modules.get('event_timeline_routes')
        if event_timeline_routes:
            app.include_router(event_timeline_routes.router)
            routers_included += 1

    # Include QuickSight routes if available (Issue #39)
    if QUICKSIGHT_AVAILABLE:
        quicksight_routes = _imported_modules.get('quicksight_routes')
        if quicksight_routes:
            app.include_router(quicksight_routes.router)
            routers_included += 1

    # Include topic routes if available (Issue #40)
    if TOPIC_ROUTES_AVAILABLE:
        topic_routes = _imported_modules.get('topic_routes')
        if topic_routes:
            app.include_router(topic_routes.router)
            routers_included += 1

    # Include graph search routes if available (Issue #41)
    if GRAPH_SEARCH_AVAILABLE:
        graph_search_routes = _imported_modules.get('graph_search_routes')
        if graph_search_routes:
            app.include_router(graph_search_routes.router)
            routers_included += 1

    # Include influence analysis routes if available (Issue #42)
    if INFLUENCE_ANALYSIS_AVAILABLE:
        influence_routes = _imported_modules.get('influence_routes')
        if influence_routes:
            app.include_router(influence_routes.router)
            routers_included += 1

    # Include auth router if available
    if AUTH_AVAILABLE:
        auth_routes_standalone = _imported_modules.get('auth_routes_standalone')
        if auth_routes_standalone:
            app.include_router(auth_routes_standalone.router, prefix="/api/v1/auth", tags=["Authentication"])
            routers_included += 1

    # Include search router if available
    if SEARCH_AVAILABLE:
        search_routes = _imported_modules.get('search_routes')
        if search_routes:
            app.include_router(search_routes.router, prefix="/api/v1/search", tags=["Search"])
            routers_included += 1

    return routers_included


def initialize_app():
    """Initialize the FastAPI application with all configurations."""
    # Create the app
    app = create_app()
    
    # Configure error handlers
    configure_error_handlers_if_available(app)
    
    # Add middleware in proper order
    add_waf_middleware_if_available(app)
    add_rate_limiting_middleware_if_available(app)
    add_api_key_middleware_if_available(app)
    add_rbac_middleware_if_available(app)
    add_cors_middleware(app)
    
    # Include routers
    include_core_routers(app)
    include_optional_routers(app)
    include_versioned_routers(app)
    
    return app


# Create the application instance
app = initialize_app()


async def root():
    """Root endpoint."""
    return {
        "status": "ok",
        "message": "Noesis API is running",
        "features": {
            "rate_limiting": RATE_LIMITING_AVAILABLE,
            "rbac": RBAC_AVAILABLE,
            "api_key_management": API_KEY_MANAGEMENT_AVAILABLE,
            "waf_security": WAF_SECURITY_AVAILABLE,
            "enhanced_kg": ENHANCED_KG_AVAILABLE,
            "event_timeline": EVENT_TIMELINE_AVAILABLE,
            "quicksight": QUICKSIGHT_AVAILABLE,
            "topic_routes": TOPIC_ROUTES_AVAILABLE,
            "graph_search": GRAPH_SEARCH_AVAILABLE,
            "influence_analysis": INFLUENCE_ANALYSIS_AVAILABLE,
        },
    }


async def health_check():
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "timestamp": "2025-08-17T22:00:00Z",
        "version": "0.1.0",
        "components": {
            "api": "operational",
            "rate_limiting": "operational" if RATE_LIMITING_AVAILABLE else "disabled",
            "rbac": "operational" if RBAC_AVAILABLE else "disabled",
            "api_key_management": (
                "operational" if API_KEY_MANAGEMENT_AVAILABLE else "disabled"
            ),
            "waf_security": "operational" if WAF_SECURITY_AVAILABLE else "disabled",
            "topic_routes": "operational" if TOPIC_ROUTES_AVAILABLE else "disabled",
            "graph_search": "operational" if GRAPH_SEARCH_AVAILABLE else "disabled",
            "influence_analysis": "operational" if INFLUENCE_ANALYSIS_AVAILABLE else "disabled",
            "database": "unknown",  # Would check actual DB connection
            "cache": "unknown",  # Would check Redis connection
        },
    }


# Add the endpoints to the app
app.add_api_route("/", root, methods=["GET"])
app.add_api_route("/health", health_check, methods=["GET"])
