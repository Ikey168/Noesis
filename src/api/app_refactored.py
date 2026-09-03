"""Compatibility import for the canonical application factory.

The experimental second application assembly used the retired remote graph
stack and drifted from ``src.api.app``. Keep this module importable for older
deployments while maintaining one active route surface.
"""

from src.api.app import (  # noqa: F401
    add_cors_middleware,
    create_app,
    health_check,
    include_core_routers,
    include_optional_routers,
    include_versioned_routers,
    initialize_app,
    root,
)

__all__ = [
    "add_cors_middleware",
    "create_app",
    "health_check",
    "include_core_routers",
    "include_optional_routers",
    "include_versioned_routers",
    "initialize_app",
    "root",
]
