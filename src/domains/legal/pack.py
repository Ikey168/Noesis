"""Built-in legal domain pack (disabled until enabled; independent of the political pack)."""

from src.domains.base import DomainPack

LEGAL_OBJECT_TYPES = ("legal_work", "legal_expression", "legal_version", "legal_passage", "court_decision",
                      "legal_citation", "legal_temporal_fact")
LEGAL_RELATION_TYPES = ("expression_of", "version_of", "cites", "amends", "corrects", "cites_norm",
                        "prior_instance", "enacted_from_procedure")

LegalDomainPack = DomainPack(
    name="legal",
    description=(
        "Bounded EU, German federal and Berlin legal research: exact works, language expressions, "
        "source-supported versions, cited passages and explicit citations; current force is never inferred."
    ),
    source_types=["web"],
    enrichers=[],
    ui_flags={},
    capabilities=[
        "cellar-work-expression-manifestation",
        "federal-court-decisions",
        "berlin-legal-publications",
        "as-of-version-selection",
        "cited-passage-retrieval",
        "version-comparison",
    ],
    schema_versions={"legal-work": "1.0.0", "legal-version": "1.0.0", "legal-version-selection": "1.0.0"},
    ontology_extensions={
        "extends": "noesis-canonical-entity-relation-ontology",
        "object_types": list(LEGAL_OBJECT_TYPES),
        "relation_types": list(LEGAL_RELATION_TYPES),
    },
)
