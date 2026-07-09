"""
Connector registry: maps a document ``source_type`` to its connector.

Connectors register themselves (typically at import time) so callers can resolve
the right connector for a source type without importing it directly::

    from src.ingestion.connectors.registry import get_connector
    connector = get_connector("news")
    for document in connector.harvest():
        ...
"""

from __future__ import annotations

from typing import Callable, Dict, List, Type, Union

from src.ingestion.connectors.base import Connector

# source_type -> connector class (instantiated lazily on first get).
_REGISTRY: Dict[str, Type[Connector]] = {}
_INSTANCES: Dict[str, Connector] = {}


def register_connector(
    connector_cls: Type[Connector] = None,
) -> Union[Type[Connector], Callable[[Type[Connector]], Type[Connector]]]:
    """Register a connector class under its ``source_type``.

    Usable as a plain call or a class decorator::

        @register_connector
        class NewsConnector(Connector):
            source_type = "news"
    """

    def _do_register(cls: Type[Connector]) -> Type[Connector]:
        source_type = getattr(cls, "source_type", "")
        if not source_type:
            raise ValueError(f"{cls.__name__} must set a non-empty source_type to register")
        key = getattr(cls, "name", "") or source_type
        _REGISTRY[key] = cls
        _INSTANCES.pop(key, None)  # drop any stale cached instance
        return cls

    if connector_cls is not None:
        return _do_register(connector_cls)
    return _do_register


def get_connector(name: str) -> Connector:
    """Return a (cached) connector instance by registry ``name``.

    The registry key is the connector's ``name`` (defaulting to its
    ``source_type``), so for the built-in connectors this is still the source
    type (``"news"``, ``"paper"``, …), while a connector that sets a distinct
    ``name`` (e.g. ``"filings"``) is resolved by that name.
    """
    if name not in _REGISTRY:
        raise KeyError(
            f"No connector registered as {name!r}; available: {available_source_types()}"
        )
    if name not in _INSTANCES:
        _INSTANCES[name] = _REGISTRY[name]()
    return _INSTANCES[name]


def available_source_types() -> List[str]:
    """List the registry keys (connector names) that resolve via ``get_connector``.

    For the built-in connectors these equal their ``source_type``; a connector
    with a distinct ``name`` appears under that name.
    """
    return sorted(_REGISTRY)


def is_registered(name: str) -> bool:
    return name in _REGISTRY


def source_types() -> List[str]:
    """List the distinct document source types the registered connectors emit."""
    return sorted({getattr(cls, "source_type", "") for cls in _REGISTRY.values()})
