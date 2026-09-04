"""Built-in technical knowledge pack."""

from src.domains.base import DomainPack
from src.domains.technical.model import OBJECT_TYPES, RELATION_TYPES

TechnicalDomainPack = DomainPack(
    name="technology",
    description=(
        "Version-aware software, repository, standard, dependency, and "
        "security-advisory knowledge with immutable identities and citations."
    ),
    source_types=["web", "note"],
    capabilities=[
        "canonical-package-coordinates",
        "incremental-git-ingestion",
        "section-addressable-specifications",
        "osv-cve-advisories",
        "dependency-compatibility-graph",
        "temporal-technical-queries",
    ],
    schema_versions={"technical-model": "1.0.0", "technical-research": "1.0.0"},
    ontology_extensions={
        "extends": "noesis-canonical-entity-relation-ontology",
        "reuses": ["noesis-temporal-v1", "noesis-evidence-bundle-v1"],
        "object_types": sorted(OBJECT_TYPES),
        "relation_types": sorted(RELATION_TYPES),
    },
)

__all__ = ["TechnicalDomainPack"]
