"""
Main FastAPI application configuration.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Initialize feature flags - will be set by check_imports()
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
DOCUMENT_ROUTES_AVAILABLE = False
REPORT_ROUTES_AVAILABLE = False
ALERT_ROUTES_AVAILABLE = False
ARGUMENT_ROUTES_AVAILABLE = False
KG_STREAM_ROUTES_AVAILABLE = False
ENTITY_CORRECTION_ROUTES_AVAILABLE = False
SOURCE_COMPARISON_ROUTES_AVAILABLE = False
METRICS_ROUTES_AVAILABLE = False
PRIVACY_ROUTES_AVAILABLE = False
SECURITY_ROUTES_AVAILABLE = False
GENUI_ROUTES_AVAILABLE = False

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
    """Try to import quicksight dashboard routes (Issue #49)."""
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
    """Try to import topic routes (Issue #29)."""
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
    """Try to import graph search routes (Issue #39)."""
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
    """Try to import influence analysis routes (Issue #40)."""
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
    """Try to import auth routes."""
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


def try_import_document_routes():
    """Try to import generic document routes (issue #520)."""
    global DOCUMENT_ROUTES_AVAILABLE
    try:
        from src.api.routes import document_routes
        _imported_modules['document_routes'] = document_routes
        DOCUMENT_ROUTES_AVAILABLE = True
        return True
    except ImportError:
        DOCUMENT_ROUTES_AVAILABLE = False
        return False


def try_import_alert_routes():
    """Try to import real-time alert routes (issue #53)."""
    global ALERT_ROUTES_AVAILABLE
    try:
        from src.api.routes import alert_routes
        _imported_modules['alert_routes'] = alert_routes
        ALERT_ROUTES_AVAILABLE = True
        try:
            from src.alerts.dispatcher import start_alert_scheduler
            start_alert_scheduler()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Alert scheduler could not be started", exc_info=True)
        return True
    except ImportError:
        ALERT_ROUTES_AVAILABLE = False
        return False


def try_import_argument_routes():
    """Try to import argument mining routes (issue #90, #95)."""
    global ARGUMENT_ROUTES_AVAILABLE
    try:
        from src.api.routes import argument_routes
        _imported_modules['argument_routes'] = argument_routes
        ARGUMENT_ROUTES_AVAILABLE = True
        try:
            from src.argument_mining.stance_aggregator import schedule_stance_job
            from src.argument_mining.drift_detector import schedule_drift_job
            from apscheduler.schedulers.background import BackgroundScheduler
            _sched = BackgroundScheduler()
            schedule_stance_job(_sched, hour=3)
            schedule_drift_job(_sched, hour=4)
            _sched.start()
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Argument mining schedulers could not be started", exc_info=True
            )
        return True
    except ImportError:
        ARGUMENT_ROUTES_AVAILABLE = False
        return False


def try_import_kg_stream_routes():
    """Try to import knowledge-graph streaming-update routes (issue #42)."""
    global KG_STREAM_ROUTES_AVAILABLE
    try:
        from src.api.routes import kg_stream_routes
        _imported_modules['kg_stream_routes'] = kg_stream_routes
        KG_STREAM_ROUTES_AVAILABLE = True
        return True
    except ImportError:
        KG_STREAM_ROUTES_AVAILABLE = False
        return False


def try_import_entity_correction_routes():
    """Try to import user-driven entity correction routes (issue #44)."""
    global ENTITY_CORRECTION_ROUTES_AVAILABLE
    try:
        from src.api.routes import entity_correction_routes
        _imported_modules['entity_correction_routes'] = entity_correction_routes
        ENTITY_CORRECTION_ROUTES_AVAILABLE = True
        return True
    except ImportError:
        ENTITY_CORRECTION_ROUTES_AVAILABLE = False
        return False


def try_import_source_comparison_routes():
    """Try to import multi-source news comparison routes (issue #46)."""
    global SOURCE_COMPARISON_ROUTES_AVAILABLE
    try:
        from src.api.routes import source_comparison_routes
        _imported_modules['source_comparison_routes'] = source_comparison_routes
        SOURCE_COMPARISON_ROUTES_AVAILABLE = True
        return True
    except ImportError:
        SOURCE_COMPARISON_ROUTES_AVAILABLE = False
        return False


def try_import_metrics_routes():
    """Try to import resource metrics routes (issue #334)."""
    global METRICS_ROUTES_AVAILABLE
    try:
        from src.api.routes import metrics_routes
        _imported_modules['metrics_routes'] = metrics_routes
        METRICS_ROUTES_AVAILABLE = True
        return True
    except ImportError:
        METRICS_ROUTES_AVAILABLE = False
        return False


def try_import_privacy_routes():
    """Try to import offline privacy routes (issue #64)."""
    global PRIVACY_ROUTES_AVAILABLE
    try:
        from src.api.routes import privacy_routes
        _imported_modules['privacy_routes'] = privacy_routes
        PRIVACY_ROUTES_AVAILABLE = True
        return True
    except ImportError:
        PRIVACY_ROUTES_AVAILABLE = False
        return False


def try_import_security_routes():
    """Try to import local storage security routes (issue #66)."""
    global SECURITY_ROUTES_AVAILABLE
    try:
        from src.api.routes import security_routes
        _imported_modules['security_routes'] = security_routes
        SECURITY_ROUTES_AVAILABLE = True
        return True
    except ImportError:
        SECURITY_ROUTES_AVAILABLE = False
        return False


def try_import_genui_routes():
    """Try to import generative-UI routes (adaptive Noesis canvas)."""
    global GENUI_ROUTES_AVAILABLE
    try:
        from src.api.routes import genui_routes
        _imported_modules['genui_routes'] = genui_routes
        GENUI_ROUTES_AVAILABLE = True
        return True
    except ImportError:
        GENUI_ROUTES_AVAILABLE = False
        return False


def try_import_report_routes():
    """Try to import report generation routes (issues #51, #52)."""
    global REPORT_ROUTES_AVAILABLE
    try:
        from src.api.routes import report_routes
        _imported_modules['report_routes'] = report_routes
        REPORT_ROUTES_AVAILABLE = True
        # Start the background scheduler for scheduled email delivery (#52)
        try:
            from src.reports.scheduler import start_scheduler
            start_scheduler()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("Report scheduler could not be started", exc_info=True)
        return True
    except ImportError:
        REPORT_ROUTES_AVAILABLE = False
        return False


def _load_domain_packs():
    """Load domain-pack config and register built-in packs."""
    try:
        from src.domains.registry import load_config
        import src.domains.news  # noqa: F401 — triggers register_pack(NewsDomainPack)
        load_config()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Domain-pack config could not be loaded", exc_info=True)


def check_all_imports():
    """Check all optional imports and set feature flags."""
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
    try_import_document_routes()
    try_import_report_routes()
    try_import_alert_routes()
    try_import_argument_routes()
    try_import_kg_stream_routes()
    try_import_entity_correction_routes()
    try_import_source_comparison_routes()
    try_import_metrics_routes()
    try_import_privacy_routes()
    try_import_security_routes()
    try_import_genui_routes()
    _load_domain_packs()


def try_import_core_routes():
    """Try to import core routes that are always needed."""
    global _imported_modules
    try:
        from src.api.routes import event_routes, graph_routes, news_routes, veracity_routes, knowledge_graph_routes, sentiment_routes
        _imported_modules['event_routes'] = event_routes
        _imported_modules['graph_routes'] = graph_routes
        _imported_modules['news_routes'] = news_routes
        _imported_modules['veracity_routes'] = veracity_routes
        _imported_modules['knowledge_graph_routes'] = knowledge_graph_routes
        _imported_modules['sentiment_routes'] = sentiment_routes
        return True
    except ImportError:
        return False


def create_app():
    """Create and configure the FastAPI application."""
    # Check all optional imports
    check_all_imports()
    
    # Try to import core routes
    core_routes_available = try_import_core_routes()
    
    # Create FastAPI app instance
    app = FastAPI(
        title="NeuroNews API",
        description=(
            "API for accessing news articles and knowledge graph with RBAC, "
            "rate limiting, API key management, and AWS WAF security"
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


def _dev_mode_enabled() -> bool:
    """Whether to skip heavy security middleware for local development.

    Set NEURONEWS_DEV_MODE=true to disable the WAF, rate limiting, API-key and
    RBAC middlewares so the local frontend can talk to the API without
    authentication or throttling. Defaults to off (production-safe).
    """
    import os

    return os.getenv("NEURONEWS_DEV_MODE", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def add_waf_middleware_if_available(app):
    """Add WAF security middleware first for maximum protection (Issue #65)."""
    if _dev_mode_enabled():
        return False
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
    if _dev_mode_enabled():
        return False
    if RATE_LIMITING_AVAILABLE:
        rate_limit_middleware = _imported_modules.get('RateLimitMiddleware')
        rate_limit_config = _imported_modules.get('RateLimitConfig')
        if rate_limit_middleware and rate_limit_config:
            app.add_middleware(rate_limit_middleware, config=rate_limit_config())
            return True
    return False


def add_api_key_middleware_if_available(app):
    """Add API key authentication middleware (Issue #61)."""
    if _dev_mode_enabled():
        return False
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
    if _dev_mode_enabled():
        return False
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
    from src.domains.registry import is_pack_enabled

    graph_routes = _imported_modules.get('graph_routes')
    knowledge_graph_routes = _imported_modules.get('knowledge_graph_routes')
    document_routes = _imported_modules.get('document_routes')
    news_routes = _imported_modules.get('news_routes')
    event_routes = _imported_modules.get('event_routes')
    veracity_routes = _imported_modules.get('veracity_routes')
    sentiment_routes = _imported_modules.get('sentiment_routes')

    # Generic routes — always on regardless of domain packs.
    if graph_routes:
        app.include_router(graph_routes.router)
    if knowledge_graph_routes:
        app.include_router(knowledge_graph_routes.router)
    if document_routes:
        app.include_router(document_routes.router)

    # News-domain routes — gated behind the news domain pack flag (#520).
    if is_pack_enabled("news"):
        if news_routes:
            app.include_router(news_routes.router)
        if event_routes:
            app.include_router(event_routes.router)
        if veracity_routes:
            app.include_router(veracity_routes.router)
        if sentiment_routes:
            app.include_router(sentiment_routes.router)
    return True


def include_optional_routers(app):
    """Include optional routers based on availability."""
    from src.domains.registry import is_pack_enabled
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

    # Include event timeline routes if available — news pack only (Issue #38, #520).
    if EVENT_TIMELINE_AVAILABLE and is_pack_enabled("news"):
        event_timeline_routes = _imported_modules.get('event_timeline_routes')
        if event_timeline_routes:
            app.include_router(event_timeline_routes.router)
            routers_included += 1

    # Include quicksight dashboard routes if available (Issue #49)
    if QUICKSIGHT_AVAILABLE:
        quicksight_routes = _imported_modules.get('quicksight_routes')
        if quicksight_routes:
            app.include_router(quicksight_routes.router)
            routers_included += 1

    # Include topic routes if available (Issue #29)
    if TOPIC_ROUTES_AVAILABLE:
        topic_routes = _imported_modules.get('topic_routes')
        if topic_routes:
            app.include_router(topic_routes.router)
            routers_included += 1

    # Include graph search routes if available (Issue #39)
    if GRAPH_SEARCH_AVAILABLE:
        graph_search_routes = _imported_modules.get('graph_search_routes')
        if graph_search_routes:
            app.include_router(graph_search_routes.router)
            routers_included += 1

    # Include influence analysis routes if available — news pack only (Issue #40, #520).
    if INFLUENCE_ANALYSIS_AVAILABLE and is_pack_enabled("news"):
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

    # Include real-time alert routes (issue #53)
    if ALERT_ROUTES_AVAILABLE:
        alert_routes = _imported_modules.get('alert_routes')
        if alert_routes:
            app.include_router(alert_routes.router)
            routers_included += 1

    # Include report generation routes (issue #51)
    if REPORT_ROUTES_AVAILABLE:
        report_routes = _imported_modules.get('report_routes')
        if report_routes:
            app.include_router(report_routes.router)
            routers_included += 1

    # Include argument mining routes (issue #90, #95)
    if ARGUMENT_ROUTES_AVAILABLE:
        argument_routes = _imported_modules.get('argument_routes')
        if argument_routes:
            app.include_router(argument_routes.router)
            routers_included += 1

    # Include KG streaming-update routes (issue #42)
    if KG_STREAM_ROUTES_AVAILABLE:
        kg_stream_routes = _imported_modules.get('kg_stream_routes')
        if kg_stream_routes:
            app.include_router(kg_stream_routes.router)
            routers_included += 1

    # Include entity correction routes (issue #44)
    if ENTITY_CORRECTION_ROUTES_AVAILABLE:
        entity_correction_routes = _imported_modules.get('entity_correction_routes')
        if entity_correction_routes:
            app.include_router(entity_correction_routes.router)
            routers_included += 1

    # Include multi-source comparison routes (issue #46)
    if SOURCE_COMPARISON_ROUTES_AVAILABLE:
        source_comparison_routes = _imported_modules.get('source_comparison_routes')
        if source_comparison_routes:
            app.include_router(source_comparison_routes.router)
            routers_included += 1

    # Include resource metrics routes (issue #334)
    if METRICS_ROUTES_AVAILABLE:
        metrics_routes = _imported_modules.get('metrics_routes')
        if metrics_routes:
            app.include_router(metrics_routes.router)
            routers_included += 1

    # Include offline privacy routes (issue #64)
    if PRIVACY_ROUTES_AVAILABLE:
        privacy_routes = _imported_modules.get('privacy_routes')
        if privacy_routes:
            app.include_router(privacy_routes.router)
            routers_included += 1

    # Include local storage security routes (issue #66)
    if SECURITY_ROUTES_AVAILABLE:
        security_routes = _imported_modules.get('security_routes')
        if security_routes:
            app.include_router(security_routes.router)
            routers_included += 1

    # Include generative-UI routes (adaptive Noesis canvas)
    if GENUI_ROUTES_AVAILABLE:
        genui_routes = _imported_modules.get('genui_routes')
        if genui_routes:
            app.include_router(genui_routes.router)
            routers_included += 1

    return routers_included


def include_versioned_routers(app):
    """Include versioned routers."""
    from src.domains.registry import is_pack_enabled

    graph_routes = _imported_modules.get('graph_routes')
    knowledge_graph_routes = _imported_modules.get('knowledge_graph_routes')
    document_routes = _imported_modules.get('document_routes')
    news_routes = _imported_modules.get('news_routes')
    event_routes = _imported_modules.get('event_routes')
    veracity_routes = _imported_modules.get('veracity_routes')

    # Generic — always on.
    if graph_routes:
        app.include_router(graph_routes.router, prefix="/api/v1")
    if knowledge_graph_routes:
        app.include_router(knowledge_graph_routes.router, prefix="/api/v1")
    if document_routes:
        app.include_router(document_routes.router, prefix="/api/v1")

    # News pack only (#520).
    if is_pack_enabled("news"):
        if news_routes:
            app.include_router(news_routes.router, prefix="/api/v1")
        if event_routes:
            app.include_router(event_routes.router, prefix="/api/v1")
        if veracity_routes:
            app.include_router(veracity_routes.router, prefix="/api/v1")
    return True


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

    # Start background resource collector (best-effort)
    if METRICS_ROUTES_AVAILABLE:
        try:
            from src.monitoring.resource_monitor import start_background_collector
            start_background_collector()
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Resource monitor could not be started", exc_info=True
            )

    return app


# Create the application instance - but only if not in test mode
import os
if not os.environ.get('TESTING', False):
    app = initialize_app()
else:
    # In test mode, create a minimal app to avoid heavy imports
    app = FastAPI(title="NeuroNews API", version="0.1.0")


async def root():
    """Root endpoint."""
    from src.domains.registry import is_pack_enabled
    return {
        "status": "ok",
        "message": "NeuroNews API is running",
        "domain_packs": {
            "news": is_pack_enabled("news"),
        },
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
            "document_routes": DOCUMENT_ROUTES_AVAILABLE,
            "kg_stream": KG_STREAM_ROUTES_AVAILABLE,
            "entity_corrections": ENTITY_CORRECTION_ROUTES_AVAILABLE,
            "source_comparison": SOURCE_COMPARISON_ROUTES_AVAILABLE,
            "resource_metrics": METRICS_ROUTES_AVAILABLE,
            "privacy": PRIVACY_ROUTES_AVAILABLE,
            "local_storage_security": SECURITY_ROUTES_AVAILABLE,
            "generative_ui": GENUI_ROUTES_AVAILABLE,
        },
    }


async def health_check():
    """Health check endpoint for monitoring."""
    return {
        "status": "healthy",
        "timestamp": "2025-08-30T22:00:00Z",
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
