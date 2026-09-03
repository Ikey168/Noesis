"""
Domain-pack base types.

A :class:`DomainPack` groups the enrichers, FastAPI route modules, and UI
feature flags that belong to one knowledge domain. The news pack is the first;
future packs (research, legal, finance, …) follow the same pattern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class Enricher:
    """An enrichment function that runs on Documents of matching source types.

    ``fn`` receives a Document (as a plain dict or the ``Document`` dataclass)
    and returns a dict of enrichment results, or ``None`` to skip.
    ``source_types`` is an allowlist; an empty list means "all source types".
    """

    name: str
    fn: Callable[[Any], Optional[Dict[str, Any]]]
    source_types: List[str] = field(default_factory=list)
    description: str = ""

    def applies_to(self, source_type: str) -> bool:
        """Return True if this enricher should run for the given source_type."""
        return not self.source_types or source_type in self.source_types

    def run(self, document: Any) -> Optional[Dict[str, Any]]:
        """Run the enrichment function, swallowing exceptions so one bad
        enricher never crashes the whole pipeline."""
        try:
            return self.fn(document)
        except Exception:
            import logging
            logging.getLogger(__name__).warning(
                "Enricher %r failed for document %s",
                self.name,
                getattr(document, "document_id", "?"),
                exc_info=True,
            )
            return None


@dataclass
class DomainPack:
    """A pluggable feature bundle for one knowledge domain.

    Attributes:
        name: Unique identifier used in ``config/domain_packs.json``.
        description: Human-readable summary.
        source_types: Source types this pack primarily handles.
        enrichers: List of :class:`Enricher` instances registered by this pack.
        route_modules: Fully-qualified module paths whose ``.router`` attribute
            should be mounted when this pack is enabled.
            e.g. ``["src.api.routes.news_routes"]``
        ui_flags: Feature flags forwarded to the frontend.
            e.g. ``{"timeline": True, "clusters": True}``
        telemetry: Optional zero-arg callable advertising this pack's
            ambient empty-canvas telemetry (R3 / Track N2). Returns a dict
            with any of ``signals`` (KPI strip entries: {label, value}),
            ``movers`` (suggestion rows: {label, intent, change?}) and
            ``ticker`` ({label, items}). Exceptions are swallowed by the
            collector — a pack's telemetry must never break the canvas.
        capabilities: Stable machine-readable feature names.
        schema_versions: Contract name to semantic-version mapping.
        ontology_extensions: Domain vocabulary mapped onto canonical objects.
    """

    name: str
    description: str = ""
    source_types: List[str] = field(default_factory=list)
    enrichers: List[Enricher] = field(default_factory=list)
    route_modules: List[str] = field(default_factory=list)
    ui_flags: Dict[str, bool] = field(default_factory=dict)
    telemetry: Optional[Callable[[], Dict[str, Any]]] = None
    capabilities: List[str] = field(default_factory=list)
    schema_versions: Dict[str, str] = field(default_factory=dict)
    ontology_extensions: Dict[str, Any] = field(default_factory=dict)
