"""Research-entity registry sources for the Science ``research-entities`` features (#2579).

Four providers are recorded under an access contract (:data:`PROVIDER_CONTRACTS`, RE01 #2584) and implemented as
formats of the ``research-entities`` source-pack connector:

* **ROR** (``ror``, format ``ror-dump-zip``, RE03 #2594) - one declared ROR data-dump release (a Zenodo file: a zip
  holding the schema v2 JSON) per document. Only the declared ROR IDs are read from it; each release is a vintage.
  Status (``active``, ``inactive``, ``withdrawn``), relationships (``parent``, ``child``, ``related``,
  ``predecessor``, ``successor``) and external identifiers (GRID, ISNI, Wikidata, FundRef) are kept as published. A
  declared ID the release does not contain is reported as ``not_in_release``.
* **ORCID** (``orcid``, format ``orcid-record-json``, RE04 #2601) - one public ORCID record (``/v3.0/{iD}/record`` on
  the Public API) per document. Only the fields the RE01 data-minimisation decision allows are kept
  (:data:`ORCID_KEPT` / :data:`ORCID_EXCLUDED`): the iD, the public display name, public employments (organisation,
  its disambiguated identifier, start and end) and public works as asserted identifiers (DOI, arXiv, PMID), with the
  record's last-modified time as the revision marker. Works are what the researcher asserts, never authorship facts.
* **DataCite** (``datacite``, format ``datacite-doi-json``, RE05 #2606) - one DOI (``/dois/{doi}``) or one bounded
  query page per document; ``metadataVersion`` and ``updated`` mark the metadata version, related identifiers are
  kept exactly as published, and creators keep only an ORCID iD, an organisational name and affiliation identifiers.
* **CORDIS** (``cordis``, format ``cordis-csv-zip``, RE06 #2609) - one declared CORDIS bulk file (a zip of
  semicolon-separated CSVs) per document; only the declared project IDs are read, keyed by project ID and programme,
  with every participant's PIC, name, role and contributions as published and the currency the document declares.

Every provider is ``unverified-live`` until a dated live run (RE14, #2649); endpoint, file and field names marked
*verify* come from the providers' public documentation as audited in
``docs/development/research-entities-evidence/source-audit.md``. Nothing here ranks researchers or organisations,
counts citations, infers affiliation from co-authorship, or matches authors by name.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "research-entities"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RELEASE_CONTRACT = "noesis-research-entities-release-v1"
RECORD_CONTRACT = "noesis-research-entity-record-v1"
NEVER_SENTENCE = (
    "Registry records as ROR, ORCID, DataCite and CORDIS published them, each with source, record revision and as-of "
    "time: no researcher rankings or metrics, no inference of affiliation from co-authorship, no author "
    "disambiguation by name and no personal data beyond the public ORCID fields the minimisation decision allows."
)
EXCLUSIONS = (
    "researcher rankings, league tables or metrics (h-index, citation or usage counts, impact scores)",
    "inference of affiliation from co-authorship",
    "author disambiguation or matching by name",
    "personal data beyond the public ORCID fields the data-minimisation decision allows",
    "entity merges of researchers",
    "summing contributions across currencies or converting currencies",
    "inferred collaboration or influence links",
)

PROVIDERS = ("ror", "orcid", "datacite", "cordis")
PROVIDER_HOSTS = {
    "ror": {"zenodo.org"},
    "orcid": {"pub.orcid.org"},
    "datacite": {"api.datacite.org"},
    "cordis": {"cordis.europa.eu"},
}
FORMATS = {
    "ror-dump-zip": {"provider": "ror", "record_kind": "organisation"},
    "orcid-record-json": {"provider": "orcid", "record_kind": "researcher"},
    "datacite-doi-json": {"provider": "datacite", "record_kind": "dataset"},
    "cordis-csv-zip": {"provider": "cordis", "record_kind": "project"},
}
RECORD_KINDS = ("organisation", "researcher", "dataset", "project")
ROR_STATUSES = ("active", "inactive", "withdrawn")
ROR_RELATIONSHIPS = ("parent", "child", "related", "predecessor", "successor")
ROR_EXTERNAL_TYPES = ("grid", "isni", "wikidata", "fundref")
REMOVAL_STATUSES = ("not_in_release", "not_found", "deactivated", "locked")
# Work identifier types kept from ORCID works (all bibliographic, none personal).
ORCID_WORK_IDS = ("doi", "arxiv", "pmid")

# RE01 data-minimisation decision: the only researcher fields ever stored (enforced by the store at write time).
ORCID_KEPT = {
    "record": ("orcid", "display_name", "employments", "works"),
    "employment": ("put_code", "organisation", "start", "end", "last_modified"),
    "organisation": ("name", "city", "country", "disambiguated"),
    "work": ("put_code", "title", "type", "publication_year", "identifiers", "last_modified"),
}
ORCID_EXCLUDED = (
    "emails",
    "addresses",
    "biography",
    "keywords",
    "other-names",
    "researcher-urls",
    "person external-identifiers",
    "educations",
    "qualifications",
    "invited-positions",
    "distinctions",
    "memberships",
    "services",
    "fundings",
    "peer-reviews",
    "research-resources",
    "employment department-name and role-title",
    "work contributors, citations, journal titles and URLs",
    "items whose visibility is not public",
)
DATACITE_CREATOR_KEPT = ("position", "name_type", "name", "orcid", "affiliation_identifiers")
DATACITE_EXCLUDED = (
    "personal creator and contributor names",
    "given and family names",
    "affiliation names of personal creators (only published affiliation identifiers are kept)",
    "contributors",
    "citationCount, viewCount, downloadCount and other usage metrics",
    "descriptions and subjects beyond titles",
)
CORDIS_EXCLUDED = ("street", "postCode", "geolocation", "contactForm", "organizationURL outside website-domain evidence",
                   "objective texts")

PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "ror": {
        "delivers": "research organisations keyed by ROR ID with names, types, status, relationships, successors and "
        "external identifiers, per data-dump release",
        "access_decision": "unverified-live",
        "reason": "the ROR data dump is published on Zenodo under CC0 without authentication; the release file and "
        "member names and the dump size against the byte budget are not yet checked live from this runtime",
        "access": "bulk file: one ROR data-dump release (zip holding the schema v2 JSON) per declared document",
        "entry_points": ["https://zenodo.org/records/{record}/files/{file} (verify)",
                         "concept DOI https://doi.org/10.5281/zenodo.6347574 (all releases)"],
        "format": "zip holding a JSON array of ROR schema v2 records (a CSV copy is not read)",
        "authentication": "none",
        "key_handling": "no credential",
        "rate_limits": "Zenodo download limits apply (not documented per file; verify); one download per declared "
        "release, bounded by max_bytes (100 MB ceiling; verify the dump size) and the declared ROR IDs",
        "identifiers": {"organisation": "ROR ID (https://ror.org/0xxxxxxxx)",
                        "external": "GRID, ISNI, Wikidata, FundRef as published in external_ids"},
        "licence": "CC0 1.0 (ROR data dump)",
        "attribution": "Research Organization Registry (ROR), data dump release as named",
        "update_cadence": "roughly monthly data-dump releases, each with a version and date (verify)",
        "revision_model": "each release is a vintage; a record whose content changed between releases gains a "
        "revision; inactive and withdrawn records stay in the dump with their status and successors",
        "corrections_and_removals": "ROR does not delete records: a record is made inactive or withdrawn and names "
        "a successor where one exists; a declared ID missing from a release is recorded as not_in_release",
        "temporal_semantics": "the declared release date dates the vintage; admin.last_modified is kept as the "
        "provider's modification date",
        "personal_data": "none (organisation records)",
        "retained_evidence": "zip digest, member name, release version",
        "verify": ["Zenodo record and file names per release", "JSON member name", "dump size against max_bytes"],
        "sources": [
            {"url": "https://ror.readme.io/docs/data-dump", "read_on": "2026-09-30",
             "note": "not fetched (egress blocked); the web-search summary states CC0, all statuses in the dump"},
            {"url": "https://ror.readme.io/docs/zenodo", "read_on": "2026-09-30",
             "note": "not fetched; retrieval of releases from Zenodo (verify)"},
        ],
    },
    "orcid": {
        "delivers": "public ORCID records for a bounded list of iDs: display name, public employments and public "
        "works as asserted identifiers, with the record's last-modified time",
        "access_decision": "unverified-live",
        "reason": "the ORCID Public API (pub.orcid.org, v3.0) serves public data; its terms grant a non-commercial "
        "licence, so an operator must confirm non-commercial use (or use the CC0 annual public data file) before "
        "enabling it; not yet run live",
        "access": "api (ORCID Public API v3.0, JSON), one record per declared iD",
        "entry_points": ["https://pub.orcid.org/v3.0/{orcid}/record (verify)"],
        "format": "JSON (Accept: application/json)",
        "authentication": "optional /read-public bearer token (client credentials); anonymous reads have lower quotas",
        "key_handling": "the token is referenced as NOESIS_ORCID_READ_PUBLIC_TOKEN and resolved at run time; never "
        "stored in manifests, receipts or records",
        "rate_limits": "12 requests/second for the Public and Anonymous APIs (from February 2025), burst 40, and a "
        "usage quota of 100 000 reads a day per client (verify); one request per declared iD",
        "identifiers": {"researcher": "ORCID iD (ISO 7064 11,2 check digit)",
                        "works": "DOI, arXiv, PMID as external-ids with relationship self",
                        "employer": "disambiguated-organization identifier (ROR, GRID, Ringgold, FundRef)"},
        "licence": "ORCID Public API terms: limited royalty-free licence for non-commercial use; the annual public "
        "data file is CC0 (verify the current terms text)",
        "attribution": "ORCID, record as retrieved",
        "update_cadence": "records change whenever the researcher or a trusted party edits them",
        "revision_model": "history.last-modified-date is the revision marker; a changed record is a new revision",
        "corrections_and_removals": "an item made private disappears from the public record (a new revision without "
        "it); a deactivated record states a deactivation date; a locked record answers HTTP 409 and an unknown iD "
        "404 (verify): each becomes a removal revision without personal fields",
        "temporal_semantics": "the record's last-modified time dates the revision",
        "personal_data": "yes: governed by the data-minimisation decision in the audit (ORCID_KEPT / ORCID_EXCLUDED)",
        "retained_evidence": "response digest per record",
        "verify": ["path and media type", "deactivation and lock responses", "current terms text", "quotas"],
        "sources": [
            {"url": "https://info.orcid.org/documentation/features/public-api/", "read_on": "2026-09-30",
             "note": "not fetched (egress blocked); facts from the web-search summaries of the pages below"},
            {"url": "https://info.orcid.org/refining-api-traffic-management/", "read_on": "2026-09-30",
             "note": "search summary: 24 req/s reduced to 12 for Public/Anonymous APIs from Feb 2025; burst 40; "
             "100k reads a day per client"},
            {"url": "https://info.orcid.org/terms-of-use/", "read_on": "2026-09-30",
             "note": "search summary: Public API free for non-commercial use; public data file CC0"},
        ],
    },
    "datacite": {
        "delivers": "DataCite DOI metadata for declared DOIs or a bounded query page: titles, publisher, year, "
        "resource type, version, rights and related identifiers as published",
        "access_decision": "unverified-live",
        "reason": "the DataCite REST API serves findable DOI metadata without authentication under CC0; not yet run "
        "live from this runtime",
        "access": "api (DataCite REST API, JSON:API)",
        "entry_points": ["https://api.datacite.org/dois/{doi}?affiliation=true&publisher=true (verify)",
                         "https://api.datacite.org/dois?query=...&page[size]=N&affiliation=true (verify)"],
        "format": "JSON:API",
        "authentication": "none for retrieval; identified requests (mailto) get a higher limit",
        "key_handling": "no credential",
        "rate_limits": "3000 requests per 5 minutes per IP for authenticated, 1000 for identified and 500 for "
        "unidentified requests (verify); one request per declared document",
        "identifiers": {"dataset": "DOI", "creators": "ORCID nameIdentifier", "affiliations": "ROR "
                        "affiliationIdentifier"},
        "licence": "DataCite metadata is CC0 1.0 (the waiver covers the metadata, not the described datasets)",
        "attribution": "DataCite, DOI metadata",
        "update_cadence": "whenever the registering client updates the DOI metadata",
        "revision_model": "metadataVersion and updated mark a metadata version; a changed record is a new revision",
        "corrections_and_removals": "metadata updates raise metadataVersion; a DOI that is no longer findable "
        "answers 404 and becomes a removal revision",
        "temporal_semantics": "attributes.updated dates the metadata version",
        "personal_data": "creator names and affiliations: only ORCID iDs, organisational creator names and "
        "affiliation identifiers are kept (minimisation decision)",
        "retained_evidence": "response digest per call",
        "verify": ["affiliation and publisher parameters", "query syntax for related identifiers", "rate limits"],
        "sources": [
            {"url": "https://support.datacite.org/docs/api", "read_on": "2026-09-30",
             "note": "not fetched (egress blocked); facts from the web-search summaries of the pages below"},
            {"url": "https://support.datacite.org/docs/rate-limit", "read_on": "2026-09-30",
             "note": "search summary: 3000/1000/500 requests per 5 minutes per IP by tier"},
            {"url": "https://support.datacite.org/docs/datacite-metadata-license", "read_on": "2026-09-30",
             "note": "search summary: metadata waived under CC0 1.0"},
        ],
    },
    "cordis": {
        "delivers": "EU framework-programme projects keyed by project ID and programme with participants (PIC, "
        "name, role, activity type, country) and contributions as published",
        "access_decision": "unverified-live",
        "reason": "CORDIS bulk project files are published for reuse (CC BY 4.0 for EU-owned CORDIS content under "
        "the Commission reuse decision 2011/833/EU); file names, delimiter and number format not yet checked live",
        "access": "bulk file: one CORDIS zip of CSVs (project.csv, organization.csv) per declared document",
        "entry_points": ["https://cordis.europa.eu/data/cordis-HORIZONprojects-csv.zip (verify)",
                         "https://cordis.europa.eu/data/cordis-h2020projects-csv.zip (verify)",
                         ("catalogue: https://data.europa.eu/data/datasets/cordis-eu-research-projects-under-"
                          "horizon-europe-2021-2027")],
        "format": "zip of CSVs; delimiter and decimal separator declared per document (semicolon and comma; verify)",
        "authentication": "none",
        "key_handling": "no credential",
        "rate_limits": "not documented; one download per declared file, bounded by max_bytes and the declared "
        "project IDs",
        "identifiers": {"project": "CORDIS project id with frameworkProgramme", "participant": "PIC (organisationID) "
                        "and VAT number as published"},
        "licence": "CC BY 4.0 for EU-owned CORDIS content (Commission Decision 2011/833/EU); attribution to CORDIS "
        "(verify the file-level licence on data.europa.eu)",
        "attribution": "European Commission, CORDIS",
        "update_cadence": "bulk files regenerated periodically; each row states contentUpdateDate",
        "revision_model": "each declared file release is a vintage; a project whose content changed is a new "
        "revision; contentUpdateDate is kept as the provider's modification date",
        "corrections_and_removals": "corrected rows carry a later contentUpdateDate; a declared project missing from "
        "a later file is recorded as not_in_release",
        "temporal_semantics": "the project's contentUpdateDate, else the declared file date, dates the revision",
        "personal_data": "none kept: participants are organisations; addresses, contact forms and geolocations are "
        "dropped",
        "retained_evidence": "zip digest, member names, row numbers",
        "verify": ["file names per programme", "column names", "delimiter", "decimal separator", "licence"],
        "sources": [
            {"url": "https://cordis.europa.eu/about/services", "read_on": "2026-09-30",
             "note": "not fetched (egress blocked); facts from the web-search summaries of the pages below"},
            {"url": "https://cordis.europa.eu/about/legal", "read_on": "2026-09-30",
             "note": "search summary: CORDIS content reusable under CC BY 4.0 (Decision 2011/833/EU)"},
            {"url": "https://data.europa.eu/data/datasets/cordish2020projects?locale=en", "read_on": "2026-09-30",
             "note": "search summary: organization.csv columns projectID, organisationID, vatNumber, name, role, "
             "ecContribution, netEcContribution, totalCost, contentUpdateDate"},
        ],
    },
}
NOT_IMPLEMENTED = {
    "openaire-graph": {
        "decision": "not_implemented",
        "reason": "a gap-table candidate outside the tracker's initial sources; its graph deduplicates organisations "
        "and derives inferred author and affiliation relations, which the exclusions forbid unless filtered; needs "
        "its own audit before any use (not audited here)",
    },
    "orcid-member-api": {
        "decision": "not_implemented",
        "reason": "the Member API reads limited-visibility data; the minimisation decision allows public fields only",
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"], "note": "no dated live run from this runtime; offline fixtures "
               "only (RE14 #2649 records live evidence)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
# The bounded first coverage (RE01). No record set implies complete coverage of any provider.
BOUNDED_COVERAGE = {
    "ror": {"records": "at most 200 declared ROR IDs per release (the organisations of the journeys and their "
                       "declared relatives)", "releases": "the two most recent data-dump releases"},
    "orcid": {"records": "at most 50 declared ORCID iDs, each named by an operator for a reason recorded in the "
                         "source entry; no search, no crawling of co-authors", "revisions": "one fetch per run"},
    "datacite": {"records": "at most 100 declared DOIs, or query pages of at most 100 DOIs of resource type Dataset "
                            "for a declared repository client or related paper DOI", "revisions": "one fetch per run"},
    "cordis": {"programmes": ["HORIZON (2021-2027)", "H2020 (2014-2020)"],
               "records": "at most 200 declared project IDs per file", "releases": "the two most recent files"},
    "out_of_scope": "researcher rankings or metrics, citation and usage counts, co-authorship graphs and any "
    "personal data outside ORCID_KEPT are never acquired or stored",
}
MINIMISATION = {
    "decision": "RE01 data-minimisation decision (docs/development/research-entities-evidence/source-audit.md)",
    "orcid_kept": ORCID_KEPT,
    "orcid_excluded": list(ORCID_EXCLUDED),
    "datacite_creator_kept": list(DATACITE_CREATOR_KEPT),
    "datacite_excluded": list(DATACITE_EXCLUDED),
    "cordis_excluded": list(CORDIS_EXCLUDED),
    "retention": "revisions are immutable and kept for as-of answers; once ORCID reports a record deactivated, locked "
    "or unknown, answers withhold the display name of every earlier revision",
    "access": "researcher records are answered only with knowledge:research-entities:researchers in addition to the "
    "read scope; organisation, dataset and project records need the read scope",
    "merges": "researchers are never subjects of entity merges or identity matches",
}


class ResearchEntitiesFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def unverified(provider: str) -> bool:
    return PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"


def text(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return raw or None


def iso_day(value: Any) -> str | None:
    raw = text(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def iso_instant(value: Any) -> str | None:
    """An ISO instant (UTC) from ISO text or epoch milliseconds."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC).isoformat()
    raw = text(value)
    if raw is None:
        return None
    if raw.isdigit() and len(raw) > 8:
        return datetime.fromtimestamp(int(raw) / 1000, tz=UTC).isoformat()
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat()


# ------------------------------------------------------------------ identifiers

_ROR = re.compile(r"0[0-9a-hj-km-np-tv-z]{6}[0-9]{2}")
_ORCID = re.compile(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]")
_DOI = re.compile(r"10\.\d{4,9}/\S+")


def ror_id(value: Any) -> str:
    """A ROR ID as its canonical URL; refused when it is not a ROR ID."""
    raw = str(value or "").strip().removeprefix("https://ror.org/").removeprefix("ror.org/")
    if not _ROR.fullmatch(raw):
        raise ResearchEntitiesFormatError("invalid_identifier", "not a ROR identifier")
    return "https://ror.org/" + raw


def orcid_id(value: Any) -> str:
    """An ORCID iD (bare, ``0000-0000-0000-0000``) with a valid ISO 7064 11,2 check digit."""
    raw = str(value or "").strip()
    for prefix in ("https://orcid.org/", "http://orcid.org/", "orcid.org/"):
        raw = raw.removeprefix(prefix)
    raw = raw.upper()
    if not _ORCID.fullmatch(raw):
        raise ResearchEntitiesFormatError("invalid_identifier", "not an ORCID iD")
    total = 0
    for char in raw.replace("-", "")[:-1]:
        total = (total + int(char)) * 2
    check = (12 - total % 11) % 11
    if raw[-1] != ("X" if check == 10 else str(check)):
        raise ResearchEntitiesFormatError("invalid_identifier", "ORCID iD check digit does not match")
    return raw


def doi(value: Any) -> str:
    """A DOI in its case-insensitive normal form (lower case, no resolver prefix)."""
    raw = str(value or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi.org/", "doi:"):
        if raw.casefold().startswith(prefix):
            raw = raw[len(prefix):]
    raw = raw.casefold()
    if not _DOI.fullmatch(raw):
        raise ResearchEntitiesFormatError("invalid_identifier", "not a DOI")
    return raw


def _safe(parser: Callable[[Any], str], value: Any) -> str | None:
    try:
        return parser(value)
    except ResearchEntitiesFormatError:
        return None


def decimal_text(value: Any, separator: str = ".") -> str | None:
    """A published amount as exact decimal text; ``None`` for a missing or non-numeric value (never zero)."""
    raw = text(value)
    if raw is None:
        return None
    if separator == ",":
        raw = raw.replace(".", "").replace(" ", "").replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return format(number, "f")


# ------------------------------------------------------------------ declarations


def check_document(fmt: str, document: Mapping[str, Any]) -> None:
    if not text(document.get("label")):
        raise ResearchEntitiesFormatError("invalid_document", "a document has a label")
    release = dict(document.get("release") or {})
    if release and (iso_day(release.get("published_on")) is None or not text(release.get("label"))):
        raise ResearchEntitiesFormatError("invalid_document", "a declared release states its label and date")
    if fmt == "ror-dump-zip":
        if not re.fullmatch(r"\d{1,12}", str(document.get("record") or "")) or not text(document.get("file")) \
                or not text(document.get("member")) or not text(document.get("version")) or not release:
            raise ResearchEntitiesFormatError("invalid_document", "a ROR document names the Zenodo record, file, JSON "
                                              "member, version and release date")
        ids = list(document.get("ror_ids") or [])
        if not ids or len(ids) > 200 or any(_safe(ror_id, i) is None for i in ids):
            raise ResearchEntitiesFormatError("invalid_document", "a ROR document declares 1-200 ROR IDs")
    elif fmt == "orcid-record-json":
        if _safe(orcid_id, document.get("orcid")) is None:
            raise ResearchEntitiesFormatError("invalid_document", "an ORCID document declares one valid ORCID iD")
        if not text(document.get("reason")):
            raise ResearchEntitiesFormatError("invalid_document", "an ORCID document records why the researcher is "
                                              "in the bounded set")
    elif fmt == "datacite-doi-json":
        query = dict(document.get("query") or {})
        if bool(document.get("doi")) == bool(query):
            raise ResearchEntitiesFormatError("invalid_document", "a DataCite document names one DOI or one query")
        if document.get("doi") and _safe(doi, document["doi"]) is None:
            raise ResearchEntitiesFormatError("invalid_document", "a DataCite document names a valid DOI")
        if query and (set(query) - {"query", "resource_type_id", "client_id", "page_size"}
                      or not (text(query.get("query")) or text(query.get("client_id")))
                      or not 1 <= int(query.get("page_size") or 25) <= 100):
            raise ResearchEntitiesFormatError("invalid_document", "a DataCite query names a query or client and a "
                                              "page size of at most 100")
    elif fmt == "cordis-csv-zip":
        if not text(document.get("programme")) or not re.fullmatch(r"[A-Za-z0-9._-]+\.zip", str(document.get("file")
                                                                                              or "")):
            raise ResearchEntitiesFormatError("invalid_document", "a CORDIS document names its programme and zip file")
        if not text(document.get("project_member")) or not text(document.get("organization_member")) or not release:
            raise ResearchEntitiesFormatError("invalid_document", "a CORDIS document names its CSV members and "
                                              "release")
        ids = list(document.get("project_ids") or [])
        if not ids or len(ids) > 200 or not all(re.fullmatch(r"\d{1,12}", str(i)) for i in ids):
            raise ResearchEntitiesFormatError("invalid_document", "a CORDIS document declares 1-200 project IDs")
        if not re.fullmatch(r"[A-Z]{3}", str(document.get("currency") or "")):
            raise ResearchEntitiesFormatError("invalid_document", "a CORDIS document declares the currency of its "
                                              "amounts as the documentation states it")


def document_url(fmt: str, document: Mapping[str, Any], endpoint: str = "") -> str:
    if fmt == "ror-dump-zip":
        return f"https://zenodo.org/records/{document['record']}/files/{quote(str(document['file']))}"
    if fmt == "orcid-record-json":
        base = str(endpoint or "https://pub.orcid.org/v3.0").rstrip("/")
        return f"{base}/{orcid_id(document['orcid'])}/record"
    if fmt == "datacite-doi-json":
        base = str(endpoint or "https://api.datacite.org/dois").rstrip("/")
        if document.get("doi"):
            return f"{base}/{quote(doi(document['doi']), safe='/')}?" + urlencode(
                [("affiliation", "true"), ("publisher", "true")])
        query = dict(document["query"])
        params = [("affiliation", "true"), ("page[size]", str(int(query.get("page_size") or 25)))]
        if query.get("query"):
            params.append(("query", str(query["query"])))
        if query.get("resource_type_id"):
            params.append(("resource-type-id", str(query["resource_type_id"])))
        if query.get("client_id"):
            params.append(("client-id", str(query["client_id"])))
        return f"{base}?" + urlencode(sorted(params))
    if fmt == "cordis-csv-zip":
        return f"https://cordis.europa.eu/data/{document['file']}"
    raise ResearchEntitiesFormatError("invalid_document", f"no URL for format {fmt}")


def research_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("research_entities") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "research-entity sources declare a known provider and its format")
    documents = list(declared.get("documents") or [])
    if not documents:
        raise SourcePackError("invalid_manifest", "a research-entity source declares its documents")
    if len(documents) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared documents than the source's page budget")
    host = (urlsplit(source["endpoint"]).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[declared["provider"]]:
        raise SourcePackError("invalid_manifest", "the endpoint is not the provider's documented host")
    urls = []
    for document in documents:
        try:
            check_document(fmt, document)
            url = document_url(fmt, document, source["endpoint"])
        except ResearchEntitiesFormatError as exc:
            raise SourcePackError("invalid_manifest", str(exc)) from exc
        parts = urlsplit(url)
        if parts.scheme != "https" or (parts.hostname or "").casefold() != host:
            raise SourcePackError("invalid_manifest", "declared documents are HTTPS resources on the endpoint's host")
        urls.append(url)
    if len(set(urls)) != len(urls):
        raise SourcePackError("invalid_manifest", "each declared document is a distinct request")
    return declared


# ------------------------------------------------------------------ parsers


def _statement(kind: str, provider: str, native_id: str, status: str, revision: Mapping[str, Any],
               body: Mapping[str, Any], citation: Mapping[str, Any]) -> dict[str, Any]:
    return {"contract": RECORD_CONTRACT, "record_kind": kind, "provider": provider, "native_id": native_id,
            "status": status, "revision": dict(revision), "body": dict(body), "citation": dict(citation)}


def _zip_member(raw: bytes, member: str) -> bytes:
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ResearchEntitiesFormatError("schema_drift", "the release file is not a zip archive") from exc
    if member not in archive.namelist():
        raise ResearchEntitiesFormatError("schema_drift", f"the archive has no member {member}")
    info = archive.getinfo(member)
    if info.file_size > 2_000_000_000:
        raise ResearchEntitiesFormatError("input_limit", "the member exceeds the decompression limit")
    return archive.read(member)


def _ror_body(native: Mapping[str, Any]) -> dict[str, Any]:
    names = [{"value": str(n.get("value")), "types": sorted(str(t) for t in n.get("types") or []),
              "lang": n.get("lang")} for n in native.get("names") or [] if n.get("value")]
    if not names:
        raise ResearchEntitiesFormatError("schema_drift", "a ROR record has names")
    display = next((n["value"] for n in names if "ror_display" in n["types"]), names[0]["value"])
    relationships = []
    for relation in native.get("relationships") or []:
        if relation.get("type") not in ROR_RELATIONSHIPS:
            raise ResearchEntitiesFormatError("schema_drift", "unknown ROR relationship type")
        relationships.append({"type": relation["type"], "id": ror_id(relation.get("id")),
                              "label": relation.get("label")})
    external = []
    for item in native.get("external_ids") or []:
        kind = str(item.get("type") or "").casefold()
        if kind not in ROR_EXTERNAL_TYPES:
            continue
        external.append({"type": kind, "all": [str(v) for v in item.get("all") or []],
                         "preferred": item.get("preferred")})
    locations = []
    for location in native.get("locations") or []:
        details = dict(location.get("geonames_details") or {})
        locations.append({"geonames_id": location.get("geonames_id"), "name": details.get("name"),
                          "country_code": details.get("country_code"), "country_name": details.get("country_name")})
    admin = dict(native.get("admin") or {})
    return {
        "ror_id": ror_id(native.get("id")),
        "display_name": display,
        "names": sorted(names, key=lambda n: (n["value"], canonical(n["types"]))),
        "types": sorted(str(t) for t in native.get("types") or []),
        "established": native.get("established"),
        "external_ids": sorted(external, key=lambda e: e["type"]),
        "links": sorted(({"type": link.get("type"), "value": link.get("value")} for link in native.get("links") or []),
                        key=canonical),
        "domains": sorted(str(d) for d in native.get("domains") or []),
        "locations": locations,
        "relationships": sorted(relationships, key=lambda r: (r["type"], r["id"])),
        "provider_modified": dict(admin.get("last_modified") or {}).get("date"),
    }


def parse_ror(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    body = _zip_member(raw, str(document["member"]))
    try:
        records = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ResearchEntitiesFormatError("schema_drift", "the ROR member is not JSON") from exc
    if not isinstance(records, list):
        raise ResearchEntitiesFormatError("schema_drift", "the ROR dump is a JSON array of records")
    wanted = {ror_id(i) for i in document["ror_ids"]}
    release = dict(document["release"])
    revision = {"marker": str(document["version"]), "basis": "ror-release",
                "effective_at": iso_instant(release["published_on"])}
    citation = {"url": url, "licence": "CC0-1.0", "attribution": PROVIDER_CONTRACTS["ror"]["attribution"],
                "release": str(document["version"])}
    items, found = [], set()
    for native in records:
        identifier = _safe(ror_id, dict(native).get("id"))
        if identifier not in wanted:
            continue
        if native.get("status") not in ROR_STATUSES:
            raise ResearchEntitiesFormatError("schema_drift", "unknown ROR status")
        found.add(identifier)
        body_view = _ror_body(native)
        items.append(_statement("organisation", "ror", identifier, native["status"],
                                {**revision, "provider_modified": body_view["provider_modified"]}, body_view,
                                {**citation, "record_url": identifier}))
    for identifier in sorted(wanted - found):
        items.append(_statement("organisation", "ror", identifier, "not_in_release", revision, {},
                                {**citation, "record_url": identifier}))
    return {"items": items, "published_on": iso_day(release["published_on"]), "basis": "declared_release",
            "label": str(release.get("label") or document["version"]), "missing": sorted(wanted - found),
            "excluded_fields": ["CSV copy", "undeclared records"]}


def _date_parts(value: Any) -> str | None:
    value = dict(value or {})
    parts = []
    for key in ("year", "month", "day"):
        part = dict(value.get(key) or {}).get("value")
        if part is None:
            break
        parts.append(str(part).zfill(4 if key == "year" else 2))
    return "-".join(parts) or None


def _public(item: Mapping[str, Any]) -> bool:
    return str(item.get("visibility") or "public").casefold() == "public"


def parse_orcid(raw: bytes, *, status: int, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    """A public ORCID record reduced to the fields :data:`ORCID_KEPT` allows (nothing else is read)."""
    identifier = orcid_id(document["orcid"])
    citation = {"url": url, "licence": "ORCID Public API terms (non-commercial)", "record_url":
                f"https://orcid.org/{identifier}", "attribution": PROVIDER_CONTRACTS["orcid"]["attribution"]}
    if status in {404, 410, 409}:
        removal = {404: "not_found", 410: "not_found", 409: "locked"}[status]
        return {"items": [_statement("researcher", "orcid", identifier, removal,
                                     {"marker": f"http-{status}", "basis": "orcid-response", "effective_at": None},
                                     {}, citation)],
                "published_on": None, "basis": "retrieval_time", "label": f"ORCID {identifier} ({removal})",
                "missing": [identifier], "excluded_fields": list(ORCID_EXCLUDED)}
    try:
        native = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResearchEntitiesFormatError("schema_drift", "the ORCID record is not JSON") from exc
    stated = dict(native.get("orcid-identifier") or {}).get("path")
    if orcid_id(stated) != identifier:
        raise ResearchEntitiesFormatError("identity_mismatch", "the ORCID record names another iD")
    history = dict(native.get("history") or {})
    modified = iso_instant(dict(history.get("last-modified-date") or {}).get("value"))
    if modified is None:
        raise ResearchEntitiesFormatError("schema_drift", "the ORCID record states no last-modified date")
    revision = {"marker": modified, "basis": "orcid-last-modified", "effective_at": modified}
    if history.get("deactivation-date"):
        return {"items": [_statement("researcher", "orcid", identifier, "deactivated", revision, {}, citation)],
                "published_on": modified[:10], "basis": "provider_modified", "label": f"ORCID {identifier}",
                "missing": [], "excluded_fields": list(ORCID_EXCLUDED)}
    name = dict(dict(native.get("person") or {}).get("name") or {})
    display = None
    if name and _public(name):
        credit = dict(name.get("credit-name") or {}).get("value")
        given = dict(name.get("given-names") or {}).get("value")
        family = dict(name.get("family-name") or {}).get("value")
        display = text(credit) or text(" ".join(p for p in (given, family) if p))
    activities = dict(native.get("activities-summary") or {})
    employments = []
    for group in dict(activities.get("employments") or {}).get("affiliation-group") or []:
        for summary in group.get("summaries") or []:
            item = dict(summary.get("employment-summary") or {})
            if not item or not _public(item):
                continue
            organisation = dict(item.get("organization") or {})
            address = dict(organisation.get("address") or {})
            disambiguated = dict(organisation.get("disambiguated-organization") or {})
            source = text(disambiguated.get("disambiguation-source"))
            value = text(disambiguated.get("disambiguated-organization-identifier"))
            if source and source.upper() == "ROR" and value:
                value = _safe(ror_id, value) or value
            employments.append({
                "put_code": item.get("put-code"),
                "organisation": {"name": text(organisation.get("name")), "city": text(address.get("city")),
                                 "country": text(address.get("country")),
                                 "disambiguated": {"source": source, "identifier": value} if source and value
                                 else None},
                "start": _date_parts(item.get("start-date")),
                "end": _date_parts(item.get("end-date")),
                "last_modified": iso_instant(dict(item.get("last-modified-date") or {}).get("value")),
            })
    works = []
    for group in dict(activities.get("works") or {}).get("group") or []:
        for summary in group.get("work-summary") or []:
            if not _public(summary):
                continue
            identifiers = []
            for external in dict(summary.get("external-ids") or {}).get("external-id") or []:
                kind = str(external.get("external-id-type") or "").casefold()
                if kind not in ORCID_WORK_IDS or str(external.get("external-id-relationship") or "self") != "self":
                    continue
                value = dict(external.get("external-id-normalized") or {}).get("value") or \
                    external.get("external-id-value")
                if kind == "doi":
                    value = _safe(doi, value)
                if value:
                    identifiers.append({"type": kind, "value": str(value)})
            publication = dict(summary.get("publication-date") or {})
            works.append({
                "put_code": summary.get("put-code"),
                "title": text(dict(dict(summary.get("title") or {}).get("title") or {}).get("value")),
                "type": text(summary.get("type")),
                "publication_year": text(dict(publication.get("year") or {}).get("value")),
                "identifiers": sorted(identifiers, key=lambda i: (i["type"], i["value"])),
                "last_modified": iso_instant(dict(summary.get("last-modified-date") or {}).get("value")),
            })
    body = {"orcid": identifier, "display_name": display,
            "employments": sorted(employments, key=lambda e: (str(e["start"] or ""), str(e["put_code"]))),
            "works": sorted(works, key=lambda w: (str(w["publication_year"] or ""), str(w["put_code"])))}
    return {"items": [_statement("researcher", "orcid", identifier, "active", revision, body, citation)],
            "published_on": modified[:10], "basis": "provider_modified", "label": f"ORCID {identifier}",
            "missing": [], "excluded_fields": list(ORCID_EXCLUDED)}


def _datacite_item(data: Mapping[str, Any], url: str) -> dict[str, Any]:
    attributes = dict(data.get("attributes") or {})
    identifier = doi(attributes.get("doi") or data.get("id"))
    creators = []
    for position, creator in enumerate(attributes.get("creators") or []):
        creator = dict(creator)
        name_type = text(creator.get("nameType"))
        orcid = None
        for name_id in creator.get("nameIdentifiers") or []:
            if str(dict(name_id).get("nameIdentifierScheme") or "").upper() == "ORCID":
                orcid = _safe(orcid_id, dict(name_id).get("nameIdentifier")) or orcid
        affiliations = []
        for affiliation in creator.get("affiliation") or []:
            if not isinstance(affiliation, Mapping):
                continue
            scheme, value = text(affiliation.get("affiliationIdentifierScheme")), \
                text(affiliation.get("affiliationIdentifier"))
            if scheme and value:
                if scheme.upper() == "ROR":
                    value = _safe(ror_id, value) or value
                affiliations.append({"scheme": scheme.upper(), "identifier": value})
        creators.append({"position": position, "name_type": name_type,
                         "name": text(creator.get("name")) if name_type == "Organizational" else None,
                         "orcid": orcid, "affiliation_identifiers": affiliations})
    related = []
    for item in attributes.get("relatedIdentifiers") or []:
        item = dict(item)
        related.append({k: item[k] for k in ("relatedIdentifier", "relatedIdentifierType", "relationType",
                                              "resourceTypeGeneral", "relatedMetadataScheme", "schemeUri",
                                              "schemeType") if item.get(k) is not None})
    publisher = attributes.get("publisher")
    if isinstance(publisher, Mapping):
        publisher = publisher.get("name")
    types = dict(attributes.get("types") or {})
    updated = iso_instant(attributes.get("updated"))
    version = attributes.get("metadataVersion")
    body = {
        "doi": identifier,
        "titles": [str(dict(t).get("title")) for t in attributes.get("titles") or [] if dict(t).get("title")],
        "publisher": text(publisher),
        "publication_year": text(attributes.get("publicationYear")),
        "resource_type_general": text(types.get("resourceTypeGeneral")),
        "resource_type": text(types.get("resourceType")),
        "version": text(attributes.get("version")),
        "rights": [{k: dict(r).get(k) for k in ("rights", "rightsIdentifier", "rightsUri") if dict(r).get(k)}
                   for r in attributes.get("rightsList") or []],
        "url": text(attributes.get("url")),
        "state": text(attributes.get("state")),
        "created": iso_instant(attributes.get("created")),
        "registered": iso_instant(attributes.get("registered")),
        "metadata_version": version,
        "creators": creators,
        "related_identifiers": related,
    }
    marker = f"v{version}:{updated}" if version is not None else f"updated:{updated}"
    return _statement("dataset", "datacite", identifier, "findable" if body["state"] in (None, "findable")
                      else str(body["state"]),
                      {"marker": marker, "basis": "datacite-metadata-version", "effective_at": updated,
                       "provider_modified": updated}, body,
                      {"url": url, "record_url": f"https://doi.org/{identifier}", "licence": "CC0-1.0",
                       "attribution": PROVIDER_CONTRACTS["datacite"]["attribution"]})


def parse_datacite(raw: bytes, *, status: int, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    if status in {404, 410} and document.get("doi"):
        identifier = doi(document["doi"])
        return {"items": [_statement("dataset", "datacite", identifier, "not_found",
                                     {"marker": f"http-{status}", "basis": "datacite-response", "effective_at": None},
                                     {}, {"url": url, "record_url": f"https://doi.org/{identifier}",
                                          "licence": "CC0-1.0",
                                          "attribution": PROVIDER_CONTRACTS["datacite"]["attribution"]})],
                "published_on": None, "basis": "retrieval_time", "label": f"DataCite {identifier} (not found)",
                "missing": [identifier], "excluded_fields": list(DATACITE_EXCLUDED)}
    try:
        native = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResearchEntitiesFormatError("schema_drift", "the DataCite response is not JSON") from exc
    data = native.get("data")
    rows = data if isinstance(data, list) else [data] if isinstance(data, Mapping) else None
    if rows is None:
        raise ResearchEntitiesFormatError("schema_drift", "the DataCite response has no data")
    if document.get("doi") and (len(rows) != 1 or doi(dict(rows[0].get("attributes") or {}).get("doi")
                                                       or rows[0].get("id")) != doi(document["doi"])):
        raise ResearchEntitiesFormatError("identity_mismatch", "the DataCite response names another DOI")
    items = [_datacite_item(dict(row), url) for row in rows]
    stamps = sorted(i["revision"]["effective_at"] for i in items if i["revision"]["effective_at"])
    label = f"DataCite {document.get('doi') or canonical(document.get('query'))}"
    return {"items": items, "published_on": stamps[-1][:10] if stamps else None,
            "basis": "provider_modified" if stamps else "retrieval_time", "label": label, "missing": [],
            "excluded_fields": list(DATACITE_EXCLUDED)}


def _csv(body: bytes, delimiter: str) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8-sig")), delimiter=delimiter)
    return [{str(k).strip(): (v if v is None else str(v)) for k, v in row.items() if k is not None} for row in reader]


def parse_cordis(raw: bytes, *, document: Mapping[str, Any], url: str) -> dict[str, Any]:
    delimiter = str(document.get("delimiter") or ";")
    separator = str(document.get("decimal_separator") or ",")
    currency = str(document["currency"])
    projects = _csv(_zip_member(raw, str(document["project_member"])), delimiter)
    organisations = _csv(_zip_member(raw, str(document["organization_member"])), delimiter)
    if projects and not {"id", "frameworkProgramme"} <= set(projects[0]):
        raise ResearchEntitiesFormatError("schema_drift", "project.csv lacks id or frameworkProgramme")
    if organisations and not {"projectID", "organisationID", "name", "role"} <= set(organisations[0]):
        raise ResearchEntitiesFormatError("schema_drift", "organization.csv lacks projectID, organisationID, name or "
                                          "role")
    wanted = {str(i) for i in document["project_ids"]}
    release = dict(document["release"])

    def money(value: Any) -> dict[str, Any] | None:
        amount = decimal_text(value, separator)
        return None if amount is None else {"amount": amount, "currency": currency, "published": text(value),
                                            "currency_basis": "declared by the document"}

    participants: dict[str, list[dict[str, Any]]] = {}
    for number, row in enumerate(organisations, start=2):
        project = text(row.get("projectID"))
        if project not in wanted:
            continue
        pic = text(row.get("organisationID"))
        if pic is None:
            raise ResearchEntitiesFormatError("schema_drift", "a participant row states no organisationID (PIC)")
        participants.setdefault(project, []).append({
            "pic": pic, "vat_number": text(row.get("vatNumber")), "name": text(row.get("name")),
            "short_name": text(row.get("shortName")), "role": text(row.get("role")), "order": text(row.get("order")),
            "activity_type": text(row.get("activityType")), "sme": text(row.get("SME")),
            "city": text(row.get("city")), "country": text(row.get("country")),
            "website": text(row.get("organizationURL")),
            "ec_contribution": money(row.get("ecContribution")),
            "net_ec_contribution": money(row.get("netEcContribution")),
            "total_cost": money(row.get("totalCost")),
            "end_of_participation": text(row.get("endOfParticipation")), "active": text(row.get("active")),
            "content_update_date": text(row.get("contentUpdateDate")), "row": number,
        })
    items, found = [], set()
    for row in projects:
        project = text(row.get("id"))
        if project not in wanted:
            continue
        found.add(project)
        programme = str(row.get("frameworkProgramme") or document["programme"])
        updated = iso_instant(row.get("contentUpdateDate")) or iso_instant(release["published_on"])
        body = {
            "project_id": project, "programme": programme, "acronym": text(row.get("acronym")),
            "title": text(row.get("title")), "status": text(row.get("status")),
            "start_date": iso_day(row.get("startDate")), "end_date": iso_day(row.get("endDate")),
            "total_cost": money(row.get("totalCost")), "ec_max_contribution": money(row.get("ecMaxContribution")),
            "legal_basis": text(row.get("legalBasis")), "funding_scheme": text(row.get("fundingScheme")),
            "topics": text(row.get("topics")), "grant_doi": _safe(doi, row.get("grantDoi")),
            "content_update_date": text(row.get("contentUpdateDate")),
            "participants": sorted(participants.get(project, []), key=lambda p: (str(p["order"] or ""), p["pic"])),
        }
        items.append(_statement("project", "cordis", f"{programme}:{project}", "published",
                                {"marker": f"{document['release']['label']}:{row.get('contentUpdateDate')}",
                                 "basis": "cordis-content-update", "effective_at": updated,
                                 "provider_modified": text(row.get("contentUpdateDate"))},
                                body, {"url": url, "record_url": f"https://cordis.europa.eu/project/id/{project}",
                                       "licence": "CC-BY-4.0",
                                       "attribution": PROVIDER_CONTRACTS["cordis"]["attribution"],
                                       "release": release["label"]}))
    programme = str(document["programme"])
    for project in sorted(wanted - found):
        items.append(_statement("project", "cordis", f"{programme}:{project}", "not_in_release",
                                {"marker": f"{release['label']}:absent", "basis": "cordis-content-update",
                                 "effective_at": iso_instant(release["published_on"])}, {},
                                {"url": url, "record_url": f"https://cordis.europa.eu/project/id/{project}",
                                 "licence": "CC-BY-4.0", "attribution": PROVIDER_CONTRACTS["cordis"]["attribution"],
                                 "release": release["label"]}))
    return {"items": items, "published_on": iso_day(release["published_on"]), "basis": "declared_release",
            "label": str(release["label"]), "missing": sorted(wanted - found),
            "excluded_fields": list(CORDIS_EXCLUDED)}


# ------------------------------------------------------------------ runtime adapter


class ResearchEntitiesAdapter:
    """Fetch the declared registry documents on the runtime's default transport; one page per document."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = research_declaration(self.source)
        # Only ORCID takes an optional /read-public token; it is used as a header and never stored.
        self._secret = secret if self.declared["provider"] == "orcid" else None
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
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
            "research_entities": {
                "provider": self.declared["provider"],
                "format": self.declared["format"],
                "live_verification": LIVE_VERIFICATION[self.declared["provider"]]["status"],
            },
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "research-entity runs fetch the declared documents only")

    def _get(self, url: str, *, removable: bool) -> tuple[bytes, int, str]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        host = (urlsplit(self.source["endpoint"]).hostname or "").casefold()
        parts = urlsplit(url)
        if (parts.hostname or "").casefold() != host or parts.scheme != "https":
            raise SourcePackError("network_policy", "declared documents are fetched from the endpoint's host only")
        base, _, query = url.partition("?")
        headers = {"Accept": "application/json, application/zip, application/octet-stream"}
        if self._secret:
            headers["Authorization"] = f"Bearer {self._secret}"
        response = self.transport(url=base, params=parse_qsl(query, keep_blank_values=True), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "response was served from another host")
        status = int(response.get("status", 200))
        headers_in = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
        content = response.get("content", b"")
        raw = content.encode() if isinstance(content, str) else bytes(content)
        if len(raw) > int(self.definition["limits"]["max_bytes"]):
            raise SourcePackError("response_too_large", "response exceeds its byte limit")
        if status == 429:
            raise SourcePackError("rate_limited", "provider quota is temporarily exhausted",
                                  retry_after_ms=_retry_after_ms(headers_in.get("retry-after")))
        if status in {401, 403}:
            raise SourcePackError("authentication_failed", f"request refused (HTTP {status})")
        if status >= 500:
            raise SourcePackError("source_unavailable", f"provider returned HTTP {status}")
        if status >= 400 and not (removable and status in {404, 409, 410}):
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        return raw, status, "fixture" if response.get("origin") == "fixture" else "live"

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        documents = list(self.declared["documents"])
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(documents):
            raise SourcePackError("cursor_drift", "cursor names no declared document")
        document = dict(documents[index])
        fmt, provider = self.declared["format"], self.declared["provider"]
        url = document_url(fmt, document, self.source["endpoint"])
        removable = fmt == "orcid-record-json" or (fmt == "datacite-doi-json" and bool(document.get("doi")))
        raw, status, origin = self._get(url, removable=removable)
        try:
            if fmt == "ror-dump-zip":
                release = parse_ror(raw, document=document, url=url)
            elif fmt == "orcid-record-json":
                release = parse_orcid(raw, status=status, document=document, url=url)
            elif fmt == "datacite-doi-json":
                release = parse_datacite(raw, status=status, document=document, url=url)
            else:
                release = parse_cordis(raw, document=document, url=url)
        except ResearchEntitiesFormatError as exc:
            raise SourcePackError(
                "response_too_large" if exc.code == "input_limit" else "schema_drift", f"{exc.code}: {exc}"
            ) from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(release["items"]) > limit:
            # Never a truncated release: a missing record would read as a removal.
            raise SourcePackError("budget_exhausted", "document has more records than the run's result budget")
        items = sorted(release["items"], key=lambda i: (i["record_kind"], i["native_id"]))
        file_sha = hashlib.sha256(raw).hexdigest()
        header = {
            "contract": RELEASE_CONTRACT,
            "provider": provider,
            "format": fmt,
            "document": document,
            "label": release["label"],
            "published_on": release["published_on"],
            "release_basis": release["basis"],
            "http_status": status,
            "file_sha256": file_sha,
            "content_sha256": digest(items),
            "item_count": len(items),
            "missing": release["missing"],
            "excluded_fields": release["excluded_fields"],
            # Only a fixture transport says so; the runtime's HTTPS transport is live evidence.
            "evidence_origin": origin,
            "live_verification": LIVE_VERIFICATION[provider]["status"],
            "url": url,
        }
        records = [
            {
                "id": f"{file_sha[:16]}:{number}",
                "title": f"{item['record_kind']} {item['native_id']} ({release['label']})",
                "url": item["citation"].get("record_url") or url,
                "language": "en",
                "published_at": release["published_on"],
                "content": json.dumps(item, sort_keys=True, ensure_ascii=False),
                "research_entities_release": header,
                "research_entity": item,
            }
            for number, item in enumerate(items)
        ]
        receipt = {
            "status": status,
            "provider": provider,
            "document": document.get("label"),
            "published_on": release["published_on"],
            "release_basis": release["basis"],
            "file_sha256": file_sha,
            "items": len(records),
            "missing": release["missing"],
            "evidence_origin": origin,
            "final_page": index + 1 >= len(documents),
        }
        next_cursor = str(index + 1) if index + 1 < len(documents) else None
        return RuntimePage(tuple(records), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = None
ADAPTERS = {CONNECTOR: ResearchEntitiesAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); ``body_base64`` carries binary bodies."""
    import base64

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        pairs = list(params.items()) if isinstance(params, Mapping) else list(params or [])
        query = urlencode(sorted(pairs))
        key = urlsplit(url).path + ("?" + query if query else "")
        page = by_key.get(key)
        if page is None:
            raise SourcePackError("fixture_missing", f"no native page for {key}")
        if page.get("body_base64") is not None:
            content = base64.b64decode(page["body_base64"])
        else:
            body = page.get("body")
            content = body.encode() if isinstance(body, str) else b"" if body is None else json.dumps(body).encode()
        return {"status": int(page.get("status", 200)), "headers": dict(page.get("headers") or {}),
                "content": content, "origin": "fixture",
                **({"final_url": page["final_url"]} if page.get("final_url") else {})}

    return transport


def fixture_request(fmt: str, document: Mapping[str, Any], endpoint: str = "") -> str:
    """The key :func:`fixture_transport` files a response under (path and sorted query)."""
    parts = urlsplit(document_url(fmt, document, endpoint))
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return parts.path + ("?" + query if query else "")


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ResearchEntitiesAdapter(source, transport=fixture_transport(list(fixture["native_pages"])))
    records, cursor = [], None
    while True:
        page = adapter.fetch_page(
            {"operation": min(source["operations"]), "parameters": {}, "limit": int(source["budgets"]["max_results"])},
            cursor=cursor,
        )
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            return records


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CONNECTOR",
    "EXCLUSIONS",
    "FORMATS",
    "LIVE_VERIFICATION",
    "MINIMISATION",
    "NEVER_SENTENCE",
    "NOT_IMPLEMENTED",
    "ORCID_EXCLUDED",
    "ORCID_KEPT",
    "PROVIDER_CONTRACTS",
    "ResearchEntitiesAdapter",
    "ResearchEntitiesFormatError",
    "doi",
    "fixture_request",
    "fixture_transport",
    "orcid_id",
    "parse_cordis",
    "parse_datacite",
    "parse_orcid",
    "parse_ror",
    "replay_native_fixture",
    "ror_id",
]
