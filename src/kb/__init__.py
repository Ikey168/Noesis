"""
Knowledge-base domain layer.

A *knowledge domain* (economics, technology, web3, papers, …) is a named scope
of synthesized knowledge that applications consume through the KB contract.
Every domain is served through the :class:`~src.kb.backing.DomainBacking`
interface and can be realized by one of two backings, invisible to consumers:

- ``corpus-view`` — membership rows + views over the shared ``documents`` sink
  (right for overlapping daily topics that share enrichments), and
- ``namespace`` — a provisioned knowledge graph with its own storage and
  pipelines (right for domains with their own lifecycle: retention, re-index
  cadence, quotas, teardown).

This package is distinct from :mod:`src.domains`, which registers domain
*packs* (enrichers, routes, and feature flags — capabilities). A KB domain
scopes *content*; a pack extends *behaviour*.

Usage::

    from src.kb import load_registry

    registry = load_registry()            # reads config/domains.yml
    backing = registry.resolve("web3")    # -> DomainBacking, whatever the type
    backing.coverage()
"""

from src.kb.backing import (
    CorpusViewBacking,
    DomainBacking,
    NamespaceBacking,
)
from src.kb.registry import (
    DomainConfigError,
    DomainDefinition,
    FeedSpec,
    KnowledgeDomainRegistry,
    load_registry,
)

__all__ = [
    "CorpusViewBacking",
    "DomainBacking",
    "DomainConfigError",
    "DomainDefinition",
    "FeedSpec",
    "KnowledgeDomainRegistry",
    "NamespaceBacking",
    "load_registry",
]
