"""
Coverage tests for src/api/app_refactored.py.

Targets the REMAINING uncovered lines: the ``except ImportError`` branches of
every ``try_import_*`` helper, the middleware-registration helpers when a
feature flag is enabled, the ``include_*_routers`` helpers, and the ``root`` /
``health_check`` endpoint coroutines.

Design constraints honoured here:
  * We NEVER patch ``src.api.routes.<mod>.router`` (importing/instantiating the
    real routers can drag in torch and segfault). Instead we inject lightweight
    stand-in "route module" objects (a SimpleNamespace carrying a real, empty
    ``fastapi.APIRouter``) straight into ``app_refactored._imported_modules``
    and toggle the module-level flags ourselves.
  * ImportError branches are driven by temporarily patching ``builtins.__import__``
    so the specific ``from ... import ...`` statement inside each helper raises
    ImportError, without disturbing any other import.
  * Module-level flags and the ``_imported_modules`` registry are snapshotted and
    restored around every test so state never leaks between tests.
"""

import builtins
import types

import pytest
from fastapi import APIRouter, FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

import src.api.app_refactored as appref


# --------------------------------------------------------------------------- #
# Fixtures: snapshot / restore mutable module state
# --------------------------------------------------------------------------- #

_FLAG_NAMES = [
    "ERROR_HANDLERS_AVAILABLE",
    "ENHANCED_KG_AVAILABLE",
    "EVENT_TIMELINE_AVAILABLE",
    "QUICKSIGHT_AVAILABLE",
    "TOPIC_ROUTES_AVAILABLE",
    "GRAPH_SEARCH_AVAILABLE",
    "INFLUENCE_ANALYSIS_AVAILABLE",
    "RATE_LIMITING_AVAILABLE",
    "RBAC_AVAILABLE",
    "API_KEY_MANAGEMENT_AVAILABLE",
    "WAF_SECURITY_AVAILABLE",
    "AUTH_AVAILABLE",
    "SEARCH_AVAILABLE",
]


@pytest.fixture
def clean_state():
    """Snapshot flags + _imported_modules, restore after the test."""
    saved_flags = {name: getattr(appref, name) for name in _FLAG_NAMES}
    saved_modules = dict(appref._imported_modules)
    try:
        yield
    finally:
        for name, value in saved_flags.items():
            setattr(appref, name, value)
        appref._imported_modules.clear()
        appref._imported_modules.update(saved_modules)


def _route_module():
    """A stand-in 'routes' module: a namespace with a real empty APIRouter."""
    return types.SimpleNamespace(router=APIRouter())


class _NoOpMiddleware(BaseHTTPMiddleware):
    """A real Starlette middleware that accepts arbitrary kwargs and does nothing."""

    def __init__(self, app, **kwargs):
        super().__init__(app)

    async def dispatch(self, request, call_next):  # pragma: no cover - not exercised
        return await call_next(request)


class _Config:
    """Stand-in for RateLimitConfig() - just needs to be callable/constructible."""


# --------------------------------------------------------------------------- #
# ImportError branches of every try_import_* helper
# --------------------------------------------------------------------------- #

def _patch_import_failure(target_module, fromlist_names):
    """Return a fake __import__ that raises ImportError for one specific import.

    ``target_module`` is the module named in the ``from <target_module> import ...``
    statement; ``fromlist_names`` is the set of names any of which, if requested,
    should trigger the failure.
    """
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == target_module and fromlist and any(
            n in fromlist for n in fromlist_names
        ):
            raise ImportError(f"forced failure for {target_module}")
        return real_import(name, globals, locals, fromlist, level)

    return fake_import


# (helper, module-string, fromlist, flag-name)
_IMPORT_CASES = [
    (appref.try_import_error_handlers, "src.api.error_handlers",
     ["configure_error_handlers"], "ERROR_HANDLERS_AVAILABLE"),
    (appref.try_import_enhanced_kg_routes, "src.api.routes",
     ["enhanced_kg_routes"], "ENHANCED_KG_AVAILABLE"),
    (appref.try_import_event_timeline_routes, "src.api.routes",
     ["event_timeline_routes"], "EVENT_TIMELINE_AVAILABLE"),
    (appref.try_import_quicksight_routes, "src.api.routes",
     ["quicksight_routes"], "QUICKSIGHT_AVAILABLE"),
    (appref.try_import_topic_routes, "src.api.routes",
     ["topic_routes"], "TOPIC_ROUTES_AVAILABLE"),
    (appref.try_import_graph_search_routes, "src.api.routes",
     ["graph_search_routes"], "GRAPH_SEARCH_AVAILABLE"),
    (appref.try_import_influence_routes, "src.api.routes",
     ["influence_routes"], "INFLUENCE_ANALYSIS_AVAILABLE"),
    (appref.try_import_rate_limiting, "src.api.middleware.rate_limit_middleware",
     ["RateLimitConfig", "RateLimitMiddleware"], "RATE_LIMITING_AVAILABLE"),
    (appref.try_import_rbac, "src.api.rbac.rbac_middleware",
     ["EnhancedRBACMiddleware", "RBACMetricsMiddleware"], "RBAC_AVAILABLE"),
    (appref.try_import_api_key_management, "src.api.auth.api_key_middleware",
     ["APIKeyAuthMiddleware", "APIKeyMetricsMiddleware"], "API_KEY_MANAGEMENT_AVAILABLE"),
    (appref.try_import_waf_security, "src.api.routes",
     ["waf_security_routes"], "WAF_SECURITY_AVAILABLE"),
    (appref.try_import_auth_routes, "src.api.routes",
     ["auth_routes"], "AUTH_AVAILABLE"),
    (appref.try_import_search_routes, "src.api.routes",
     ["search_routes"], "SEARCH_AVAILABLE"),
]


@pytest.mark.parametrize(
    "helper,module_str,fromlist,flag",
    _IMPORT_CASES,
    ids=[case[3] for case in _IMPORT_CASES],
)
def test_try_import_helpers_import_error_branch(
    helper, module_str, fromlist, flag, clean_state
):
    """Each helper returns False and clears its flag when its import fails."""
    fake_import = _patch_import_failure(module_str, fromlist)
    real_import = builtins.__import__
    builtins.__import__ = fake_import
    try:
        result = helper()
    finally:
        builtins.__import__ = real_import

    assert result is False
    assert getattr(appref, flag) is False


def test_try_import_core_routes_import_error_branch(clean_state):
    """try_import_core_routes returns False when the core routes import fails."""
    fake_import = _patch_import_failure(
        "src.api.routes", ["event_routes", "graph_routes", "news_routes"]
    )
    real_import = builtins.__import__
    builtins.__import__ = fake_import
    try:
        result = appref.try_import_core_routes()
    finally:
        builtins.__import__ = real_import

    assert result is False


# --------------------------------------------------------------------------- #
# try_import_* success paths + check_all_imports
# --------------------------------------------------------------------------- #

def test_check_all_imports_runs_every_helper(clean_state):
    """check_all_imports drives every helper; imports succeed in this env."""
    # Reset flags to False so we observe them being set True.
    for name in _FLAG_NAMES:
        setattr(appref, name, False)

    appref.check_all_imports()

    # All optional modules are importable here, so flags flip to True.
    assert appref.ERROR_HANDLERS_AVAILABLE is True
    assert appref.RATE_LIMITING_AVAILABLE is True
    assert appref.RBAC_AVAILABLE is True
    assert appref.SEARCH_AVAILABLE is True
    # Registry populated with the imported objects.
    assert "configure_error_handlers" in appref._imported_modules
    assert "search_routes" in appref._imported_modules


def test_try_import_core_routes_success_populates_registry(clean_state):
    appref._imported_modules.pop("news_routes", None)
    assert appref.try_import_core_routes() is True
    for key in ("event_routes", "graph_routes", "news_routes",
                "veracity_routes", "knowledge_graph_routes"):
        assert key in appref._imported_modules


# --------------------------------------------------------------------------- #
# create_app
# --------------------------------------------------------------------------- #

def test_create_app_returns_configured_fastapi(clean_state):
    app = appref.create_app()
    assert isinstance(app, FastAPI)
    assert app.title == "Noesis API"
    assert app.version == "0.1.0"


# --------------------------------------------------------------------------- #
# Middleware registration helpers - "available" branches
# --------------------------------------------------------------------------- #

def test_configure_error_handlers_if_available_true(clean_state):
    called = {}

    def fake_configure(app):
        called["app"] = app

    appref.ERROR_HANDLERS_AVAILABLE = True
    appref._imported_modules["configure_error_handlers"] = fake_configure

    app = FastAPI()
    assert appref.configure_error_handlers_if_available(app) is True
    assert called["app"] is app


def test_configure_error_handlers_if_available_false_when_flag_off(clean_state):
    appref.ERROR_HANDLERS_AVAILABLE = False
    assert appref.configure_error_handlers_if_available(FastAPI()) is False


def test_add_waf_middleware_if_available_true(clean_state):
    appref.WAF_SECURITY_AVAILABLE = True
    appref._imported_modules["WAFSecurityMiddleware"] = _NoOpMiddleware
    appref._imported_modules["WAFMetricsMiddleware"] = _NoOpMiddleware

    app = FastAPI()
    before = len(app.user_middleware)
    assert appref.add_waf_middleware_if_available(app) is True
    assert len(app.user_middleware) == before + 2


def test_add_waf_middleware_if_available_false_when_flag_off(clean_state):
    appref.WAF_SECURITY_AVAILABLE = False
    assert appref.add_waf_middleware_if_available(FastAPI()) is False


def test_add_rate_limiting_middleware_if_available_true(clean_state):
    appref.RATE_LIMITING_AVAILABLE = True
    appref._imported_modules["RateLimitMiddleware"] = _NoOpMiddleware
    appref._imported_modules["RateLimitConfig"] = _Config

    app = FastAPI()
    before = len(app.user_middleware)
    assert appref.add_rate_limiting_middleware_if_available(app) is True
    assert len(app.user_middleware) == before + 1


def test_add_rate_limiting_middleware_if_available_false_when_flag_off(clean_state):
    appref.RATE_LIMITING_AVAILABLE = False
    assert appref.add_rate_limiting_middleware_if_available(FastAPI()) is False


def test_add_api_key_middleware_if_available_true(clean_state):
    appref.API_KEY_MANAGEMENT_AVAILABLE = True
    appref._imported_modules["APIKeyAuthMiddleware"] = _NoOpMiddleware
    appref._imported_modules["APIKeyMetricsMiddleware"] = _NoOpMiddleware

    app = FastAPI()
    before = len(app.user_middleware)
    assert appref.add_api_key_middleware_if_available(app) is True
    assert len(app.user_middleware) == before + 2


def test_add_api_key_middleware_if_available_false_when_flag_off(clean_state):
    appref.API_KEY_MANAGEMENT_AVAILABLE = False
    assert appref.add_api_key_middleware_if_available(FastAPI()) is False


def test_add_rbac_middleware_if_available_true(clean_state):
    appref.RBAC_AVAILABLE = True
    appref._imported_modules["EnhancedRBACMiddleware"] = _NoOpMiddleware
    appref._imported_modules["RBACMetricsMiddleware"] = _NoOpMiddleware

    app = FastAPI()
    before = len(app.user_middleware)
    assert appref.add_rbac_middleware_if_available(app) is True
    assert len(app.user_middleware) == before + 2


def test_add_rbac_middleware_if_available_false_when_flag_off(clean_state):
    appref.RBAC_AVAILABLE = False
    assert appref.add_rbac_middleware_if_available(FastAPI()) is False


def test_add_cors_middleware_always_true(clean_state):
    app = FastAPI()
    before = len(app.user_middleware)
    assert appref.add_cors_middleware(app) is True
    assert len(app.user_middleware) == before + 1


# --------------------------------------------------------------------------- #
# Router inclusion helpers - with injected stand-in route modules
# --------------------------------------------------------------------------- #

def _install_core_route_modules():
    for key in ("graph_routes", "knowledge_graph_routes", "news_routes",
                "event_routes", "veracity_routes"):
        appref._imported_modules[key] = _route_module()


def test_include_core_routers_includes_all(clean_state):
    _install_core_route_modules()
    app = FastAPI()
    before = len(app.routes)
    assert appref.include_core_routers(app) is True
    # Five routers (each empty) were included -> route count unchanged is fine,
    # but include_router with an empty APIRouter still returns True path taken.
    assert len(app.routes) >= before


def test_include_versioned_routers_includes_all(clean_state):
    _install_core_route_modules()
    app = FastAPI()
    assert appref.include_versioned_routers(app) is True


def test_include_core_routers_skips_missing_modules(clean_state):
    # No core modules registered -> every ``if x:`` is falsy but function still True.
    for key in ("graph_routes", "knowledge_graph_routes", "news_routes",
                "event_routes", "veracity_routes"):
        appref._imported_modules.pop(key, None)
    assert appref.include_core_routers(FastAPI()) is True
    assert appref.include_versioned_routers(FastAPI()) is True


def test_include_optional_routers_counts_enabled_features(clean_state):
    """Enable every optional feature with a stand-in module and count inclusions."""
    # Feature flag -> (registry key). One-router features:
    single = {
        "RBAC_AVAILABLE": "rbac_routes",
        "API_KEY_MANAGEMENT_AVAILABLE": "api_key_routes",
        "WAF_SECURITY_AVAILABLE": "waf_security_routes",
        "ENHANCED_KG_AVAILABLE": "enhanced_kg_routes",
        "EVENT_TIMELINE_AVAILABLE": "event_timeline_routes",
        "QUICKSIGHT_AVAILABLE": "quicksight_routes",
        "TOPIC_ROUTES_AVAILABLE": "topic_routes",
        "GRAPH_SEARCH_AVAILABLE": "graph_search_routes",
        "INFLUENCE_ANALYSIS_AVAILABLE": "influence_routes",
        "AUTH_AVAILABLE": "auth_routes_standalone",
        "SEARCH_AVAILABLE": "search_routes",
    }
    for flag, key in single.items():
        setattr(appref, flag, True)
        appref._imported_modules[key] = _route_module()

    # Rate limiting bundles two routers under one increment.
    appref.RATE_LIMITING_AVAILABLE = True
    appref._imported_modules["auth_routes"] = _route_module()
    appref._imported_modules["rate_limit_routes"] = _route_module()

    app = FastAPI()
    count = appref.include_optional_routers(app)

    # 11 single-feature increments + 1 for the rate-limiting bundle = 12.
    assert count == 12


def test_include_optional_routers_none_when_all_disabled(clean_state):
    for name in _FLAG_NAMES:
        setattr(appref, name, False)
    assert appref.include_optional_routers(FastAPI()) == 0


# --------------------------------------------------------------------------- #
# initialize_app - full assembly (uses real, already-imported route modules)
# --------------------------------------------------------------------------- #

def test_initialize_app_returns_fastapi(clean_state):
    app = appref.initialize_app()
    assert isinstance(app, FastAPI)
    # CORS middleware is always added, so there is at least one middleware.
    assert len(app.user_middleware) >= 1


# --------------------------------------------------------------------------- #
# Endpoint coroutines: root() and health_check()
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_root_endpoint_reports_status_and_features():
    body = await appref.root()
    assert body["status"] == "ok"
    assert "Noesis API" in body["message"]
    features = body["features"]
    # Feature booleans mirror the module-level flags.
    assert features["rate_limiting"] == appref.RATE_LIMITING_AVAILABLE
    assert features["rbac"] == appref.RBAC_AVAILABLE
    assert set(features) >= {
        "rate_limiting", "rbac", "api_key_management", "waf_security",
        "enhanced_kg", "event_timeline", "quicksight", "topic_routes",
        "graph_search", "influence_analysis",
    }


@pytest.mark.asyncio
async def test_health_check_reports_component_status():
    body = await appref.health_check()
    assert body["status"] == "healthy"
    assert body["version"] == "0.1.0"
    comps = body["components"]
    assert comps["api"] == "operational"
    # rate_limiting reflects the flag as operational/disabled.
    expected = "operational" if appref.RATE_LIMITING_AVAILABLE else "disabled"
    assert comps["rate_limiting"] == expected
    assert comps["database"] == "unknown"
    assert comps["cache"] == "unknown"
