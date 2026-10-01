"""Life-science reference records for the Science pack: proteins, genes, taxa, structures and bioactivity.

``noesis-lifesci-record-v1`` (tracker #2652, LS02 #2661) is the record model of
the ``science.life-sciences`` provider. Every record is a *statement* as one
reference database published it, carrying its source, record revision and
as-of time (retrieval time, plus the provider's release where it publishes
one):

* **protein** - one UniProtKB entry keyed by accession: reviewed/unreviewed
  label as UniProt states it, entry and sequence versions, the UniProt release,
  the sequence as stored (never analysed), cross-references and literature as
  published; an inactive entry keeps UniProt's reason (merged, demerged,
  deleted) and the successor accessions it names;
* **entry_version** - one row of the UniSave entry-version history (entry and
  sequence version, first and last release), so "which version was in force
  at release R" has an answer for versions never acquired in full;
* **gene** - one NCBI Gene record keyed by Gene ID with status (live,
  discontinued, replaced) and the replacing Gene ID when NCBI names one;
* **taxon** - one NCBI Taxonomy node keyed by Tax ID with rank and lineage as
  published; a merged node keeps the Tax ID it was merged into;
* **structure** - one experimental RCSB PDB entry keyed by PDB ID: methods and
  resolution as published, the revision history, obsolete/superseding entries
  and each polymer entity's UniProt mapping as the PDB states it;
* **target**, **compound**, **activity**, **document** - ChEMBL records keyed
  by ChEMBL ID *and* release. Activity values keep the published and the
  ChEMBL-standardised type, relation, value and unit as strings exactly as
  published, with data-validity comments, and cite their document.

Records are immutable revisions (:mod:`src.kb.lifesci_store`); removals and
corrections by the source are revisions, never deletions. The LS01
data-minimisation decision (:data:`MINIMISATION`) is enforced here at write
time: no person names or contact details are stored (author lists, depositor
and submitter names are dropped by the adapters and rejected by
:func:`statement`). :data:`FORBIDDEN_KEYS` rejects predicted, inferred,
aggregated or converted values: nothing here infers biology, predicts
activity or analyses sequences.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

CONTRACT = "noesis-lifesci-record-v1"
IDENTITY_CONTRACT = "noesis-lifesci-identity-match-v1"
LINK_CONTRACT = "noesis-lifesci-link-v1"
ENTRY_ANSWER_CONTRACT = "noesis-lifesci-entry-as-of-v1"
ACTIVITY_ANSWER_CONTRACT = "noesis-lifesci-target-activities-v1"
NOTIFICATION_CONTRACT = "noesis-lifesci-notification-v1"
SOURCE_PACK = "primary-scientific-evidence"
BUNDLE = "science"
PROVIDER_ID = "science.life-sciences"
READ_SCOPE = "knowledge:lifesci:read"
WRITE_SCOPE = "knowledge:lifesci:write"
REVIEW_SCOPE = "knowledge:lifesci:review"
SCHEMA_VERSIONS = {"lifesci-record": "1.0.0", "lifesci-identity-match": "1.0.0", "lifesci-link": "1.0.0",
                   "lifesci-answer": "1.0.0"}
PROVIDERS = ("uniprot", "ncbi", "pdb", "chembl")
# One optional Science feature per provider (LS12): each can be selected alone.
FEATURES = {"uniprot": "life-sciences-uniprot", "ncbi": "life-sciences-ncbi", "pdb": "life-sciences-pdb",
            "chembl": "life-sciences-chembl"}
RECORD_TYPES = ("protein", "entry_version", "gene", "taxon", "structure", "target", "compound", "activity",
                "document")
PROVIDER_TYPES = {"uniprot": {"protein", "entry_version"}, "ncbi": {"gene", "taxon"}, "pdb": {"structure"},
                  "chembl": {"target", "compound", "activity", "document"}}
SUBJECT_PREFIX = {"protein": "uniprot", "entry_version": "uniprot-version", "gene": "ncbigene",
                  "taxon": "ncbitaxon", "structure": "pdb", "target": "chembl-target", "compound": "chembl-compound",
                  "activity": "chembl-activity", "document": "chembl-document"}
EVENTS = ("published", "obsoleted", "removed")
INACTIVE_REASONS = ("MERGED", "DEMERGED", "DELETED")
GENE_STATUSES = ("live", "discontinued", "replaced")
TAXON_STATUSES = ("active", "merged", "deleted")
STRUCTURE_STATUSES = ("released", "obsolete")
ACCESSION = re.compile(r"^(?:[OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9](?:[A-Z][A-Z0-9]{2}[0-9]){1,2})$")
PDB_ID = re.compile(r"^[0-9][A-Z0-9]{3}$")
CHEMBL_ID = re.compile(r"^CHEMBL[0-9]{1,12}$")
INCHIKEY = re.compile(r"^[A-Z]{14}-[A-Z]{10}-[A-Z]$")
NUMERIC_ID = re.compile(r"^[0-9]{1,12}$")

# LS01 data-minimisation decision, enforced at write time and in every answer.
PERSONAL_KEYS = frozenset({
    "author", "authors", "author_list", "rcsb_authors", "authorlist", "audit_author", "citation_author", "submitter",
    "submission_names", "submitters", "depositor", "depositors", "contact", "contact_author", "email", "orcid",
    "person", "persons", "curator", "curators",
})
MINIMISATION = {
    "decision": "no personal data stored",
    "personal_data_in_sources": [
        "UniProt reference author lists and submission names",
        "RCSB PDB audit authors, citation authors and depositor contact fields",
        "ChEMBL document author lists",
        "NCBI Gene and Taxonomy: none in the selected fields (nomenclature authority names are organisations)",
    ],
    "stored": "literature is cited by DOI, PubMed ID, title, journal and year only; no author, depositor, "
              "submitter, curator, contact or e-mail field is stored",
    "redacted": "none - person fields are dropped at acquisition, not masked",
    "excluded": sorted(PERSONAL_KEYS),
    "enforcement": "adapters drop the fields and report them in the page receipt (personal_fields_dropped); "
                   "statement() rejects any person field with code personal_field; answers never carry them",
    "retention": "records are retained as immutable revisions of reference data; since no personal data is "
                 "stored, no personal-data retention period applies",
    "access": "every principal with knowledge:lifesci:read and namespace access; nothing personal to restrict",
}
# Predicted, inferred, aggregated, converted or analysed values: never accepted.
FORBIDDEN_KEYS = frozenset({
    "prediction", "predicted", "predicted_activity", "predicted_structure", "predicted_function",
    "inferred_function", "inferred_target", "inferred_interaction", "druggability", "efficacy", "toxicity_prediction",
    "activity_score", "binding_score", "docking_score", "similarity", "sequence_similarity", "alignment",
    "blast_hits", "aggregated_value", "mean_value", "median_value", "converted_value", "pchembl_value",
    "isoelectric_point", "hydrophobicity", "noesis_assessment", "disease_association", "clinical_relevance",
})
NEVER = (
    "infer biological function, interactions or disease relevance",
    "predict activity, binding or toxicity",
    "analyse, align or compare sequences beyond storing them as published",
    "convert, normalise or aggregate activity values across assays or assay types",
    "merge records across sources without a reviewed identity decision",
    "redistribute beyond each source's licence",
    "store person names or contact details",
)
NEVER_SENTENCE = ("Reference records as UniProt, NCBI, RCSB PDB and ChEMBL published them: no biological or clinical "
                  "inference, no activity prediction, no sequence analysis beyond storage, activity values never "
                  "converted or aggregated, and no person names stored.")

_FIELDS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    # record type: (required non-null, optional - null goes to unknowns)
    "protein": (("accession", "entry_type", "entry_status"),
                ("reviewed", "entry_name", "entry_version", "sequence_version", "release", "protein_name",
                 "gene_names", "organism", "sequence", "secondary_accessions", "cross_references", "citations",
                 "first_public", "last_annotation_update", "last_sequence_update", "inactive_reason")),
    "entry_version": (("accession", "entry_version", "sequence_version", "database"),
                      ("first_release", "first_release_date", "last_release", "last_release_date", "entry_name")),
    "gene": (("gene_id", "symbol", "tax_id", "status"),
             ("description", "organism_name", "gene_type", "chromosomes", "replaced_by", "cross_references")),
    "taxon": (("tax_id", "scientific_name", "rank", "status"),
              ("authority", "lineage", "merged_into", "genetic_code")),
    "structure": (("pdb_id", "status"),
                  ("title", "experimental_methods", "resolution_angstrom", "deposit_date", "initial_release_date",
                   "revision", "revision_history", "entities", "primary_citation", "superseded_by", "supersedes",
                   "obsoleted_on")),
    "target": (("target_chembl_id", "release", "pref_name", "target_type"),
               ("organism", "tax_id", "components", "cross_references")),
    "compound": (("molecule_chembl_id", "release"),
                 ("pref_name", "molecule_type", "standard_inchikey", "standard_inchi", "canonical_smiles",
                  "cross_references")),
    "activity": (("activity_id", "release", "assay_chembl_id", "target_chembl_id", "molecule_chembl_id",
                  "document_chembl_id"),
                 ("assay_type", "assay_description", "published", "standard", "data_validity_comment",
                  "data_validity_description", "activity_comment", "document_citation")),
    "document": (("document_chembl_id", "release"),
                 ("doi", "pubmed_id", "title", "journal", "year", "doc_type", "volume", "first_page")),
}
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?)?)?$")
_ACTIVITY_PARTS = {"type", "relation", "value", "units", "flag"}


class LifeSciError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code, self.message, self.details = code, message, details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    """Life-sciences scope plus namespace access (operator bypasses)."""
    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise LifeSciError("unauthorized", f"{required} and namespace access are required")


def require(scopes: Iterable[str], *required: str) -> None:
    scopes = set(scopes or ())
    missing = [s for s in required if s not in scopes and "operator" not in scopes]
    if missing:
        raise LifeSciError("unauthorized", f"{missing[0]} scope is required")


def _fail(message: str, code: str = "invalid_lifesci_record") -> None:
    raise LifeSciError(code, message)


# ------------------------------------------------------------------ keys and releases


def release_order(label: Any) -> tuple[int, ...]:
    """Sort key of a published release label (UniProt ``2026_03``, ChEMBL ``ChEMBL_35``, PDB ``1.2``)."""
    return tuple(int(n) for n in re.findall(r"\d+", str(label or "")))


def subject_key(record_type: str, record_key: str) -> str:
    return f"{SUBJECT_PREFIX[record_type]}:{record_key}"


def forbidden_keys(value: Any, path: str = "") -> list[str]:
    """Paths of predicted/derived or personal fields anywhere in a value (for answers and tests)."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            folded = str(key).casefold()
            if folded in FORBIDDEN_KEYS or folded in PERSONAL_KEYS:
                found.append(f"{path}{key}")
            found += forbidden_keys(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            found += forbidden_keys(item, path)
    return found


def _screen(value: Any, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            folded = str(key).casefold()
            if folded in PERSONAL_KEYS:
                _fail(f"{path}{key} is personal data and is never stored (LS01 minimisation decision)",
                      "personal_field")
            if folded in FORBIDDEN_KEYS:
                _fail(f"{path}{key} is a predicted, inferred, aggregated or converted value and is never stored",
                      "forbidden_field")
            _screen(item, f"{path}{key}.")
    elif isinstance(value, list):
        for item in value:
            _screen(item, path)


# ------------------------------------------------------------------ validation


def _date(value: Any, field: str) -> None:
    if value is not None and (not isinstance(value, str) or not _DATE.fullmatch(value)):
        _fail(f"{field} must be an ISO-8601 date or instant as published")


def _refs(items: Any, field: str) -> None:
    for ref in items or []:
        if not isinstance(ref, Mapping) or not ref.get("database") or not ref.get("id"):
            _fail(f"{field} state a database and an id as published")


def _citations(items: Any, field: str) -> None:
    for item in items or []:
        if not isinstance(item, Mapping) or set(item) - {"doi", "pubmed_id", "title", "journal", "year",
                                                          "reference_number"}:
            _fail(f"{field} keep doi, pubmed_id, title, journal and year only (no author lists)")


def _published_value(part: Any, field: str) -> None:
    if part is None:
        return
    if not isinstance(part, Mapping) or set(part) - _ACTIVITY_PARTS:
        _fail(f"{field} uses type, relation, value, units and flag")
    for key in ("type", "relation", "value", "units"):
        if part.get(key) is not None and not isinstance(part[key], str):
            _fail(f"{field}.{key} is kept as the published text, never converted to a number", "converted_value")


def _typed(record_type: str, published: Mapping[str, Any]) -> None:
    if record_type == "protein":
        if not ACCESSION.fullmatch(published["accession"]):
            _fail("accession is a UniProtKB accession")
        if published["entry_status"] not in {"active", "obsolete"}:
            _fail("entry_status is active or obsolete")
        if published["entry_status"] == "active":
            for field in ("entry_version", "sequence_version"):
                if type(published.get(field)) is not int or published[field] < 1:
                    _fail(f"an active entry keeps its {field} as published")
            if type(published.get("reviewed")) is not bool:
                _fail("reviewed is UniProt's own reviewed/unreviewed label")
            sequence = published.get("sequence")
            if sequence is not None and (not isinstance(sequence, Mapping) or not sequence.get("value")
                                         or set(sequence) - {"value", "length", "mol_weight", "crc64", "md5"}):
                _fail("sequence is stored as published (value, length, mol_weight, crc64, md5) and never analysed")
        else:
            reason = published.get("inactive_reason")
            if not isinstance(reason, Mapping) or reason.get("type") not in INACTIVE_REASONS or \
                    not isinstance(reason.get("successors"), list):
                _fail("an obsolete entry keeps UniProt's inactive reason (MERGED, DEMERGED, DELETED) and successors")
            if reason["type"] != "DELETED" and not reason["successors"]:
                _fail("a merged or demerged entry names the accessions UniProt gives as successors")
        _refs(published.get("cross_references"), "cross_references")
        _citations(published.get("citations"), "citations")
        for field in ("first_public", "last_annotation_update", "last_sequence_update"):
            _date(published.get(field), field)
    elif record_type == "entry_version":
        if not ACCESSION.fullmatch(published["accession"]):
            _fail("accession is a UniProtKB accession")
        for field in ("entry_version", "sequence_version"):
            if type(published[field]) is not int or published[field] < 1:
                _fail(f"{field} is a positive integer as published")
        for field in ("first_release_date", "last_release_date"):
            _date(published.get(field), field)
    elif record_type == "gene":
        if not NUMERIC_ID.fullmatch(published["gene_id"]) or not NUMERIC_ID.fullmatch(published["tax_id"]):
            _fail("gene_id and tax_id are NCBI numeric ids")
        if published["status"] not in GENE_STATUSES:
            _fail("status is live, discontinued or replaced as NCBI states it")
        if published["status"] == "replaced" and not published.get("replaced_by"):
            _fail("a replaced gene keeps the Gene ID NCBI names as current")
        _refs(published.get("cross_references"), "cross_references")
    elif record_type == "taxon":
        if not NUMERIC_ID.fullmatch(published["tax_id"]):
            _fail("tax_id is an NCBI numeric id")
        if published["status"] not in TAXON_STATUSES:
            _fail("status is active, merged or deleted")
        if published["status"] == "merged" and not published.get("merged_into"):
            _fail("a merged taxon keeps the Tax ID it was merged into")
        for node in published.get("lineage") or []:
            if not isinstance(node, Mapping) or not node.get("tax_id"):
                _fail("lineage nodes keep the published tax_id (and name and rank when published)")
    elif record_type == "structure":
        if not PDB_ID.fullmatch(published["pdb_id"]):
            _fail("pdb_id is a four-character PDB ID (computed structure models are excluded)")
        if published["status"] not in STRUCTURE_STATUSES:
            _fail("status is released or obsolete")
        if published["status"] == "obsolete":
            _date(published.get("obsoleted_on"), "obsoleted_on")
        for field in ("deposit_date", "initial_release_date"):
            _date(published.get(field), field)
        for item in published.get("revision_history") or []:
            if not isinstance(item, Mapping) or type(item.get("major")) is not int or type(item.get("minor")) \
                    is not int:
                _fail("revision history keeps each published major and minor revision")
        for entity in published.get("entities") or []:
            if not isinstance(entity, Mapping) or not entity.get("entity_id") or \
                    not isinstance(entity.get("uniprot_accessions"), list):
                _fail("entities keep the entity id and the UniProt accessions the PDB states (possibly none)")
        _citations([published["primary_citation"]] if published.get("primary_citation") else [],
                   "primary_citation")
    elif record_type in {"target", "compound", "activity", "document"}:
        key_field = {"target": "target_chembl_id", "compound": "molecule_chembl_id", "activity": "assay_chembl_id",
                     "document": "document_chembl_id"}[record_type]
        if not CHEMBL_ID.fullmatch(published[key_field]):
            _fail(f"{key_field} is a ChEMBL ID")
        if not str(published["release"]).startswith("ChEMBL_"):
            _fail("release is the ChEMBL release label as published (ChEMBL_nn)")
        if record_type == "target":
            for component in published.get("components") or []:
                if not isinstance(component, Mapping) or "accession" not in component:
                    _fail("target components keep the published accession (possibly null)")
        if record_type == "compound" and published.get("standard_inchikey") is not None and \
                not INCHIKEY.fullmatch(published["standard_inchikey"]):
            _fail("standard_inchikey is a standard InChIKey as published")
        if record_type == "activity":
            _published_value(published.get("published"), "published")
            _published_value(published.get("standard"), "standard")
            for field in ("target_chembl_id", "molecule_chembl_id", "document_chembl_id"):
                if not CHEMBL_ID.fullmatch(published[field]):
                    _fail(f"{field} is a ChEMBL ID")
        if record_type in {"target", "compound"}:
            _refs(published.get("cross_references"), "cross_references")


def statement(record_type: str, provider: str, record_key: str, *, subject_name: str | None,
              as_published: Mapping[str, Any], source: Mapping[str, Any], event: str = "published",
              effective_date: str | None = None, date_basis: str | None = None,
              unknowns: Iterable[str] = ()) -> dict[str, Any]:
    """Build and validate one provider statement."""
    if record_type not in RECORD_TYPES:
        _fail("unknown life-sciences record type")
    if provider not in PROVIDERS or record_type not in PROVIDER_TYPES[provider]:
        _fail(f"{provider} does not publish {record_type} records")
    if not isinstance(record_key, str) or not record_key.strip() or len(record_key) > 200:
        _fail("record_key must be nonempty text")
    if event not in EVENTS:
        _fail("event is published, obsoleted (by the source) or removed (absent from a complete release page)")
    required, optional = _FIELDS[record_type]
    published = dict(as_published)
    extra = set(published) - set(required) - set(optional)
    if extra:
        _fail(f"unsupported {record_type} field {min(extra)}")
    _screen(published)
    _screen(dict(source or {}))
    for field in required:
        if published.get(field) is None:
            _fail(f"{record_type}.{field} is required as published")
    _typed(record_type, published)
    if not isinstance(source, Mapping) or not str(source.get("url") or "").startswith("https://"):
        _fail("source.url must be the provider's https URL")
    _date(effective_date, "effective.date")
    missing = {f for f in optional if published.get(f) is None}
    return {
        "contract": CONTRACT, "record_type": record_type, "provider": provider, "record_key": record_key,
        "subject": {"key": subject_key(record_type, record_key), "kind": record_type, "name": subject_name},
        "as_published": {**{f: None for f in optional}, **published},
        "effective": {"event": event, "date": effective_date, "date_basis": date_basis},
        "source": dict(source), "unknowns": sorted(missing | set(unknowns)),
    }


def validate_statement(value: Mapping[str, Any]) -> dict[str, Any]:
    """Re-validate a stored or received statement by rebuilding it (round trip)."""
    if not isinstance(value, Mapping) or value.get("contract") != CONTRACT:
        _fail("not a noesis-lifesci-record-v1 statement")
    required, _ = _FIELDS.get(value.get("record_type"), ((), ()))
    published = {k: v for k, v in dict(value.get("as_published") or {}).items() if k in required or v is not None}
    effective = dict(value.get("effective") or {})
    return statement(value.get("record_type"), value.get("provider"), value.get("record_key"),
                     subject_name=dict(value.get("subject") or {}).get("name"), as_published=published,
                     source=dict(value.get("source") or {}), event=effective.get("event", "published"),
                     effective_date=effective.get("date"), date_basis=effective.get("date_basis"),
                     unknowns=value.get("unknowns") or ())


def release_of(value: Mapping[str, Any]) -> str | None:
    """The release label a statement belongs to (UniProt, ChEMBL, PDB revision), or None (NCBI)."""
    published = value["as_published"]
    if value["provider"] in {"uniprot", "chembl"}:
        return published.get("release") or value["source"].get("release")
    if value["provider"] == "pdb" and published.get("revision"):
        revision = published["revision"]
        return f"{revision.get('major')}.{revision.get('minor')}"
    return None


# ------------------------------------------------------------------ composition


def selected_features(conn: Any) -> list[str]:
    """The Science bundle features selected in the active composition plan ([] when unmanaged or unreadable)."""
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN ('composition_authority', "
            "'composition_active', 'composition_generations', 'composition_plans')").fetchall()}
        if len(tables) < 4:
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON g.generation_id="
            "a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1").fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get(BUNDLE) or [])


def feature_state(conn: Any) -> dict[str, bool]:
    """Which per-provider life-sciences features are selected (every one default off)."""
    selected = set(selected_features(conn))
    return {provider: feature in selected for provider, feature in FEATURES.items()}


# ------------------------------------------------------------------ schema registry

SCHEMA_FILES = {"noesis-lifesci-record": "contracts/schemas/jsonschema/noesis-lifesci-record-v1.json"}


def schema_definitions(root: Any = None) -> dict[str, Any]:
    root = Path(root) if root else Path(__file__).resolve().parents[2]
    return {name: json.loads((root / path).read_text()) for name, path in SCHEMA_FILES.items()}


def register_schemas(conn: Any, *, principal_id: str, scopes: Iterable[str], root: Any = None) -> list[dict]:
    """Register the life-sciences record schema in the existing schema registry (idempotent per version)."""
    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": "1.0.0",
            "content": content, "owner": PROVIDER_ID, "dependencies": [], "compatibility_policy": "backward",
            "provenance": {"kind": "imported", "source": "packs/science (life-sciences features)"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"lifesci-schema:{name}:1.0.0:{digest(content)[:16]}",
                                         principal_id=principal_id, scopes=set(scopes)))
    return results


__all__ = [
    "ACCESSION", "CHEMBL_ID", "CONTRACT", "FEATURES", "FORBIDDEN_KEYS", "INCHIKEY", "MINIMISATION", "NEVER",
    "NEVER_SENTENCE", "PDB_ID", "PERSONAL_KEYS", "PROVIDERS", "READ_SCOPE", "RECORD_TYPES", "REVIEW_SCOPE",
    "SOURCE_PACK", "WRITE_SCOPE", "LifeSciError", "authorize", "canonical", "digest", "feature_state",
    "forbidden_keys", "register_schemas", "release_of", "release_order", "require", "selected_features",
    "statement", "subject_key", "validate_statement",
]
