"""Biodiversity sources for the Climate and Environment pack: Catalogue of Life, GBIF and IUCN (#2220, BD01 and BD03-BD05).

Three providers run as sources of the ``climate-environment-biodiversity``
source pack (``packs/climate-environment/source_packs/``, connector
``biodiversity``) through :mod:`src.ingestion.source_pack_runtime` - licence
acceptance, budgets, receipts, checkpoints and the runtime's same-host HTTPS
transport - each under a recorded access contract (:data:`PROVIDER_CONTRACTS`,
documented in ``docs/development/biodiversity-evidence/source-audit.md``). It
sits beside the environment adapter (:mod:`src.ingestion.environment_providers`)
and follows the same rules: explicit bounded selections, fail-closed parsers,
provider hosts only, values as published.

* **Catalogue of Life** (``col``, BD03) - ChecklistBank release metadata and
  name usages keyed by CoL ID *and* release: accepted names, synonyms (with
  the accepted name the release states) and higher classification per
  release. Two releases in one selection give two revisions per usage.
* **GBIF** (``gbif``, BD04) - backbone species (``taxonKey``), one small
  occurrence search page per declared taxon and place (``gbifID``,
  ``occurrenceID``, ``datasetKey``), publishing dataset metadata (licence, DOI,
  citation) and existing download DOIs. Coordinates are kept exactly as
  published, with ``coordinateUncertaintyInMeters``, ``dataGeneralizations``,
  ``informationWithheld`` and issue flags; withheld coordinates stay withheld.
* **IUCN Red List** (``iucn``, BD05) - under the BD01 **reference-only**
  decision (:data:`IUCN_LICENCE_DECISION`): a taxon's assessment list and at
  most ``max_assessments`` assessments, stored at citation level; narrative,
  threats, habitats, population and spatial data are never stored and are
  reported as dropped in the page receipt.

Every provider is ``unverified-live`` until a dated live run (BD13, #2533);
request paths and field names marked *verify* are authored from public
documentation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from src.ingestion.source_packs import SourcePackError
from src.kb.biodiversity_records import (
    IUCN_REDISTRIBUTION,
    LICENCE_TIER_IUCN,
    BiodiversityError,
    digest,
    generalisation,
    licence,
    statement,
    status_class,
)

CONNECTOR = "biodiversity"
ADAPTER_CONTRACT = "noesis-source-pack-runtime-adapter-v1"
PROVIDERS = ("col", "gbif", "iucn")
PROVIDER_HOSTS = {"col": ("api.checklistbank.org",), "gbif": ("api.gbif.org",), "iucn": ("api.iucnredlist.org",)}
GBIF_OCCURRENCE_LIMIT = 300  # the API's own page bound; selections stay far below it
MAX_ASSESSMENTS = 10
PROVIDER_CONTRACTS: dict[str, dict[str, Any]] = {
    "col": {
        "publisher": "Catalogue of Life (via ChecklistBank)",
        "access": "ChecklistBank API, HTTPS GET /dataset/{releaseKey} and /dataset/{releaseKey}/nameusage/{id}",
        "endpoints": ["/dataset/{releaseKey}", "/dataset/{releaseKey}/nameusage/{id} (verify whether classification "
                      "needs /dataset/{releaseKey}/taxon/{id}/classification)"],
        "authentication": "none",
        "licence": "CC BY 4.0 for Catalogue of Life releases (verify per release metadata)",
        "terms_url": "https://www.catalogueoflife.org/about/citing",
        "attribution": "Catalogue of Life Checklist, release version and DOI as cited by the release metadata",
        "redistribution": "permitted with attribution and the release citation",
        "rate_limits": "no published hard limit (verify); one request per selected usage",
        "versioning": "monthly releases and an annual checklist, each its own dataset key, version, issue date and "
                      "DOI; the same CoL ID in the next release is a new revision; status changes between releases "
                      "are dated by the later release and cite both",
        "revision_behaviour": "new revision per release; earlier releases never deleted",
    },
    "gbif": {
        "publisher": "Global Biodiversity Information Facility (GBIF)",
        "access": "GBIF API v1, HTTPS GET /v1/species/{key}, /v1/occurrence/search (one small page), "
                  "/v1/dataset/{key}, /v1/occurrence/download/{key} (metadata of an existing download only)",
        "endpoints": ["/v1/species/{taxonKey}", "/v1/occurrence/search?taxonKey&country|geometry&limit&offset=0",
                      "/v1/dataset/{datasetKey}", "/v1/occurrence/download/{downloadKey}"],
        "authentication": "none for search and metadata; download creation (asynchronous, account-gated) is not "
                          "implemented",
        "licence": "per publishing dataset: CC0 1.0, CC BY 4.0 or CC BY-NC 4.0 (the record's license); the backbone "
                   "taxonomy CC BY 4.0 (verify)",
        "terms_url": "https://www.gbif.org/terms",
        "attribution": "each publishing dataset (title, publisher, DOI); a download's DOI must be cited for any use "
                       "of the downloaded data",
        "redistribution": "by each record's dataset licence; CC BY-NC records are flagged non-commercial",
        "rate_limits": "search limit <= 300 and offset + limit <= 100000 (API bounds); undocumented soft limits on "
                       "the public API (verify); downloads are asynchronous and rate-limited per user",
        "versioning": "occurrences are re-interpreted and re-published; each retrieval is a revision; a record "
                      "absent from a later complete page of the same selection gets a dated tombstone",
        "revision_behaviour": "new revision on change; tombstone on removal; earlier revisions stay queryable",
    },
    "iucn": {
        "publisher": "IUCN Red List of Threatened Species",
        "access": "IUCN Red List API v4, HTTPS GET /api/v4/taxa/sis/{sis_id} and /api/v4/assessment/{id} (verify "
                  "field names)",
        "endpoints": ["/api/v4/taxa/sis/{sis_id}", "/api/v4/assessment/{assessment_id}"],
        "authentication": "API token issued by IUCN to a registered user, held as the NOESIS_IUCN_API_TOKEN secret "
                          "reference; never stored in manifests, receipts or records",
        "licence": "IUCN Red List Terms of Use: non-commercial use; no redistribution or derivative datasets without "
                   "IUCN permission; cite each assessment (verify current terms)",
        "terms_url": "https://www.iucnredlist.org/terms/terms-of-use",
        "attribution": "the assessment citation as published (e.g. 'The IUCN Red List of Threatened Species <year>: "
                       "e.T...A...') with its URL",
        "redistribution": "not permitted beyond citation-level facts (reference-only decision)",
        "rate_limits": "polite use requested (verify limits); one taxon and at most 10 assessments per selection",
        "versioning": "every historical assessment is its own record; the assessor's `latest` flag designates the "
                      "current one; global and regional assessments are separate by scope",
        "revision_behaviour": "append-only; a changed latest designation is a new revision of that assessment",
        "licence_decision": "reference-only",
    },
}
IUCN_LICENCE_DECISION = {
    "decision": "reference-only",
    "alternatives_considered": ["acquire in full (rejected: redistribution and derivative-dataset restrictions)",
                                "excluded (rejected: citation-level facts are needed to cite conservation status)"],
    "stored": ["assessment_id", "taxon_id", "scientific_name", "category", "criteria", "assessment_date",
               "year_published", "scope", "latest", "possibly_extinct", "possibly_extinct_in_wild", "change_reason",
               "citation", "url"],
    "withheld": ["rationale and narrative documentation", "habitats and ecology", "threats and stresses",
                 "conservation actions", "use and trade", "population size and trend", "range maps and locations",
                 "bibliography text", "supplementary information"],
    "answers": "citation-level fields with the licence tier and redistribution notice",
    "exports": "category, year published, scope, latest designation, citation and URL only",
    "notice": IUCN_REDISTRIBUTION,
}
SENSITIVE_SPECIES_POLICY = {
    "coordinates": "stored exactly as published (already generalised by the publisher where it chose to); never "
                   "de-generalised, never geocoded from locality text",
    "flags_kept": ["coordinateUncertaintyInMeters", "coordinatePrecision", "dataGeneralizations",
                   "informationWithheld", "issues"],
    "withheld": "a record without published coordinates stays without coordinates",
    "linking": "generalised records link only to places at or above the stated precision; without a stated "
               "precision only by published country code",
}
BOUNDED_COVERAGE = {
    "taxa": ["Passer domesticus", "Lutra lutra", "Corvus cornix", "Corvus corone"],
    "checklist_releases": "two named Catalogue of Life releases (never 'latest')",
    "places": ["country DE", "a Berlin bounding polygon"],
    "occurrence_page_limit": 20,
    "iucn": "assessment history of the selected taxa, at most 10 assessments per taxon",
    "datasets": "publishing datasets named in the selection and one existing download",
}
LIVE_VERIFICATION = {
    provider: {"status": "unverified-live", "intended": "live-verified after a dated bounded run (BD13, #2533)",
               "note": "no dated live run from this runtime; offline fixtures only"}
    for provider in PROVIDERS
}
LIVE_VERIFICATION["iucn"]["credential"] = "NOESIS_IUCN_API_TOKEN not configured"
LIVE_VERIFICATION["gbif-download-creation"] = {"status": "not-implemented",
                                               "note": "asynchronous downloads need a GBIF account; out of scope"}
# IUCN assessment keys that are stored (citation level); every other top-level key is dropped and reported.
IUCN_KEPT = frozenset({"assessment_id", "sis_taxon_id", "taxon", "year_published", "assessment_date", "latest",
                       "possibly_extinct", "possibly_extinct_in_the_wild", "red_list_category",
                       "red_list_category_code", "criteria", "scopes", "url", "citation", "reason_for_change"})


class BiodiversityFormatError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _number(value: Any) -> float | int | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            number = float(str(value))
        except ValueError as exc:
            raise BiodiversityFormatError("schema_drift", f"not a number: {value!r}") from exc
        return int(number) if number.is_integer() else number
    return value


def _day(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    match = re.match(r"^(\d{4}(?:-\d{2}(?:-\d{2})?)?)", text)
    return match.group(1) if match else None


def _event_date(value: Any) -> str | None:
    text = _text(value)
    if text is None:
        return None
    match = re.match(r"^(\d{4}(?:-\d{2}(?:-\d{2})?)?)(?:T[0-9:.]+(?:Z|[+-]\d{2}:?\d{2})?)?"
                     r"(/\d{4}(?:-\d{2}(?:-\d{2})?)?)?$", text)
    return text if match else None


# ------------------------------------------------------------------ selections


def selection_entries(source: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    declared = dict(source.get("biodiversity") or {})
    provider = str(declared.get("provider") or "")
    if provider not in PROVIDERS:
        raise SourcePackError("invalid_manifest", f"biodiversity sources declare a provider in {PROVIDERS}")
    host = (urlsplit(str(source.get("endpoint") or "")).hostname or "").casefold()
    if host not in PROVIDER_HOSTS[provider]:
        raise SourcePackError("invalid_manifest", f"{provider} is fetched from {PROVIDER_HOSTS[provider][0]} only")
    entries = [dict(e) for e in declared.get("selection") or []]
    if not 1 <= len(entries) <= int(dict(source.get("budgets") or {}).get("max_pages", 1)):
        raise SourcePackError("invalid_manifest", "a biodiversity source selects 1..max_pages pages explicitly")
    kinds = {"col": {"release", "usage"}, "gbif": {"species", "occurrences", "dataset", "download"},
             "iucn": {"taxon"}}[provider]
    for entry in entries:
        kind = entry.get("kind")
        if kind not in kinds:
            raise SourcePackError("invalid_manifest", f"{provider} selections are one of {sorted(kinds)}")
        if provider == "col" and not (entry.get("dataset_key") and (kind == "release" or entry.get("id"))):
            raise SourcePackError("invalid_manifest", "CoL selections name a release dataset key (and a CoL ID)")
        if provider == "gbif":
            if kind == "occurrences":
                limit = int(entry.get("limit") or 0)
                if not entry.get("taxon_key") or not (entry.get("country") or entry.get("geometry")):
                    raise SourcePackError("invalid_manifest", "an occurrence page is bounded to a taxon and a place "
                                                              "(country code or WKT geometry)")
                if not 1 <= limit <= min(GBIF_OCCURRENCE_LIMIT, int(source["budgets"]["max_results"])):
                    raise SourcePackError("invalid_manifest", "occurrence pages are small: 1 <= limit <= 300")
            elif not entry.get("key"):
                raise SourcePackError("invalid_manifest", f"a GBIF {kind} selection names its key")
        if provider == "iucn":
            if not re.fullmatch(r"\d{1,12}", str(entry.get("sis_id") or "")):
                raise SourcePackError("invalid_manifest", "IUCN selections name a numeric SIS taxon id")
            if not 1 <= int(entry.get("max_assessments") or MAX_ASSESSMENTS) <= MAX_ASSESSMENTS:
                raise SourcePackError("invalid_manifest", "at most 10 assessments per IUCN taxon")
    return provider, entries


def selection_key(provider: str, entry: Mapping[str, Any]) -> str:
    return provider + ":" + digest({k: entry[k] for k in sorted(entry) if k != "label"})[:16]


def request_for(provider: str, entry: Mapping[str, Any]) -> tuple[str, str, dict[str, str]]:
    """(role, path, query) of the first request of one selected page; paths are relative to the endpoint."""
    kind = entry["kind"]
    if provider == "col":
        if kind == "release":
            return "release", f"/dataset/{quote(str(entry['dataset_key']))}", {}
        return "usage", f"/dataset/{quote(str(entry['dataset_key']))}/nameusage/{quote(str(entry['id']))}", {}
    if provider == "gbif":
        if kind == "species":
            return "species", f"/v1/species/{quote(str(entry['key']))}", {}
        if kind == "dataset":
            return "dataset", f"/v1/dataset/{quote(str(entry['key']))}", {}
        if kind == "download":
            return "download", f"/v1/occurrence/download/{quote(str(entry['key']))}", {}
        query = {"taxonKey": str(entry["taxon_key"]), "limit": str(int(entry["limit"])), "offset": "0"}
        if entry.get("country"):
            query["country"] = str(entry["country"])
        if entry.get("geometry"):
            query["geometry"] = str(entry["geometry"])
        return "occurrences", "/v1/occurrence/search", query
    return "taxon", f"/api/v4/taxa/sis/{quote(str(entry['sis_id']))}", {}


# ------------------------------------------------------------------ parsers


def _source(url: str, provider: str, origin: str, **extra: Any) -> dict[str, Any]:
    return {"url": url, "attribution": PROVIDER_CONTRACTS[provider]["attribution"],
            "terms_url": PROVIDER_CONTRACTS[provider]["terms_url"], "evidence_origin": origin,
            **{k: v for k, v in extra.items() if v is not None}}


def parse_col_release(body: Mapping[str, Any], url: str, *, origin: str) -> tuple[list[dict], dict[str, Any]]:
    key = _text(body.get("key"))
    version = _text(body.get("version"))
    if key is None or version is None:
        raise BiodiversityFormatError("schema_drift", "ChecklistBank release lacks key or version")
    meta = {"dataset_key": key, "version": version, "released": _day(body.get("issued")),
            "doi": _text(body.get("doi")), "title": _text(body.get("title")) or "Catalogue of Life",
            "licence": licence(body.get("license")), "citation": _text(body.get("citation"))}
    record = statement("dataset", "col", key, subject_name=meta["title"], source=_source(url, "col", origin),
                       as_published={"dataset_key": key, "kind": "checklist-release", "title": meta["title"],
                                     "publisher": _text(body.get("publisher")) or "Catalogue of Life",
                                     "licence": meta["licence"], "doi": meta["doi"], "citation": meta["citation"],
                                     "version": version, "released": meta["released"], "cited_references": None})
    return [record], meta


def parse_col_usage(body: Mapping[str, Any], url: str, *, origin: str, release: Mapping[str, Any]) -> list[dict]:
    usage_id = _text(body.get("id"))
    name = dict(body.get("name") or {})
    scientific = _text(name.get("scientificName"))
    status = _text(body.get("status"))
    if usage_id is None or scientific is None or status is None or not _text(name.get("rank")):
        raise BiodiversityFormatError("schema_drift", "ChecklistBank name usage lacks id, name, rank or status")
    accepted = None
    if body.get("accepted"):
        other = dict(body["accepted"])
        accepted_name = dict(other.get("name") or {})
        accepted = {"id": _text(other.get("id")), "scientific_name": _text(accepted_name.get("scientificName")),
                    "authorship": _text(accepted_name.get("authorship"))}
    classification = [{"id": _text(c.get("id")), "name": _text(c.get("name")), "rank": _text(c.get("rank"))}
                      for c in body.get("classification") or [] if isinstance(c, Mapping)] or None
    checklist = {"dataset_key": release["dataset_key"], "version": release["version"],
                 "released": release.get("released"), "doi": release.get("doi")}
    source = _source(url, "col", origin, release=f"{release['dataset_key']}@{release['version']}",
                     licence=release.get("licence"), citation=release.get("citation"))
    klass = status_class(status)
    taxon = statement("taxon", "col", usage_id, subject_name=scientific, source=source,
                      as_published={"native_id": usage_id, "scientific_name": scientific,
                                    "authorship": _text(name.get("authorship")), "rank": _text(name.get("rank")),
                                    "status": status, "status_class": klass, "accepted": accepted,
                                    "classification": classification, "checklist": checklist})
    identity = statement("taxon_identity", "col", usage_id, subject_name=scientific, source=source,
                         as_published={"key_scheme": "col-id", "native_key": usage_id, "scientific_name": scientific,
                                       "authorship": _text(name.get("authorship")), "rank": _text(name.get("rank")),
                                       "status": status, "status_class": klass,
                                       "accepted_key": (accepted or {}).get("id"),
                                       "accepted_name": (accepted or {}).get("scientific_name"),
                                       "cross_references": None, "checklist": checklist})
    return [taxon, identity]


_GBIF_REF_SCHEMES = {"col": "col-id", "catalogue of life": "col-id", "iucn": "iucn-sis-id"}


def parse_gbif_species(body: Mapping[str, Any], url: str, *, origin: str) -> list[dict]:
    key = _text(body.get("key"))
    scientific = _text(body.get("canonicalName")) or _text(body.get("scientificName"))
    status = _text(body.get("taxonomicStatus"))
    if key is None or scientific is None or status is None:
        raise BiodiversityFormatError("schema_drift", "GBIF species lacks key, name or taxonomicStatus")
    refs = []
    for item in body.get("identifiers") or []:  # verify: published cross-references of a backbone usage
        scheme = _GBIF_REF_SCHEMES.get(str(item.get("type") or "").casefold())
        if scheme and _text(item.get("identifier")):
            refs.append({"scheme": scheme, "value": _text(item["identifier"]), "field": "identifiers",
                         "basis": "cross-reference published by GBIF"})
    checklist = None
    if _text(body.get("datasetKey")):
        checklist = {"dataset_key": _text(body.get("datasetKey")),
                     "version": _text(body.get("datasetVersion")) or "not stated",
                     "released": None, "doi": None}
    return [statement("taxon_identity", "gbif", key, subject_name=scientific,
                      source=_source(url, "gbif", origin, licence=licence("CC BY 4.0")),
                      as_published={"key_scheme": "gbif-taxon-key", "native_key": key, "scientific_name": scientific,
                                    "authorship": _text(body.get("authorship")),
                                    "rank": (_text(body.get("rank")) or "").casefold() or None,
                                    "status": status.casefold().replace("_", " "),
                                    "status_class": status_class(status),
                                    "accepted_key": _text(body.get("acceptedKey")),
                                    "accepted_name": _text(body.get("accepted")),
                                    "cross_references": refs or None, "checklist": checklist})]


def parse_gbif_occurrences(body: Mapping[str, Any], url: str, *, origin: str) -> tuple[list[dict], bool]:
    if not isinstance(body.get("results"), list) or "endOfRecords" not in body:
        raise BiodiversityFormatError("schema_drift", "GBIF occurrence search lacks results or endOfRecords")
    records = []
    for item in body["results"]:
        gbif_id = _text(item.get("key") or item.get("gbifID"))
        dataset_key = _text(item.get("datasetKey"))
        if gbif_id is None or dataset_key is None:
            raise BiodiversityFormatError("schema_drift", "occurrence lacks gbifID or datasetKey")
        lat, lon = _number(item.get("decimalLatitude")), _number(item.get("decimalLongitude"))
        coordinates = None if lat is None or lon is None else {"latitude": lat, "longitude": lon}
        issues = sorted(str(i) for i in item.get("issues") or [])
        records.append(statement(
            "occurrence", "gbif", gbif_id, subject_name=_text(item.get("scientificName")),
            source=_source(f"https://www.gbif.org/occurrence/{gbif_id}", "gbif", origin, api_url=url,
                           dataset_key=dataset_key),
            as_published={
                "gbif_id": gbif_id, "occurrence_id": _text(item.get("occurrenceID")), "dataset_key": dataset_key,
                "basis_of_record": _text(item.get("basisOfRecord")), "event_date": _event_date(item.get("eventDate")),
                "taxon_key": _text(item.get("taxonKey")), "accepted_taxon_key": _text(item.get("acceptedTaxonKey")),
                "scientific_name": _text(item.get("scientificName")), "coordinates": coordinates,
                "coordinate_uncertainty_m": _number(item.get("coordinateUncertaintyInMeters")),
                "coordinate_precision": _number(item.get("coordinatePrecision")),
                "country_code": _text(item.get("countryCode")), "state_province": _text(item.get("stateProvince")),
                "locality": _text(item.get("locality")),
                "generalisation": generalisation(data_generalizations=item.get("dataGeneralizations"),
                                                 information_withheld=item.get("informationWithheld"),
                                                 coordinates_published=coordinates is not None, issues=issues),
                "issues": issues or None, "licence": licence(item.get("license")),
                "modified": _text(item.get("modified"))}))
    return records, bool(body["endOfRecords"])


def parse_gbif_dataset(body: Mapping[str, Any], url: str, *, origin: str) -> list[dict]:
    key = _text(body.get("key"))
    title = _text(body.get("title"))
    if key is None or title is None:
        raise BiodiversityFormatError("schema_drift", "GBIF dataset lacks key or title")
    citation = dict(body.get("citation") or {})
    refs = []
    for item in body.get("bibliographicCitations") or []:
        if isinstance(item, Mapping) and (_text(item.get("text")) or _text(item.get("identifier"))):
            refs.append({"text": _text(item.get("text")), "identifier": _text(item.get("identifier"))})
    kind = "checklist" if str(body.get("type") or "").upper() == "CHECKLIST" else "occurrence"
    return [statement("dataset", "gbif", key, subject_name=title,
                      source=_source(f"https://www.gbif.org/dataset/{key}", "gbif", origin, api_url=url),
                      as_published={"dataset_key": key, "kind": kind, "title": title,
                                    "publisher": _text(body.get("publishingOrganizationTitle")),
                                    "licence": licence(body.get("license")), "doi": _text(body.get("doi")),
                                    "citation": _text(citation.get("text")),
                                    "version": _text(body.get("version")) or _text(body.get("pubDate")),
                                    "released": _day(body.get("pubDate")), "cited_references": refs or None,
                                    "total_records": None})]


def parse_gbif_download(body: Mapping[str, Any], url: str, *, origin: str) -> list[dict]:
    key = _text(body.get("key"))
    doi = _text(body.get("doi"))
    if key is None or doi is None:
        raise BiodiversityFormatError("schema_drift", "GBIF download lacks key or DOI")
    return [statement("dataset", "gbif", key, subject_name=f"GBIF occurrence download {key}",
                      source=_source(f"https://www.gbif.org/occurrence/download/{key}", "gbif", origin, api_url=url),
                      as_published={"dataset_key": key, "kind": "download", "title": f"GBIF occurrence download {key}",
                                    "publisher": "GBIF.org", "licence": licence(body.get("license")), "doi": doi,
                                    "citation": f"GBIF.org ({_day(body.get('created')) or 'undated'}) GBIF "
                                                f"Occurrence Download https://doi.org/{doi}",
                                    "version": None, "released": _day(body.get("created")),
                                    "cited_references": None,
                                    "total_records": _number(body.get("totalRecords"))})]


def _scope(scopes: Any) -> dict[str, Any]:
    items = [s for s in scopes or [] if isinstance(s, Mapping)]
    if not items:
        return {"kind": "global", "label": "Global", "code": None, "basis": "no scope published; IUCN default"}
    first = items[0]
    label = _text(dict(first.get("description") or {}).get("en")) or _text(first.get("description")) or "unstated"
    return {"kind": "global" if label.casefold() == "global" else "regional", "label": label,
            "code": _text(first.get("code"))}


def parse_iucn_taxon(body: Mapping[str, Any], url: str, *, origin: str) -> tuple[list[dict], list[dict]]:
    taxon = dict(body.get("taxon") or {})
    sis = _text(taxon.get("sis_id"))
    scientific = _text(taxon.get("scientific_name"))
    if sis is None or scientific is None or not isinstance(body.get("assessments"), list):
        raise BiodiversityFormatError("schema_drift", "IUCN taxon lacks sis_id, scientific_name or assessments")
    identity = statement("taxon_identity", "iucn", sis, subject_name=scientific,
                         source=_source(url, "iucn", origin, licence_tier=LICENCE_TIER_IUCN),
                         as_published={"key_scheme": "iucn-sis-id", "native_key": sis, "scientific_name": scientific,
                                       "authorship": _text(taxon.get("authority")),
                                       "rank": "species" if taxon.get("species_taxa", True) else None,
                                       "status": "accepted", "status_class": "accepted", "accepted_key": None,
                                       "accepted_name": None, "cross_references": None, "checklist": None},
                         unknowns=["checklist (IUCN publishes no checklist version with the taxon)"])
    listing = [dict(a) for a in body["assessments"] if isinstance(a, Mapping) and a.get("assessment_id")]
    return [identity], listing


def parse_iucn_assessment(detail: Mapping[str, Any] | None, listed: Mapping[str, Any], url: str, *, origin: str,
                          taxon_id: str, scientific_name: str) -> dict:
    """One assessment at citation level: listing fields, completed by the assessment document when available."""
    detail = dict(detail or {})
    merged = {**listed, **{k: v for k, v in detail.items() if k in IUCN_KEPT and v is not None}}
    category = _text(dict(merged.get("red_list_category") or {}).get("code")) or _text(
        merged.get("red_list_category_code"))
    year = _text(merged.get("year_published"))
    if category is None or year is None or not isinstance(merged.get("latest"), bool):
        raise BiodiversityFormatError("schema_drift", "IUCN assessment lacks category, year_published or latest")
    change = merged.get("reason_for_change")
    if isinstance(change, Mapping):
        change = _text(dict(change.get("description") or {}).get("en")) or _text(change.get("code"))
    assessment_id = _text(merged["assessment_id"])
    page = _text(merged.get("url")) or f"https://www.iucnredlist.org/species/{taxon_id}/{assessment_id}"
    return statement(
        "conservation_assessment", "iucn", assessment_id, subject_name=scientific_name,
        source=_source(page, "iucn", origin, api_url=url, licence_tier=LICENCE_TIER_IUCN,
                       redistribution=IUCN_REDISTRIBUTION, detail="assessment document" if detail else
                       "taxon assessment listing only"),
        as_published={"assessment_id": assessment_id, "taxon_id": taxon_id, "scientific_name": scientific_name,
                      "category": category, "criteria": _text(merged.get("criteria")),
                      "assessment_date": _day(merged.get("assessment_date")), "year_published": year,
                      "scope": _scope(merged.get("scopes")), "latest": merged["latest"],
                      "possibly_extinct": merged.get("possibly_extinct"),
                      "possibly_extinct_in_wild": merged.get("possibly_extinct_in_the_wild"),
                      "change_reason": _text(change), "citation": _text(merged.get("citation")), "url": page,
                      "licence_tier": LICENCE_TIER_IUCN})


# ------------------------------------------------------------------ runtime adapter


class BiodiversitySourceAdapter:
    """One page per selected release, usage, species, occurrence page, dataset, download or IUCN taxon."""

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
        self._secret = secret
        self.definition = {
            "contract": ADAPTER_CONTRACT, "source_id": source["source_id"], "connector": source["connector"],
            "endpoint": source["endpoint"], "operations": list(source["operations"]),
            "source_hash": source["source_hash"], "mapping": source["mapping"],
            "extractor_versions": source["extractor_versions"], "limits": source["budgets"],
            "biodiversity": {"provider": self.provider, "selected": len(self.entries),
                             "live_verification": LIVE_VERIFICATION[self.provider]["status"]},
        }

    def describe(self) -> dict[str, Any]:
        return dict(self.definition)

    def _check(self, request: Mapping[str, Any]) -> None:
        if str(request.get("operation") or "") not in self.definition["operations"]:
            raise SourcePackError("operation_forbidden", "operation is not declared by the source")
        if set(request) - {"operation", "parameters", "limit", "from_ms", "to_ms"}:
            raise SourcePackError("parameter_forbidden", "runtime adapter received undeclared controls")
        if dict(request.get("parameters") or {}):
            raise SourcePackError("parameter_forbidden", "biodiversity runs fetch the declared selection only")

    def _get(self, path: str, query: Mapping[str, str]) -> tuple[int, Any, str, str]:
        base = self.source["endpoint"].rstrip("/") + path
        url = base + ("?" + urlencode(sorted(query.items())) if query else "")
        headers = {"Accept": "application/json"}
        if self.provider == "iucn":
            if not self._secret:
                raise SourcePackError("authentication_failed", "the IUCN API token secret is not configured")
            headers["Authorization"] = f"Bearer {self._secret}"  # verify the v4 header form
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
        if status == 404:
            return status, None, url, origin
        if status == 429:
            from src.ingestion.source_pack_runtime import _retry_after_ms

            folded = {str(k).casefold(): v for k, v in dict(response.get("headers") or {}).items()}
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
            return status, json.loads(raw.decode("utf-8-sig")), url, origin
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourcePackError("schema_drift", "response is not UTF-8 JSON") from exc

    def _page(self, entry: Mapping[str, Any], carry: dict[str, Any]) -> dict[str, Any]:
        role, path, query = request_for(self.provider, entry)
        status, body, url, origin = self._get(path, query)
        result: dict[str, Any] = {"url": url, "origin": origin, "statements": [], "dropped": [], "snapshot": None,
                                  "requests": 1}
        if body is None:
            result["outcome"] = "not_found"
            return result
        result["outcome"] = "found"
        if self.provider == "col" and role == "release":
            statements, meta = parse_col_release(body, url, origin=origin)
            carry.setdefault("releases", {})[meta["dataset_key"]] = meta
            result["statements"] = statements
        elif self.provider == "col":
            release = dict(carry.get("releases") or {}).get(str(entry["dataset_key"]))
            if release is None:
                raise SourcePackError("invalid_manifest", "a CoL usage is selected after its release metadata")
            result["statements"] = parse_col_usage(body, url, origin=origin, release=release)
        elif role == "species":
            result["statements"] = parse_gbif_species(body, url, origin=origin)
        elif role == "dataset":
            result["statements"] = parse_gbif_dataset(body, url, origin=origin)
        elif role == "download":
            result["statements"] = parse_gbif_download(body, url, origin=origin)
        elif role == "occurrences":
            result["statements"], complete = parse_gbif_occurrences(body, url, origin=origin)
            result["snapshot"] = {"selection_key": selection_key(self.provider, entry), "provider": "gbif",
                                  "complete": complete, "url": url}
        else:
            identity, listing = parse_iucn_taxon(body, url, origin=origin)
            taxon_id = identity[0]["record_key"]
            name = identity[0]["as_published"]["scientific_name"]
            statements = list(identity)
            limit = int(entry.get("max_assessments") or MAX_ASSESSMENTS)
            for listed in listing[:limit]:
                assessment_path = f"/api/v4/assessment/{quote(str(listed['assessment_id']))}"
                _, detail, detail_url, _ = self._get(assessment_path, {})
                result["requests"] += 1
                if detail is not None:
                    result["dropped"] += [f"assessment:{k}" for k in sorted(set(detail) - IUCN_KEPT)]
                statements.append(parse_iucn_assessment(detail, listed, detail_url, origin=origin,
                                                        taxon_id=taxon_id, scientific_name=name))
            result["dropped"] += [f"taxon:{k}" for k in sorted(set(body) - {"taxon", "assessments"})]
            result["truncated"] = max(0, len(listing) - limit)
            result["statements"] = statements
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
        except (BiodiversityFormatError, BiodiversityError, KeyError, TypeError, ValueError) as exc:
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
                "url": item["source"]["url"], "language": "en", "content": content, "biodiversity_record": item})
        label = {k: entry[k] for k in sorted(entry) if k in {"kind", "key", "id", "dataset_key", "taxon_key",
                                                              "country", "sis_id", "label"}}
        receipt = {"status": 200, "provider": self.provider, "selection": label, "outcome": page["outcome"],
                   "statements": len(records), "requests": page["requests"],
                   "withheld_fields_dropped": sorted(set(page["dropped"])), "evidence_origin": page["origin"],
                   "truncated_assessments": page.get("truncated", 0), "final_page": index + 1 >= len(self.entries),
                   "snapshot": page["snapshot"], "live_verification": LIVE_VERIFICATION[self.provider]["status"]}
        next_cursor = (json.dumps({"i": index + 1, "scope": self.source["source_hash"], "carry": carry},
                                  sort_keys=True) if index + 1 < len(self.entries) else None)
        return RuntimePage(tuple(records), next_cursor, sum(len(r["content"]) for r in records), receipt=receipt)


FIXTURE_SECRET = "fixture-credential-not-a-real-token"
ADAPTERS = {CONNECTOR: BiodiversitySourceAdapter}


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
    adapter = BiodiversitySourceAdapter(source, transport=fixture_transport(list(fixture["native_pages"])),
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
    "ADAPTERS", "BOUNDED_COVERAGE", "CONNECTOR", "FIXTURE_SECRET", "IUCN_KEPT", "IUCN_LICENCE_DECISION",
    "LIVE_VERIFICATION", "PROVIDERS", "PROVIDER_CONTRACTS", "PROVIDER_HOSTS", "SENSITIVE_SPECIES_POLICY",
    "BiodiversityFormatError", "BiodiversitySourceAdapter", "fixture_transport", "parse_col_release",
    "parse_col_usage", "parse_gbif_dataset", "parse_gbif_download", "parse_gbif_occurrences", "parse_gbif_species",
    "parse_iucn_assessment", "parse_iucn_taxon", "replay_native_fixture", "request_for", "selection_entries",
    "selection_key",
]
