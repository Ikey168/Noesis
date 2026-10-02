"""Research-entity registries for the Science ``research-entities`` features (#2579).

Four providers are recorded under an access contract (:data:`PROVIDER_CONTRACTS`, RE01) and implemented as formats of
the ``research-entities`` source-pack connector; a fifth candidate (the OpenAIRE Graph) is documented and not acquired:

* **ROR** (``ror``, format ``ror-dump-zip``) - a declared ROR data dump release (a Zenodo zip holding the v2 JSON
  member) read for a declared set of ROR ids. Each release is a vintage; withdrawn and inactive records are kept with
  their successor relationships; external identifiers (GRID, ISNI, Wikidata, FundRef) are stored as published.
* **ORCID** (``orcid``, format ``orcid-record-json``) - the public ORCID API v3.0 ``/{orcid}/record`` for a declared
  set of ORCID iDs, with a registered public-API client token (``NOESIS_ORCID_PUBLIC_TOKEN``). Only the fields the
  RE01 minimisation decision allows are stored (:data:`MINIMISATION`): the iD, the public name, the record's
  last-modified time, public employments and public works as asserted identifiers - never as authorship facts.
* **DataCite** (``datacite``, format ``datacite-doi-json``) - the DataCite REST API ``/dois/{doi}`` for a declared set
  of DOIs: metadata version, related identifiers (IsSupplementTo, Cites, IsVersionOf, ...) and funding references as
  published; personal creators and contributors are reduced to their published ORCID iD and affiliation identifiers.
* **CORDIS** (``cordis``, format ``cordis-projects-csv-zip``) - the CORDIS open-data project export of a framework
  programme (a zip holding ``project.csv`` and ``organization.csv``) read for a declared set of project ids:
  programme, participants with their PIC and names as published and contributions with the currency as published.

Every provider is ``unverified-live`` until a dated live run (RE14, #2649); endpoint, parameter and column names marked
*verify* come from the providers' documentation as recorded in
``docs/development/research-entities-evidence/source-audit.md``. Nothing here ranks researchers or organisations,
computes a metric, infers an affiliation from co-authorship or disambiguates authors by name.
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
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError

CONNECTOR = "research-entities"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
RECORD_CONTRACT = "noesis-research-entity-record-v2"
RECEIPT_CONTRACT = "noesis-research-entity-acquisition-receipt-v1"
MINIMISATION_POLICY = "research-entities-minimisation-v1"
MAX_UNITS = 50
MAX_SELECTED_IDS = 200
REVIEW_BOUNDARY = ("Registry records as each registry published them. No researcher rankings or metrics, no "
                   "inference of affiliation from co-authorship, no author disambiguation by name and no personal data "
                   "beyond the public ORCID fields the RE01 minimisation decision allows.")
EXCLUSIONS = (
    "researcher rankings, league tables or metrics (h-index, citation counts, productivity scores)",
    "inference of affiliation from co-authorship",
    "author disambiguation or matching by name",
    "personal data beyond the public ORCID fields allowed by the RE01 minimisation decision",
    "summing contributions across currencies",
    "inferred collaboration or influence links",
)

# format -> provider, record kind, the selection list it reads and whether a secret (client token) is required
FORMATS: dict[str, dict[str, Any]] = {
    "ror-dump-zip": {"provider": "ror", "kind": "organisation", "unit": "releases", "keyed": False},
    "orcid-record-json": {"provider": "orcid", "kind": "researcher", "unit": "orcids", "keyed": True},
    "datacite-doi-json": {"provider": "datacite", "kind": "dataset", "unit": "dois", "keyed": False},
    "cordis-projects-csv-zip": {"provider": "cordis", "kind": "project", "unit": "programmes", "keyed": False},
}
RECORD_KINDS = ("organisation", "researcher", "dataset", "project")
PROVIDER_HOSTS = {
    "ror": {"zenodo.org"},
    "orcid": {"pub.orcid.org"},
    "datacite": {"api.datacite.org"},
    "cordis": {"cordis.europa.eu"},
}
FEATURES = {"ror": "research-entities-ror", "orcid": "research-entities-orcid",
            "datacite": "research-entities-datacite", "cordis": "research-entities-cordis"}

# RE01 access decisions. Recorded without network access to the providers' documentation (the audit host's proxy
# refused the documentation hosts); every item marked ``verify`` must be checked before a dated live run (RE14, #2649).
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "ror": {
        "publisher": "Research Organization Registry (ROR)",
        "delivers": "organisation records keyed by ROR id: names, types, status (active, inactive, withdrawn), "
        "locations, links, external identifiers and relationships (parent, child, related, predecessor, successor)",
        "access_decision": "unverified-live",
        "access": "bulk file: the ROR data dump release published on Zenodo (a zip holding the v2 schema JSON member)",
        "endpoints": [
            "https://zenodo.org/records/{record}/files/{release}-ror-data.zip (verify the file path per release)",
            ("https://api.ror.org/v2/organizations/{id} (the API serves the latest release only; used by "
             "src/ingestion/ror.py, not by this connector)"),
        ],
        "authentication": "none (the ROR API introduces an optional client id for higher limits; verify)",
        "rate_limits": "the dump is one download per release; the API allows about 2000 requests per 5 minutes "
        "(verify); this connector never calls the API",
        "licence": "CC0 1.0 (ROR data is public domain dedicated; attribution appreciated)",
        "attribution": "Research Organization Registry (ROR), data dump release as cited",
        "redistribution": "unrestricted (CC0)",
        "identifiers": {"organisation": "ROR id (https://ror.org/0xxxxxxNN)",
                        "external": "GRID, ISNI, Wikidata, FundRef (Crossref Funder id) as published"},
        "revisions": "each dump release (vN.N, dated) is a vintage; a record's admin.last_modified states its own "
        "change date; records are never deleted: a withdrawn or inactive record stays with its successor or "
        "predecessor relationships (verify that successor relationships are always published)",
        "corrections_and_removals": "corrections appear as changed records in a later release; removals appear as "
        "status withdrawn (with a successor where merged); a declared id missing from a release is reported as "
        "not_in_release and never read as a deletion",
        "personal_data": "none (organisations only)",
        "verify": ["Zenodo record and file names per release", "member name inside the zip", "v2 field names"],
    },
    "orcid": {
        "publisher": "ORCID, Inc. (public API v3.0)",
        "delivers": "a researcher's public ORCID record: iD, public name, record last-modified time, public "
        "employments and public works (identifiers as asserted)",
        "access_decision": "unverified-live",
        "access": "api (GET https://pub.orcid.org/v3.0/{orcid}/record, Accept: application/vnd.orcid+json)",
        "endpoints": ["https://pub.orcid.org/v3.0/{orcid}/record (verify)",
                      "https://orcid.org/oauth/token (client-credentials /read-public token; operator-side)"],
        "authentication": "registered public-API client credentials; the /read-public bearer token is supplied as "
        "the secret NOESIS_ORCID_PUBLIC_TOKEN and never stored in a manifest, record or receipt; without it the "
        "source fails with authentication_failed and the feature reports itself degraded",
        "rate_limits": "public API: about 24 requests per second and a burst of 40 per client (verify); the connector "
        "makes one request per declared iD",
        "licence": "ORCID public data is released under CC0 (verify); the ORCID Public API terms of service apply "
        "to the client and forbid presenting ORCID data as verified by ORCID (verify wording)",
        "attribution": "ORCID public record as cited (https://orcid.org/{orcid})",
        "redistribution": "CC0 for public data; only the minimised fields are stored or exported",
        "identifiers": {"researcher": "ORCID iD with ISO 7064 11,2 checksum", "works": "external ids (DOI etc.) as "
                        "asserted", "organisations": "disambiguated organisation ids (ROR, GRID, Ringgold, FundRef)"},
        "revisions": "history.last-modified-date dates a record version; each changed public record is a revision",
        "corrections_and_removals": "an item made private or deleted by the researcher disappears from the next "
        "response and becomes a revision without it; a deactivated or deprecated record (HTTP 409/410, verify) "
        "becomes a withdrawn revision with no personal field",
        "personal_data": "yes - see MINIMISATION",
        "verify": ["response codes for deactivated, locked and deprecated records", "rate limits", "licence wording"],
    },
    "datacite": {
        "publisher": "DataCite (REST API)",
        "delivers": "DOI metadata of datasets: titles, publisher, publication year, version, metadata version, "
        "related identifiers, funding references, creators and contributors",
        "access_decision": "unverified-live",
        "access": "api (GET https://api.datacite.org/dois/{doi}?affiliation=true, JSON:API)",
        "endpoints": ["https://api.datacite.org/dois/{doi} (verify parameter names)"],
        "authentication": "none for public (findable) DOIs",
        "rate_limits": "about 3000 requests per 5 minutes per client IP (verify); one request per declared DOI",
        "licence": "DataCite metadata is CC0 (verify)",
        "attribution": "DataCite DOI metadata as cited (https://doi.org/{doi})",
        "redistribution": "unrestricted (CC0); personal creators are minimised before storage",
        "identifiers": {"dataset": "DOI (case-insensitive, stored lower-case)",
                        "related": "relatedIdentifier with relatedIdentifierType and relationType as published"},
        "revisions": "attributes.metadataVersion and attributes.updated identify a metadata version; each changed "
        "response is a revision",
        "corrections_and_removals": "a changed metadata version is a revision; a DOI no longer served (HTTP 404: "
        "made registered-only or deleted draft) becomes an unavailable revision, never a deletion",
        "personal_data": "creators and contributors of nameType Personal - see MINIMISATION",
        "verify": ["affiliation=true parameter", "metadataVersion semantics", "404 behaviour for registered DOIs"],
    },
    "cordis": {
        "publisher": "European Commission, CORDIS (open data project exports)",
        "delivers": "EU framework-programme projects: identifiers, programme, topics and calls, dates, costs and EU "
        "contributions, participants with PIC, role and contribution as published",
        "access_decision": "unverified-live",
        "access": "bulk file: the CORDIS project export of a programme (zip with project.csv and organization.csv, "
        "semicolon-delimited)",
        "endpoints": ["https://cordis.europa.eu/data/cordis-HORIZONprojects-csv.zip (verify)",
                      "https://cordis.europa.eu/data/cordis-h2020projects-csv.zip (verify)"],
        "authentication": "none",
        "rate_limits": "not documented; one download per declared programme per run, bounded by max_bytes",
        "licence": "Commission reuse policy (Decision 2011/833/EU), CC BY 4.0 with attribution to CORDIS (verify)",
        "attribution": "CORDIS - EU research results, European Commission",
        "redistribution": "reuse with attribution",
        "identifiers": {"project": "CORDIS project id (the grant agreement number) within its framework programme",
                        "participant": "organisationID (the participant identification code, PIC; verify) and VAT "
                        "number as published"},
        "revisions": "contentUpdateDate per project and participant row dates a version; each changed export row "
        "set is a revision of the project record",
        "corrections_and_removals": "a corrected row is a revision; a declared project missing from a later export "
        "is reported as not_in_export and never read as a deletion",
        "personal_data": "participant contact forms and street addresses are not needed and never stored; "
        "participants are organisations",
        "verify": ["file names", "column names and delimiter", "decimal separator", "that organisationID is the PIC"],
    },
    "openaire-graph": {
        "publisher": "OpenAIRE (OpenAIRE Graph)",
        "delivers": "an aggregated research graph (publications, datasets, projects, organisations, links)",
        "access_decision": "documented-not-acquired",
        "access": "none in the first coverage",
        "endpoints": ["https://api.openaire.eu/graph/ (verify)"],
        "authentication": "none for the public API (verify)",
        "rate_limits": "undocumented here (verify)",
        "licence": "CC BY 4.0 for the graph dumps (verify)",
        "attribution": "OpenAIRE Graph",
        "redistribution": "reuse with attribution",
        "identifiers": {"all": "OpenAIRE identifiers with original PIDs"},
        "revisions": "graph releases",
        "corrections_and_removals": "graph releases",
        "personal_data": "author names as aggregated",
        "reason": "an aggregator: its organisation and author links include inferred and deduplicated relations "
        "(the exclusions forbid inferred affiliation and name-based author disambiguation), and every record it "
        "holds for the first coverage is available from the primary registries (ROR, ORCID, DataCite, CORDIS); "
        "recorded as not implemented until a decision allows filtering to asserted links only",
        "verify": ["whether provenance of each relation is exposed"],
    },
}
LIVE_VERIFICATION = {
    provider: {"status": contract["access_decision"],
               "note": "no dated live run from this runtime; offline fixtures only (RE14 #2649 records live evidence)"}
    for provider, contract in PROVIDER_CONTRACTS.items()
}
BOUNDED_COVERAGE = {
    "ror": {"releases": "the two most recent dump releases", "organisations": "a declared list of ROR ids (at most "
            "200): the organisations of the declared CORDIS participants and ORCID employments, their parents, "
            "children and successors", "why": "enough to answer lineage across two releases without mirroring the "
            "registry (about 110 000 records)"},
    "orcid": {"researchers": "a declared list of ORCID iDs (at most 50) whose records name a declared ROR "
              "organisation; each record read once per run", "why": "researcher records are personal data; the "
              "coverage is limited to named, declared iDs and never expanded through co-authorship"},
    "datacite": {"dois": "a declared list of dataset DOIs (at most 200): datasets whose metadata cites a declared "
                 "organisation's ROR id or a declared CORDIS project", "why": "DataCite holds tens of millions of "
                 "DOIs; related datasets are reached by declared DOI, never by crawling"},
    "cordis": {"programmes": ["HORIZON (Horizon Europe, 2021-2027)", "H2020 (Horizon 2020, 2014-2020)"],
               "projects": "a declared list of project ids per programme (at most 200)",
               "why": "the exports hold every project of a programme; only declared projects are kept"},
    "periods": "records as published in the acquired releases; no backfill of earlier ROR releases or CORDIS "
    "exports",
    "caps": {"units_per_source": MAX_UNITS, "selected_ids_per_unit": MAX_SELECTED_IDS},
}
MINIMISATION: dict[str, Any] = {
    "policy": MINIMISATION_POLICY,
    "subjects": "natural persons: ORCID record holders; DataCite creators and contributors of nameType Personal",
    "stored_for_researchers": [
        "ORCID iD",
        "public name (given names, family name, credit name) exactly as published with PUBLIC visibility",
        "record last-modified time",
        ("public employments: organisation name, city, region and country, disambiguated organisation id, "
         "department, role title, start and end dates, put-code, asserting source kind, last-modified time"),
        ("public works: put-code, type, title, publication year, external ids as asserted, asserting source kind, "
         "last-modified time"),
    ],
    "never_stored_for_researchers": [
        "biography", "emails", "addresses (country of residence)", "keywords", "other names", "researcher URLs",
        "person external identifiers", "educations and qualifications",
        "distinctions, invited positions, memberships and services", "fundings", "peer reviews",
        "research resources", "non-public items of any section", "the name of a self-asserting source",
    ],
    "stored_for_dataset_persons": ["name type", "ORCID iD when the metadata publishes one",
                                   "affiliation identifiers (ROR) when published", "position in the creator list",
                                   "contributor type"],
    "never_stored_for_dataset_persons": ["given name", "family name", "full name", "affiliation names of persons",
                                         "other name identifiers"],
    "matching": "researchers are never matched, merged or disambiguated by name; a researcher links to papers only "
    "through DOIs asserted in their public ORCID record and to organisations only through the disambiguated "
    "organisation id of an asserted employment; persons in dataset metadata are never matched",
    "query_scope": "researcher records are returned only to principals holding "
    "knowledge:science:research-entities:researchers:read in addition to the read scope; others see that a "
    "researcher record exists behind a count",
    "retention": "revisions are retained with the minimised fields; a deactivated or deprecated ORCID record becomes a "
    "withdrawn revision without name, and an operator redaction (ResearchEntityStore.redact_researcher) removes the "
    "name from every stored revision of that researcher and records the redaction",
    "notices_and_exports": "monitor notices and evidence bundles carry the ORCID iD and the minimised fields only",
}
RESEARCHER_ALLOWED_FIELDS = frozenset({"orcid", "name", "name_status", "last_modified", "status", "employments",
                                       "works", "withheld_sections"})
NAME_KEYS = frozenset({"given_names", "family_name", "credit_name"})
EMPLOYMENT_KEYS = frozenset({"put_code", "organisation", "department", "role", "start_date", "end_date",
                             "asserted_by", "last_modified"})
WORK_KEYS = frozenset({"put_code", "type", "title", "publication_year", "external_ids", "asserted_by",
                       "last_modified"})
PERSON_FORBIDDEN_KEYS = frozenset({"name", "given_name", "givenname", "family_name", "familyname", "full_name",
                                   "affiliation_names"})
PARTICIPANT_FORBIDDEN_KEYS = frozenset({"street", "post_code", "postcode", "contact_form", "contactform",
                                        "geolocation"})
ROR_RELATIONSHIP_TYPES = ("parent", "child", "related", "predecessor", "successor")
ROR_STATUSES = ("active", "inactive", "withdrawn")
RELATION_TYPES = frozenset({
    "IsCitedBy", "Cites", "IsSupplementTo", "IsSupplementedBy", "IsContinuedBy", "Continues", "IsDescribedBy",
    "Describes", "HasMetadata", "IsMetadataFor", "HasVersion", "IsVersionOf", "IsNewVersionOf",
    "IsPreviousVersionOf", "IsPartOf", "HasPart", "IsPublishedIn", "IsReferencedBy", "References",
    "IsDocumentedBy", "Documents", "IsCompiledBy", "Compiles", "IsVariantFormOf", "IsOriginalFormOf",
    "IsIdenticalTo", "IsReviewedBy", "Reviews", "IsDerivedFrom", "IsSourceOf", "IsRequiredBy", "Requires",
    "IsObsoletedBy", "Obsoletes", "IsCollectedBy", "Collects",
})
ROR_ID = re.compile(r"^0[0-9a-hj-km-np-tv-z]{6}[0-9]{2}$")
DOI = re.compile(r"^10\.\d{4,9}/\S+$")
CORDIS_PROGRAMMES = {"HORIZON": "/data/cordis-HORIZONprojects-csv.zip", "H2020": "/data/cordis-h2020projects-csv.zip"}
PROJECT_COLUMNS = ("id", "acronym", "status", "title", "startDate", "endDate", "totalCost", "ecMaxContribution",
                   "frameworkProgramme", "contentUpdateDate")
ORGANIZATION_COLUMNS = ("projectID", "organisationID", "name", "role", "ecContribution", "country",
                        "contentUpdateDate")
CORDIS_CURRENCY = "EUR"


class ResearchEntityFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def clean(value: Any) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    return raw or None


def iso_day(value: Any) -> str | None:
    raw = clean(value)
    if raw is None:
        return None
    try:
        return date.fromisoformat(raw[:10]).isoformat()
    except ValueError:
        return None


def iso_instant(value: Any) -> str | None:
    """An ISO timestamp (or epoch milliseconds) as UTC ISO text; a bare date means its start."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    raw = clean(value)
    if raw is None:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        raw += "T00:00:00+00:00"
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(UTC).isoformat()


def ror_id(value: Any) -> str | None:
    raw = str(value or "").strip().removeprefix("https://ror.org/").removeprefix("http://ror.org/").lower()
    return raw if ROR_ID.fullmatch(raw) else None


def ror_url(value: Any) -> str | None:
    rid = ror_id(value)
    return None if rid is None else "https://ror.org/" + rid


def normalize_doi(value: Any) -> str | None:
    raw = str(value or "").strip()
    raw = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", raw, flags=re.IGNORECASE).lower()
    return raw if DOI.fullmatch(raw) else None


def valid_orcid(value: Any) -> str | None:
    raw = str(value or "").strip().removeprefix("https://orcid.org/").removeprefix("http://orcid.org/").upper()
    if not re.fullmatch(r"\d{4}-\d{4}-\d{4}-\d{3}[\dX]", raw):
        return None
    total = 0
    for digit in raw.replace("-", "")[:-1]:
        total = (total + int(digit)) * 2
    check = (12 - total % 11) % 11
    return raw if raw[-1] == ("X" if check == 10 else str(check)) else None


def organisation_key(value: Any) -> str:
    return f"research-entities:ror:{ror_id(value)}"


def researcher_key(value: Any) -> str:
    return f"research-entities:orcid:{valid_orcid(value)}"


def dataset_key(value: Any) -> str:
    return f"research-entities:doi:{normalize_doi(value)}"


def project_key(programme: Any, project_id: Any) -> str:
    return f"research-entities:cordis:{str(programme).upper()}:{str(project_id).strip()}"


def participant_key(pic: Any) -> str:
    return f"research-entities:cordis-participant:{str(pic).strip()}"


def unverified(provider: str) -> bool:
    return PROVIDER_CONTRACTS.get(provider, {}).get("access_decision") != "verified-live"


def amount(value: Any) -> str | None:
    """A published amount as exact decimal text (CORDIS may use a decimal comma); ``None`` when not numeric."""
    raw = clean(value)
    if raw is None:
        return None
    if "," in raw and "." not in raw:
        raw = raw.replace(",", ".")
    try:
        number = Decimal(raw)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return format(number.normalize(), "f") if number == number.to_integral_value() else format(number, "f")


def _order(*parts: Any) -> str:
    out = []
    for part in parts:
        if isinstance(part, int) or (isinstance(part, str) and part.isdigit()):
            out.append(f"{int(part):012d}")
        else:
            out.append(str(part or ""))
    return "|".join(out)


def _record(fmt: str, record_key: str, *, native_id: str, title: Any, locator: str, fields: Mapping[str, Any],
            as_of: str | None, native_revision: Any, revision_order: str, status: str,
            release: Mapping[str, Any] | None = None, minimisation: Mapping[str, Any] | None = None
            ) -> dict[str, Any]:
    spec = FORMATS[fmt]
    if not str(locator or "").startswith("https://"):
        raise ResearchEntityFormatError("schema_drift", f"{record_key} has no HTTPS locator")
    return {
        "contract": RECORD_CONTRACT,
        "format": fmt,
        "provider": spec["provider"],
        "record_kind": spec["kind"],
        "record_key": record_key,
        "native_id": native_id,
        "native_revision": clean(native_revision),
        "revision_order": revision_order,
        "as_of": as_of,
        "status": status,
        "release": dict(release) if release else None,
        "title": clean(title) or record_key,
        "locator": locator,
        "minimisation": dict(minimisation or {"policy": MINIMISATION_POLICY, "subject": "organisation",
                                              "withheld": []}),
        "fields": dict(fields),
    }


# ------------------------------------------------------------------ minimisation guard


def minimisation_violations(record: Mapping[str, Any]) -> list[str]:
    """Paths of personal fields a record still carries against the RE01 decision (empty when it complies)."""
    found: list[str] = []
    fields = dict(record.get("fields") or {})
    kind = record.get("record_kind")
    if kind == "researcher":
        found += [f"$.fields.{k}" for k in sorted(set(fields) - RESEARCHER_ALLOWED_FIELDS)]
        name = fields.get("name")
        if name is not None:
            if not isinstance(name, Mapping):
                found.append("$.fields.name")
            else:
                found += [f"$.fields.name.{k}" for k in sorted(set(name) - NAME_KEYS)]
                if fields.get("name_status") != "public" and any(v is not None for v in name.values()):
                    found.append("$.fields.name")
        for index, item in enumerate(fields.get("employments") or []):
            found += [f"$.fields.employments[{index}].{k}" for k in sorted(set(item) - EMPLOYMENT_KEYS)]
            if (item.get("asserted_by") or {}).get("name") and (item.get("asserted_by") or {}).get("kind") == "self":
                found.append(f"$.fields.employments[{index}].asserted_by.name")
        for index, item in enumerate(fields.get("works") or []):
            found += [f"$.fields.works[{index}].{k}" for k in sorted(set(item) - WORK_KEYS)]
            if (item.get("asserted_by") or {}).get("name") and (item.get("asserted_by") or {}).get("kind") == "self":
                found.append(f"$.fields.works[{index}].asserted_by.name")
    elif kind == "dataset":
        for role in ("creators", "contributors"):
            for index, person in enumerate(fields.get(role) or []):
                if person.get("name_type") == "Organizational":
                    continue
                for key, value in person.items():
                    if str(key).casefold() in PERSON_FORBIDDEN_KEYS and value not in (None, "", [], {}):
                        found.append(f"$.fields.{role}[{index}].{key}")
    elif kind == "project":
        for index, participant in enumerate(fields.get("participants") or []):
            for key, value in participant.items():
                if str(key).casefold() in PARTICIPANT_FORBIDDEN_KEYS and value not in (None, "", [], {}):
                    found.append(f"$.fields.participants[{index}].{key}")
    return sorted(set(found))


# ------------------------------------------------------------------ ROR


def parse_ror_release(raw: bytes, unit: Mapping[str, Any], ror_ids: Sequence[str]) -> tuple[list[dict], list[str]]:
    """The declared organisations of one ROR dump release (zip with the v2 JSON member), as published."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            member = str(unit["member"])
            if member not in archive.namelist():
                raise ResearchEntityFormatError("schema_drift", f"the release has no member {member}")
            info = archive.getinfo(member)
            if info.file_size > 400_000_000:
                raise ResearchEntityFormatError("input_limit", "the dump member is larger than the parser allows")
            payload = json.loads(archive.read(member))
    except zipfile.BadZipFile as exc:
        raise ResearchEntityFormatError("schema_drift", "the release is not a zip file") from exc
    except json.JSONDecodeError as exc:
        raise ResearchEntityFormatError("schema_drift", "the dump member is not JSON") from exc
    if not isinstance(payload, list):
        raise ResearchEntityFormatError("schema_drift", "the dump member is not a list of organisations")
    wanted = {ror_id(i) for i in ror_ids}
    release = {"label": str(unit["label"]), "published_on": iso_day(unit["published_on"]),
               "path": str(unit["path"]), "member": str(unit["member"])}
    out = []
    for native in payload:
        rid = ror_id(native.get("id")) if isinstance(native, Mapping) else None
        if rid is None:
            raise ResearchEntityFormatError("schema_drift", "a dump item has no ROR id")
        if rid not in wanted:
            continue
        status = clean(native.get("status"))
        if status not in ROR_STATUSES:
            raise ResearchEntityFormatError("schema_drift", f"unknown ROR status {status!r}")
        names = [{"value": clean(n.get("value")), "types": sorted(n.get("types") or []), "lang": clean(n.get("lang"))}
                 for n in native.get("names") or []]
        if not names:
            raise ResearchEntityFormatError("schema_drift", f"ROR record {rid} has no names")
        display = next((n["value"] for n in names if "ror_display" in n["types"]), names[0]["value"])
        relationships = []
        for relation in native.get("relationships") or []:
            if relation.get("type") not in ROR_RELATIONSHIP_TYPES or ror_id(relation.get("id")) is None:
                raise ResearchEntityFormatError("schema_drift", f"unknown relationship in {rid}")
            relationships.append({"type": relation["type"], "id": ror_url(relation["id"]),
                                  "label": clean(relation.get("label"))})
        relationships.sort(key=lambda r: (r["type"], r["id"]))
        external_ids = [{"type": clean(e.get("type")), "all": [str(v) for v in e.get("all") or []],
                         "preferred": clean(e.get("preferred"))} for e in native.get("external_ids") or []]
        external_ids.sort(key=lambda e: str(e["type"]))
        locations = []
        for location in native.get("locations") or []:
            details = dict(location.get("geonames_details") or {})
            locations.append({"geonames_id": location.get("geonames_id"), "name": clean(details.get("name")),
                              "country_code": clean(details.get("country_code")),
                              "country_name": clean(details.get("country_name"))})
        admin = dict(native.get("admin") or {})
        last_modified = iso_day((admin.get("last_modified") or {}).get("date"))
        fields = {
            "ror_id": ror_url(rid), "display_name": display, "names": names,
            "types": sorted(native.get("types") or []), "status": status, "established": native.get("established"),
            "links": [{"type": clean(link.get("type")), "value": clean(link.get("value"))}
                      for link in native.get("links") or []],
            "locations": locations, "external_ids": external_ids, "relationships": relationships,
            "successors": [r["id"] for r in relationships if r["type"] == "successor"],
            "predecessors": [r["id"] for r in relationships if r["type"] == "predecessor"],
            "admin_last_modified": last_modified,
            "schema_version": clean((admin.get("last_modified") or {}).get("schema_version")),
            "domains": sorted(str(d) for d in native.get("domains") or []),
        }
        out.append(_record("ror-dump-zip", organisation_key(rid), native_id=ror_url(rid), title=display,
                           locator=ror_url(rid), fields=fields, as_of=release["published_on"],
                           native_revision=f"release:{release['label']}",
                           revision_order=_order(release["published_on"], release["label"]), status=status,
                           release=release))
    found = {r["fields"]["ror_id"] for r in out}
    missing = sorted(ror_url(i) for i in wanted if ror_url(i) not in found)
    out.sort(key=lambda r: r["record_key"])
    return out, missing


# ------------------------------------------------------------------ ORCID


def _asserted_by(source: Mapping[str, Any] | None, orcid: str) -> dict[str, Any]:
    source = dict(source or {})
    own = (source.get("source-orcid") or {}).get("path")
    client = (source.get("source-client-id") or {}).get("path")
    assertion = (source.get("assertion-origin-orcid") or {}).get("path")
    if own == orcid or (not client and assertion == orcid) or (not client and not own):
        return {"kind": "self", "name": None, "client_id": None}
    return {"kind": "member-client" if client else "other-orcid-holder",
            "name": clean((source.get("source-name") or {}).get("value")) if client else None,
            "client_id": clean(client)}


def _orcid_date(value: Mapping[str, Any] | None) -> str | None:
    value = dict(value or {})
    parts = [(value.get(k) or {}).get("value") for k in ("year", "month", "day")]
    parts = [p for p in parts if p]
    return "-".join(parts) if parts else None


def _last_modified(item: Mapping[str, Any]) -> str | None:
    return iso_instant((item.get("last-modified-date") or {}).get("value"))


def parse_orcid_record(raw: bytes, orcid: str, *, status: int = 200) -> dict[str, Any]:
    """One public ORCID record reduced to the fields allowed by the RE01 minimisation decision."""
    locator = f"https://orcid.org/{orcid}"
    withheld_sections = ["biography", "emails", "addresses", "keywords", "other-names", "researcher-urls",
                         "external-identifiers", "educations", "qualifications", "distinctions", "invited-positions",
                         "memberships", "services", "fundings", "peer-reviews", "research-resources"]
    if status in {409, 410}:
        fields = {"orcid": orcid, "name": None, "name_status": "withheld", "last_modified": None,
                  "status": "deactivated" if status == 409 else "deprecated", "employments": [], "works": [],
                  "withheld_sections": withheld_sections}
        return _record("orcid-record-json", researcher_key(orcid), native_id=locator, title=orcid, locator=locator,
                       fields=fields, as_of=None, native_revision=f"http-{status}", revision_order="",
                       status=fields["status"],
                       minimisation={"policy": MINIMISATION_POLICY, "subject": "natural-person",
                                     "withheld": ["name", *withheld_sections]})
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResearchEntityFormatError("schema_drift", "the ORCID response is not JSON") from exc
    if (payload.get("orcid-identifier") or {}).get("path") != orcid:
        raise ResearchEntityFormatError("schema_drift", "the ORCID response identifies another record")
    person = dict(payload.get("person") or {})
    name = dict(person.get("name") or {})
    public = name.get("visibility") == "PUBLIC"
    stored_name = ({"given_names": clean((name.get("given-names") or {}).get("value")),
                    "family_name": clean((name.get("family-name") or {}).get("value")),
                    "credit_name": clean((name.get("credit-name") or {}).get("value"))} if public else None)
    withheld = [k for k in ("biography", "emails", "addresses", "keywords", "other-names", "researcher-urls",
                            "external-identifiers")
                if isinstance(person.get(k), Mapping)
                and any(v for key, v in person[k].items() if key not in {"last-modified-date", "path"})]
    activities = dict(payload.get("activities-summary") or {})
    employments = []
    for group in (activities.get("employments") or {}).get("affiliation-group") or []:
        for summary in group.get("summaries") or []:
            item = dict(summary.get("employment-summary") or {})
            if item.get("visibility") != "PUBLIC":
                continue
            organisation = dict(item.get("organization") or {})
            address = dict(organisation.get("address") or {})
            disambiguated = dict(organisation.get("disambiguated-organization") or {})
            source = clean(disambiguated.get("disambiguation-source"))
            identifier = clean(disambiguated.get("disambiguated-organization-identifier"))
            employments.append({
                "put_code": item.get("put-code"),
                "organisation": {"name": clean(organisation.get("name")), "city": clean(address.get("city")),
                                 "region": clean(address.get("region")), "country": clean(address.get("country")),
                                 "disambiguated": ({"source": source, "id": identifier,
                                                    "ror_id": ror_url(identifier) if source == "ROR" else None}
                                                   if identifier else None)},
                "department": clean(item.get("department-name")), "role": clean(item.get("role-title")),
                "start_date": _orcid_date(item.get("start-date")), "end_date": _orcid_date(item.get("end-date")),
                "asserted_by": _asserted_by(item.get("source"), orcid), "last_modified": _last_modified(item),
            })
    for section in ("educations", "qualifications", "distinctions", "invited-positions", "memberships", "services",
                    "fundings", "peer-reviews", "research-resources"):
        if (activities.get(section) or {}).get("affiliation-group") or (activities.get(section) or {}).get("group"):
            withheld.append(section)
    works = []
    for group in (activities.get("works") or {}).get("group") or []:
        for summary in group.get("work-summary") or []:
            if summary.get("visibility") != "PUBLIC":
                continue
            external = []
            for ext in (summary.get("external-ids") or {}).get("external-id") or []:
                kind = clean(ext.get("external-id-type"))
                value = clean((ext.get("external-id-normalized") or {}).get("value")) or clean(
                    ext.get("external-id-value"))
                if kind == "doi":
                    value = normalize_doi(value) or value
                external.append({"type": kind, "value": value, "relationship": clean(ext.get("external-id-relationship"))})
            external.sort(key=lambda e: (str(e["type"]), str(e["value"])))
            year = ((summary.get("publication-date") or {}).get("year") or {}).get("value")
            works.append({
                "put_code": summary.get("put-code"), "type": clean(summary.get("type")),
                "title": clean(((summary.get("title") or {}).get("title") or {}).get("value")),
                "publication_year": clean(year), "external_ids": external,
                "asserted_by": _asserted_by(summary.get("source"), orcid), "last_modified": _last_modified(summary),
            })
    employments.sort(key=lambda e: (str(e["start_date"] or ""), str(e["put_code"])))
    works.sort(key=lambda w: str(w["put_code"]))
    last_modified = iso_instant(((payload.get("history") or {}).get("last-modified-date") or {}).get("value"))
    fields = {"orcid": orcid, "name": stored_name, "name_status": "public" if public else "not-public",
              "last_modified": last_modified, "status": "active", "employments": employments, "works": works,
              "withheld_sections": sorted(set(withheld))}
    display = None
    if stored_name:
        display = stored_name["credit_name"] or " ".join(
            v for v in (stored_name["given_names"], stored_name["family_name"]) if v) or None
    return _record("orcid-record-json", researcher_key(orcid), native_id=locator, title=display or orcid,
                   locator=locator, fields=fields, as_of=last_modified, native_revision=last_modified,
                   revision_order=_order(last_modified), status="active",
                   minimisation={"policy": MINIMISATION_POLICY, "subject": "natural-person",
                                 "withheld": sorted(set(withheld) | ({"name"} if not public else set()))})


# ------------------------------------------------------------------ DataCite


def _person(person: Mapping[str, Any], position: int) -> tuple[dict[str, Any], list[str]]:
    kind = clean(person.get("nameType")) or "Personal"
    affiliation_ids = sorted({ror_url(a.get("affiliationIdentifier")) for a in person.get("affiliation") or []
                              if isinstance(a, Mapping) and ror_url(a.get("affiliationIdentifier"))})
    identifiers = [{"scheme": clean(i.get("nameIdentifierScheme")), "value": clean(i.get("nameIdentifier"))}
                   for i in person.get("nameIdentifiers") or []]
    if kind == "Organizational":
        return ({"position": position, "name_type": kind, "name": clean(person.get("name")),
                 "identifiers": identifiers, "affiliation_ids": affiliation_ids,
                 "contributor_type": clean(person.get("contributorType"))}, [])
    orcid = next((valid_orcid(i["value"]) for i in identifiers if str(i["scheme"] or "").upper() == "ORCID"
                  and valid_orcid(i["value"])), None)
    withheld = [k for k in ("name", "givenName", "familyName") if clean(person.get(k))]
    if any(isinstance(a, Mapping) and clean(a.get("name")) for a in person.get("affiliation") or []):
        withheld.append("affiliation names")
    if any(str(i["scheme"] or "").upper() != "ORCID" for i in identifiers):
        withheld.append("other name identifiers")
    return ({"position": position, "name_type": "Personal", "orcid": orcid, "affiliation_ids": affiliation_ids,
             "contributor_type": clean(person.get("contributorType"))}, withheld)


def parse_datacite_doi(raw: bytes, doi: str, *, status: int = 200) -> dict[str, Any]:
    locator = f"https://doi.org/{doi}"
    if status == 404:
        fields = {"doi": doi, "state": "not-served", "note": "DataCite no longer serves this DOI (HTTP 404)"}
        return _record("datacite-doi-json", dataset_key(doi), native_id=doi, title=doi, locator=locator, fields=fields,
                       as_of=None, native_revision="http-404", revision_order="",
                       status="unavailable",
                       minimisation={"policy": MINIMISATION_POLICY, "subject": "organisation", "withheld": []})
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResearchEntityFormatError("schema_drift", "the DataCite response is not JSON") from exc
    data = dict(payload.get("data") or {})
    attributes = dict(data.get("attributes") or {})
    if normalize_doi(attributes.get("doi") or data.get("id")) != doi:
        raise ResearchEntityFormatError("schema_drift", "the DataCite response describes another DOI")
    withheld: set[str] = set()
    creators, contributors = [], []
    for position, person in enumerate(attributes.get("creators") or []):
        view, dropped = _person(person, position)
        creators.append(view)
        withheld |= {f"creators.{d}" for d in dropped}
    for position, person in enumerate(attributes.get("contributors") or []):
        view, dropped = _person(person, position)
        contributors.append(view)
        withheld |= {f"contributors.{d}" for d in dropped}
    related = []
    for item in attributes.get("relatedIdentifiers") or []:
        relation = clean(item.get("relationType"))
        if relation not in RELATION_TYPES:
            raise ResearchEntityFormatError("schema_drift", f"unknown relationType {relation!r}")
        identifier_type = clean(item.get("relatedIdentifierType"))
        value = clean(item.get("relatedIdentifier"))
        related.append({"relation_type": relation, "identifier_type": identifier_type, "identifier": value,
                        "doi": normalize_doi(value) if identifier_type == "DOI" else None,
                        "resource_type_general": clean(item.get("resourceTypeGeneral"))})
    related.sort(key=lambda r: (r["relation_type"], str(r["identifier_type"]), str(r["identifier"])))
    funding = [{"funder_name": clean(f.get("funderName")), "funder_identifier": clean(f.get("funderIdentifier")),
                "funder_identifier_type": clean(f.get("funderIdentifierType")),
                "award_number": clean(f.get("awardNumber")), "award_title": clean(f.get("awardTitle")),
                "award_uri": clean(f.get("awardUri"))} for f in attributes.get("fundingReferences") or []]
    titles = [clean(t.get("title")) for t in attributes.get("titles") or [] if clean(t.get("title"))]
    types = dict(attributes.get("types") or {})
    updated = iso_instant(attributes.get("updated"))
    metadata_version = attributes.get("metadataVersion")
    publisher = attributes.get("publisher")
    fields = {
        "doi": doi, "state": clean(attributes.get("state")),
        "resource_type_general": clean(types.get("resourceTypeGeneral")), "resource_type": clean(types.get("resourceType")),
        "titles": titles, "publisher": clean(publisher.get("name") if isinstance(publisher, Mapping) else publisher),
        "publication_year": attributes.get("publicationYear"), "version": clean(attributes.get("version")),
        "metadata_version": metadata_version, "created": iso_instant(attributes.get("created")),
        "registered": iso_instant(attributes.get("registered")), "updated": updated,
        "url": clean(attributes.get("url")), "creators": creators, "contributors": contributors,
        "related_identifiers": related, "funding_references": funding,
        "rights": sorted({clean(r.get("rightsIdentifier")) or clean(r.get("rights")) or ""
                          for r in attributes.get("rightsList") or []} - {""}),
    }
    return _record("datacite-doi-json", dataset_key(doi), native_id=doi, title=titles[0] if titles else doi,
                   locator=locator, fields=fields, as_of=updated,
                   native_revision=f"metadataVersion:{metadata_version}|updated:{updated}",
                   revision_order=_order(updated, metadata_version if isinstance(metadata_version, int) else 0),
                   status=clean(attributes.get("state")) or "findable",
                   minimisation={"policy": MINIMISATION_POLICY,
                                 "subject": "natural-person" if withheld else "organisation",
                                 "withheld": sorted(withheld)})


# ------------------------------------------------------------------ CORDIS


def _csv_member(archive: zipfile.ZipFile, member: str, required: Sequence[str]) -> list[dict[str, str]]:
    names = {n.rsplit("/", 1)[-1]: n for n in archive.namelist()}
    if member not in names:
        raise ResearchEntityFormatError("schema_drift", f"the export has no {member}")
    text = archive.read(names[member]).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    header = [h.strip() for h in reader.fieldnames or []]
    missing = [c for c in required if c not in header]
    if missing:
        raise ResearchEntityFormatError("schema_drift", f"{member} lacks columns {missing}")
    return [{str(k).strip(): (v if v is None else str(v)) for k, v in row.items()} for row in reader]


def _money(value: Any) -> dict[str, Any] | None:
    raw = clean(value)
    if raw is None:
        return None
    return {"amount": amount(raw), "currency": CORDIS_CURRENCY, "as_published": raw,
            "currency_basis": "CORDIS exports publish euro amounts without a currency column; stated as EUR"}


def parse_cordis_export(raw: bytes, unit: Mapping[str, Any], project_ids: Sequence[str]
                        ) -> tuple[list[dict], list[str]]:
    programme = str(unit["programme"]).upper()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            projects = _csv_member(archive, "project.csv", PROJECT_COLUMNS)
            organisations = _csv_member(archive, "organization.csv", ORGANIZATION_COLUMNS)
    except zipfile.BadZipFile as exc:
        raise ResearchEntityFormatError("schema_drift", "the export is not a zip file") from exc
    wanted = {str(p) for p in project_ids}
    participants: dict[str, list[dict[str, Any]]] = {}
    for row in organisations:
        pid = clean(row.get("projectID"))
        if pid not in wanted:
            continue
        participants.setdefault(pid, []).append({
            "pic": clean(row.get("organisationID")), "name": clean(row.get("name")),
            "short_name": clean(row.get("shortName")), "vat_number": clean(row.get("vatNumber")),
            "sme": clean(row.get("SME")), "activity_type": clean(row.get("activityType")),
            "city": clean(row.get("city")), "country": clean(row.get("country")),
            "nuts_code": clean(row.get("nutsCode")), "organization_url": clean(row.get("organizationURL")),
            "role": clean(row.get("role")), "order": clean(row.get("order")),
            "ec_contribution": _money(row.get("ecContribution")),
            "net_ec_contribution": _money(row.get("netEcContribution")),
            "total_cost": _money(row.get("totalCost")),
            "end_of_participation": clean(row.get("endOfParticipation")), "active": clean(row.get("active")),
            "content_update_date": iso_instant(row.get("contentUpdateDate")),
        })
    out = []
    for row in projects:
        pid = clean(row.get("id"))
        if pid not in wanted:
            continue
        if str(row.get("frameworkProgramme") or "").upper() != programme:
            raise ResearchEntityFormatError("schema_drift", f"project {pid} names another programme")
        members = sorted(participants.get(pid, []), key=lambda p: (int(p["order"]) if str(p["order"] or "").isdigit()
                                                                   else 10**6, str(p["pic"])))
        updates = [iso_instant(row.get("contentUpdateDate"))] + [p["content_update_date"] for p in members]
        updated = max(u for u in updates if u) if any(updates) else None
        fields = {
            "project_id": pid, "programme": programme, "acronym": clean(row.get("acronym")),
            "title": clean(row.get("title")), "status": clean(row.get("status")),
            "start_date": iso_day(row.get("startDate")), "end_date": iso_day(row.get("endDate")),
            "total_cost": _money(row.get("totalCost")), "ec_max_contribution": _money(row.get("ecMaxContribution")),
            "legal_basis": clean(row.get("legalBasis")),
            "topics": sorted(t.strip() for t in str(row.get("topics") or "").split(",") if t.strip()),
            "master_call": clean(row.get("masterCall")), "sub_call": clean(row.get("subCall")),
            "funding_scheme": clean(row.get("fundingScheme")), "ec_signature_date": iso_day(row.get("ecSignatureDate")),
            "grant_doi": normalize_doi(row.get("grantDoi")), "rcn": clean(row.get("rcn")),
            "content_update_date": iso_instant(row.get("contentUpdateDate")), "participants": members,
            "withheld_as_unneeded": ["street", "postCode", "contactForm", "geolocation"],
        }
        locator = f"https://cordis.europa.eu/project/id/{quote(pid)}"
        out.append(_record("cordis-projects-csv-zip", project_key(programme, pid), native_id=pid,
                           title=fields["acronym"] or fields["title"], locator=locator, fields=fields, as_of=updated,
                           native_revision=f"contentUpdateDate:{updated}", revision_order=_order(updated),
                           status=fields["status"] or "unknown",
                           release={"label": f"{programme} export", "path": str(unit["path"])}))
    found = {r["native_id"] for r in out}
    out.sort(key=lambda r: r["record_key"])
    return out, sorted(wanted - found)


# ------------------------------------------------------------------ declarations and units


def _units(fmt: str, selection: Mapping[str, Any]) -> list[dict[str, Any]]:
    key = FORMATS[fmt]["unit"]
    raw = selection.get(key) or []
    units = [dict(u) if isinstance(u, Mapping) else {"id": u} for u in raw]
    if not 1 <= len(units) <= MAX_UNITS:
        raise SourcePackError("invalid_manifest", f"a research-entities selection names 1-{MAX_UNITS} {key}")
    for unit in units:
        if fmt == "ror-dump-zip":
            if not clean(unit.get("label")) or iso_day(unit.get("published_on")) is None:
                raise SourcePackError("invalid_manifest", "a ROR release states its label and publication date")
            if not str(unit.get("path") or "").startswith("/records/") or not clean(unit.get("member")):
                raise SourcePackError("invalid_manifest", "a ROR release names its Zenodo file path and JSON member")
        elif fmt == "orcid-record-json":
            if valid_orcid(unit.get("id")) is None:
                raise SourcePackError("invalid_manifest", f"not a valid ORCID iD: {unit.get('id')!r}")
        elif fmt == "datacite-doi-json":
            if normalize_doi(unit.get("id")) is None:
                raise SourcePackError("invalid_manifest", f"not a DOI: {unit.get('id')!r}")
        elif fmt == "cordis-projects-csv-zip":
            programme = str(unit.get("programme") or "").upper()
            if programme not in CORDIS_PROGRAMMES or unit.get("path") != CORDIS_PROGRAMMES[programme]:
                raise SourcePackError("invalid_manifest", "a CORDIS unit names a known programme and its export path")
            ids = [str(i) for i in unit.get("project_ids") or []]
            if not 1 <= len(ids) <= MAX_SELECTED_IDS or not all(re.fullmatch(r"\d{5,9}", i) for i in ids):
                raise SourcePackError("invalid_manifest", "a CORDIS unit declares 1-200 numeric project ids")
    if fmt == "ror-dump-zip":
        ids = selection.get("ror_ids") or []
        if not 1 <= len(ids) <= MAX_SELECTED_IDS or any(ror_id(i) is None for i in ids):
            raise SourcePackError("invalid_manifest", "a ROR selection declares 1-200 valid ROR ids")
    return units


def research_entities_declaration(source: Mapping[str, Any]) -> dict[str, Any]:
    declared = dict(source.get("research_entities") or {})
    fmt = declared.get("format")
    if fmt not in FORMATS or FORMATS[fmt]["provider"] != declared.get("provider"):
        raise SourcePackError("invalid_manifest", "research-entities sources declare a matching provider and format")
    if declared.get("live_verification") not in {"unverified-live", "verified-live"}:
        raise SourcePackError("invalid_manifest", "research-entities sources state their LIVE_VERIFICATION status")
    if declared.get("minimisation") != MINIMISATION_POLICY:
        raise SourcePackError("invalid_manifest", "research-entities sources declare the RE01 minimisation policy")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[declared["provider"]]:
        raise SourcePackError("invalid_manifest", "the endpoint is not the provider's documented host")
    units = _units(fmt, dict(declared.get("selection") or {}))
    if len(units) > int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "more declared units than the source's page budget")
    if FORMATS[fmt]["keyed"] != (dict(source.get("auth") or {}).get("kind") == "required-secret"):
        raise SourcePackError("invalid_manifest", "keyed research-entities formats declare a required secret")
    return declared


def request_for(fmt: str, unit: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """The request path (relative to the endpoint) and its parameters for one unit."""
    if fmt == "ror-dump-zip":
        return str(unit["path"]), {}
    if fmt == "orcid-record-json":
        return f"/{valid_orcid(unit['id'])}/record", {}
    if fmt == "datacite-doi-json":
        return f"/dois/{quote(normalize_doi(unit['id']) or '', safe='/')}", {"affiliation": "true", "publisher": "true"}
    if fmt == "cordis-projects-csv-zip":
        return str(unit["path"]), {}
    raise SourcePackError("invalid_manifest", f"unknown research-entities format {fmt!r}")


def parse_unit(fmt: str, raw: bytes, unit: Mapping[str, Any], selection: Mapping[str, Any], *, status: int = 200
               ) -> tuple[list[dict[str, Any]], list[str]]:
    """Parse one unit's response; every record honours the RE01 minimisation decision."""
    if fmt == "ror-dump-zip":
        records, missing = parse_ror_release(raw, unit, list(selection.get("ror_ids") or []))
    elif fmt == "orcid-record-json":
        records, missing = [parse_orcid_record(raw, valid_orcid(unit["id"]) or "", status=status)], []
    elif fmt == "datacite-doi-json":
        records, missing = [parse_datacite_doi(raw, normalize_doi(unit["id"]) or "", status=status)], []
    elif fmt == "cordis-projects-csv-zip":
        records, missing = parse_cordis_export(raw, unit, [str(i) for i in unit["project_ids"]])
    else:
        raise ResearchEntityFormatError("schema_drift", f"unknown research-entities format {fmt!r}")
    for record in records:
        if minimisation_violations(record):
            raise ResearchEntityFormatError("minimisation_violation", f"{record['record_key']} carries personal "
                                            "fields the RE01 decision does not allow")
    return records, missing


# ------------------------------------------------------------------ runtime adapter


class ResearchEntitiesAdapter:
    """Fetch one declared unit (a release, an iD, a DOI or a programme export) per page and emit its records."""

    accepts_transport = True
    connector = CONNECTOR

    def __init__(self, source: Mapping[str, Any], *, transport: Callable[..., Mapping[str, Any]] | None = None,
                 secret: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import HTTPSPageAdapter

        self.source = json.loads(json.dumps(source))
        self.declared = research_entities_declaration(self.source)
        self.format = self.declared["format"]
        self.selection = dict(self.declared.get("selection") or {})
        self.units = _units(self.format, self.selection)
        self.secret = secret
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=int(source["budgets"]["max_bytes"]))
        self.transport = transport
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "research_entities": {"provider": self.declared["provider"], "format": self.format,
                                  "units": len(self.units), "keyed": bool(FORMATS[self.format]["keyed"]),
                                  "minimisation": MINIMISATION_POLICY,
                                  "live_verification": self.declared["live_verification"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "research-entities runs fetch the declared selection only")

    def _get(self, path: str, params: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]]:
        from src.ingestion.source_pack_runtime import _retry_after_ms

        endpoint = self.source["endpoint"].rstrip("/")
        url = endpoint + path
        host = (urlsplit(endpoint).hostname or "").casefold()
        headers = {"Accept": "application/vnd.orcid+json" if self.format == "orcid-record-json"
                   else "application/vnd.api+json, application/json, application/zip"}
        if FORMATS[self.format]["keyed"]:
            if not self.secret:
                raise SourcePackError("authentication_failed", "the ORCID public API needs its client token "
                                      "(NOESIS_ORCID_PUBLIC_TOKEN); the source is unavailable without it")
            headers["Authorization"] = "Bearer " + self.secret
        response = self.transport(url=url, params=dict(sorted(params.items())), headers=headers,
                                  timeout=int(self.definition["limits"]["timeout_ms"]) / 1000)
        final_host = (urlsplit(str(response.get("final_url") or url)).hostname or "").casefold()
        if final_host != host:
            raise SourcePackError("network_policy", "research-entities response was served from another host")
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
        removal = (self.format == "orcid-record-json" and status in {409, 410}) or (
            self.format == "datacite-doi-json" and status == 404)
        if status >= 400 and not removal:
            raise SourcePackError("schema_drift", f"request returned HTTP {status}")
        query = urlencode(sorted(params.items()))
        return raw, {"path": path + ("?" + query if query else ""), "status": status,
                     "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                     "origin": "fixture" if response.get("origin") == "fixture" else "live"}

    def fetch_page(self, request: Mapping[str, Any], *, cursor: str | None):
        from src.ingestion.source_pack_runtime import RuntimePage

        self._check(request)
        index = 0 if cursor is None else int(cursor) if str(cursor).isdigit() else -1
        if not 0 <= index < len(self.units):
            raise SourcePackError("cursor_drift", "cursor is outside the declared selection")
        unit = self.units[index]
        path, params = request_for(self.format, unit)
        raw, receipt_request = self._get(path, params)
        try:
            records, missing = parse_unit(self.format, raw, unit, self.selection, status=receipt_request["status"])
        except ResearchEntityFormatError as exc:
            raise SourcePackError("response_too_large" if exc.code == "input_limit" else "schema_drift",
                                  f"{exc.code}: {exc}") from exc
        limit = int(request.get("limit") or self.definition["limits"]["max_results"])
        if len(records) > limit:
            # Never a truncated unit: a missing record would read as one the registry did not publish.
            raise SourcePackError("budget_exhausted", "unit has more records than the run's result budget")
        origin = receipt_request["origin"]
        receipt = {
            "contract": RECEIPT_CONTRACT, "source_id": self.source["source_id"],
            "provider": self.declared["provider"], "format": self.format, "unit_index": index,
            "unit": {k: v for k, v in unit.items() if k != "project_ids"},
            "requests": [receipt_request], "records": len(records), "not_in_response": missing,
            "evidence_origin": origin, "live_verification": self.declared["live_verification"],
            "minimisation": MINIMISATION_POLICY, "final_page": index + 1 >= len(self.units),
        }
        out = []
        for record in records:
            record = {**record, "evidence_origin": origin}
            out.append({
                "id": record["record_key"], "title": record["title"], "url": record["locator"], "language": "en",
                "published_at": record["as_of"], "updated_at": record["native_revision"],
                "content": json.dumps(record, sort_keys=True, ensure_ascii=False),
                "research_entity_record": record, "research_entity_receipt": receipt,
            })
        next_cursor = None if receipt["final_page"] else str(index + 1)
        return RuntimePage(tuple(out), next_cursor, len(raw), receipt=receipt)


FIXTURE_SECRET = "fixture-token-not-a-real-credential"
ADAPTERS = {CONNECTOR: ResearchEntitiesAdapter}


def fixture_transport(pages: Sequence[Mapping[str, Any]]) -> Callable[..., Mapping[str, Any]]:
    """Replay authored responses keyed by URL path (+ sorted query); ``body_base64`` carries binary bodies."""
    import base64

    by_key = {page["request"]: page for page in pages}

    def transport(*, url, params, headers, timeout):
        del headers, timeout
        query = urlencode(sorted(dict(params or {}).items()))
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


def fixture_request(fmt: str, unit: Mapping[str, Any], endpoint: str) -> str:
    """The key :func:`fixture_transport` files a response under (endpoint path + request path and sorted query)."""
    path, params = request_for(fmt, unit)
    query = urlencode(sorted(params.items()))
    return (urlsplit(endpoint).path.rstrip("/") + path) + ("?" + query if query else "")


def replay_native_fixture(source: Mapping[str, Any], fixture: Mapping[str, Any]) -> list[dict[str, Any]]:
    adapter = ResearchEntitiesAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
                                      secret=FIXTURE_SECRET)
    records, cursor = [], None
    for _ in range(len(adapter.units)):
        page = adapter.fetch_page({"operation": min(source["operations"]), "parameters": {},
                                   "limit": int(source["budgets"]["max_results"])}, cursor=cursor)
        records += [dict(item) for item in page.records]
        cursor = page.next_cursor
        if cursor is None:
            break
    return records


__all__ = [
    "ADAPTERS",
    "BOUNDED_COVERAGE",
    "CONNECTOR",
    "EXCLUSIONS",
    "FIXTURE_SECRET",
    "FORMATS",
    "LIVE_VERIFICATION",
    "MINIMISATION",
    "PROVIDER_CONTRACTS",
    "RECORD_CONTRACT",
    "REVIEW_BOUNDARY",
    "ResearchEntitiesAdapter",
    "ResearchEntityFormatError",
    "fixture_request",
    "fixture_transport",
    "minimisation_violations",
    "normalize_doi",
    "parse_unit",
    "replay_native_fixture",
    "request_for",
    "research_entities_declaration",
    "ror_url",
    "valid_orcid",
]
