"""Life-science reference sources for the Science pack: UniProt, NCBI, RCSB PDB and ChEMBL (#2652, LS01 and LS03-LS06).

Four providers run as sources of the ``primary-scientific-evidence`` source pack
(``config/source_packs/scientific.json``, connector ``life-sciences``) through
:mod:`src.ingestion.source_pack_runtime` - licence acceptance, budgets,
receipts, checkpoints and the runtime's same-host HTTPS transport - each under a
recorded access contract (:data:`PROVIDER_CONTRACTS`, documented in
``docs/development/life-sciences-evidence/source-audit.md``). Selections are
explicit and bounded, parsers fail closed, only the provider host is contacted
and values are kept as published.

* **UniProt** (``uniprot``, LS03) - UniProtKB entries by accession (reviewed and
  unreviewed as UniProt labels them, entry and sequence versions, the release
  from the response headers, sequence, cross-references and literature) and the
  UniSave entry-version history; inactive entries keep UniProt's reason and
  successor accessions.
* **NCBI** (``ncbi``, LS04) - NCBI Datasets v2 gene reports by Gene ID and
  taxonomy reports by Tax ID; replaced and discontinued genes and merged taxa
  are kept with the successor NCBI names. The optional API key travels in the
  ``api-key`` header only, never in a URL, receipt or record.
* **RCSB PDB** (``pdb``, LS05) - experimental entries by PDB ID from the Data API
  with the revision history, methods and resolution as published and each
  polymer entity's UniProt mapping; removed entries come from the removed
  holdings with their superseding IDs. Computed structure models are excluded.
* **ChEMBL** (``chembl``, LS06) - the release (``/status``), targets, molecules,
  one bounded activity page per declared target and the documents those
  activities cite, keyed by ChEMBL ID and release; values stay strings exactly
  as published.

Person fields (author lists, depositors, submitters) are dropped at acquisition
(LS01 minimisation decision) and listed in each page receipt. Every provider is
``unverified-live`` until a dated live run (LS14, #2721); request paths and
field names marked *verify* are authored from public documentation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.lifesci_records import (
    ACCESSION,
    CHEMBL_ID,
    MINIMISATION,
    PDB_ID,
    PERSONAL_KEYS,
    LifeSciError,
    digest,
    statement,
)

CONNECTOR = "life-sciences"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("uniprot", "ncbi", "pdb", "chembl")
PROVIDER_HOSTS = {"uniprot": ("rest.uniprot.org",), "ncbi": ("api.ncbi.nlm.nih.gov",), "pdb": ("data.rcsb.org",),
                  "chembl": ("www.ebi.ac.uk",)}
MAX_VERSIONS = 20
MAX_ENTITIES = 10
MAX_ACTIVITIES = 100
MAX_DOCUMENTS = 10
READ_ON = "2026-09-30"
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "uniprot": {
        "publisher": "UniProt Consortium (EMBL-EBI, SIB Swiss Institute of Bioinformatics, PIR)",
        "access": "UniProt REST API, HTTPS GET /uniprotkb/{accession}?format=json and /unisave/{accession}?format=json",
        "endpoints": ["https://rest.uniprot.org/uniprotkb/{accession}?format=json",
                      "https://rest.uniprot.org/unisave/{accession}?format=json (verify JSON field names)"],
        "authentication": "none (open access, no login)",
        "licence": "CC BY 4.0 (UniProt licence page; confirmed only through a search-result extract on "
                   f"{READ_ON}, the page itself was not fetchable from this environment)",
        "terms_url": "https://www.uniprot.org/help/license",
        "attribution": "UniProt Consortium, UniProtKB entry accession, entry version and release",
        "redistribution": "permitted with attribution under CC BY 4.0",
        "rate_limits": "no hard published limit found (unverified); one request per selected entry and one per "
                       "entry history",
        "versioning": "entry version and sequence version per entry; UniProt releases (YYYY_MM) stated in the "
                      "X-UniProt-Release response header (verify); UniSave lists every entry version with its first "
                      "and last release",
        "corrections_and_removals": "an entry that leaves UniProtKB becomes 'Inactive' with inactiveReason "
                                    "MERGED, DEMERGED or DELETED and the successor accessions (verify field names); "
                                    "kept as an obsoleted revision",
        "revision_behaviour": "new revision per release in which the entry is read; earlier revisions never deleted",
    },
    "ncbi": {
        "publisher": "National Center for Biotechnology Information (NCBI), U.S. National Library of Medicine",
        "access": "NCBI Datasets v2 REST API, HTTPS GET /datasets/v2/gene/id/{gene_id} and "
                  "/datasets/v2/taxonomy/taxon/{tax_id}",
        "endpoints": ["https://api.ncbi.nlm.nih.gov/datasets/v2/gene/id/{gene_id} (verify report field names)",
                      "https://api.ncbi.nlm.nih.gov/datasets/v2/taxonomy/taxon/{tax_id} (verify report field names)"],
        "authentication": "optional NCBI API key sent in the api-key request header, held as the NOESIS_NCBI_API_KEY "
                          "secret reference; never stored in manifests, URLs, receipts or records",
        "licence": "NCBI places no restrictions on the use or distribution of molecular database data; submitters "
                   "may claim rights in portions (NCBI Website and Data Usage Policies, via a search-result extract "
                   f"on {READ_ON}; page not fetchable from this environment)",
        "terms_url": "https://www.ncbi.nlm.nih.gov/home/about/policies/",
        "attribution": "NCBI Gene / NCBI Taxonomy with the Gene ID or Tax ID",
        "redistribution": "permitted for the stored identity, status and lineage fields",
        "rate_limits": "5 requests per second without an API key, 10 with a key (NCBI Datasets API keys page, via a "
                       f"search-result extract on {READ_ON})",
        "versioning": "no release label in the gene and taxonomy reports: every change of the published content is "
                      "a new revision dated by retrieval",
        "corrections_and_removals": "replaced and discontinued Gene IDs and merged Tax IDs are kept as obsoleted "
                                    "revisions naming the current ID (verify how Datasets reports them)",
        "revision_behaviour": "new revision on change; earlier revisions stay queryable",
    },
    "pdb": {
        "publisher": "RCSB Protein Data Bank (wwPDB archive)",
        "access": "RCSB PDB Data API, HTTPS GET /rest/v1/core/entry/{id}, /rest/v1/core/polymer_entity/{id}/{entity} "
                  "and /rest/v1/holdings/removed/{id}",
        "endpoints": ["https://data.rcsb.org/rest/v1/core/entry/{pdb_id}",
                      "https://data.rcsb.org/rest/v1/core/polymer_entity/{pdb_id}/{entity_id}",
                      "https://data.rcsb.org/rest/v1/holdings/removed/{pdb_id} (verify)"],
        "authentication": "none",
        "licence": "CC0 1.0 for PDB archive data and for data from RCSB PDB programmatic APIs (RCSB PDB Usage "
                   f"Policies, via a search-result extract on {READ_ON}; page not fetchable from this environment)",
        "terms_url": "https://www.rcsb.org/pages/usage-policy",
        "attribution": "cite the PDB ID and its primary citation (attribution encouraged, not required)",
        "redistribution": "permitted (CC0)",
        "rate_limits": "no published hard limit found (unverified); at most 1 + 10 entity requests per entry",
        "versioning": "major.minor revision per entry with pdbx_audit_revision_history; each revision is a new "
                      "record revision",
        "corrections_and_removals": "obsolete entries leave the current holdings; the removed holdings name the "
                                    "superseding IDs and the superseding entry names what it supersedes",
        "revision_behaviour": "new revision per PDB revision; obsolete entries kept as obsoleted revisions",
    },
    "chembl": {
        "publisher": "ChEMBL, EMBL-EBI",
        "access": "ChEMBL web services, HTTPS GET /chembl/api/data/{status,target,molecule,activity,document}",
        "endpoints": ["https://www.ebi.ac.uk/chembl/api/data/status.json",
                      "https://www.ebi.ac.uk/chembl/api/data/target/{chembl_id}.json",
                      "https://www.ebi.ac.uk/chembl/api/data/molecule/{chembl_id}.json",
                      "https://www.ebi.ac.uk/chembl/api/data/activity.json?target_chembl_id&limit&offset=0",
                      "https://www.ebi.ac.uk/chembl/api/data/document/{chembl_id}.json"],
        "authentication": "none",
        "licence": "CC BY-SA 3.0 Unported (ChEMBL interface documentation, via a search-result extract on "
                   f"{READ_ON}; page not fetchable from this environment); computed properties from commercial "
                   "software carry their own terms and are not stored",
        "terms_url": "https://chembl.gitbook.io/chembl-interface-documentation/about",
        "attribution": "ChEMBL release (ChEMBL_nn) and the ChEMBL IDs; activities cite their source document",
        "redistribution": "permitted with attribution; adaptations share-alike under CC BY-SA 3.0",
        "rate_limits": "no published hard limit found (unverified); one activity page of at most 100 records per "
                       "declared target and at most 10 cited documents per page",
        "versioning": "numbered releases (chembl_db_version from /status); every record is keyed by ChEMBL ID and "
                      "release",
        "corrections_and_removals": "data_validity_comment flags values ChEMBL considers suspect; an activity "
                                    "absent from a later complete release page gets a dated removed revision",
        "revision_behaviour": "new revision per release; values never converted",
    },
}
BOUNDED_COVERAGE = {
    "proteins": "one fictional reviewed entry with its UniSave history, one unreviewed entry and one merged "
                "accession (fixture); live: a declared list of at most 20 accessions",
    "genes_and_taxa": "the genes those entries cross-reference (one replaced Gene ID) and their organisms' Tax IDs "
                      "(one merged Tax ID)",
    "structures": "experimental PDB entries the proteins cross-reference, at most 10 polymer entities each, and one "
                  "obsolete entry with its superseding entry",
    "bioactivity": "the ChEMBL targets of the selected proteins, one activity page of at most 100 records per "
                   "target, the compounds named and the documents cited, for one named ChEMBL release",
    "excluded": ["computed structure models (AlphaFold, ModelArchive)", "UniProt comments, features and keywords",
                 "ChEMBL computed molecule properties, max_phase and pChEMBL values", "bulk downloads and mirrors"],
    "justification": "enough to reproduce protein-to-reference-records journeys with versions, obsolescence and "
                     "cross-references across all four sources while staying far below each provider's limits",
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "intended": "live-verified after a dated bounded run (LS14, #2721)",
               "note": "no dated live run from this runtime; offline fixtures only"}
    for provider in PROVIDERS
}
LIVE_VERIFICATION["ncbi"]["credential"] = "NOESIS_NCBI_API_KEY optional; not configured"
NOT_IMPLEMENTED: dict[str, str] = {}
EXCLUSIONS = (
    "biological or clinical inference", "activity prediction", "sequence analysis beyond storage",
    "conversion or aggregation of activity values", "redistribution beyond each source's licence",
    "person names and contact details",
)
_EXCLUDED_CHEMBL = {"molecule_properties", "max_phase", "pchembl_value", "ligand_efficiency", "withdrawn_flag",
                    "black_box_warning", "indication_class"}
_UNIPROT_NOT_STORED = ("comments", "features", "keywords", "extraAttributes")


class LifeSciFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _as_published(value: Any) -> str | None:
    """A value as the published text: strings unchanged, JSON numbers as their JSON text (never re-computed)."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def _day(value: Any) -> str | None:
    match = re.match(r"^(\d{4}(?:-\d{2}(?:-\d{2})?)?)", _text(value) or "")
    return match.group(1) if match else None


def _personal(prefix: str, body: Any) -> list[str]:
    """Names of person fields present anywhere in a payload (dropped, reported in the receipt)."""
    found: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).casefold() in PERSONAL_KEYS:
                    found.add(f"{prefix}:{key}")
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(body)
    return sorted(found)


# ------------------------------------------------------------------ selections


def selection_entries(source: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    declared = dict(source.get("life_sciences") or {})
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", f"life-sciences sources declare a provider in {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]} only")
    entries = [dict(e) for e in declared.get("selection") or []]
    if not 1 <= len(entries) <= int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "a life-sciences source selects 1..max_pages pages explicitly")
    kinds = {"uniprot": {"entry", "history"}, "ncbi": {"gene", "taxon"}, "pdb": {"entry"},
             "chembl": {"release", "target", "molecule", "activities", "document"}}[provider]
    for index, entry in enumerate(entries):
        kind = entry.get("kind")
        if kind not in kinds:
            raise SourcePackError("invalid_manifest", f"{provider} selections are one of {sorted(kinds)}")
        if provider == "uniprot":
            if not ACCESSION.fullmatch(str(entry.get("accession") or "")):
                raise SourcePackError("invalid_manifest", "UniProt selections name a UniProtKB accession")
            if kind == "history" and not 1 <= int(entry.get("max_versions") or MAX_VERSIONS) <= MAX_VERSIONS:
                raise SourcePackError("invalid_manifest", f"at most {MAX_VERSIONS} entry versions per history")
        elif provider == "ncbi":
            if not re.fullmatch(r"[0-9]{1,12}", str(entry.get("id") or "")):
                raise SourcePackError("invalid_manifest", "NCBI selections name a numeric Gene ID or Tax ID")
        elif provider == "pdb":
            if not PDB_ID.fullmatch(str(entry.get("pdb_id") or "")):
                raise SourcePackError("invalid_manifest", "PDB selections name a four-character experimental PDB ID; "
                                                          "computed structure models are excluded")
        else:
            if kind == "release":
                if index != 0:
                    raise SourcePackError("invalid_manifest", "the ChEMBL release is read first")
                continue
            key = entry.get("target_chembl_id") if kind == "activities" else entry.get("chembl_id")
            if not CHEMBL_ID.fullmatch(str(key or "")):
                raise SourcePackError("invalid_manifest", f"a ChEMBL {kind} selection names a ChEMBL ID")
            if kind == "activities" and not 1 <= int(entry.get("limit") or 0) <= min(
                    MAX_ACTIVITIES, int(source["budgets"]["max_results"])):
                raise SourcePackError("invalid_manifest", f"activity pages are small: 1 <= limit <= {MAX_ACTIVITIES}")
    if provider == "chembl" and entries[0].get("kind") != "release":
        raise SourcePackError("invalid_manifest", "ChEMBL selections start with the release they are read from")
    return provider, entries


def selection_key(provider: str, entry: Mapping[str, Any]) -> str:
    return provider + ":" + digest({k: entry[k] for k in sorted(entry) if k != "label"})[:16]


def request_for(provider: str, entry: Mapping[str, Any]) -> tuple[str, str, dict[str, str]]:
    """(role, path, query) of the first request of one selected page; paths are relative to the endpoint."""
    kind = entry["kind"]
    if provider == "uniprot":
        accession = quote(str(entry["accession"]))
        if kind == "history":
            return "history", f"/unisave/{accession}", {"format": "json"}
        return "entry", f"/uniprotkb/{accession}", {"format": "json"}
    if provider == "ncbi":
        if kind == "gene":
            return "gene", f"/datasets/v2/gene/id/{quote(str(entry['id']))}", {}
        return "taxon", f"/datasets/v2/taxonomy/taxon/{quote(str(entry['id']))}", {}
    if provider == "pdb":
        return "entry", f"/rest/v1/core/entry/{quote(str(entry['pdb_id']))}", {}
    if kind == "release":
        return "release", "/status.json", {}
    if kind == "activities":
        return "activities", "/activity.json", {"target_chembl_id": str(entry["target_chembl_id"]),
                                                "limit": str(int(entry["limit"])), "offset": "0"}
    return kind, f"/{kind}/{quote(str(entry['chembl_id']))}.json", {}


# ------------------------------------------------------------------ parsers


def _source(url: str, provider: str, origin: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "attribution": PROVIDER_CONTRACTS[provider]["attribution"],
            "terms_url": PROVIDER_CONTRACTS[provider]["terms_url"], "licence": PROVIDER_CONTRACTS[provider]["licence"],
            "evidence_origin": origin, **{k: v for k, v in extra.items() if v is not None}}


def _uniprot_citations(body: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    items = []
    for ref in body.get("references") or []:
        citation = dict(ref.get("citation") or {})
        xrefs = {str(x.get("database") or "").casefold(): _text(x.get("id"))
                 for x in citation.get("citationCrossReferences") or [] if isinstance(x, Mapping)}
        items.append({"reference_number": ref.get("referenceNumber"), "pubmed_id": xrefs.get("pubmed"),
                      "doi": xrefs.get("doi"), "title": _text(citation.get("title")),
                      "journal": _text(citation.get("journal")), "year": _day(citation.get("publicationDate"))})
    return items or None


def parse_uniprot_entry(body: Mapping[str, Any], url: str, *, origin: str, release: str | None,
                        release_date: str | None) -> list[dict]:
    accession = _text(body.get("primaryAccession"))
    entry_type = _text(body.get("entryType"))
    if accession is None or entry_type is None:
        raise LifeSciFormatError("schema_drift", "UniProt entry lacks primaryAccession or entryType")
    audit = dict(body.get("entryAudit") or {})
    page = f"https://www.uniprot.org/uniprotkb/{accession}/entry"
    source = _source(page, "uniprot", origin, api_url=url, release=release, release_date=release_date)
    if entry_type.casefold() == "inactive":
        reason = dict(body.get("inactiveReason") or {})
        kind = _text(reason.get("inactiveReasonType"))
        successors = [str(a) for a in reason.get("mergeDemergeTo") or []]
        published = {"accession": accession, "entry_type": entry_type, "entry_status": "obsolete",
                     "entry_name": _text(body.get("uniProtkbId")), "release": release,
                     "inactive_reason": {"type": kind, "successors": successors}}
        return [statement("protein", "uniprot", accession, subject_name=accession, as_published=published,
                          source=source, event="obsoleted", effective_date=release_date,
                          date_basis=f"inactive ({kind}) as of UniProt release {release or 'unstated'}")]
    reviewed = "unreviewed" not in entry_type.casefold() and "reviewed" in entry_type.casefold()
    organism = dict(body.get("organism") or {})
    description = dict(body.get("proteinDescription") or {})
    name = dict(dict(description.get("recommendedName") or {}).get("fullName") or {}).get("value") or \
        next((dict(s.get("fullName") or {}).get("value") for s in description.get("submissionNames") or []), None)
    sequence = dict(body.get("sequence") or {})
    xrefs = [{"database": _text(x.get("database")), "id": _text(x.get("id")),
              "properties": {str(p.get("key")): p.get("value") for p in x.get("properties") or []
                             if isinstance(p, Mapping)} or None}
             for x in body.get("uniProtKBCrossReferences") or [] if isinstance(x, Mapping)]
    published = {
        "accession": accession, "entry_type": entry_type, "entry_status": "active", "reviewed": reviewed,
        "entry_name": _text(body.get("uniProtkbId")), "entry_version": audit.get("entryVersion"),
        "sequence_version": audit.get("sequenceVersion"), "release": release, "protein_name": _text(name),
        "gene_names": [g["geneName"]["value"] for g in body.get("genes") or []
                       if isinstance(g, Mapping) and dict(g.get("geneName") or {}).get("value")] or None,
        "organism": {"taxon_id": _text(organism.get("taxonId")),
                     "scientific_name": _text(organism.get("scientificName"))} if organism else None,
        "sequence": {"value": sequence["value"], "length": sequence.get("length"),
                     "mol_weight": sequence.get("molWeight"), "crc64": sequence.get("crc64"),
                     "md5": sequence.get("md5")} if sequence.get("value") else None,
        "secondary_accessions": [str(a) for a in body.get("secondaryAccessions") or []] or None,
        "cross_references": [x for x in xrefs if x["database"] and x["id"]] or None,
        "citations": _uniprot_citations(body), "first_public": _day(audit.get("firstPublicDate")),
        "last_annotation_update": _day(audit.get("lastAnnotationUpdateDate")),
        "last_sequence_update": _day(audit.get("lastSequenceUpdateDate")),
    }
    return [statement("protein", "uniprot", accession, subject_name=_text(name) or accession, as_published=published,
                      source=source, effective_date=release_date)]


def parse_unisave(body: Mapping[str, Any], url: str, *, origin: str, accession: str, limit: int) -> list[dict]:
    rows = body.get("results")
    if not isinstance(rows, list):
        raise LifeSciFormatError("schema_drift", "UniSave history lacks results")
    out = []
    ordered = sorted((r for r in rows if isinstance(r, Mapping)), key=lambda r: -int(r.get("entryVersion") or 0))
    for row in ordered[:limit]:
        version, seq = row.get("entryVersion"), row.get("sequenceVersion")
        if type(version) is not int or type(seq) is not int:
            raise LifeSciFormatError("schema_drift", "UniSave row lacks entryVersion or sequenceVersion")
        page = f"https://rest.uniprot.org/unisave/{accession}?format=txt&versions={version}"
        out.append(statement(
            "entry_version", "uniprot", f"{accession}:{version}", subject_name=accession,
            source=_source(page, "uniprot", origin, api_url=url),
            as_published={"accession": accession, "entry_version": version, "sequence_version": seq,
                          "database": _text(row.get("database")) or "unstated",
                          "first_release": _text(row.get("firstRelease")),
                          "first_release_date": _day(row.get("firstReleaseDate")),
                          "last_release": _text(row.get("lastRelease")),
                          "last_release_date": _day(row.get("lastReleaseDate")),
                          "entry_name": _text(row.get("name"))}))
    return out


def _report(body: Mapping[str, Any], what: str) -> Mapping[str, Any]:
    reports = body.get("reports")
    if not isinstance(reports, list) or len(reports) != 1 or not isinstance(reports[0], Mapping):
        raise LifeSciFormatError("schema_drift", f"NCBI {what} response is not one report")
    return reports[0]


def parse_ncbi_gene(body: Mapping[str, Any], url: str, *, origin: str, queried: str) -> list[dict]:
    report = _report(body, "gene")
    page = f"https://www.ncbi.nlm.nih.gov/gene/{queried}"
    warning = dict(report.get("warning") or {})
    code = str(warning.get("gene_warning_code") or "").upper()
    if code in {"REPLACED", "DISCONTINUED"}:  # verify: how Datasets reports a secondary or retired Gene ID
        replaced = _text(dict(warning.get("replaced_id") or {}).get("gene_id"))
        return [statement(
            "gene", "ncbi", queried, subject_name=_text(warning.get("symbol")) or queried,
            source=_source(page, "ncbi", origin, api_url=url),
            as_published={"gene_id": queried, "symbol": _text(warning.get("symbol")) or "unstated",
                          "tax_id": _text(warning.get("tax_id")) or "0",
                          "status": "replaced" if code == "REPLACED" else "discontinued", "replaced_by": replaced,
                          "description": _text(warning.get("message"))},
            event="obsoleted", date_basis=f"NCBI reports Gene ID {queried} as {code.lower()}")]
    gene = dict(report.get("gene") or {})
    gene_id, symbol, tax_id = _text(gene.get("gene_id")), _text(gene.get("symbol")), _text(gene.get("tax_id"))
    if gene_id is None or symbol is None or tax_id is None:
        raise LifeSciFormatError("schema_drift", "NCBI gene report lacks gene_id, symbol or tax_id")
    refs = [{"database": "UniProtKB/Swiss-Prot", "id": str(a)} for a in gene.get("swiss_prot_accessions") or []]
    refs += [{"database": "Ensembl", "id": str(a)} for a in gene.get("ensembl_gene_ids") or []]
    return [statement("gene", "ncbi", gene_id, subject_name=symbol,
                      source=_source(f"https://www.ncbi.nlm.nih.gov/gene/{gene_id}", "ncbi", origin, api_url=url),
                      as_published={"gene_id": gene_id, "symbol": symbol, "tax_id": tax_id, "status": "live",
                                    "description": _text(gene.get("description")),
                                    "organism_name": _text(gene.get("taxname")), "gene_type": _text(gene.get("type")),
                                    "chromosomes": [str(c) for c in gene.get("chromosomes") or []] or None,
                                    "cross_references": refs or None})]


def parse_ncbi_taxon(body: Mapping[str, Any], url: str, *, origin: str, queried: str) -> list[dict]:
    report = _report(body, "taxonomy")
    taxonomy = dict(report.get("taxonomy") or {})
    tax_id = _text(taxonomy.get("tax_id"))
    name = dict(taxonomy.get("current_scientific_name") or {})
    scientific, rank = _text(name.get("name")), _text(taxonomy.get("rank"))
    if tax_id is None or scientific is None or rank is None:
        raise LifeSciFormatError("schema_drift", "NCBI taxonomy report lacks tax_id, scientific name or rank")
    lineage = [{"tax_id": _text(node.get("id")), "name": _text(node.get("name")), "rank": rank_name}
               for rank_name, node in dict(taxonomy.get("classification") or {}).items()
               if isinstance(node, Mapping) and node.get("id") is not None]
    parents = [str(p) for p in taxonomy.get("parents") or []]
    known = {n["tax_id"] for n in lineage}
    lineage = [{"tax_id": p, "name": None, "rank": None} for p in parents if p not in known] + lineage \
        if parents else lineage
    browser = "https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id="
    out = [statement("taxon", "ncbi", tax_id, subject_name=scientific,
                     source=_source(browser + tax_id, "ncbi", origin, api_url=url),
                     as_published={"tax_id": tax_id, "scientific_name": scientific, "rank": rank.casefold(),
                                   "status": "active", "authority": _text(name.get("authority")),
                                   "lineage": lineage or None, "genetic_code": _text(taxonomy.get("genetic_code_id"))})]
    if queried != tax_id:  # verify: the queried (merged) Tax ID answered with its current node
        out.append(statement("taxon", "ncbi", queried, subject_name=scientific,
                             source=_source(browser + queried, "ncbi", origin, api_url=url),
                             as_published={"tax_id": queried, "scientific_name": scientific, "rank": rank.casefold(),
                                           "status": "merged", "merged_into": tax_id},
                             event="obsoleted", date_basis=f"NCBI answers Tax ID {queried} with Tax ID {tax_id}"))
    return out


def parse_pdb_entry(body: Mapping[str, Any], url: str, *, origin: str,
                    entities: Sequence[Mapping[str, Any]]) -> list[dict]:
    pdb_id = _text(body.get("rcsb_id"))
    if pdb_id is None or not PDB_ID.fullmatch(pdb_id):
        raise LifeSciFormatError("schema_drift", "RCSB entry lacks a PDB rcsb_id (computed models are excluded)")
    info = dict(body.get("rcsb_accession_info") or {})
    history = []
    details: dict[Any, list[str]] = {}
    for item in body.get("pdbx_audit_revision_details") or []:
        details.setdefault(item.get("revision_ordinal"), []).append(_text(item.get("type")) or "unstated")
    for item in body.get("pdbx_audit_revision_history") or []:
        history.append({"ordinal": item.get("ordinal"), "major": item.get("major_revision"),
                        "minor": item.get("minor_revision"), "date": _day(item.get("revision_date")),
                        "data_content_type": _text(item.get("data_content_type")),
                        "types": details.get(item.get("ordinal")) or None})
    citation = dict(body.get("rcsb_primary_citation") or {})
    entry_info = dict(body.get("rcsb_entry_info") or {})
    supersedes = sorted({_text(s.get("replace_pdb_id")) for s in body.get("pdbx_database_PDB_obs_spr") or []
                         if str(s.get("id") or "").upper() == "SPRSDE" and _text(s.get("replace_pdb_id"))})
    mapped = []
    for entity in entities:
        ids = dict(entity.get("rcsb_polymer_entity_container_identifiers") or {})
        accessions = [str(a) for a in ids.get("uniprot_ids") or []]
        accessions += [str(r.get("database_accession")) for r in ids.get("reference_sequence_identifiers") or []
                       if str(r.get("database_name") or "").casefold() == "uniprot"
                       and str(r.get("database_accession")) not in accessions]
        mapped.append({"entity_id": _text(ids.get("entity_id")),
                       "description": _text(dict(entity.get("rcsb_polymer_entity") or {}).get("pdbx_description")),
                       "uniprot_accessions": accessions, "basis": "as stated by the PDB entity container identifiers"})
    published = {
        "pdb_id": pdb_id, "status": "released", "title": _text(dict(body.get("struct") or {}).get("title")),
        "experimental_methods": [_text(e.get("method")) for e in body.get("exptl") or [] if _text(e.get("method"))]
        or None,
        "resolution_angstrom": [_as_published(r) for r in entry_info.get("resolution_combined") or []] or None,
        "deposit_date": _day(info.get("deposit_date")), "initial_release_date": _day(info.get("initial_release_date")),
        "revision": {"major": info.get("major_revision"), "minor": info.get("minor_revision"),
                     "date": _day(info.get("revision_date"))} if info.get("major_revision") is not None else None,
        "revision_history": history or None, "entities": mapped or None,
        "primary_citation": {"title": _text(citation.get("title")), "journal": _text(citation.get("journal_abbrev")),
                             "year": _as_published(citation.get("year")),
                             "doi": _text(citation.get("pdbx_database_id_DOI")),
                             "pubmed_id": _as_published(citation.get("pdbx_database_id_PubMed"))} if citation else None,
        "supersedes": supersedes or None,
    }
    return [statement("structure", "pdb", pdb_id, subject_name=published["title"] or pdb_id,
                      source=_source(f"https://www.rcsb.org/structure/{pdb_id}", "pdb", origin, api_url=url),
                      as_published=published, effective_date=(published["revision"] or {}).get("date"))]


def parse_pdb_removed(body: Mapping[str, Any], url: str, *, origin: str, pdb_id: str) -> list[dict]:
    removed = dict(body.get("rcsb_repository_holdings_removed") or {})
    if not removed:
        raise LifeSciFormatError("schema_drift", "RCSB removed holdings lack rcsb_repository_holdings_removed")
    replaced = [str(i) for i in removed.get("id_codes_replaced_by") or []]
    return [statement("structure", "pdb", pdb_id, subject_name=_text(removed.get("title")) or pdb_id,
                      source=_source(f"https://www.rcsb.org/structure/removed/{pdb_id}", "pdb", origin, api_url=url),
                      as_published={"pdb_id": pdb_id, "status": "obsolete", "title": _text(removed.get("title")),
                                    "obsoleted_on": _day(removed.get("remove_date")),
                                    "superseded_by": replaced or None},
                      event="obsoleted", effective_date=_day(removed.get("remove_date")),
                      date_basis="removed from the PDB holdings on the published date")]


def _chembl_refs(body: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    refs = [{"database": _text(x.get("xref_src")), "id": _text(x.get("xref_id")), "name": _text(x.get("xref_name"))}
            for x in body.get("cross_references") or [] if isinstance(x, Mapping)]
    return [r for r in refs if r["database"] and r["id"]] or None


def _chembl_page(kind: str, key: str) -> str:
    return f"https://www.ebi.ac.uk/chembl/explore/{kind}/{key}"


def parse_chembl_release(body: Mapping[str, Any]) -> dict[str, Any]:
    release = _text(body.get("chembl_db_version"))
    if release is None or not release.startswith("ChEMBL_"):
        raise LifeSciFormatError("schema_drift", "ChEMBL status lacks chembl_db_version")
    return {"release": release, "released": _day(body.get("chembl_release_date"))}


def parse_chembl_target(body: Mapping[str, Any], url: str, *, origin: str, release: Mapping[str, Any]) -> list[dict]:
    key, name, kind = _text(body.get("target_chembl_id")), _text(body.get("pref_name")), _text(body.get("target_type"))
    if key is None or name is None or kind is None:
        raise LifeSciFormatError("schema_drift", "ChEMBL target lacks target_chembl_id, pref_name or target_type")
    components = [{"accession": _text(c.get("accession")), "component_type": _text(c.get("component_type")),
                   "relationship": _text(c.get("relationship"))}
                  for c in body.get("target_components") or [] if isinstance(c, Mapping)]
    return [statement("target", "chembl", key, subject_name=name,
                      source=_source(_chembl_page("target", key), "chembl", origin, api_url=url,
                                     release=release["release"]),
                      as_published={"target_chembl_id": key, "release": release["release"], "pref_name": name,
                                    "target_type": kind, "organism": _text(body.get("organism")),
                                    "tax_id": _as_published(body.get("tax_id")), "components": components or None,
                                    "cross_references": _chembl_refs(body)},
                      effective_date=release.get("released"))]


def parse_chembl_molecule(body: Mapping[str, Any], url: str, *, origin: str,
                          release: Mapping[str, Any]) -> list[dict]:
    key = _text(body.get("molecule_chembl_id"))
    if key is None:
        raise LifeSciFormatError("schema_drift", "ChEMBL molecule lacks molecule_chembl_id")
    structures = dict(body.get("molecule_structures") or {})
    return [statement("compound", "chembl", key, subject_name=_text(body.get("pref_name")) or key,
                      source=_source(_chembl_page("compound", key), "chembl", origin, api_url=url,
                                     release=release["release"]),
                      as_published={"molecule_chembl_id": key, "release": release["release"],
                                    "pref_name": _text(body.get("pref_name")),
                                    "molecule_type": _text(body.get("molecule_type")),
                                    "standard_inchikey": _text(structures.get("standard_inchi_key")),
                                    "standard_inchi": _text(structures.get("standard_inchi")),
                                    "canonical_smiles": _text(structures.get("canonical_smiles")),
                                    "cross_references": _chembl_refs(body)},
                      effective_date=release.get("released"))]


def parse_chembl_activities(body: Mapping[str, Any], url: str, *, origin: str,
                            release: Mapping[str, Any]) -> tuple[list[dict], bool]:
    rows, meta = body.get("activities"), body.get("page_meta")
    if not isinstance(rows, list) or not isinstance(meta, Mapping):
        raise LifeSciFormatError("schema_drift", "ChEMBL activity page lacks activities or page_meta")
    out = []
    for row in rows:
        activity_id = _as_published(row.get("activity_id"))
        if activity_id is None:
            raise LifeSciFormatError("schema_drift", "ChEMBL activity lacks activity_id")
        out.append(statement(
            "activity", "chembl", activity_id,
            subject_name=f"{row.get('standard_type') or row.get('type')} of {row.get('molecule_chembl_id')} on "
                         f"{row.get('target_chembl_id')}",
            source=_source(url, "chembl", origin, api_url=url, release=release["release"]),
            as_published={
                "activity_id": activity_id, "release": release["release"],
                "assay_chembl_id": _text(row.get("assay_chembl_id")), "assay_type": _text(row.get("assay_type")),
                "assay_description": _text(row.get("assay_description")),
                "target_chembl_id": _text(row.get("target_chembl_id")),
                "molecule_chembl_id": _text(row.get("molecule_chembl_id")),
                "document_chembl_id": _text(row.get("document_chembl_id")),
                "published": {"type": _as_published(row.get("type")), "relation": _as_published(row.get("relation")),
                              "value": _as_published(row.get("value")), "units": _as_published(row.get("units"))},
                "standard": {"type": _as_published(row.get("standard_type")),
                             "relation": _as_published(row.get("standard_relation")),
                             "value": _as_published(row.get("standard_value")),
                             "units": _as_published(row.get("standard_units")),
                             "flag": row.get("standard_flag")},
                "data_validity_comment": _text(row.get("data_validity_comment")),
                "data_validity_description": _text(row.get("data_validity_description")),
                "activity_comment": _text(row.get("activity_comment")),
                "document_citation": {"journal": _text(row.get("document_journal")),
                                      "year": _as_published(row.get("document_year"))}},
            effective_date=release.get("released")))
    return out, meta.get("next") in (None, "")


def parse_chembl_document(body: Mapping[str, Any], url: str, *, origin: str,
                          release: Mapping[str, Any]) -> list[dict]:
    key = _text(body.get("document_chembl_id"))
    if key is None:
        raise LifeSciFormatError("schema_drift", "ChEMBL document lacks document_chembl_id")
    return [statement("document", "chembl", key, subject_name=_text(body.get("title")) or key,
                      source=_source(_chembl_page("document", key), "chembl", origin, api_url=url,
                                     release=release["release"]),
                      as_published={"document_chembl_id": key, "release": release["release"],
                                    "doi": _text(body.get("doi")), "pubmed_id": _as_published(body.get("pubmed_id")),
                                    "title": _text(body.get("title")), "journal": _text(body.get("journal")),
                                    "year": _as_published(body.get("year")), "doc_type": _text(body.get("doc_type")),
                                    "volume": _text(body.get("volume")), "first_page": _text(body.get("first_page"))},
                      effective_date=release.get("released"))]


# ------------------------------------------------------------------ runtime adapter


class LifeSciSourceAdapter:
    """One page per selected entry, history, gene, taxon, structure, ChEMBL record or activity page."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.provider, self.entries = selection_entries(self.source)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self._secret = secret if self.provider == "ncbi" else None
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "life_sciences": {"provider": self.provider, "selected": len(self.entries),
                              "live_verification": LIVE_VERIFICATION[self.provider]["status"],
                              "minimisation": MINIMISATION["decision"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "life-sciences runs fetch the declared selection only")

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[int, Any, str, str, dict[str, Any]]:
        base = self.source["endpoint"].rstrip("/") + path
        url = base + ("?" + urlencode(sorted(query.items())) if query else "")
        headers = {"Accept": "application/json"}
        if self._secret:
            headers["api-key"] = self._secret  # NCBI Datasets v2 header; never part of the URL
        response = self.transport(url=base, params=dict(query), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        if (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold() != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        origin = "fixture" if response.get("origin") == "fixture" else "live"
        folded = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        if status == 404:
            return status, None, url, origin, folded
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            raise SourcePackError("rate_limited", f"{self.provider} rate limit reached",
                                  retry_after_ms=_retry_after_ms(folded.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        if self._secret and len(self._secret) >= 8 and self._secret.encode() in raw:
            raise SourcePackError("schema_drift", "provider echoed a credential; response is not safe evidence")
        try:
            return status, json.loads(raw.decode("utf-8-sig")), url, origin, folded
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcePackError("schema_drift", "response is not UTF-8 JSON") from exc

    def _chembl_release(self, carry: Mapping[str, Any]) -> dict[str, Any]:
        release = carry.get("release")
        if not release:
            raise SourcePackError("invalid_manifest", "ChEMBL records are read after their release")
        return dict(release)

    def _page(self, entry: Mapping[str, Any], carry: dict[str, Any]) -> dict[str, Any]:
        role, path, query = request_for(self.provider, entry)
        _, body, url, origin, headers = self._get(path, query)
        result: dict[str, Any] = {"url": url, "origin": origin, "statements": [], "personal": [], "excluded": [],
                                  "snapshot": None, "requests": 1, "release": None}
        if body is None and self.provider != "pdb":
            result["outcome"] = "not_found"
            return result
        result["outcome"] = "found"
        if self.provider == "uniprot":
            result["personal"] = _personal("uniprot", body)
            if role == "history":
                limit = int(entry.get("max_versions") or MAX_VERSIONS)
                result["statements"] = parse_unisave(body, url, origin=origin, accession=str(entry["accession"]),
                                                     limit=limit)
            else:
                result["release"] = _text(headers.get("x-uniprot-release"))
                result["excluded"] = [f"uniprot:{k}" for k in _UNIPROT_NOT_STORED if k in body]
                result["statements"] = parse_uniprot_entry(
                    body, url, origin=origin, release=result["release"],
                    release_date=_day(headers.get("x-uniprot-release-date")))
        elif self.provider == "ncbi":
            parser = parse_ncbi_gene if role == "gene" else parse_ncbi_taxon
            result["statements"] = parser(body, url, origin=origin, queried=str(entry["id"]))
        elif self.provider == "pdb":
            pdb_id = str(entry["pdb_id"])
            if body is None:
                _, removed, removed_url, _, _ = self._get(f"/rest/v1/holdings/removed/{quote(pdb_id)}", {})
                result["requests"] += 1
                if removed is None:
                    result["outcome"] = "not_found"
                    return result
                result["statements"] = parse_pdb_removed(removed, removed_url, origin=origin, pdb_id=pdb_id)
                return result
            result["personal"] = _personal("pdb", body)
            entity_ids = [str(e) for e in dict(body.get("rcsb_entry_container_identifiers") or {}).get(
                "polymer_entity_ids") or []][:MAX_ENTITIES]
            entities = []
            for entity_id in entity_ids:
                _, entity, _, _, _ = self._get(f"/rest/v1/core/polymer_entity/{quote(pdb_id)}/{quote(entity_id)}", {})
                result["requests"] += 1
                if entity is not None:
                    entities.append(entity)
            result["statements"] = parse_pdb_entry(body, url, origin=origin, entities=entities)
            revision = result["statements"][0]["as_published"].get("revision") or {}
            result["release"] = f"{revision.get('major')}.{revision.get('minor')}" if revision else None
        else:
            result["personal"] = _personal("chembl", body)
            if role == "release":
                carry["release"] = parse_chembl_release(body)
                result["release"] = carry["release"]["release"]
                return result
            release = self._chembl_release(carry)
            result["release"] = release["release"]
            if role == "target":
                result["statements"] = parse_chembl_target(body, url, origin=origin, release=release)
            elif role == "molecule":
                result["excluded"] = sorted(f"molecule:{k}" for k in set(body) & _EXCLUDED_CHEMBL)
                result["statements"] = parse_chembl_molecule(body, url, origin=origin, release=release)
            elif role == "document":
                result["statements"] = parse_chembl_document(body, url, origin=origin, release=release)
            else:
                statements, complete = parse_chembl_activities(body, url, origin=origin, release=release)
                result["excluded"] = sorted({f"activity:{k}" for row in body["activities"]
                                             for k in set(row) & _EXCLUDED_CHEMBL})
                documents = sorted({s["as_published"]["document_chembl_id"] for s in statements})
                for document_id in documents[:MAX_DOCUMENTS]:
                    _, document, document_url, _, _ = self._get(f"/document/{quote(document_id)}.json", {})
                    result["requests"] += 1
                    if document is not None:
                        result["personal"] += _personal("document", document)
                        statements += parse_chembl_document(document, document_url, origin=origin, release=release)
                result["statements"] = statements
                result["snapshot"] = {"selection_key": selection_key(self.provider, entry), "provider": "chembl",
                                      "release": release["release"], "complete": complete, "url": url}
        return result

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        state = {"i": 0, "carry": {}} if cursor is None else json.loads(cursor)
        if state.get("scope") not in (None, self.source["source_hash"]):
            raise SourcePackError("cursor_drift", "cursor belongs to a different selection")
        index = int(state.get("i", -1))
        if not 0 <= index < len(self.entries):
            raise SourcePackError("cursor_drift", "cursor names no declared selection")
        entry = self.entries[index]
        carry = dict(state.get("carry") or {})
        try:
            page = self._page(entry, carry)
        except (LifeSciFormatError, LifeSciError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, SourcePackError):
                raise
            raise SourcePackError("schema_drift", f"{getattr(exc, 'code', 'parse')}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(page["statements"]) > limit:
            raise SourcePackError("budget_exhausted", "selection has more statements than the run's result budget")
        records = []
        for item in page["statements"]:
            content = json.dumps(item, sort_keys=True, ensure_ascii=False)
            records.append({
                "id": f"{item['subject']['key']}|{item['record_type']}|{item['record_key']}|"
                      + hashlib.sha256(content.encode()).hexdigest()[:12],
                "title": f"{item['subject'].get('name') or item['subject']['key']}: {item['record_type']}",
                "url": item["source"]["url"], "language": "en", "content": content, "lifesci_record": item})
        label = {k: entry[k] for k in sorted(entry) if k in {"kind", "accession", "id", "pdb_id", "chembl_id",
                                                              "target_chembl_id", "label"}}
        receipt = {"status": 200, "provider": self.provider, "selection": label, "outcome": page["outcome"],
                   "statements": len(records), "requests": page["requests"], "release": page["release"],
                   "personal_fields_dropped": sorted(set(page["personal"])),
                   "excluded_fields_dropped": sorted(set(page["excluded"])), "evidence_origin": page["origin"],
                   "final_page": index + 1 >= len(self.entries), "snapshot": page["snapshot"],
                   "live_verification": LIVE_VERIFICATION[self.provider]["status"]}
        next_cursor = (json.dumps({"i": index + 1, "scope": self.source["source_hash"], "carry": carry},
                                  sort_keys=True) if index + 1 < len(self.entries) else None)
        return RuntimePage(tuple(records), next_cursor, sum(len(r["content"]) for r in records), receipt=receipt)


FIXTURE_SECRET = "fixture-ncbi-key-not-a-real-key"
ADAPTERS = {CONNECTOR: LifeSciSourceAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path and sorted query; responses are marked as fixture evidence."""
    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout, **_):
        del headers, timeout
        parts = urlsplit(url)
        key = parts.path + ("?" + urlencode(sorted(dict(params or {}).items())) if params else "")
        page = by_key.get(key)
        if page is None:
            return {"status": 404, "headers": {}, "content": b"", "origin": "fixture"}
        body = page.get("body")
        content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture"}

    return transport


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = LifeSciSourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                   secret=FIXTURE_SECRET)
    records, cursor = [], None
    while True:
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "EXCLUSIONS", "FIXTURE_SECRET", "LIVE_VERIFICATION",
    "NOT_IMPLEMENTED", "PROVIDERS", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS", "LifeSciFormatError",
    "LifeSciSourceAdapter", "fixture_transport", "parse_chembl_activities", "parse_chembl_document",
    "parse_chembl_molecule", "parse_chembl_release", "parse_chembl_target", "parse_ncbi_gene", "parse_ncbi_taxon",
    "parse_pdb_entry", "parse_pdb_removed", "parse_uniprot_entry", "parse_unisave", "replay_native_fixture",
    "request_for", "selection_entries", "selection_key",
]
