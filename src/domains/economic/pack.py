"""Built-in economic knowledge pack."""

from src.domains.base import DomainPack, Enricher
from src.domains.economic.enrichers import economic_record_enricher
from src.domains.economic.model import OBJECT_TYPES, RELATION_TYPES

EconomicsDomainPack = DomainPack(
    name="economics",
    description=(
        "Vintaged quantitative knowledge with canonical indicators, explicit "
        "measurement dimensions, cited comparisons, and claim-to-data links."
    ),
    source_types=["news", "web", "note"],
    enrichers=[
        Enricher(
            name="economic-record",
            fn=economic_record_enricher,
            source_types=["news", "web", "note"],
            description="Project explicit release, filing, policy, and indicator metadata.",
        )
    ],
    capabilities=[
        "canonical-economic-indicators",
        "explicit-measurement-dimensions",
        "release-vintage-history",
        "bitemporal-economic-observations",
        "cited-economic-comparisons",
        "claim-observation-links",
    ],
    schema_versions={"dataset-series": "1.0.0", "model": "1.0.0", "research": "1.0.0"},
    ontology_extensions={
        "extends": "noesis-canonical-entity-relation-ontology",
        "reuses": [
            "dataset-series-v1",
            "noesis-temporal-v1",
            "noesis-evidence-bundle-v1",
        ],
        "object_types": sorted(OBJECT_TYPES),
        "relation_types": sorted(RELATION_TYPES),
    },
)

__all__ = ["EconomicsDomainPack"]
