"""Life-science reference records for the Science ``science.life-sciences`` provider (#2652, LS02 #2661).

Contract ``noesis-lifesci-record-v2``: one provider statement of a **gene** (NCBI Gene), **protein** (UniProtKB),
**structure** (RCSB PDB entry), **taxon** (NCBI Taxonomy), **target**, **compound**, **activity** or **document**
(ChEMBL), keyed by source and native accession. Every statement carries

* the source release it was observed in (``release.label`` and ``release.published_on`` with the basis that dated
  it: the provider's own release stamp, the operator-declared release, or the retrieval time, labelled);
* a version marker as the source publishes it (UniProt entry version, PDB major.minor revision) or, where the source
  publishes none (NCBI Gene and Taxonomy summaries, ChEMBL records between releases), a content digest;
* the status as published - ``active``, ``merged``, ``demerged``, ``deleted``, ``obsolete``, ``replaced`` or
  ``discontinued`` - with the successor accessions the source names;
* the cross-references exactly as the source publishes them, citations (PubMed, DOI, ChEMBL document) and the
  source licence.

Records are immutable revisions (:mod:`src.kb.lifesci_store`); a removal, merge or correction by the source is a new
revision, never a deletion.

**Data minimisation (LS01).** The sources publish personal names in citation author lists (UniProt references,
PDB ``audit_author`` and primary-citation authors, ChEMBL document authors) and, for PDB, depositor names. These are
never stored: the adapters drop them before a statement is built and :func:`validate_statement` refuses any statement
that carries a personal field (:data:`PERSONAL_KEYS`), anywhere in its payload. Citations are kept by identifier
(PubMed ID, DOI, ChEMBL document ID) and title only.

**Exclusions.** No biological or clinical inference, no activity prediction, no sequence analysis beyond storage (a
sequence is stored with the length and checksums the source publishes, nothing is computed from it), and no
redistribution beyond each source's licence. Keys that would carry a prediction, score or derived value are refused
(:data:`INFERENCE_KEYS`).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

CONTRACT = "noesis-lifesci-record-v2"
MATCH_CONTRACT = "noesis-lifesci-match-v1"
LINK_CONTRACT = "noesis-lifesci-link-v1"
ANSWER_CONTRACT = "noesis-lifesci-answer-v1"
BUNDLE_CONTRACT = "noesis-lifesci-evidence-bundle-v1"
NOTIFICATION_CONTRACT = "noesis-lifesci-notification-v1"
READ_SCOPE = "knowledge:lifesci:read"
WRITE_SCOPE = "knowledge:lifesci:write"
REVIEW_SCOPE = "knowledge:lifesci:review"
DEFAULT_NAMESPACE = "global"
BUNDLE = "science"
PROVIDER_ID = "science.life-sciences"
SOURCE_PACK = "primary-scientific-evidence"
FEATURES = {
    "uniprot": "life-sciences-uniprot",
    "ncbi-gene": "life-sciences-ncbi",
    "ncbi-taxonomy": "life-sciences-ncbi",
    "rcsb-pdb": "life-sciences-pdb",
    "chembl": "life-sciences-chembl",
}
SOURCES = ("uniprot", "ncbi-gene", "ncbi-taxonomy", "rcsb-pdb", "chembl")
RECORD_TYPES = ("gene", "protein", "structure", "taxon", "target", "compound", "activity", "document")
SOURCE_TYPES = {
    "uniprot": ("protein",),
    "ncbi-gene": ("gene",),
    "ncbi-taxonomy": ("taxon",),
    "rcsb-pdb": ("structure",),
    "chembl": ("target", "compound", "activity", "document"),
}
STATUSES = ("active", "merged", "demerged", "deleted", "obsolete", "replaced", "discontinued")
INACTIVE = frozenset(STATUSES) - {"active"}
RELEASE_BASES = ("provider", "declared", "retrieval")
VERSION_BASES = ("entry-version", "pdb-revision", "content")
CITATION_KINDS = ("pubmed", "doi", "chembl-document")
LICENCES = {
    "uniprot": {"id": "CC-BY-4.0", "attribution": "UniProt Consortium, UniProtKB (www.uniprot.org)"},
    "ncbi-gene": {"id": "NCBI-public-domain", "attribution": "National Center for Biotechnology Information, "
                  "NCBI Gene"},
    "ncbi-taxonomy": {"id": "NCBI-public-domain", "attribution": "National Center for Biotechnology Information, "
                      "NCBI Taxonomy"},
    "rcsb-pdb": {"id": "CC0-1.0", "attribution": "RCSB Protein Data Bank (rcsb.org), wwPDB"},
    "chembl": {"id": "CC-BY-SA-3.0", "attribution": "ChEMBL, EMBL-EBI (www.ebi.ac.uk/chembl)"},
}
# Cross-reference databases that name another record of this provider (database -> (source, record type)).
XREF_TARGETS = {
    "PDB": ("rcsb-pdb", "structure"),
    "GeneID": ("ncbi-gene", "gene"),
    "ChEMBL": ("chembl", "target"),
    "UniProtKB": ("uniprot", "protein"),
    "UniProt": ("uniprot", "protein"),
    "NCBI Taxonomy": ("ncbi-taxonomy", "taxon"),
    "ChEMBL compound": ("chembl", "compound"),
    "ChEMBL document": ("chembl", "document"),
}
# Personal fields the sources publish; never stored (LS01 minimisation decision).
PERSONAL_KEYS = frozenset({
    "author", "authors", "audit_author", "citation_author", "rcsb_primary_citation_authors", "submitter",
    "submitters", "submission_names", "depositor", "depositors", "contact", "contact_author", "email", "emails",
    "orcid", "orcid_identifier", "person", "persons", "pdbx_contact_author",
})
# Keys that would carry an inference, prediction, score or converted value (exclusions).
INFERENCE_KEYS = frozenset({
    "prediction", "predicted_activity", "predicted_target", "activity_prediction", "druggability",
    "disease_association", "clinical_relevance", "efficacy", "toxicity_prediction", "score", "similarity_score",
    "pchembl_value", "converted_value", "normalised_value", "normalized_value", "aggregate_value", "mean_value",
    "sequence_alignment", "homology",
})
EXCLUSIONS = (
    "biological or clinical inference from reference records",
    "activity prediction, scoring or ranking of compounds",
    "conversion, normalisation or aggregation of activity values across assays",
    "sequence analysis beyond storage (no alignment, homology or computed properties)",
    "redistribution beyond each source's licence",
    "personal names of authors, depositors or submitters",
    "name-based merging of records across sources",
)
NEVER_SENTENCE = (
    "Reference records as each database published them - accessions, entry and sequence versions, revision "
    "history, lineage, cross-references and activity values with their relation, unit and validity comment - never "
    "inferred, predicted, converted, aggregated or merged by name; no personal names are stored."
)
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema/noesis-lifesci-record-v2.json"
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:read", "knowledge:entity-history:write",
                          "knowledge:entity-history:review", "knowledge:entity-history:execute"}

UNIPROT_ACCESSION = re.compile(r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$")
PDB_ID = re.compile(r"^[0-9][A-Z0-9]{3}$")
CHEMBL_ID = re.compile(r"^CHEMBL\d+$")
INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")


class LifeSciError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise LifeSciError("unauthorized", f"{required} and namespace access are required")


def keys_in(value: Any, wanted: frozenset[str], path: str = "$") -> list[str]:
    """Paths of keys anywhere in a value that belong to ``wanted`` (case-insensitive)."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in wanted:
                found.append(f"{path}.{key}")
            found += keys_in(item, wanted, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += keys_in(item, wanted, f"{path}[{index}]")
    return found


def personal_fields(value: Any) -> list[str]:
    return keys_in(value, PERSONAL_KEYS)


def minimised(value: Any) -> Any:
    """A copy with every personal key removed (used on tool outputs as a second guard)."""
    if isinstance(value, Mapping):
        return {k: minimised(v) for k, v in value.items() if str(k).casefold() not in PERSONAL_KEYS}
    if isinstance(value, list):
        return [minimised(v) for v in value]
    return value


def release_order(label: Any) -> tuple[int, ...]:
    """Order of a release label within one source: its integers in sequence (``2099_02`` -> (2099, 2),
    ``CHEMBL_100`` -> (100,), ``2099-01-15`` -> (2099, 1, 15))."""
    return tuple(int(n) for n in re.findall(r"\d+", str(label or "")))


def detect_reference(value: str) -> list[tuple[str, str]]:
    """Candidate (source, native id) pairs for a bare identifier; ``source:native`` is taken as given."""
    text = str(value or "").strip()
    head, sep, tail = text.partition(":")
    if sep and head in SOURCES and tail:
        return [(head, tail.strip())]
    upper = text.upper()
    if UNIPROT_ACCESSION.fullmatch(upper):
        return [("uniprot", upper)]
    if PDB_ID.fullmatch(upper):
        return [("rcsb-pdb", upper)]
    if CHEMBL_ID.fullmatch(upper):
        return [("chembl", upper)]
    if text.isdigit():
        return [("ncbi-gene", text), ("ncbi-taxonomy", text)]
    return []


def native_key(source: str, native_id: Any) -> str:
    text = str(native_id).strip()
    return text if source in {"ncbi-gene", "ncbi-taxonomy"} else text.upper()


def record_id_for(namespace: str, source: str, record_type: str, native_id: Any) -> str:
    return "lifesci-record:" + digest([namespace, source, record_type, native_key(source, native_id)])[:24]


@lru_cache(maxsize=1)
def _validator():
    import jsonschema

    return jsonschema.Draft7Validator(json.loads(SCHEMA_PATH.read_text()))


def validate_statement(statement: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a statement against the contract and the minimisation and exclusion rules (enforced at write)."""
    value = json.loads(canonical(statement))
    personal = personal_fields(value)
    if personal:
        raise LifeSciError("personal_data", f"personal fields are never stored (LS01): {', '.join(personal[:5])}")
    inferred = keys_in(value, INFERENCE_KEYS)
    if inferred:
        raise LifeSciError("excluded_field", f"inferred, predicted or converted values are never stored: "
                                             f"{', '.join(inferred[:5])}")
    errors = sorted(_validator().iter_errors(value), key=lambda e: list(e.path))
    if errors:
        first = errors[0]
        raise LifeSciError("invalid_record", f"{'/'.join(map(str, first.path)) or 'record'}: {first.message}")
    if value["record_type"] not in SOURCE_TYPES[value["source"]]:
        raise LifeSciError("invalid_record", f"{value['source']} publishes {SOURCE_TYPES[value['source']]} records")
    if value["status"] in {"merged", "replaced"} and not value.get("successors"):
        raise LifeSciError("invalid_record", "a merged or replaced record names its successor as published")
    version = value["version"]
    if version["basis"] == "entry-version" and value["source"] != "uniprot":
        raise LifeSciError("invalid_record", "entry versions are UniProt markers")
    if version["basis"] == "pdb-revision" and value["source"] != "rcsb-pdb":
        raise LifeSciError("invalid_record", "major.minor revisions are PDB markers")
    attributes = value["attributes"]
    if value["record_type"] == "protein" and value["status"] == "active":
        for field in ("entry_version", "sequence_version", "reviewed"):
            if attributes.get(field) is None:
                raise LifeSciError("invalid_record", f"an active protein entry keeps its {field} as published")
    if value["record_type"] == "taxon" and value["status"] == "active":
        for field in ("tax_id", "rank", "scientific_name"):
            if attributes.get(field) in (None, ""):
                raise LifeSciError("invalid_record", f"a taxon keeps its {field} as published")
    if value["record_type"] == "activity":
        for field in ("assay_chembl_id", "target_chembl_id", "molecule_chembl_id", "document_chembl_id"):
            if not attributes.get(field):
                raise LifeSciError("invalid_record", f"an activity keeps {field} as published")
        if not any(c["kind"] == "chembl-document" for c in value["citations"]):
            raise LifeSciError("invalid_record", "an activity cites the ChEMBL document it was published in")
    if value["record_type"] == "structure" and value["status"] == "active" \
            and not attributes.get("revision_history"):
        raise LifeSciError("invalid_record", "a PDB entry keeps its revision history as published")
    return value


__all__ = [
    "ANSWER_CONTRACT",
    "BUNDLE_CONTRACT",
    "CONTRACT",
    "EXCLUSIONS",
    "FEATURES",
    "INFERENCE_KEYS",
    "LICENCES",
    "LINK_CONTRACT",
    "MATCH_CONTRACT",
    "NEVER_SENTENCE",
    "PERSONAL_KEYS",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "SOURCES",
    "STATUSES",
    "WRITE_SCOPE",
    "XREF_TARGETS",
    "LifeSciError",
    "authorize",
    "detect_reference",
    "minimised",
    "personal_fields",
    "record_id_for",
    "release_order",
    "validate_statement",
]
