"""Life-science reference sources for the Science ``science.life-sciences`` provider (#2652, LS01/LS03-LS06).

Five providers under an access contract (:data:`PROVIDER_CONTRACTS`, LS01), implemented as the ``life-sciences``
source-pack connector. Each declared document is one bounded request set and one runtime page:

* **UniProtKB** (``uniprot``) - ``GET https://rest.uniprot.org/uniprotkb/{accession}.json`` for a declared accession
  list (a bounded proteome slice or query result pinned by accession). Entry and sequence versions, the
  reviewed/unreviewed label as UniProt publishes it (``entryType``), secondary accessions, cross-references and
  citation identifiers are kept; inactive entries (``MERGED``, ``DEMERGED``, ``DELETED``) are revisions with their
  successors. The release comes from the ``X-UniProt-Release`` / ``X-UniProt-Release-Date`` headers.
* **NCBI Gene** (``ncbi-gene``) - E-utilities ``esummary.fcgi?db=gene&retmode=json`` for declared Gene IDs; the
  summary ``status`` (live, secondary with ``currentid``, discontinued) is kept as published with the successor.
* **NCBI Taxonomy** (``ncbi-taxonomy``) - E-utilities ``efetch.fcgi?db=taxonomy&retmode=xml`` for declared Tax IDs:
  rank, parent, lineage (``Lineage`` and ``LineageEx``) as published per release; a requested Tax ID that NCBI answers
  under another Tax ID's ``AkaTaxIds`` is recorded as merged into it.
* **RCSB PDB** (``rcsb-pdb``) - Data API ``/rest/v1/core/entry/{id}`` and ``/rest/v1/core/polymer_entity/{id}/{n}``
  for declared entries, keeping experimental method, resolution, the ``pdbx_audit_revision_history`` and the
  entity-to-UniProt mappings as the PDB states them; ``/rest/v1/holdings/removed/{id}`` records obsolete entries
  with their superseding entries.
* **ChEMBL** (``chembl``) - the ChEMBL web services (``status``, ``target``, ``molecule``, ``activity``,
  ``document``) for declared targets: the release (``chembl_db_version``) is checked against the declaration; each
  activity keeps its published and standardised type, relation, value and unit and the data-validity comment, and
  cites its source document.

NCBI accepts an optional API key (``NOESIS_NCBI_API_KEY``) that raises its rate limit; it is sent only as the
``api_key`` parameter, never recorded in a URL, receipt or record. No other provider needs a credential.

Personal data (citation author lists, PDB depositors, ChEMBL document authors) is dropped before a statement is
built and refused by :func:`src.kb.lifesci_records.validate_statement`. Every provider is ``unverified-live`` until a
dated live run (LS14, #2721). See ``docs/development/life-sciences-evidence/source-audit.md``.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.lifesci_records import (
    CHEMBL_ID,
    CONTRACT,
    EXCLUSIONS,
    LICENCES,
    NEVER_SENTENCE,
    PDB_ID,
    UNIPROT_ACCESSION,
    LifeSciError,
    canonical,
    validate_statement,
)

CONNECTOR = "life-sciences"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PAGE_CONTRACT = "noesis-lifesci-page-v1"
PROVIDERS = ("uniprot", "ncbi-gene", "ncbi-taxonomy", "rcsb-pdb", "chembl")
PROVIDER_HOSTS = {
    "uniprot": {"rest.uniprot.org"},
    "ncbi-gene": {"eutils.ncbi.nlm.nih.gov"},
    "ncbi-taxonomy": {"eutils.ncbi.nlm.nih.gov"},
    "rcsb-pdb": {"data.rcsb.org"},
    "chembl": {"www.ebi.ac.uk"},
}
MAX_IDS_PER_DOCUMENT = 20
MAX_ACTIVITIES = 200

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "uniprot": {
        "delivers": "UniProtKB protein entries keyed by accession: entry and sequence versions, reviewed/unreviewed "
        "label, secondary accessions, sequence as published, cross-references and citation identifiers",
        "access_decision": "unverified-live",
        "reason": "public REST API without authentication; entry JSON field names and release headers not yet "
        "checked live from this runtime",
        "access": "api (UniProt REST, JSON)",
        "entry_points": ["https://rest.uniprot.org/uniprotkb/{accession}.json (verify)"],
        "authentication": "none",
        "rate_limits": "no published hard limit; UniProt asks for fair use and batching (verify); one request per "
        "declared accession, at most 20 accessions per document and max_pages documents per run",
        "identifiers": {"protein": "UniProtKB accession (primary; secondary accessions kept as published)",
                        "organism": "NCBI Taxonomy ID"},
        "licence": "Creative Commons Attribution 4.0 (CC BY 4.0) for UniProt data (verify)",
        "attribution": LICENCES["uniprot"]["attribution"],
        "update_cadence": "releases about every eight weeks (YYYY_NN)",
        "revision_model": "entryAudit.entryVersion and sequenceVersion are the source's own version markers; a new "
        "entry version is a revision; inactive entries (MERGED, DEMERGED, DELETED) are revisions naming their "
        "successors (inactiveReason.mergeDemergeTo)",
        "temporal_semantics": "X-UniProt-Release and X-UniProt-Release-Date response headers date the release; "
        "entryAudit dates (lastAnnotationUpdateDate, lastSequenceUpdateDate) are kept as published",
        "corrections_and_removals": "a corrected entry is a new entry version; a removal is an inactive entry "
        "(DELETED) and a merge names the surviving accession - both stored as revisions, never deletions",
        "personal_data": "reference author lists and submission names are dropped; citations kept by PubMed ID and "
        "DOI with title",
        "retained_evidence": "response digest per accession, release headers",
        "verify": ["entry JSON field names", "inactive-entry response shape", "release header names and date format",
                   "licence text"],
    },
    "ncbi-gene": {
        "delivers": "NCBI Gene records keyed by Gene ID: symbol, description, organism Tax ID, chromosome and map "
        "location, and the live/secondary/discontinued status with the current ID",
        "access_decision": "unverified-live",
        "reason": "public E-utilities without authentication (an optional API key raises the limit); esummary JSON "
        "shape not yet checked live",
        "access": "api (NCBI E-utilities esummary, JSON)",
        "entry_points": [("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=gene&id={ids}"
                          "&retmode=json (verify)")],
        "authentication": "none; optional API key NOESIS_NCBI_API_KEY sent as the api_key parameter only",
        "rate_limits": "3 requests per second without an API key, 10 with one (NCBI E-utilities policy; verify); "
        "one request per document of at most 20 Gene IDs",
        "identifiers": {"gene": "NCBI Gene ID", "organism": "NCBI Taxonomy ID"},
        "licence": "NCBI molecular data is not subject to copyright restrictions (US government work); some "
        "submitted data may carry third-party rights (NCBI policy; verify)",
        "attribution": LICENCES["ncbi-gene"]["attribution"],
        "update_cadence": "daily updates; no numbered release (the release is declared by the operator)",
        "revision_model": "no version marker is published in the summary: a changed summary is a new revision keyed "
        "by its content digest; status 1 (secondary) names currentid, status 2 is discontinued",
        "temporal_semantics": "the declared release date, else the retrieval date (labelled)",
        "corrections_and_removals": "a replaced Gene ID keeps its record as a 'replaced' revision naming currentid; "
        "a discontinued Gene ID is a 'discontinued' revision",
        "personal_data": "none in the summary fields stored",
        "retained_evidence": "response digest per request",
        "verify": ["esummary field names (status, currentid)", "API key parameter", "rate limits"],
    },
    "ncbi-taxonomy": {
        "delivers": "NCBI Taxonomy records keyed by Tax ID: scientific name, rank, parent, division and lineage "
        "(Lineage and LineageEx) as published",
        "access_decision": "unverified-live",
        "reason": "public E-utilities efetch (XML) without authentication; element names not yet checked live",
        "access": "api (NCBI E-utilities efetch, XML)",
        "entry_points": [("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=taxonomy&id={ids}"
                          "&retmode=xml (verify)")],
        "authentication": "none; optional API key NOESIS_NCBI_API_KEY sent as the api_key parameter only",
        "rate_limits": "3 requests per second without an API key, 10 with one (verify); one request per document of "
        "at most 20 Tax IDs",
        "identifiers": {"taxon": "NCBI Taxonomy ID (merged IDs in AkaTaxIds)"},
        "licence": "NCBI Taxonomy is in the public domain (NCBI policy; verify)",
        "attribution": LICENCES["ncbi-taxonomy"]["attribution"],
        "update_cadence": "daily updates; no numbered release (the release is declared by the operator)",
        "revision_model": "a changed record (name, rank, lineage) is a new revision keyed by its content digest; a "
        "requested Tax ID answered under another taxon's AkaTaxIds is a 'merged' revision naming the survivor",
        "temporal_semantics": "the declared release date, else the retrieval date; UpdateDate kept as published",
        "corrections_and_removals": "merges are revisions; a Tax ID NCBI does not return is reported in the receipt, "
        "never recorded as a deletion",
        "personal_data": "none stored (OtherNames and citations are not selected)",
        "retained_evidence": "response digest per request",
        "verify": ["XML element names", "AkaTaxIds behaviour for merged IDs"],
    },
    "rcsb-pdb": {
        "delivers": "PDB entries keyed by PDB ID: title, experimental method, resolution, revision history, "
        "entity-to-UniProt mappings and the primary citation identifiers; obsolete entries with superseding IDs",
        "access_decision": "unverified-live",
        "reason": "public RCSB Data API without authentication; field names not yet checked live",
        "access": "api (RCSB PDB Data API, JSON)",
        "entry_points": ["https://data.rcsb.org/rest/v1/core/entry/{id} (verify)",
                         "https://data.rcsb.org/rest/v1/core/polymer_entity/{id}/{entity_id} (verify)",
                         "https://data.rcsb.org/rest/v1/holdings/removed/{id} (verify)"],
        "authentication": "none",
        "rate_limits": "no published hard limit; RCSB asks for reasonable use (verify); one entry request plus one "
        "per declared polymer entity",
        "identifiers": {"structure": "PDB ID (four characters)", "entity": "polymer entity id",
                        "protein": "UniProt accession as the PDB states it"},
        "licence": "PDB data are free of all copyright restrictions (CC0 1.0; wwPDB usage policy; verify)",
        "attribution": LICENCES["rcsb-pdb"]["attribution"],
        "update_cadence": "weekly release (Wednesday 00:00 UTC)",
        "revision_model": "rcsb_accession_info major_revision.minor_revision is the version marker; "
        "pdbx_audit_revision_history is kept in full; an obsolete entry is a revision naming id_codes_replaced_by",
        "temporal_semantics": "the declared weekly release; revision dates kept as published",
        "corrections_and_removals": "remediation and corrections are new major/minor revisions; obsoletion is a "
        "revision with successors, never a deletion",
        "personal_data": "audit_author and citation author lists are dropped",
        "retained_evidence": "response digests per request",
        "verify": ["field names", "holdings/removed response shape"],
    },
    "chembl": {
        "delivers": "ChEMBL targets, compounds, activities and source documents for declared targets, per ChEMBL "
        "release",
        "access_decision": "unverified-live",
        "reason": "public ChEMBL web services without authentication; resource shapes not yet checked live",
        "access": "api (ChEMBL web services, JSON)",
        "entry_points": ["https://www.ebi.ac.uk/chembl/api/data/status.json (verify)",
                         "https://www.ebi.ac.uk/chembl/api/data/target/{id}.json (verify)",
                         "https://www.ebi.ac.uk/chembl/api/data/molecule/{id}.json (verify)",
                         ("https://www.ebi.ac.uk/chembl/api/data/activity.json?target_chembl_id={id}&limit={n} "
                          "(verify)"),
                         "https://www.ebi.ac.uk/chembl/api/data/document/{id}.json (verify)"],
        "authentication": "none",
        "rate_limits": "no published hard limit; EMBL-EBI fair use (verify); at most 200 activities per target "
        "document (a larger total is refused, never truncated)",
        "identifiers": {"target": "ChEMBL target ID", "compound": "ChEMBL molecule ID (standard InChIKey kept)",
                        "activity": "ChEMBL activity ID", "document": "ChEMBL document ID (DOI and PubMed ID)"},
        "licence": "Creative Commons Attribution-ShareAlike 3.0 Unported (CC BY-SA 3.0) (verify)",
        "attribution": LICENCES["chembl"]["attribution"],
        "update_cadence": "numbered releases (CHEMBL_NN) a few times a year",
        "revision_model": "records are keyed by ChEMBL ID and release: an unchanged record keeps one revision seen "
        "in several releases; a changed record (value, validity comment) is a new revision",
        "temporal_semantics": "status.json chembl_db_version and chembl_release_date date the release",
        "corrections_and_removals": "corrections appear in a new release as changed records or data_validity_comment "
        "flags; records absent from a later release are not deleted",
        "personal_data": "document author lists are dropped; abstracts are not stored",
        "retained_evidence": "response digests per request, release check",
        "verify": ["resource field names", "status.json fields", "activity page_meta"],
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"],
               "note": "no dated live run from this runtime; offline fixtures only (LS14 #2721 records live evidence)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# The bounded first coverage (LS01). No record set implies complete coverage of any provider.
BOUNDED_COVERAGE = {
    "anchor": "a declared set of target proteins (reviewed UniProtKB entries of one organism, at most 20 per document) "
    "and what they cross-reference",
    "uniprot": {"entries": "declared accessions only (a pinned proteome slice or query result), <= 20 per document, "
                "<= max_pages documents", "releases": "the two most recent UniProt releases"},
    "ncbi-gene": {"entries": "Gene IDs cross-referenced by the declared proteins (GeneID), <= 20 per document"},
    "ncbi-taxonomy": {"entries": "the organisms of the declared proteins and their parents, <= 20 per document"},
    "rcsb-pdb": {"entries": "PDB entries cross-referenced by the declared proteins, <= 20 polymer entities each"},
    "chembl": {"entries": "targets whose components are the declared proteins; their activities (<= 200 per target), "
               "the compounds and source documents those activities name", "releases": "the two most recent ChEMBL "
               "releases"},
    "out_of_scope": "full proteomes, sequence similarity searches, assay descriptions beyond the published label, "
    "computed molecular properties, and any prediction",
}
PERSONAL_DATA_DECISION = {
    "stored": "accessions, versions, names of genes, proteins, taxa, targets and compounds, citation identifiers "
    "(PubMed ID, DOI, ChEMBL document ID) and titles",
    "excluded": "UniProt reference author lists and submission names; PDB audit_author, primary-citation authors "
    "and depositor names; ChEMBL document authors and abstracts",
    "enforcement": "dropped by the adapters before a statement is built; refused at write time by "
    "validate_statement (personal_data); stripped again from every MCP tool output",
    "retention": "nothing personal is retained, so no retention period applies; raw responses are not stored, only "
    "their digests",
    "access": "records carry no personal data; read access needs knowledge:lifesci:read and namespace access",
}

UNIPROT_REASONS = {"MERGED": "merged", "DEMERGED": "demerged", "DELETED": "deleted"}
GENE_STATUS = {"": "active", "0": "active", "1": "replaced", "2": "discontinued"}


class LifeSciFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def digest_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def unverified(provider: str) -> bool:
    return PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"


def text(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return raw or None


def decimal_text(value: Any) -> str | None:
    """A published number as exact decimal text (never rounded or converted); None when not numeric."""
    raw = text(value)
    if raw is None:
        return None
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    return raw if number.is_finite() else None


def iso_day(value: Any) -> str | None:
    raw = text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10].replace("/", "-")).isoformat()
    except ValueError:
        pass
    match = re.fullmatch(r"(\d{1,2})-([A-Za-z]+)-(\d{4})", raw)  # UniProt: 12-January-2099
    months = {name.casefold(): number for number, name in enumerate(calendar.month_name) if name}
    months |= {name.casefold(): number for number, name in enumerate(calendar.month_abbr) if name}
    if match and match.group(2).casefold() in months:
        try:
            return date(int(match.group(3)), months[match.group(2).casefold()], int(match.group(1))).isoformat()
        except ValueError:
            return None
    return None


# ------------------------------------------------------------------ declarations


def lifesci_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("life_sciences") or {})
    provider = declared.get("provider")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", "life-sciences sources declare a known provider")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider] or urlsplit(source["endpoint"]).scheme != "https":
        raise SourcePackError("invalid_manifest", "the endpoint is not the provider's documented HTTPS host")
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_manifest", "a life-sciences source declares its documents")
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared documents than the source's page budget")
    release = dict(declared.get("release") or {})
    if release and (not text(release.get("label")) or (release.get("published_on") is not None
                                                        and iso_day(release["published_on"]) is None)):
        raise SourcePackError("invalid_manifest", "a declared release states its label and ISO publication date")
    if provider == "chembl" and not release:
        raise SourcePackError("invalid_manifest", "a ChEMBL source declares the release it expects (CHEMBL_NN)")
    for document in documents:
        try:
            check_document(provider, document)
        except LifeSciFormatError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
    keys = [canonical(d) for d in documents]
    if len(set(keys)) != len(keys):
        raise SourcePackError("invalid_manifest", "each declared document is distinct")
    return declared


def _ids(document: Mapping[str, Any], key: str, pattern: re.Pattern[str] | None) -> list[str]:
    values = [str(v).strip() for v in document.get(key) or []]
    if not values or len(values) > MAX_IDS_PER_DOCUMENT or len(set(values)) != len(values):
        raise LifeSciFormatError("invalid_document", f"a document declares 1-{MAX_IDS_PER_DOCUMENT} distinct {key}")
    if pattern is not None and not all(pattern.fullmatch(v) for v in values):
        raise LifeSciFormatError("invalid_document", f"{key} are well-formed identifiers")
    return values


def check_document(provider: str, document: Mapping[str, Any]) -> None:
    if not text(document.get("label")):
        raise LifeSciFormatError("invalid_document", "a document has a label")
    kind = document.get("kind")
    if provider == "uniprot":
        _ids(document, "accessions", UNIPROT_ACCESSION)
    elif provider == "ncbi-gene":
        _ids(document, "gene_ids", re.compile(r"^\d{1,12}$"))
    elif provider == "ncbi-taxonomy":
        _ids(document, "tax_ids", re.compile(r"^\d{1,12}$"))
    elif provider == "rcsb-pdb":
        if kind == "removed":
            _ids(document, "pdb_ids", PDB_ID)
        elif kind == "entry":
            if not PDB_ID.fullmatch(str(document.get("pdb_id") or "")):
                raise LifeSciFormatError("invalid_document", "a PDB entry document names a four-character PDB ID")
            _ids(document, "entities", re.compile(r"^\d{1,4}$"))
        else:
            raise LifeSciFormatError("invalid_document", "a PDB document is an entry or removed-entries document")
    elif provider == "chembl":
        if kind == "target":
            _ids(document, "target_ids", CHEMBL_ID)
        elif kind == "molecules":
            _ids(document, "molecule_ids", CHEMBL_ID)
        elif kind == "documents":
            _ids(document, "document_ids", CHEMBL_ID)
        elif kind == "activities":
            if not CHEMBL_ID.fullmatch(str(document.get("target_id") or "")):
                raise LifeSciFormatError("invalid_document", "an activities document names one ChEMBL target")
            if not 1 <= int(document.get("limit") or 0) <= MAX_ACTIVITIES:
                raise LifeSciFormatError("invalid_document", f"an activities document caps at 1-{MAX_ACTIVITIES}")
        else:
            raise LifeSciFormatError("invalid_document", "a ChEMBL document is target, molecules, activities or "
                                                         "documents")


def requests_for(provider: str, document: Mapping[str, Any], endpoint: str) -> list[tuple[str, str, dict[str, str]]]:
    """(role, url, params) for every request a document makes, in order."""
    base = endpoint.rstrip("/")
    if provider == "uniprot":
        return [(acc, f"{base}/uniprotkb/{acc}.json", {}) for acc in document["accessions"]]
    if provider == "ncbi-gene":
        return [("summary", f"{base}/esummary.fcgi",
                 {"db": "gene", "id": ",".join(document["gene_ids"]), "retmode": "json"})]
    if provider == "ncbi-taxonomy":
        return [("taxa", f"{base}/efetch.fcgi",
                 {"db": "taxonomy", "id": ",".join(document["tax_ids"]), "retmode": "xml"})]
    if provider == "rcsb-pdb":
        if document["kind"] == "removed":
            return [(pdb, f"{base}/rest/v1/holdings/removed/{pdb}", {}) for pdb in document["pdb_ids"]]
        pdb = document["pdb_id"]
        return [("entry", f"{base}/rest/v1/core/entry/{pdb}", {})] + [
            (f"entity:{n}", f"{base}/rest/v1/core/polymer_entity/{pdb}/{n}", {}) for n in document["entities"]]
    kind = document["kind"]
    status = [("status", f"{base}/status.json", {})]
    if kind == "target":
        return status + [(t, f"{base}/target/{t}.json", {}) for t in document["target_ids"]]
    if kind == "molecules":
        return status + [(m, f"{base}/molecule/{m}.json", {}) for m in document["molecule_ids"]]
    if kind == "documents":
        return status + [(d, f"{base}/document/{d}.json", {}) for d in document["document_ids"]]
    return status + [("activities", f"{base}/activity.json",
                      {"target_chembl_id": document["target_id"], "limit": str(int(document["limit"])),
                       "offset": "0"})]


# ------------------------------------------------------------------ statement helpers


def _statement(provider: str, record_type: str, native_id: str, *, release: Mapping[str, Any], version: Mapping,
               status: str = "active", status_published: str | None = None, successors: Sequence[str] = (),
               label: str | None = None, attributes: Mapping[str, Any] | None = None,
               xrefs: Sequence[Mapping[str, Any]] = (), citations: Sequence[Mapping[str, Any]] = (),
               url: str | None = None, excluded: Sequence[str] = ()) -> dict[str, Any]:
    statement = {
        "contract": CONTRACT, "record_type": record_type, "source": provider, "native_id": str(native_id),
        "release": dict(release), "version": dict(version), "status": status, "status_published": status_published,
        "successors": [str(s) for s in successors], "label": label, "attributes": dict(attributes or {}),
        "xrefs": [dict(x) for x in xrefs], "citations": [dict(c) for c in citations], "url": url,
        "licence": dict(LICENCES[provider]), "excluded_fields": sorted(set(excluded)),
    }
    if statement["version"]["basis"] == "content":
        body = {k: v for k, v in statement.items() if k not in {"release", "version"}}
        statement["version"] = {"marker": "content-" + hashlib.sha256(canonical(body).encode()).hexdigest()[:16],
                                "basis": "content", "order": None, "date": statement["version"].get("date")}
    try:
        return validate_statement(statement)
    except LifeSciError as exc:
        raise LifeSciFormatError("schema_drift", f"{provider} {native_id}: {exc.message}") from exc


CONTENT = {"marker": "pending", "basis": "content", "order": None}


def _xref(database: str, identifier: Any, relation: str | None = None, **properties: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"database": database, "id": str(identifier)}
    if relation:
        item["relation"] = relation
    props = {k: v for k, v in properties.items() if v not in (None, "", [])}
    if props:
        item["properties"] = props
    return item


def _citations(pubmed: Any = None, doi: Any = None, title: Any = None) -> list[dict[str, Any]]:
    out = []
    if text(pubmed):
        out.append({"kind": "pubmed", "id": str(pubmed).strip(), "title": text(title)})
    if text(doi):
        out.append({"kind": "doi", "id": str(doi).strip().lower(), "title": text(title)})
    return out


def _json(raw: bytes, what: str) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LifeSciFormatError("schema_drift", f"{what} is not JSON") from exc


# ------------------------------------------------------------------ parsers


def uniprot_release(headers: Mapping[str, Any], declared: Mapping[str, Any], retrieved_day: str) -> dict[str, Any]:
    lowered = {str(k).casefold(): v for k, v in headers.items()}
    label = text(lowered.get("x-uniprot-release"))
    if label:
        return {"label": label, "published_on": iso_day(lowered.get("x-uniprot-release-date")), "basis": "provider"}
    return declared_release(declared, retrieved_day)


def declared_release(declared: Mapping[str, Any], retrieved_day: str) -> dict[str, Any]:
    if declared:
        return {"label": str(declared["label"]), "published_on": iso_day(declared.get("published_on")),
                "basis": "declared"}
    return {"label": f"retrieved-{retrieved_day}", "published_on": retrieved_day, "basis": "retrieval"}


def parse_uniprot(body: Mapping[str, Any], *, requested: str, release: Mapping[str, Any], url: str
                  ) -> list[dict[str, Any]]:
    accession = text(body.get("primaryAccession"))
    if accession is None:
        raise LifeSciFormatError("schema_drift", f"UniProt entry {requested} has no primaryAccession")
    entry_type = str(body.get("entryType") or "")
    if entry_type == "Inactive":
        reason = dict(body.get("inactiveReason") or {})
        kind = str(reason.get("inactiveReasonType") or "")
        if kind not in UNIPROT_REASONS:
            raise LifeSciFormatError("schema_drift", f"UniProt inactive reason {kind!r} is not recognised")
        audit = dict(body.get("entryAudit") or {})
        return [_statement(
            "uniprot", "protein", accession, release=release,
            version={**CONTENT, "date": iso_day(audit.get("lastAnnotationUpdateDate"))},
            status=UNIPROT_REASONS[kind], status_published=f"Inactive:{kind}",
            successors=[str(s) for s in reason.get("mergeDemergeTo") or []],
            label=None, attributes={"entry_type": entry_type, "inactive_reason": kind}, url=url)]
    if entry_type not in {"UniProtKB reviewed (Swiss-Prot)", "UniProtKB unreviewed (TrEMBL)"}:
        raise LifeSciFormatError("schema_drift", f"UniProt entry type {entry_type!r} is not recognised")
    audit = dict(body.get("entryAudit") or {})
    organism = dict(body.get("organism") or {})
    sequence = dict(body.get("sequence") or {})
    description = dict(dict(body.get("proteinDescription") or {}).get("recommendedName") or {})
    if not description:
        names = list(dict(body.get("proteinDescription") or {}).get("submissionNames") or [])
        description = dict(names[0]) if names else {}
    name = text(dict(description.get("fullName") or {}).get("value"))
    genes = [text(dict(g.get("geneName") or {}).get("value")) for g in body.get("genes") or []]
    xrefs = [_xref(str(x["database"]), x["id"], None,
                   **{str(p.get("key")): p.get("value") for p in x.get("properties") or [] if p.get("key")})
             for x in body.get("uniProtKBCrossReferences") or [] if x.get("database") and x.get("id")]
    if organism.get("taxonId") is not None:
        xrefs.append(_xref("NCBI Taxonomy", organism["taxonId"], "organism"))
    citations: list[dict[str, Any]] = []
    for reference in body.get("references") or []:
        citation = dict(reference.get("citation") or {})
        ids = {str(c.get("database")): c.get("id") for c in citation.get("citationCrossReferences") or []}
        citations += _citations(ids.get("PubMed"), ids.get("DOI"), citation.get("title"))
    entry_version, sequence_version = audit.get("entryVersion"), audit.get("sequenceVersion")
    if entry_version is None or sequence_version is None:
        raise LifeSciFormatError("schema_drift", f"UniProt entry {accession} has no entry or sequence version")
    attributes = {
        "uniprot_id": text(body.get("uniProtkbId")),
        "reviewed": "reviewed" if "reviewed (Swiss-Prot)" in entry_type else "unreviewed",
        "entry_type": entry_type,
        "secondary_accessions": [str(s) for s in body.get("secondaryAccessions") or []],
        "entry_version": int(entry_version),
        "sequence_version": int(sequence_version),
        "first_public": iso_day(audit.get("firstPublicDate")),
        "last_annotation_update": iso_day(audit.get("lastAnnotationUpdateDate")),
        "last_sequence_update": iso_day(audit.get("lastSequenceUpdateDate")),
        "organism": {"scientific_name": text(organism.get("scientificName")), "tax_id": organism.get("taxonId")},
        "protein_name": name,
        "gene_names": [g for g in genes if g],
        # Stored as published; nothing is computed from the sequence.
        "sequence": {"value": text(sequence.get("value")), "length": sequence.get("length"),
                     "crc64": text(sequence.get("crc64")), "md5": text(sequence.get("md5")),
                     "mol_weight": sequence.get("molWeight")},
    }
    return [_statement(
        "uniprot", "protein", accession, release=release,
        version={"marker": f"entry-{int(entry_version)}", "basis": "entry-version", "order": [int(entry_version)],
                 "date": attributes["last_annotation_update"]},
        status_published=entry_type, label=name, attributes=attributes, xrefs=xrefs, citations=citations, url=url,
        excluded=["references[].citation.authors", "references[].citation.authoringGroup", "comments",
                  "features", "keywords"])]


def parse_gene_summary(body: Mapping[str, Any], *, requested: Sequence[str], release: Mapping[str, Any], url: str
                       ) -> tuple[list[dict[str, Any]], list[str]]:
    result = dict(body.get("result") or {})
    if "uids" not in result:
        raise LifeSciFormatError("schema_drift", "esummary gene response has no result.uids")
    statements, missing = [], []
    for uid in requested:
        item = dict(result.get(uid) or {})
        if not item or item.get("error"):
            missing.append(uid)
            continue
        published = str(item.get("status") if item.get("status") is not None else "")
        if published not in GENE_STATUS:
            raise LifeSciFormatError("schema_drift", f"gene {uid} status {published!r} is not recognised")
        status = GENE_STATUS[published]
        current = text(item.get("currentid"))
        organism = dict(item.get("organism") or {})
        xrefs = [_xref("NCBI Taxonomy", organism["taxid"], "organism")] if organism.get("taxid") else []
        statements.append(_statement(
            "ncbi-gene", "gene", uid, release=release, version=dict(CONTENT), status=status,
            status_published=published or "live", successors=[current] if status == "replaced" and current else [],
            label=text(item.get("description")),
            attributes={"gene_id": uid, "symbol": text(item.get("name")), "description": text(item.get("description")),
                        "organism": {"scientific_name": text(organism.get("scientificname")),
                                     "tax_id": organism.get("taxid")},
                        "chromosome": text(item.get("chromosome")), "map_location": text(item.get("maplocation")),
                        "aliases": [a.strip() for a in str(item.get("otheraliases") or "").split(",") if a.strip()],
                        "current_id": current},
            xrefs=xrefs, url=url))
    return statements, missing


def _xml_root(raw: bytes) -> ET.Element:
    head = raw[:2000].decode("utf-8", "replace")
    if "<!ENTITY" in head:
        raise LifeSciFormatError("schema_drift", "XML with entity declarations is refused")
    try:
        return ET.fromstring(raw)
    except ET.ParseError as exc:
        raise LifeSciFormatError("schema_drift", "taxonomy response is not XML") from exc


def parse_taxonomy(raw: bytes, *, requested: Sequence[str], release: Mapping[str, Any], url: str
                   ) -> tuple[list[dict[str, Any]], list[str]]:
    root = _xml_root(raw)
    if root.tag != "TaxaSet":
        raise LifeSciFormatError("schema_drift", "efetch taxonomy response is not a TaxaSet")
    statements, answered = [], set()
    for taxon in root.findall("Taxon"):
        tax_id = text(taxon.findtext("TaxId"))
        if tax_id is None:
            raise LifeSciFormatError("schema_drift", "a Taxon has no TaxId")
        aka = [text(t.text) for t in taxon.findall("AkaTaxIds/TaxId") if text(t.text)]
        lineage_ex = [{"tax_id": text(t.findtext("TaxId")), "name": text(t.findtext("ScientificName")),
                       "rank": text(t.findtext("Rank"))} for t in taxon.findall("LineageEx/Taxon")]
        parent = text(taxon.findtext("ParentTaxId"))
        xrefs = [_xref("NCBI Taxonomy", parent, "parent")] if parent else []
        attributes = {"tax_id": tax_id, "scientific_name": text(taxon.findtext("ScientificName")),
                      "rank": text(taxon.findtext("Rank")), "parent_tax_id": parent,
                      "division": text(taxon.findtext("Division")), "lineage": text(taxon.findtext("Lineage")),
                      "lineage_ex": lineage_ex, "merged_tax_ids": aka,
                      "update_date": iso_day(str(taxon.findtext("UpdateDate") or "").replace("/", "-"))}
        statements.append(_statement("ncbi-taxonomy", "taxon", tax_id, release=release, version=dict(CONTENT),
                                     status_published="active", label=attributes["scientific_name"],
                                     attributes=attributes, xrefs=xrefs, url=url,
                                     excluded=["OtherNames", "Citations"]))
        answered.add(tax_id)
        for old in aka:
            if old in requested and old not in answered:
                statements.append(_statement(
                    "ncbi-taxonomy", "taxon", old, release=release, version=dict(CONTENT), status="merged",
                    status_published=f"AkaTaxId of {tax_id}", successors=[tax_id], label=None,
                    attributes={"tax_id": old, "merged_into": tax_id}, url=url))
                answered.add(old)
    return statements, [t for t in requested if t not in answered]


def _pdb_day(value: Any) -> str | None:
    return iso_day(str(value or "")[:10])


def parse_pdb_entry(entry: Mapping[str, Any], entities: Mapping[str, Mapping[str, Any]], *,
                    release: Mapping[str, Any], url: str) -> dict[str, Any]:
    pdb_id = text(entry.get("rcsb_id"))
    info = dict(entry.get("rcsb_accession_info") or {})
    if pdb_id is None or info.get("major_revision") is None or info.get("minor_revision") is None:
        raise LifeSciFormatError("schema_drift", "a PDB entry states its id and major/minor revision")
    major, minor = int(info["major_revision"]), int(info["minor_revision"])
    history = [{"ordinal": h.get("ordinal"), "major_revision": h.get("major_revision"),
                "minor_revision": h.get("minor_revision"), "revision_date": _pdb_day(h.get("revision_date")),
                "data_content_type": text(h.get("data_content_type"))}
               for h in entry.get("pdbx_audit_revision_history") or []]
    entity_rows, xrefs = [], []
    for entity_id, body in sorted(entities.items(), key=lambda kv: int(kv[0])):
        ids = dict(body.get("rcsb_polymer_entity_container_identifiers") or {})
        references = [{"database": text(r.get("database_name")), "accession": text(r.get("database_accession"))}
                      for r in ids.get("reference_sequence_identifiers") or []]
        uniprot = sorted({str(a) for a in ids.get("uniprot_ids") or []}
                         | {r["accession"] for r in references if r["database"] == "UniProt" and r["accession"]})
        entity_rows.append({"entity_id": entity_id, "description": text(dict(body.get("rcsb_polymer_entity") or {})
                                                                         .get("pdbx_description")),
                            "uniprot_accessions": uniprot, "reference_sequences": references})
        xrefs += [_xref("UniProt", acc, "entity-reference", entity_id=entity_id) for acc in uniprot]
    citation = dict(entry.get("rcsb_primary_citation") or {})
    entry_info = dict(entry.get("rcsb_entry_info") or {})
    attributes = {
        "pdb_id": pdb_id, "title": text(dict(entry.get("struct") or {}).get("title")),
        "methods": [text(e.get("method")) for e in entry.get("exptl") or [] if text(e.get("method"))],
        "resolution": [str(r) for r in entry_info.get("resolution_combined") or []],
        "deposit_date": _pdb_day(info.get("deposit_date")),
        "initial_release_date": _pdb_day(info.get("initial_release_date")),
        "revision": {"major": major, "minor": minor, "date": _pdb_day(info.get("revision_date"))},
        "revision_history": history, "status_code": text(info.get("status_code")), "entities": entity_rows,
        "primary_citation": {"doi": text(citation.get("pdbx_database_id_doi")),
                             "pubmed_id": text(citation.get("pdbx_database_id_pub_med")),
                             "title": text(citation.get("title"))},
    }
    return _statement(
        "rcsb-pdb", "structure", pdb_id, release=release,
        version={"marker": f"rev-{major}.{minor}", "basis": "pdb-revision", "order": [major, minor],
                 "date": attributes["revision"]["date"]},
        status_published=attributes["status_code"], label=attributes["title"], attributes=attributes, xrefs=xrefs,
        citations=_citations(citation.get("pdbx_database_id_pub_med"), citation.get("pdbx_database_id_doi"),
                             citation.get("title")), url=url,
        excluded=["audit_author", "rcsb_primary_citation.rcsb_authors", "citation_author", "pdbx_contact_author"])


def parse_pdb_removed(body: Mapping[str, Any], *, requested: str, release: Mapping[str, Any], url: str
                      ) -> dict[str, Any]:
    removed = dict(body.get("rcsb_repository_holdings_removed") or {})
    pdb_id = text(body.get("rcsb_id")) or requested
    if not removed:
        raise LifeSciFormatError("schema_drift", f"{requested} is not listed as removed")
    successors = [str(s) for s in removed.get("id_codes_replaced_by") or []]
    return _statement("rcsb-pdb", "structure", pdb_id, release=release,
                      version={**CONTENT, "date": _pdb_day(removed.get("remove_date"))}, status="obsolete",
                      status_published="OBS", successors=successors, label=text(removed.get("title")),
                      attributes={"pdb_id": pdb_id, "remove_date": _pdb_day(removed.get("remove_date")),
                                  "replaced_by": successors},
                      url=url, excluded=["audit_authors"])


def chembl_release(status: Mapping[str, Any], declared: Mapping[str, Any]) -> dict[str, Any]:
    version = text(status.get("chembl_db_version"))
    if version is None:
        raise LifeSciFormatError("schema_drift", "ChEMBL status names no chembl_db_version")
    if version != str(declared.get("label")):
        raise LifeSciFormatError("release_mismatch", f"ChEMBL serves {version}; the source declares "
                                                     f"{declared.get('label')}")
    return {"label": version, "published_on": iso_day(status.get("chembl_release_date"))
            or iso_day(declared.get("published_on")), "basis": "provider"}


def parse_chembl_target(body: Mapping[str, Any], *, release: Mapping[str, Any], url: str) -> dict[str, Any]:
    target_id = text(body.get("target_chembl_id"))
    if target_id is None:
        raise LifeSciFormatError("schema_drift", "a ChEMBL target has no target_chembl_id")
    components = [{"accession": text(c.get("accession")), "component_type": text(c.get("component_type")),
                   "relationship": text(c.get("relationship")),
                   "description": text(c.get("component_description"))}
                  for c in body.get("target_components") or []]
    xrefs = [_xref("UniProt", c["accession"], "target-component", component_type=c["component_type"],
                   relationship=c["relationship"]) for c in components if c["accession"]]
    if body.get("tax_id") is not None:
        xrefs.append(_xref("NCBI Taxonomy", body["tax_id"], "organism"))
    return _statement("chembl", "target", target_id, release=release, version=dict(CONTENT),
                      label=text(body.get("pref_name")),
                      attributes={"target_chembl_id": target_id, "pref_name": text(body.get("pref_name")),
                                  "target_type": text(body.get("target_type")), "organism": text(body.get("organism")),
                                  "tax_id": body.get("tax_id"), "components": components},
                      xrefs=xrefs, url=url, excluded=["cross_references"])


def parse_chembl_molecule(body: Mapping[str, Any], *, release: Mapping[str, Any], url: str) -> dict[str, Any]:
    molecule_id = text(body.get("molecule_chembl_id"))
    if molecule_id is None:
        raise LifeSciFormatError("schema_drift", "a ChEMBL molecule has no molecule_chembl_id")
    structures = dict(body.get("molecule_structures") or {})
    return _statement("chembl", "compound", molecule_id, release=release, version=dict(CONTENT),
                      label=text(body.get("pref_name")),
                      attributes={"molecule_chembl_id": molecule_id, "pref_name": text(body.get("pref_name")),
                                  "molecule_type": text(body.get("molecule_type")),
                                  "structures": {"standard_inchi_key": text(structures.get("standard_inchi_key")),
                                                 "standard_inchi": text(structures.get("standard_inchi")),
                                                 "canonical_smiles": text(structures.get("canonical_smiles"))}},
                      url=url, excluded=["molecule_properties (computed)",
                                         "max_phase (clinical questions go to Clinical Evidence)",
                                         "cross_references"])


def parse_chembl_activities(body: Mapping[str, Any], *, target_id: str, limit: int, release: Mapping[str, Any],
                            url: str) -> list[dict[str, Any]]:
    meta = dict(body.get("page_meta") or {})
    activities = list(body.get("activities") or [])
    total = int(meta.get("total_count") if meta.get("total_count") is not None else len(activities))
    if total > len(activities) or total > limit:
        # Never a truncated activity list: a missing activity would read as one that was not published.
        raise LifeSciFormatError("budget_exhausted", f"{target_id} has {total} activities; the document caps at "
                                                     f"{limit}")
    statements = []
    for item in activities:
        activity_id = text(item.get("activity_id"))
        if activity_id is None or text(item.get("target_chembl_id")) != target_id:
            raise LifeSciFormatError("schema_drift", "an activity names its id and the requested target")
        document = text(item.get("document_chembl_id"))
        attributes = {
            "activity_id": activity_id, "assay_chembl_id": text(item.get("assay_chembl_id")),
            "assay_type": text(item.get("assay_type")), "assay_description": text(item.get("assay_description")),
            "target_chembl_id": target_id, "molecule_chembl_id": text(item.get("molecule_chembl_id")),
            "document_chembl_id": document, "document_year": item.get("document_year"),
            "published": {"type": text(item.get("type")), "relation": text(item.get("relation")),
                          "value": decimal_text(item.get("value")), "value_text": text(item.get("value")),
                          "units": text(item.get("units"))},
            "standard": {"type": text(item.get("standard_type")), "relation": text(item.get("standard_relation")),
                         "value": decimal_text(item.get("standard_value")), "units": text(item.get("standard_units")),
                         "flag": item.get("standard_flag")},
            "data_validity_comment": text(item.get("data_validity_comment")),
            "activity_comment": text(item.get("activity_comment")),
            "potential_duplicate": item.get("potential_duplicate"),
        }
        xrefs = [_xref("ChEMBL", target_id, "target"),
                 _xref("ChEMBL compound", attributes["molecule_chembl_id"], "compound")] + (
            [_xref("ChEMBL document", document, "document")] if document else [])
        statements.append(_statement(
            "chembl", "activity", activity_id, release=release, version=dict(CONTENT),
            label=f"{attributes['standard']['type'] or attributes['published']['type']} "
                  f"{attributes['molecule_chembl_id']} -> {target_id}",
            attributes=attributes, xrefs=xrefs,
            citations=[{"kind": "chembl-document", "id": document}] if document else [], url=url,
            excluded=["pchembl_value (derived)", "activity_properties", "ligand_efficiency"]))
    return statements


def parse_chembl_document(body: Mapping[str, Any], *, release: Mapping[str, Any], url: str) -> dict[str, Any]:
    document_id = text(body.get("document_chembl_id"))
    if document_id is None:
        raise LifeSciFormatError("schema_drift", "a ChEMBL document has no document_chembl_id")
    return _statement("chembl", "document", document_id, release=release, version=dict(CONTENT),
                      label=text(body.get("title")),
                      attributes={"document_chembl_id": document_id, "doc_type": text(body.get("doc_type")),
                                  "journal": text(body.get("journal")), "year": body.get("year"),
                                  "volume": text(body.get("volume")), "first_page": text(body.get("first_page")),
                                  "title": text(body.get("title"))},
                      citations=_citations(body.get("pubmed_id"), body.get("doi"), body.get("title")), url=url,
                      excluded=["authors", "abstract"])


# ------------------------------------------------------------------ runtime adapter


class LifeSciAdapter:
    """Fetch declared life-science documents on the runtime's transport; one page per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None, today: Callable[[], str] | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = lifesci_declaration(self.source)
        self.provider = self.declared["provider"]
        # Only NCBI takes a key; it is kept off every URL, receipt and record.
        self._api_key = secret if self.provider in {"ncbi-gene", "ncbi-taxonomy"} and secret else None
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.today = today or (lambda: datetime.now(UTC).date().isoformat())
        self.definition = {
            "contract": ADAPTER_CONTRACT,
            "source_id": source["source_id"],
            "connector": source["connector"],
            "endpoint": source["endpoint"],
            "operations": list(source["operations"]),
            "source_hash": source["source_hash"],
            "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"],
            "limits": source["budgets"],
            "life_sciences": {"provider": self.provider,
                              "live_verification": LIVE_VERIFICATION[self.provider]["status"],
                              "api_key": "configured" if self._api_key else "absent"},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "life-sciences runs fetch the declared documents only")

    def _get(self, url: str, params: Mapping[str, str], *, allow_missing: bool = False
             ) -> tuple[bytes | None, dict[str, Any], str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the endpoint's host only")
        sent = sorted(params.items()) + ([("api_key", self._api_key)] if self._api_key else [])
        response = self.transport(url=url, params=sent,
                                  headers={"Accept": "application/json, application/xml, text/xml"},
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        headers = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        if status == 404 and allow_missing:
            return None, headers, origin
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        return raw, headers, origin

    @staticmethod
    def _display(url: str, params: Mapping[str, str]) -> str:
        return url + ("?" + urlencode(sorted(params.items())) if params else "")

    def _parse(self, document: Mapping[str, Any], today: str) -> tuple[list[dict], dict[str, Any]]:
        endpoint, provider = self.source["endpoint"], self.provider
        declared = dict(self.declared.get("release") or {})
        statements: list[dict[str, Any]] = []
        digests, missing, origins = [], [], set()
        release = declared_release(declared, today)
        for role, url, params in requests_for(provider, document, endpoint):
            shown = self._display(url, params)
            raw, headers, origin = self._get(url, params, allow_missing=provider == "uniprot")
            origins.add(origin)
            if raw is None:
                missing.append(role)
                continue
            digests.append({"request": role, "sha256": digest_bytes(raw)})
            if provider == "uniprot":
                release = uniprot_release(headers, declared, today)
                statements += parse_uniprot(_json(raw, role), requested=role, release=release, url=shown)
            elif provider == "ncbi-gene":
                found, gone = parse_gene_summary(_json(raw, role), requested=document["gene_ids"], release=release,
                                                 url=shown)
                statements += found
                missing += gone
            elif provider == "ncbi-taxonomy":
                found, gone = parse_taxonomy(raw, requested=document["tax_ids"], release=release, url=shown)
                statements += found
                missing += gone
            elif provider == "rcsb-pdb":
                if document["kind"] == "removed":
                    statements.append(parse_pdb_removed(_json(raw, role), requested=role, release=release, url=shown))
                elif role == "entry":
                    entry, entities, entry_url = _json(raw, role), {}, shown
                else:
                    entities[role.split(":", 1)[1]] = _json(raw, role)
            elif role == "status":
                release = chembl_release(_json(raw, role), declared)
            elif document["kind"] == "target":
                statements.append(parse_chembl_target(_json(raw, role), release=release, url=shown))
            elif document["kind"] == "molecules":
                statements.append(parse_chembl_molecule(_json(raw, role), release=release, url=shown))
            elif document["kind"] == "documents":
                statements.append(parse_chembl_document(_json(raw, role), release=release, url=shown))
            else:
                statements += parse_chembl_activities(_json(raw, role), target_id=document["target_id"],
                                                      limit=int(document["limit"]), release=release, url=shown)
        if provider == "rcsb-pdb" and document["kind"] == "entry":
            statements.append(parse_pdb_entry(entry, entities, release=release, url=entry_url))
        header = {"release": release, "requests": digests, "not_returned": missing,
                  "evidence_origin": "live" if "live" in origins else "fixture"}
        return statements, header

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = list(self.declared["documents"])
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        try:
            statements, header = self._parse(document, self.today())
        except LifeSciFormatError as exc:
            code = {"budget_exhausted": "budget_exhausted", "release_mismatch": "schema_drift"}.get(exc.code,
                                                                                                  "schema_drift")
            raise SourcePackError(code, f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(statements) > limit:
            raise SourcePackError("budget_exhausted", "document yields more records than the run's result budget")
        page = {"contract": PAGE_CONTRACT, "provider": self.provider, "document": document.get("label"),
                "live_verification": LIVE_VERIFICATION[self.provider]["status"], **header}
        records = [
            {
                "id": f"{self.provider}:{s['record_type']}:{s['native_id']}:{s['version']['marker']}",
                "title": f"{s['source']} {s['record_type']} {s['native_id']} ({s['version']['marker']})",
                "url": s["url"],
                "language": "en",
                "published_at": s["release"]["published_on"],
                "content": canonical(s),
                "lifesci_page": page,
                "lifesci_statement": s,
            }
            for s in statements
        ]
        receipt = {"status": 200, "provider": self.provider, "document": document.get("label"),
                   "release": header["release"], "requests": header["requests"],
                   "not_returned": header["not_returned"], "statements": len(records),
                   "evidence_origin": header["evidence_origin"], "api_key_used": bool(self._api_key),
                   "final_page": index + 1 >= len(documents)}
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        size = sum(len(r["content"]) for r in records)
        return RuntimePage(tuple(records), next_cursor, size, receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: LifeSciAdapter}
FIXTURE_DAY = "2100-01-01"


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query without any api_key)."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
        query = urlencode(sorted((k, v) for k, v in pairs if k != "api_key"))
        key = urlsplit(url).path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def fixture_request(url: str, params: Mapping[str, str] | None = None) -> str:
    query = urlencode(sorted((params or {}).items()))
    return urlsplit(url).path + ("?" + query if query else "")


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = LifeSciAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                             today=lambda: FIXTURE_DAY)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": min(source["operations"]), "parameters": {}, "limit": int(source["budgets"]["max_results"])},
            cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CONNECTOR",
    "EXCLUSIONS",
    "LIVE_VERIFICATION",
    "NEVER_SENTENCE",
    "PERSONAL_DATA_DECISION",
    "PROVIDER_CONTRACTS",
    "LifeSciAdapter",
    "fixture_request",
    "fixture_transport",
    "parse_chembl_activities",
    "parse_gene_summary",
    "parse_pdb_entry",
    "parse_taxonomy",
    "parse_uniprot",
    "replay_native_fixture",
    "requests_for",
]
