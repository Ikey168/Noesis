"""
Knowledge-domain registry: the single source of truth for what domains exist
and how each one is realized.

Definitions live in a declarative config (``config/domains.yml`` by default,
overridable via ``NOESIS_DOMAINS_CONFIG``). The registry validates the config
against the schema below and resolves each domain name to its
:class:`~src.kb.backing.DomainBacking` implementation. Consumers only ever
hold the backing interface — switching a domain between backings is a config
change, not a consumer change.

Config schema (YAML)::

    version: 1
    domains:
      - name: web3                  # required, unique slug [a-z0-9-]
        backing: corpus-view        # required: corpus-view | namespace
        description: ...            # optional
        embedding_model: all-MiniLM-L6-v2   # required (shared space guard)
        feeds:                      # optional feed subscriptions
          - url: https://example.com/rss
            name: Example
            tags: [web3]
        tags: [web3]                # feed-tag scope for by-source membership
        keywords: [defi, staking]   # seed vocabulary for by-content membership
        embedding_anchors:          # anchor sentences for by-content membership
          - "decentralized finance protocols and on-chain governance"
        membership_threshold: 0.35  # optional, by-content assignment cutoff
        namespace: reference        # namespace backing only; defaults to name
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from src.kb.backing import CorpusViewBacking, DomainBacking, NamespaceBacking

#: repo-root-relative default config location
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "domains.yml"

#: environment variable overriding the config path
CONFIG_PATH_ENV = "NOESIS_DOMAINS_CONFIG"

VALID_BACKINGS = ("corpus-view", "namespace")

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_BACKING_CLASSES = {
    "corpus-view": CorpusViewBacking,
    "namespace": NamespaceBacking,
}


class DomainConfigError(ValueError):
    """Raised when the domain config is missing, malformed, or inconsistent."""


@dataclass
class FeedSpec:
    """One feed subscription a domain wants harvested under its tags."""

    url: str
    name: str = ""
    tags: List[str] = field(default_factory=list)


@dataclass
class DomainDefinition:
    """A validated knowledge-domain definition."""

    name: str
    backing: str
    embedding_model: str
    description: str = ""
    feeds: List[FeedSpec] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    embedding_anchors: List[str] = field(default_factory=list)
    membership_threshold: float = 0.35
    namespace: Optional[str] = None


def _require_str_list(raw: Any, domain: str, key: str) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise DomainConfigError(
            f"domain {domain!r}: {key} must be a list of strings"
        )
    return list(raw)


def _parse_feeds(raw: Any, domain: str) -> List[FeedSpec]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise DomainConfigError(f"domain {domain!r}: feeds must be a list")
    feeds: List[FeedSpec] = []
    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("url"):
            raise DomainConfigError(
                f"domain {domain!r}: every feed needs at least a url"
            )
        feeds.append(
            FeedSpec(
                url=str(entry["url"]),
                name=str(entry.get("name", "")),
                tags=_require_str_list(entry.get("tags"), domain, "feed tags"),
            )
        )
    return feeds


def _parse_domain(raw: Any) -> DomainDefinition:
    if not isinstance(raw, dict):
        raise DomainConfigError("every domain entry must be a mapping")

    name = raw.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise DomainConfigError(
            f"domain name {name!r} must be a lowercase slug ([a-z0-9-])"
        )

    backing = raw.get("backing")
    if backing not in VALID_BACKINGS:
        raise DomainConfigError(
            f"domain {name!r}: backing {backing!r} is not one of {VALID_BACKINGS}"
        )

    embedding_model = raw.get("embedding_model")
    if not isinstance(embedding_model, str) or not embedding_model.strip():
        raise DomainConfigError(
            f"domain {name!r}: embedding_model is required — cross-backing "
            "similarity needs one shared embedding space, so every domain "
            "declares (and is checked against) its model"
        )

    threshold = raw.get("membership_threshold", 0.35)
    if not isinstance(threshold, (int, float)) or not 0.0 <= float(threshold) <= 1.0:
        raise DomainConfigError(
            f"domain {name!r}: membership_threshold must be in [0, 1]"
        )

    namespace = raw.get("namespace")
    if namespace is not None and not isinstance(namespace, str):
        raise DomainConfigError(f"domain {name!r}: namespace must be a string")
    if backing == "namespace" and not namespace:
        namespace = name
    if backing == "corpus-view" and namespace:
        raise DomainConfigError(
            f"domain {name!r}: namespace is only valid for namespace backing"
        )

    return DomainDefinition(
        name=name,
        backing=backing,
        embedding_model=embedding_model.strip(),
        description=str(raw.get("description", "")),
        feeds=_parse_feeds(raw.get("feeds"), name),
        tags=_require_str_list(raw.get("tags"), name, "tags"),
        keywords=_require_str_list(raw.get("keywords"), name, "keywords"),
        embedding_anchors=_require_str_list(
            raw.get("embedding_anchors"), name, "embedding_anchors"
        ),
        membership_threshold=float(threshold),
        namespace=namespace,
    )


class KnowledgeDomainRegistry:
    """Validated domain definitions + resolution to backings."""

    def __init__(self, definitions: List[DomainDefinition]):
        names = [definition.name for definition in definitions]
        duplicates = {name for name in names if names.count(name) > 1}
        if duplicates:
            raise DomainConfigError(
                f"duplicate domain names: {sorted(duplicates)}"
            )
        self._definitions = {
            definition.name: definition for definition in definitions
        }

    @classmethod
    def from_config(
        cls, path: Optional[os.PathLike] = None
    ) -> "KnowledgeDomainRegistry":
        """Load and validate the registry from a YAML config file."""
        config_path = Path(
            path or os.environ.get(CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH
        )
        if not config_path.exists():
            raise DomainConfigError(f"domain config not found: {config_path}")
        try:
            raw = yaml.safe_load(config_path.read_text()) or {}
        except yaml.YAMLError as exc:
            raise DomainConfigError(
                f"domain config is not valid YAML: {config_path}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise DomainConfigError("domain config must be a mapping")
        if raw.get("version") != 1:
            raise DomainConfigError(
                f"unsupported domain config version {raw.get('version')!r}; "
                "expected 1"
            )
        domains_raw = raw.get("domains") or []
        if not isinstance(domains_raw, list):
            raise DomainConfigError("domains must be a list")
        return cls([_parse_domain(entry) for entry in domains_raw])

    def domains(self) -> List[DomainDefinition]:
        """All definitions, in config order."""
        return list(self._definitions.values())

    def names(self) -> List[str]:
        return list(self._definitions.keys())

    def get(self, name: str) -> DomainDefinition:
        try:
            return self._definitions[name]
        except KeyError:
            raise DomainConfigError(
                f"unknown domain {name!r}; configured: {self.names()}"
            ) from None

    def resolve(self, name: str, conn: Any = None) -> DomainBacking:
        """Resolve a domain name to its backing implementation.

        ``conn`` optionally injects a warehouse connection (tests, attached
        databases); by default the backing lazily uses the shared connection.
        """
        definition = self.get(name)
        return _BACKING_CLASSES[definition.backing](definition, conn=conn)

    def embedding_models(self) -> Dict[str, str]:
        """Domain -> embedding model, for shared-space consistency checks."""
        return {
            definition.name: definition.embedding_model
            for definition in self.domains()
        }


def load_registry(path: Optional[os.PathLike] = None) -> KnowledgeDomainRegistry:
    """Convenience wrapper: load the registry from the configured path."""
    return KnowledgeDomainRegistry.from_config(path)
