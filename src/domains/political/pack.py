"""Built-in political domain pack."""

from src.domains.base import DomainPack, Enricher
from src.domains.political.enrichers import political_record_enricher
from src.domains.political.model import OBJECT_TYPES, RELATION_TYPES

PoliticalDomainPack = DomainPack(
    name="political",
    description=(
        "Official political knowledge with jurisdiction-scoped identity, "
        "bounded office terms, proposal/vote lifecycles, and cited temporal queries."
    ),
    source_types=["note", "web"],
    enrichers=[
        Enricher(
            name="political-record",
            fn=political_record_enricher,
            source_types=["note", "web"],
            description="Project explicit official-source metadata into political fields.",
        )
    ],
    ui_flags={},
    capabilities=[
        "official-source-manifests",
        "scoped-entity-resolution",
        "bitemporal-office-terms",
        "proposal-vote-lifecycle",
        "political-research-queries",
    ],
    schema_versions={
        "source": "1.0.0",
        "model": "1.0.0",
        "research": "1.0.0",
    },
    ontology_extensions={
        "extends": "noesis-canonical-entity-relation-ontology",
        "object_types": sorted(OBJECT_TYPES),
        "relation_types": sorted(RELATION_TYPES),
    },
)

__all__ = ["PoliticalDomainPack"]
