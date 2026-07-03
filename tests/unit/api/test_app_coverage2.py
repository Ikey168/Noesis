"""Coverage-focused unit tests for src/api/app.py.

IMPORTANT — torch/segfault avoidance
------------------------------------
``src/api/routes/__init__.py`` eagerly imports every route submodule, several
of which pull in torch. Importing any ``src.api.routes.*`` module for real
segfaults the interpreter under coverage. These tests therefore install
lightweight *stub* modules into ``sys.modules`` for the routes package and its
submodules BEFORE exercising the ``try_import_*`` helpers, so no real route
module (and no torch) is ever imported. ``include_core_routers`` /
``include_optional_routers`` are driven with mock router objects placed in
``src.api.app._imported_modules`` and never touch ``src.api.routes.<mod>.router``.
"""

import sys
import types

import pytest
from fastapi import FastAPI

# Importing src.api.app under TESTING=true (set by tests/conftest.py) creates a
# minimal app and does NOT import src.api.routes — verified safe.
from src.api import app as appmod


# ---------------------------------------------------------------------------
# Stub the whole src.api.routes package so `from src.api.routes import X`
# resolves against fake modules with a cheap .router attribute.
# ---------------------------------------------------------------------------
_ROUTE_SUBMODULES = [
    "enhanced_kg_routes", "event_timeline_routes", "quicksight_routes",
    "topic_routes", "graph_search_routes", "influence_routes",
    "rate_limit_routes", "auth_routes", "rbac_routes", "api_key_routes",
    "waf_security_routes", "search_routes", "document_routes",
    "report_routes", "alert_routes", "argument_routes", "kg_stream_routes",
    "entity_correction_routes", "source_comparison_routes", "metrics_routes",
    "privacy_routes", "security_routes",
    "event_routes", "graph_routes", "news_routes", "veracity_routes",
    "knowledge_graph_routes", "sentiment_routes",
]


@pytest.fixture
def stub_routes(monkeypatch):
    """Install fake src.api.routes package + submodules into sys.modules.

    Yields a dict mapping submodule short-name -> stub module. Each stub has a
    unique sentinel .router object so tests can assert on identity.
    """
    pkg = types.ModuleType("src.api.routes")
    pkg.__path__ = []  # mark as package
    monkeypatch.setitem(sys.modules, "src.api.routes", pkg)

    stubs = {}
    for name in _ROUTE_SUBMODULES:
        sub = types.ModuleType(f"src.api.routes.{name}")
        sub.router = object()
        setattr(pkg, name, sub)
        monkeypatch.setitem(sys.modules, f"src.api.routes.{name}", sub)
        stubs[name] = sub

    # Also stub the middleware/rbac/auth/security packages used by the
    # rate-limiting / rbac / api-key / waf import helpers.
    def _mk(path, **attrs):
        m = types.ModuleType(path)
        for k, v in attrs.items():
            setattr(m, k, v)
        monkeypatch.setitem(sys.modules, path, m)
        return m

    _mk(
        "src.api.middleware.rate_limit_middleware",
        RateLimitConfig=type("RateLimitConfig", (), {}),
        RateLimitMiddleware=type("RateLimitMiddleware", (), {}),
    )
    _mk(
        "src.api.rbac.rbac_middleware",
        EnhancedRBACMiddleware=type("EnhancedRBACMiddleware", (), {}),
        RBACMetricsMiddleware=type("RBACMetricsMiddleware", (), {}),
    )
    _mk(
        "src.api.auth.api_key_middleware",
        APIKeyAuthMiddleware=type("APIKeyAuthMiddleware", (), {}),
        APIKeyMetricsMiddleware=type("APIKeyMetricsMiddleware", (), {}),
    )
    _mk(
        "src.api.security.waf_middleware",
        WAFSecurityMiddleware=type("WAFSecurityMiddleware", (), {}),
        WAFMetricsMiddleware=type("WAFMetricsMiddleware", (), {}),
    )
    yield stubs


@pytest.fixture
def clean_modules(monkeypatch):
    """Give app a fresh _imported_modules dict per test."""
    monkeypatch.setattr(appmod, "_imported_modules", {})
    return appmod._imported_modules


# ---------------------------------------------------------------------------
# create_app
# ---------------------------------------------------------------------------
def test_create_app_returns_configured_fastapi(monkeypatch):
    # Avoid the heavy real import paths inside create_app().
    monkeypatch.setattr(appmod, "check_all_imports", lambda: None)
    monkeypatch.setattr(appmod, "try_import_core_routes", lambda: True)
    application = appmod.create_app()
    assert isinstance(application, FastAPI)
    assert application.title == "Noesis API"
    assert application.version == "0.1.0"


# ---------------------------------------------------------------------------
# _dev_mode_enabled
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "On"])
def test_dev_mode_enabled_truthy(monkeypatch, value):
    monkeypatch.setenv("NEURONEWS_DEV_MODE", value)
    assert appmod._dev_mode_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_dev_mode_enabled_falsey(monkeypatch, value):
    monkeypatch.setenv("NEURONEWS_DEV_MODE", value)
    assert appmod._dev_mode_enabled() is False


def test_dev_mode_default_off(monkeypatch):
    monkeypatch.delenv("NEURONEWS_DEV_MODE", raising=False)
    assert appmod._dev_mode_enabled() is False


# ---------------------------------------------------------------------------
# try_import_* helpers (with stubbed route modules)
# ---------------------------------------------------------------------------
def test_try_import_error_handlers_success(monkeypatch, clean_modules):
    # error_handlers has no torch dependency; import it for real.
    monkeypatch.setattr(appmod, "ERROR_HANDLERS_AVAILABLE", False)
    assert appmod.try_import_error_handlers() is True
    assert appmod.ERROR_HANDLERS_AVAILABLE is True
    assert "configure_error_handlers" in clean_modules


def test_try_import_error_handlers_failure(monkeypatch, clean_modules):
    monkeypatch.setattr(appmod, "ERROR_HANDLERS_AVAILABLE", True)
    # Force the import inside the helper to raise ImportError.
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "src.api.error_handlers":
            raise ImportError("boom")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert appmod.try_import_error_handlers() is False
    assert appmod.ERROR_HANDLERS_AVAILABLE is False


def test_try_import_enhanced_kg_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "ENHANCED_KG_AVAILABLE", False)
    assert appmod.try_import_enhanced_kg_routes() is True
    assert appmod.ENHANCED_KG_AVAILABLE is True
    assert clean_modules["enhanced_kg_routes"] is stub_routes["enhanced_kg_routes"]


def test_try_import_event_timeline_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "EVENT_TIMELINE_AVAILABLE", False)
    assert appmod.try_import_event_timeline_routes() is True
    assert appmod.EVENT_TIMELINE_AVAILABLE is True


def test_try_import_quicksight_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "QUICKSIGHT_AVAILABLE", False)
    assert appmod.try_import_quicksight_routes() is True
    assert appmod.QUICKSIGHT_AVAILABLE is True


def test_try_import_topic_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "TOPIC_ROUTES_AVAILABLE", False)
    assert appmod.try_import_topic_routes() is True
    assert appmod.TOPIC_ROUTES_AVAILABLE is True


def test_try_import_graph_search_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "GRAPH_SEARCH_AVAILABLE", False)
    assert appmod.try_import_graph_search_routes() is True
    assert appmod.GRAPH_SEARCH_AVAILABLE is True


def test_try_import_influence_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "INFLUENCE_ANALYSIS_AVAILABLE", False)
    assert appmod.try_import_influence_routes() is True
    assert appmod.INFLUENCE_ANALYSIS_AVAILABLE is True


def test_try_import_rate_limiting_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "RATE_LIMITING_AVAILABLE", False)
    assert appmod.try_import_rate_limiting() is True
    assert appmod.RATE_LIMITING_AVAILABLE is True
    assert "RateLimitConfig" in clean_modules
    assert "RateLimitMiddleware" in clean_modules
    assert "auth_routes" in clean_modules
    assert "rate_limit_routes" in clean_modules


def test_try_import_rbac_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "RBAC_AVAILABLE", False)
    assert appmod.try_import_rbac() is True
    assert appmod.RBAC_AVAILABLE is True
    assert "EnhancedRBACMiddleware" in clean_modules
    assert "rbac_routes" in clean_modules


def test_try_import_api_key_management_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "API_KEY_MANAGEMENT_AVAILABLE", False)
    assert appmod.try_import_api_key_management() is True
    assert appmod.API_KEY_MANAGEMENT_AVAILABLE is True
    assert "APIKeyAuthMiddleware" in clean_modules
    assert "api_key_routes" in clean_modules


def test_try_import_waf_security_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "WAF_SECURITY_AVAILABLE", False)
    assert appmod.try_import_waf_security() is True
    assert appmod.WAF_SECURITY_AVAILABLE is True
    assert "WAFSecurityMiddleware" in clean_modules
    assert "waf_security_routes" in clean_modules


def test_try_import_auth_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "AUTH_AVAILABLE", False)
    assert appmod.try_import_auth_routes() is True
    assert appmod.AUTH_AVAILABLE is True
    assert "auth_routes_standalone" in clean_modules


def test_try_import_search_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "SEARCH_AVAILABLE", False)
    assert appmod.try_import_search_routes() is True
    assert appmod.SEARCH_AVAILABLE is True
    assert "search_routes" in clean_modules


def test_try_import_document_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "DOCUMENT_ROUTES_AVAILABLE", False)
    assert appmod.try_import_document_routes() is True
    assert appmod.DOCUMENT_ROUTES_AVAILABLE is True


def test_try_import_kg_stream_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "KG_STREAM_ROUTES_AVAILABLE", False)
    assert appmod.try_import_kg_stream_routes() is True
    assert appmod.KG_STREAM_ROUTES_AVAILABLE is True


def test_try_import_entity_correction_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "ENTITY_CORRECTION_ROUTES_AVAILABLE", False)
    assert appmod.try_import_entity_correction_routes() is True
    assert appmod.ENTITY_CORRECTION_ROUTES_AVAILABLE is True


def test_try_import_source_comparison_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "SOURCE_COMPARISON_ROUTES_AVAILABLE", False)
    assert appmod.try_import_source_comparison_routes() is True
    assert appmod.SOURCE_COMPARISON_ROUTES_AVAILABLE is True


def test_try_import_metrics_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "METRICS_ROUTES_AVAILABLE", False)
    assert appmod.try_import_metrics_routes() is True
    assert appmod.METRICS_ROUTES_AVAILABLE is True


def test_try_import_privacy_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "PRIVACY_ROUTES_AVAILABLE", False)
    assert appmod.try_import_privacy_routes() is True
    assert appmod.PRIVACY_ROUTES_AVAILABLE is True


def test_try_import_security_routes_success(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "SECURITY_ROUTES_AVAILABLE", False)
    assert appmod.try_import_security_routes() is True
    assert appmod.SECURITY_ROUTES_AVAILABLE is True


def test_try_import_alert_routes_success_scheduler_fails(monkeypatch, clean_modules, stub_routes):
    """alert_routes imports; the alert scheduler side-effect fails gracefully."""
    monkeypatch.setattr(appmod, "ALERT_ROUTES_AVAILABLE", False)
    # Stub the dispatcher so start_alert_scheduler raises -> hits the except block.
    dispatcher = types.ModuleType("src.alerts.dispatcher")

    def boom():
        raise RuntimeError("scheduler down")

    dispatcher.start_alert_scheduler = boom
    monkeypatch.setitem(sys.modules, "src.alerts.dispatcher", dispatcher)

    assert appmod.try_import_alert_routes() is True
    assert appmod.ALERT_ROUTES_AVAILABLE is True
    assert "alert_routes" in clean_modules


def test_try_import_argument_routes_success_scheduler_fails(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "ARGUMENT_ROUTES_AVAILABLE", False)
    # Stub the scheduler deps: import succeeds, BackgroundScheduler.start raises.
    stance = types.ModuleType("src.argument_mining.stance_aggregator")
    stance.schedule_stance_job = lambda *a, **k: None
    drift = types.ModuleType("src.argument_mining.drift_detector")
    drift.schedule_drift_job = lambda *a, **k: None
    apsched = types.ModuleType("apscheduler.schedulers.background")

    class _Sched:
        def start(self):
            raise RuntimeError("cannot start")

    apsched.BackgroundScheduler = _Sched
    monkeypatch.setitem(sys.modules, "src.argument_mining.stance_aggregator", stance)
    monkeypatch.setitem(sys.modules, "src.argument_mining.drift_detector", drift)
    monkeypatch.setitem(sys.modules, "apscheduler.schedulers.background", apsched)

    assert appmod.try_import_argument_routes() is True
    assert appmod.ARGUMENT_ROUTES_AVAILABLE is True
    assert "argument_routes" in clean_modules


def test_try_import_report_routes_success_scheduler_started(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "REPORT_ROUTES_AVAILABLE", False)
    # Stub the scheduler so start_scheduler is invoked without side effects.
    started = {"called": False}
    sched_mod = types.ModuleType("src.reports.scheduler")
    sched_mod.start_scheduler = lambda: started.__setitem__("called", True)
    monkeypatch.setitem(sys.modules, "src.reports.scheduler", sched_mod)

    assert appmod.try_import_report_routes() is True
    assert appmod.REPORT_ROUTES_AVAILABLE is True
    assert "report_routes" in clean_modules
    assert started["called"] is True


def test_try_import_report_routes_scheduler_failure_swallowed(monkeypatch, clean_modules, stub_routes):
    monkeypatch.setattr(appmod, "REPORT_ROUTES_AVAILABLE", False)
    sched_mod = types.ModuleType("src.reports.scheduler")

    def boom():
        raise RuntimeError("scheduler exploded")

    sched_mod.start_scheduler = boom
    monkeypatch.setitem(sys.modules, "src.reports.scheduler", sched_mod)

    # Import still succeeds even though the scheduler start fails.
    assert appmod.try_import_report_routes() is True
    assert appmod.REPORT_ROUTES_AVAILABLE is True
    assert "report_routes" in clean_modules


def test_try_import_report_routes_import_error(monkeypatch, clean_modules):
    monkeypatch.setattr(appmod, "REPORT_ROUTES_AVAILABLE", True)
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "src.api.routes" or name.startswith("src.api.routes"):
            raise ImportError("no report routes")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert appmod.try_import_report_routes() is False
    assert appmod.REPORT_ROUTES_AVAILABLE is False


# ---------------------------------------------------------------------------
# try_import_core_routes
# ---------------------------------------------------------------------------
def test_try_import_core_routes_success(monkeypatch, clean_modules, stub_routes):
    assert appmod.try_import_core_routes() is True
    for key in ("event_routes", "graph_routes", "news_routes",
                "veracity_routes", "knowledge_graph_routes", "sentiment_routes"):
        assert key in clean_modules


def test_try_import_core_routes_import_error(monkeypatch, clean_modules):
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("src.api.routes"):
            raise ImportError("no core routes")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert appmod.try_import_core_routes() is False


# ---------------------------------------------------------------------------
# include_core_routers  (uses mock routers, never real .router)
# ---------------------------------------------------------------------------
class _RouterHolder:
    def __init__(self):
        self.router = object()


def _register(monkeypatch, **routers):
    monkeypatch.setattr(appmod, "_imported_modules", dict(routers))


class _RecordingApp:
    def __init__(self):
        self.included = []

    def include_router(self, router, **kwargs):
        self.included.append((router, kwargs))


def test_include_core_routers_news_pack_enabled(monkeypatch):
    holders = {k: _RouterHolder() for k in (
        "graph_routes", "knowledge_graph_routes", "document_routes",
        "news_routes", "event_routes", "veracity_routes", "sentiment_routes",
    )}
    _register(monkeypatch, **holders)
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: True)

    app = _RecordingApp()
    assert appmod.include_core_routers(app) is True
    included_routers = {r for r, _ in app.included}
    # All 7 routers included when news pack is on
    assert len(app.included) == 7
    assert holders["news_routes"].router in included_routers
    assert holders["sentiment_routes"].router in included_routers


def test_include_core_routers_news_pack_disabled(monkeypatch):
    holders = {k: _RouterHolder() for k in (
        "graph_routes", "knowledge_graph_routes", "document_routes",
        "news_routes", "event_routes", "veracity_routes", "sentiment_routes",
    )}
    _register(monkeypatch, **holders)
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: False)

    app = _RecordingApp()
    assert appmod.include_core_routers(app) is True
    included_routers = {r for r, _ in app.included}
    # Only the 3 generic routers; news-domain routers gated out
    assert len(app.included) == 3
    assert holders["graph_routes"].router in included_routers
    assert holders["news_routes"].router not in included_routers


def test_include_core_routers_missing_optional_routers(monkeypatch):
    # Only graph_routes present; the rest absent -> skipped without error.
    _register(monkeypatch, graph_routes=_RouterHolder())
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: True)
    app = _RecordingApp()
    assert appmod.include_core_routers(app) is True
    assert len(app.included) == 1


# ---------------------------------------------------------------------------
# include_optional_routers  (mock routers only)
# ---------------------------------------------------------------------------
def test_include_optional_routers_counts_included(monkeypatch):
    holders = {
        "auth_routes": _RouterHolder(),
        "rate_limit_routes": _RouterHolder(),
        "rbac_routes": _RouterHolder(),
    }
    _register(monkeypatch, **holders)
    # Enable only rate limiting + rbac; everything else off.
    for flag in (
        "RATE_LIMITING_AVAILABLE", "RBAC_AVAILABLE", "API_KEY_MANAGEMENT_AVAILABLE",
        "WAF_SECURITY_AVAILABLE", "ENHANCED_KG_AVAILABLE", "EVENT_TIMELINE_AVAILABLE",
        "QUICKSIGHT_AVAILABLE", "TOPIC_ROUTES_AVAILABLE", "GRAPH_SEARCH_AVAILABLE",
        "INFLUENCE_ANALYSIS_AVAILABLE", "AUTH_AVAILABLE", "SEARCH_AVAILABLE",
        "ALERT_ROUTES_AVAILABLE", "REPORT_ROUTES_AVAILABLE", "ARGUMENT_ROUTES_AVAILABLE",
        "KG_STREAM_ROUTES_AVAILABLE", "ENTITY_CORRECTION_ROUTES_AVAILABLE",
        "SOURCE_COMPARISON_ROUTES_AVAILABLE", "METRICS_ROUTES_AVAILABLE",
        "PRIVACY_ROUTES_AVAILABLE", "SECURITY_ROUTES_AVAILABLE",
    ):
        monkeypatch.setattr(appmod, flag, False)
    monkeypatch.setattr(appmod, "RATE_LIMITING_AVAILABLE", True)
    monkeypatch.setattr(appmod, "RBAC_AVAILABLE", True)
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: True)

    app = _RecordingApp()
    count = appmod.include_optional_routers(app)
    # rate-limiting feature counts once (adds 2 routers), rbac counts once.
    assert count == 2
    # 3 routers actually included: auth, rate_limit, rbac
    assert len(app.included) == 3


def test_include_optional_routers_none_available(monkeypatch):
    _register(monkeypatch)
    for flag in (
        "RATE_LIMITING_AVAILABLE", "RBAC_AVAILABLE", "API_KEY_MANAGEMENT_AVAILABLE",
        "WAF_SECURITY_AVAILABLE", "ENHANCED_KG_AVAILABLE", "EVENT_TIMELINE_AVAILABLE",
        "QUICKSIGHT_AVAILABLE", "TOPIC_ROUTES_AVAILABLE", "GRAPH_SEARCH_AVAILABLE",
        "INFLUENCE_ANALYSIS_AVAILABLE", "AUTH_AVAILABLE", "SEARCH_AVAILABLE",
        "ALERT_ROUTES_AVAILABLE", "REPORT_ROUTES_AVAILABLE", "ARGUMENT_ROUTES_AVAILABLE",
        "KG_STREAM_ROUTES_AVAILABLE", "ENTITY_CORRECTION_ROUTES_AVAILABLE",
        "SOURCE_COMPARISON_ROUTES_AVAILABLE", "METRICS_ROUTES_AVAILABLE",
        "PRIVACY_ROUTES_AVAILABLE", "SECURITY_ROUTES_AVAILABLE",
    ):
        monkeypatch.setattr(appmod, flag, False)
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: True)
    app = _RecordingApp()
    assert appmod.include_optional_routers(app) == 0
    assert app.included == []


# ---------------------------------------------------------------------------
# add_*_middleware_if_available
# ---------------------------------------------------------------------------
class _MiddlewareApp:
    def __init__(self):
        self.middlewares = []

    def add_middleware(self, cls, **kwargs):
        self.middlewares.append((cls, kwargs))


def test_add_waf_middleware_dev_mode_skips(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: True)
    monkeypatch.setattr(appmod, "WAF_SECURITY_AVAILABLE", True)
    app = _MiddlewareApp()
    assert appmod.add_waf_middleware_if_available(app) is False
    assert app.middlewares == []


def test_add_waf_middleware_added(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: False)
    monkeypatch.setattr(appmod, "WAF_SECURITY_AVAILABLE", True)
    waf = type("WAF", (), {})
    metrics = type("WAFMetrics", (), {})
    _register(monkeypatch, WAFSecurityMiddleware=waf, WAFMetricsMiddleware=metrics)
    app = _MiddlewareApp()
    assert appmod.add_waf_middleware_if_available(app) is True
    added = [c for c, _ in app.middlewares]
    assert waf in added and metrics in added


def test_add_waf_middleware_unavailable(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: False)
    monkeypatch.setattr(appmod, "WAF_SECURITY_AVAILABLE", False)
    app = _MiddlewareApp()
    assert appmod.add_waf_middleware_if_available(app) is False


def test_add_rate_limiting_middleware_added(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: False)
    monkeypatch.setattr(appmod, "RATE_LIMITING_AVAILABLE", True)
    mw = type("RLMw", (), {})
    cfg = type("RLCfg", (), {})
    _register(monkeypatch, RateLimitMiddleware=mw, RateLimitConfig=cfg)
    app = _MiddlewareApp()
    assert appmod.add_rate_limiting_middleware_if_available(app) is True
    assert app.middlewares[0][0] is mw
    # config kwarg is an instance of RateLimitConfig
    assert isinstance(app.middlewares[0][1]["config"], cfg)


def test_add_rate_limiting_dev_mode_skips(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: True)
    monkeypatch.setattr(appmod, "RATE_LIMITING_AVAILABLE", True)
    app = _MiddlewareApp()
    assert appmod.add_rate_limiting_middleware_if_available(app) is False


def test_add_rate_limiting_middleware_missing_modules(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: False)
    monkeypatch.setattr(appmod, "RATE_LIMITING_AVAILABLE", True)
    _register(monkeypatch)  # empty -> falls through to final return False
    app = _MiddlewareApp()
    assert appmod.add_rate_limiting_middleware_if_available(app) is False


def test_add_api_key_middleware_dev_mode_skips(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: True)
    monkeypatch.setattr(appmod, "API_KEY_MANAGEMENT_AVAILABLE", True)
    app = _MiddlewareApp()
    assert appmod.add_api_key_middleware_if_available(app) is False


def test_add_rbac_middleware_missing_modules(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: False)
    monkeypatch.setattr(appmod, "RBAC_AVAILABLE", True)
    _register(monkeypatch)  # empty -> final return False
    app = _MiddlewareApp()
    assert appmod.add_rbac_middleware_if_available(app) is False


def test_add_api_key_middleware_added(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: False)
    monkeypatch.setattr(appmod, "API_KEY_MANAGEMENT_AVAILABLE", True)
    auth = type("APIAuth", (), {})
    metrics = type("APIMetrics", (), {})
    _register(monkeypatch, APIKeyAuthMiddleware=auth, APIKeyMetricsMiddleware=metrics)
    app = _MiddlewareApp()
    assert appmod.add_api_key_middleware_if_available(app) is True
    added = [c for c, _ in app.middlewares]
    assert auth in added and metrics in added


def test_add_api_key_middleware_missing_modules(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: False)
    monkeypatch.setattr(appmod, "API_KEY_MANAGEMENT_AVAILABLE", True)
    _register(monkeypatch)  # empty
    app = _MiddlewareApp()
    assert appmod.add_api_key_middleware_if_available(app) is False


def test_add_rbac_middleware_added(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: False)
    monkeypatch.setattr(appmod, "RBAC_AVAILABLE", True)
    rbac = type("RBAC", (), {})
    metrics = type("RBACMetrics", (), {})
    _register(monkeypatch, EnhancedRBACMiddleware=rbac, RBACMetricsMiddleware=metrics)
    app = _MiddlewareApp()
    assert appmod.add_rbac_middleware_if_available(app) is True
    added = [c for c, _ in app.middlewares]
    assert rbac in added and metrics in added


def test_add_rbac_middleware_dev_mode_skips(monkeypatch):
    monkeypatch.setattr(appmod, "_dev_mode_enabled", lambda: True)
    monkeypatch.setattr(appmod, "RBAC_AVAILABLE", True)
    app = _MiddlewareApp()
    assert appmod.add_rbac_middleware_if_available(app) is False


def test_add_cors_middleware(monkeypatch):
    app = _MiddlewareApp()
    assert appmod.add_cors_middleware(app) is True
    from fastapi.middleware.cors import CORSMiddleware
    assert app.middlewares[0][0] is CORSMiddleware


# ---------------------------------------------------------------------------
# configure_error_handlers_if_available
# ---------------------------------------------------------------------------
def test_configure_error_handlers_invoked(monkeypatch):
    monkeypatch.setattr(appmod, "ERROR_HANDLERS_AVAILABLE", True)
    calls = []
    _register(monkeypatch, configure_error_handlers=lambda app: calls.append(app))
    app = object()
    assert appmod.configure_error_handlers_if_available(app) is True
    assert calls == [app]


def test_configure_error_handlers_unavailable(monkeypatch):
    monkeypatch.setattr(appmod, "ERROR_HANDLERS_AVAILABLE", False)
    assert appmod.configure_error_handlers_if_available(object()) is False


# ---------------------------------------------------------------------------
# include_versioned_routers  (mock routers only)
# ---------------------------------------------------------------------------
def test_include_versioned_routers_news_enabled(monkeypatch):
    holders = {k: _RouterHolder() for k in (
        "graph_routes", "knowledge_graph_routes", "document_routes",
        "news_routes", "event_routes", "veracity_routes",
    )}
    _register(monkeypatch, **holders)
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: True)
    app = _RecordingApp()
    assert appmod.include_versioned_routers(app) is True
    # 6 routers with /api/v1 prefix
    assert len(app.included) == 6
    assert all(kw.get("prefix") == "/api/v1" for _, kw in app.included)


def test_include_versioned_routers_news_disabled(monkeypatch):
    holders = {k: _RouterHolder() for k in (
        "graph_routes", "knowledge_graph_routes", "document_routes",
        "news_routes", "event_routes", "veracity_routes",
    )}
    _register(monkeypatch, **holders)
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: False)
    app = _RecordingApp()
    assert appmod.include_versioned_routers(app) is True
    # Only the 3 generic routers
    assert len(app.included) == 3


# ---------------------------------------------------------------------------
# root / health_check endpoints
# ---------------------------------------------------------------------------
def test_root_endpoint(monkeypatch):
    import asyncio
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: True)
    result = asyncio.run(appmod.root())
    assert result["status"] == "ok"
    assert result["domain_packs"]["news"] is True
    assert isinstance(result["features"], dict)
    assert "rate_limiting" in result["features"]


def test_health_check_endpoint():
    import asyncio
    result = asyncio.run(appmod.health_check())
    assert result["status"] == "healthy"
    assert result["version"] == "0.1.0"
    assert result["components"]["api"] == "operational"


# ---------------------------------------------------------------------------
# try_import_* ImportError failure branches
# ---------------------------------------------------------------------------
_ROUTE_IMPORT_HELPERS = [
    ("try_import_enhanced_kg_routes", "ENHANCED_KG_AVAILABLE"),
    ("try_import_event_timeline_routes", "EVENT_TIMELINE_AVAILABLE"),
    ("try_import_quicksight_routes", "QUICKSIGHT_AVAILABLE"),
    ("try_import_topic_routes", "TOPIC_ROUTES_AVAILABLE"),
    ("try_import_graph_search_routes", "GRAPH_SEARCH_AVAILABLE"),
    ("try_import_influence_routes", "INFLUENCE_ANALYSIS_AVAILABLE"),
    ("try_import_rate_limiting", "RATE_LIMITING_AVAILABLE"),
    ("try_import_rbac", "RBAC_AVAILABLE"),
    ("try_import_api_key_management", "API_KEY_MANAGEMENT_AVAILABLE"),
    ("try_import_waf_security", "WAF_SECURITY_AVAILABLE"),
    ("try_import_auth_routes", "AUTH_AVAILABLE"),
    ("try_import_search_routes", "SEARCH_AVAILABLE"),
    ("try_import_document_routes", "DOCUMENT_ROUTES_AVAILABLE"),
    ("try_import_alert_routes", "ALERT_ROUTES_AVAILABLE"),
    ("try_import_argument_routes", "ARGUMENT_ROUTES_AVAILABLE"),
    ("try_import_kg_stream_routes", "KG_STREAM_ROUTES_AVAILABLE"),
    ("try_import_entity_correction_routes", "ENTITY_CORRECTION_ROUTES_AVAILABLE"),
    ("try_import_source_comparison_routes", "SOURCE_COMPARISON_ROUTES_AVAILABLE"),
    ("try_import_metrics_routes", "METRICS_ROUTES_AVAILABLE"),
    ("try_import_privacy_routes", "PRIVACY_ROUTES_AVAILABLE"),
    ("try_import_security_routes", "SECURITY_ROUTES_AVAILABLE"),
]


@pytest.mark.parametrize("func_name,flag", _ROUTE_IMPORT_HELPERS)
def test_try_import_helpers_import_error(monkeypatch, clean_modules, func_name, flag):
    """Every route import helper returns False and clears its flag on ImportError."""
    monkeypatch.setattr(appmod, flag, True)

    real_import = __import__

    def fake_import(name, *args, **kwargs):
        # Fail any attempt to import route / middleware modules used by helpers.
        if name.startswith("src.api.routes") or name in (
            "src.api.middleware.rate_limit_middleware",
            "src.api.rbac.rbac_middleware",
            "src.api.auth.api_key_middleware",
            "src.api.security.waf_middleware",
        ):
            raise ImportError(f"blocked {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)
    func = getattr(appmod, func_name)
    assert func() is False
    assert getattr(appmod, flag) is False


# ---------------------------------------------------------------------------
# check_all_imports  — patch every helper so no real import happens
# ---------------------------------------------------------------------------
def test_check_all_imports_calls_all_helpers(monkeypatch):
    helper_names = [
        "try_import_error_handlers", "try_import_enhanced_kg_routes",
        "try_import_event_timeline_routes", "try_import_quicksight_routes",
        "try_import_topic_routes", "try_import_graph_search_routes",
        "try_import_influence_routes", "try_import_rate_limiting",
        "try_import_rbac", "try_import_api_key_management",
        "try_import_waf_security", "try_import_auth_routes",
        "try_import_search_routes", "try_import_document_routes",
        "try_import_report_routes", "try_import_alert_routes",
        "try_import_argument_routes", "try_import_kg_stream_routes",
        "try_import_entity_correction_routes", "try_import_source_comparison_routes",
        "try_import_metrics_routes", "try_import_privacy_routes",
        "try_import_security_routes",
    ]
    called = {name: 0 for name in helper_names}

    def make(name):
        def _fn():
            called[name] += 1
            return False
        return _fn

    for name in helper_names:
        monkeypatch.setattr(appmod, name, make(name))
    monkeypatch.setattr(appmod, "_load_domain_packs", lambda: called.__setitem__("_load", 1))

    appmod.check_all_imports()
    assert all(called[name] == 1 for name in helper_names)
    assert called.get("_load") == 1


# ---------------------------------------------------------------------------
# _load_domain_packs
# ---------------------------------------------------------------------------
def test_load_domain_packs_success(monkeypatch):
    calls = {"load": 0}
    reg = types.ModuleType("src.domains.registry")
    reg.load_config = lambda: calls.__setitem__("load", calls["load"] + 1)
    news = types.ModuleType("src.domains.news")
    monkeypatch.setitem(sys.modules, "src.domains.registry", reg)
    monkeypatch.setitem(sys.modules, "src.domains.news", news)
    appmod._load_domain_packs()
    assert calls["load"] == 1


def test_load_domain_packs_swallows_errors(monkeypatch):
    reg = types.ModuleType("src.domains.registry")

    def boom():
        raise RuntimeError("config broken")

    reg.load_config = boom
    monkeypatch.setitem(sys.modules, "src.domains.registry", reg)
    monkeypatch.setitem(sys.modules, "src.domains.news", types.ModuleType("src.domains.news"))
    # Must not raise — the helper logs and continues.
    appmod._load_domain_packs()


# ---------------------------------------------------------------------------
# include_optional_routers — exercise every feature block
# ---------------------------------------------------------------------------
def test_include_optional_routers_all_features_enabled(monkeypatch):
    feature_routers = {
        "auth_routes": _RouterHolder(), "rate_limit_routes": _RouterHolder(),
        "rbac_routes": _RouterHolder(), "api_key_routes": _RouterHolder(),
        "waf_security_routes": _RouterHolder(), "enhanced_kg_routes": _RouterHolder(),
        "event_timeline_routes": _RouterHolder(), "quicksight_routes": _RouterHolder(),
        "topic_routes": _RouterHolder(), "graph_search_routes": _RouterHolder(),
        "influence_routes": _RouterHolder(), "auth_routes_standalone": _RouterHolder(),
        "search_routes": _RouterHolder(), "alert_routes": _RouterHolder(),
        "report_routes": _RouterHolder(), "argument_routes": _RouterHolder(),
        "kg_stream_routes": _RouterHolder(), "entity_correction_routes": _RouterHolder(),
        "source_comparison_routes": _RouterHolder(), "metrics_routes": _RouterHolder(),
        "privacy_routes": _RouterHolder(), "security_routes": _RouterHolder(),
    }
    _register(monkeypatch, **feature_routers)
    for flag in (
        "RATE_LIMITING_AVAILABLE", "RBAC_AVAILABLE", "API_KEY_MANAGEMENT_AVAILABLE",
        "WAF_SECURITY_AVAILABLE", "ENHANCED_KG_AVAILABLE", "EVENT_TIMELINE_AVAILABLE",
        "QUICKSIGHT_AVAILABLE", "TOPIC_ROUTES_AVAILABLE", "GRAPH_SEARCH_AVAILABLE",
        "INFLUENCE_ANALYSIS_AVAILABLE", "AUTH_AVAILABLE", "SEARCH_AVAILABLE",
        "ALERT_ROUTES_AVAILABLE", "REPORT_ROUTES_AVAILABLE", "ARGUMENT_ROUTES_AVAILABLE",
        "KG_STREAM_ROUTES_AVAILABLE", "ENTITY_CORRECTION_ROUTES_AVAILABLE",
        "SOURCE_COMPARISON_ROUTES_AVAILABLE", "METRICS_ROUTES_AVAILABLE",
        "PRIVACY_ROUTES_AVAILABLE", "SECURITY_ROUTES_AVAILABLE",
    ):
        monkeypatch.setattr(appmod, flag, True)
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: True)

    app = _RecordingApp()
    count = appmod.include_optional_routers(app)
    # 21 features each increment the counter once.
    assert count == 21
    # rate-limiting feature adds 2 routers; the other 20 add 1 each => 22 routers.
    assert len(app.included) == 22


def test_include_optional_routers_news_gated_features_off(monkeypatch):
    """event_timeline and influence routes are gated behind the news pack."""
    _register(
        monkeypatch,
        event_timeline_routes=_RouterHolder(),
        influence_routes=_RouterHolder(),
    )
    for flag in (
        "RATE_LIMITING_AVAILABLE", "RBAC_AVAILABLE", "API_KEY_MANAGEMENT_AVAILABLE",
        "WAF_SECURITY_AVAILABLE", "ENHANCED_KG_AVAILABLE",
        "QUICKSIGHT_AVAILABLE", "TOPIC_ROUTES_AVAILABLE", "GRAPH_SEARCH_AVAILABLE",
        "AUTH_AVAILABLE", "SEARCH_AVAILABLE",
        "ALERT_ROUTES_AVAILABLE", "REPORT_ROUTES_AVAILABLE", "ARGUMENT_ROUTES_AVAILABLE",
        "KG_STREAM_ROUTES_AVAILABLE", "ENTITY_CORRECTION_ROUTES_AVAILABLE",
        "SOURCE_COMPARISON_ROUTES_AVAILABLE", "METRICS_ROUTES_AVAILABLE",
        "PRIVACY_ROUTES_AVAILABLE", "SECURITY_ROUTES_AVAILABLE",
    ):
        monkeypatch.setattr(appmod, flag, False)
    monkeypatch.setattr(appmod, "EVENT_TIMELINE_AVAILABLE", True)
    monkeypatch.setattr(appmod, "INFLUENCE_ANALYSIS_AVAILABLE", True)
    # News pack OFF -> these two gated features are skipped.
    monkeypatch.setattr("src.domains.registry.is_pack_enabled", lambda name: False)

    app = _RecordingApp()
    assert appmod.include_optional_routers(app) == 0
    assert app.included == []


# ---------------------------------------------------------------------------
# initialize_app  — patch all sub-steps so no real imports occur
# ---------------------------------------------------------------------------
def test_initialize_app_orchestrates_setup(monkeypatch):
    order = []
    monkeypatch.setattr(appmod, "create_app", lambda: order.append("create") or FastAPI())
    monkeypatch.setattr(appmod, "configure_error_handlers_if_available", lambda a: order.append("errors"))
    monkeypatch.setattr(appmod, "add_waf_middleware_if_available", lambda a: order.append("waf"))
    monkeypatch.setattr(appmod, "add_rate_limiting_middleware_if_available", lambda a: order.append("rate"))
    monkeypatch.setattr(appmod, "add_api_key_middleware_if_available", lambda a: order.append("apikey"))
    monkeypatch.setattr(appmod, "add_rbac_middleware_if_available", lambda a: order.append("rbac"))
    monkeypatch.setattr(appmod, "add_cors_middleware", lambda a: order.append("cors"))
    monkeypatch.setattr(appmod, "include_core_routers", lambda a: order.append("core"))
    monkeypatch.setattr(appmod, "include_optional_routers", lambda a: order.append("optional"))
    monkeypatch.setattr(appmod, "include_versioned_routers", lambda a: order.append("versioned"))
    monkeypatch.setattr(appmod, "METRICS_ROUTES_AVAILABLE", False)

    application = appmod.initialize_app()
    assert isinstance(application, FastAPI)
    assert order == [
        "create", "errors", "waf", "rate", "apikey", "rbac",
        "cors", "core", "optional", "versioned",
    ]


def test_initialize_app_starts_metrics_collector(monkeypatch):
    monkeypatch.setattr(appmod, "create_app", lambda: FastAPI())
    for name in (
        "configure_error_handlers_if_available", "add_waf_middleware_if_available",
        "add_rate_limiting_middleware_if_available", "add_api_key_middleware_if_available",
        "add_rbac_middleware_if_available", "add_cors_middleware",
        "include_core_routers", "include_optional_routers", "include_versioned_routers",
    ):
        monkeypatch.setattr(appmod, name, lambda a: None)
    monkeypatch.setattr(appmod, "METRICS_ROUTES_AVAILABLE", True)

    started = {"called": False}
    mon = types.ModuleType("src.monitoring.resource_monitor")
    mon.start_background_collector = lambda: started.__setitem__("called", True)
    monkeypatch.setitem(sys.modules, "src.monitoring.resource_monitor", mon)

    appmod.initialize_app()
    assert started["called"] is True


def test_initialize_app_metrics_collector_failure_swallowed(monkeypatch):
    monkeypatch.setattr(appmod, "create_app", lambda: FastAPI())
    for name in (
        "configure_error_handlers_if_available", "add_waf_middleware_if_available",
        "add_rate_limiting_middleware_if_available", "add_api_key_middleware_if_available",
        "add_rbac_middleware_if_available", "add_cors_middleware",
        "include_core_routers", "include_optional_routers", "include_versioned_routers",
    ):
        monkeypatch.setattr(appmod, name, lambda a: None)
    monkeypatch.setattr(appmod, "METRICS_ROUTES_AVAILABLE", True)

    mon = types.ModuleType("src.monitoring.resource_monitor")

    def boom():
        raise RuntimeError("collector down")

    mon.start_background_collector = boom
    monkeypatch.setitem(sys.modules, "src.monitoring.resource_monitor", mon)
    # Must not raise.
    application = appmod.initialize_app()
    assert isinstance(application, FastAPI)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
