"""Build the pinned biodiversity source-pack fixtures and manifest from the authored payloads below.

Every payload is authored in the provider's documented response shape. Keys,
identifiers, DOIs (``10.5555`` test prefix), coordinates, dates and Red List
categories are ILLUSTRATIVE and are not live evidence; scientific names are
real only so the bounded coverage reads naturally. Run
``python -m tests.unit.biodiversity.fixture_builder`` after editing a payload;
``test_fixtures_are_pinned_and_in_sync`` fails when they drift.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "packs/climate-environment/source_packs/climate-environment-biodiversity.json"
OUT = ROOT / "tests/fixtures/source_packs"
LATER = ROOT / "tests/fixtures/biodiversity/later_payloads.json"
NOTE = ("Authored offline fixture in the provider's documented response shape. Keys, identifiers, DOIs (10.5555 test "
        "prefix), coordinates, dates and Red List categories are ILLUSTRATIVE, not live evidence; verify against the "
        "live sources before any live run (BD13, #2533).")
BERLIN_WKT = "POLYGON((13.08 52.33,13.76 52.33,13.76 52.68,13.08 52.68,13.08 52.33))"
RELEASE_1, RELEASE_2 = "310001", "310002"


def request(path, query=None):
    return path + ("?" + urlencode(sorted(dict(query).items())) if query else "")


# ------------------------------------------------------------------ Catalogue of Life

RELEASES = {
    RELEASE_1: {"key": 310001, "title": "Catalogue of Life Checklist", "version": "2026-07-10", "issued": "2026-07-10",
                "doi": "10.5555/col.fixture.2026-07", "license": "CC BY 4.0", "origin": "release",
                "publisher": "Catalogue of Life",
                "citation": "Catalogue of Life Checklist (Version 2026-07-10). https://doi.org/10.5555/col.fixture.2026-07"},
    RELEASE_2: {"key": 310002, "title": "Catalogue of Life Checklist", "version": "2026-08-12", "issued": "2026-08-12",
                "doi": "10.5555/col.fixture.2026-08", "license": "CC BY 4.0", "origin": "release",
                "publisher": "Catalogue of Life",
                "citation": "Catalogue of Life Checklist (Version 2026-08-12). https://doi.org/10.5555/col.fixture.2026-08"},
}
BIRDS = [{"id": "V", "name": "Animalia", "rank": "kingdom"}, {"id": "CH2", "name": "Chordata", "rank": "phylum"},
         {"id": "AVES", "name": "Aves", "rank": "class"}]
MAMMALS = [{"id": "V", "name": "Animalia", "rank": "kingdom"}, {"id": "CH2", "name": "Chordata", "rank": "phylum"},
           {"id": "MAM", "name": "Mammalia", "rank": "class"}]


def usage(uid, name, authorship, classification, *, status="accepted", accepted=None):
    body = {"id": uid, "name": {"scientificName": name, "authorship": authorship, "rank": "species"},
            "status": status, "classification": classification}
    if accepted:
        body["accepted"] = {"id": accepted[0], "name": {"scientificName": accepted[1], "authorship": accepted[2]}}
    return body


def col_usages(release):
    cornix = (usage("CCRN1", "Corvus cornix", "Linnaeus, 1758", BIRDS + [{"id": "CORV", "name": "Corvus", "rank": "genus"}])
              if release == RELEASE_1 else
              usage("CCRN1", "Corvus cornix", "Linnaeus, 1758", BIRDS + [{"id": "CORV", "name": "Corvus", "rank": "genus"}],
                    status="synonym", accepted=("CCOR1", "Corvus corone", "Linnaeus, 1758")))
    return [usage("PDOM1", "Passer domesticus", "(Linnaeus, 1758)", BIRDS + [{"id": "PASS", "name": "Passer", "rank": "genus"}]),
            usage("LLUT1", "Lutra lutra", "(Linnaeus, 1758)", MAMMALS + [{"id": "LUTR", "name": "Lutra", "rank": "genus"}]),
            usage("CCOR1", "Corvus corone", "Linnaeus, 1758", BIRDS + [{"id": "CORV", "name": "Corvus", "rank": "genus"}]),
            cornix]


def col_pages():
    pages = []
    for release in (RELEASE_1, RELEASE_2):
        pages.append({"request": request(f"/dataset/{release}"), "status": 200, "body": RELEASES[release]})
        for body in col_usages(release):
            pages.append({"request": request(f"/dataset/{release}/nameusage/{body['id']}"), "status": 200,
                          "body": body})
    return pages


COL_SELECTION = ([{"kind": "release", "dataset_key": RELEASE_1, "label": "CoL monthly release 2026-07-10"}]
                 + [{"kind": "usage", "dataset_key": RELEASE_1, "id": uid} for uid in ("PDOM1", "LLUT1", "CCOR1", "CCRN1")]
                 + [{"kind": "release", "dataset_key": RELEASE_2, "label": "CoL monthly release 2026-08-12"}]
                 + [{"kind": "usage", "dataset_key": RELEASE_2, "id": uid} for uid in ("PDOM1", "LLUT1", "CCOR1", "CCRN1")])

# ------------------------------------------------------------------ GBIF

BACKBONE = "d7dddbf4-2cf0-4f39-9b2a-bb099caae36c"
SPECIES = {
    "5231190": {"key": 5231190, "scientificName": "Passer domesticus (Linnaeus, 1758)", "canonicalName": "Passer domesticus",
                "authorship": "(Linnaeus, 1758)", "rank": "SPECIES", "taxonomicStatus": "ACCEPTED",
                "datasetKey": BACKBONE, "identifiers": [{"type": "COL", "identifier": "PDOM1"}]},
    "2433753": {"key": 2433753, "scientificName": "Lutra lutra (Linnaeus, 1758)", "canonicalName": "Lutra lutra",
                "authorship": "(Linnaeus, 1758)", "rank": "SPECIES", "taxonomicStatus": "ACCEPTED", "datasetKey": BACKBONE},
    "2482492": {"key": 2482492, "scientificName": "Corvus corone Linnaeus, 1758", "canonicalName": "Corvus corone",
                "authorship": "Linnaeus, 1758", "rank": "SPECIES", "taxonomicStatus": "ACCEPTED", "datasetKey": BACKBONE},
    "2482468": {"key": 2482468, "scientificName": "Corvus cornix Linnaeus, 1758", "canonicalName": "Corvus cornix",
                "authorship": "Linnaeus, 1758", "rank": "SPECIES", "taxonomicStatus": "ACCEPTED", "datasetKey": BACKBONE},
}
DS_BIRDS, DS_SURVEY, DS_MAMMALS = ("aaaa0001-0000-4000-8000-000000000001", "aaaa0002-0000-4000-8000-000000000002",
                                   "aaaa0003-0000-4000-8000-000000000003")
DATASETS = {
    DS_BIRDS: {"key": DS_BIRDS, "type": "OCCURRENCE", "title": "Sample Berlin bird observations (fixture)",
               "publishingOrganizationTitle": "Sample Ornithological Society (fixture)",
               "license": "http://creativecommons.org/licenses/by/4.0/legalcode", "doi": "10.5555/gbif.fixture.ds1",
               "citation": {"text": "Sample Ornithological Society (2026). Sample Berlin bird observations (fixture). "
                                    "Occurrence dataset https://doi.org/10.5555/gbif.fixture.ds1"},
               "pubDate": "2026-06-01", "version": "1.4",
               "bibliographicCitations": [{"text": "Fictional, A. (2099). Urban sparrows of a fictional city. "
                                                   "https://doi.org/10.5555/fict.bio.2099.1",
                                           "identifier": "https://doi.org/10.5555/fict.bio.2099.1"}]},
    DS_SURVEY: {"key": DS_SURVEY, "type": "OCCURRENCE", "title": "Sample city breeding bird survey (fixture)",
                "publishingOrganizationTitle": "Sample City Environment Office (fixture)",
                "license": "http://creativecommons.org/publicdomain/zero/1.0/legalcode",
                "doi": "10.5555/gbif.fixture.ds2",
                "citation": {"text": "Sample City Environment Office (2026). Sample city breeding bird survey (fixture). "
                                     "https://doi.org/10.5555/gbif.fixture.ds2"},
                "pubDate": "2026-03-15", "version": "2.0",
                "bibliographicCitations": [{"text": "Survey methods handbook, internal report (fixture; no identifier)"}]},
    DS_MAMMALS: {"key": DS_MAMMALS, "type": "OCCURRENCE", "title": "Sample protected mammal records (fixture)",
                 "publishingOrganizationTitle": "Sample Mammal Atlas (fixture)",
                 "license": "http://creativecommons.org/licenses/by-nc/4.0/legalcode",
                 "doi": "10.5555/gbif.fixture.ds3",
                 "citation": {"text": "Sample Mammal Atlas (2026). Sample protected mammal records (fixture). "
                                      "https://doi.org/10.5555/gbif.fixture.ds3"},
                 "pubDate": "2026-05-20", "version": "3.1", "bibliographicCitations": []},
}


def occurrence(key, dataset, taxon, name, date, *, lat=None, lon=None, uncertainty=None, generalised=None,
               withheld=None, issues=(), licence=None, basis="HUMAN_OBSERVATION", state=None):
    body = {"key": key, "occurrenceID": f"urn:fixture:occ:{key}", "datasetKey": dataset, "basisOfRecord": basis,
            "eventDate": date, "taxonKey": taxon, "acceptedTaxonKey": taxon, "scientificName": name,
            "countryCode": "DE", "stateProvince": state or "Berlin", "locality": "Berlin (fixture locality text)",
            "issues": list(issues), "license": licence or DATASETS[dataset]["license"],
            "modified": "2026-06-02T08:00:00.000+00:00"}
    if lat is not None:
        body.update(decimalLatitude=lat, decimalLongitude=lon)
    if uncertainty is not None:
        body["coordinateUncertaintyInMeters"] = uncertainty
    if generalised:
        body["dataGeneralizations"] = generalised
    if withheld:
        body["informationWithheld"] = withheld
    return body


SPARROWS = [
    occurrence(4011001, DS_BIRDS, 5231190, "Passer domesticus (Linnaeus, 1758)", "2026-05-03", lat=52.52, lon=13.405,
               uncertainty=30.0),
    occurrence(4011002, DS_SURVEY, 5231190, "Passer domesticus (Linnaeus, 1758)", "2026-04-11", lat=52.517,
               lon=13.3889, uncertainty=5000.0),
    occurrence(4011003, DS_BIRDS, 5231190, "Passer domesticus (Linnaeus, 1758)", "2025-06-01", lat=52.515, lon=13.4,
               issues=["COORDINATE_UNCERTAINTY_METERS_INVALID"]),
]
OTTERS = [
    occurrence(4022001, DS_MAMMALS, 2433753, "Lutra lutra (Linnaeus, 1758)", "2026-02-14", lat=52.55, lon=13.45,
               uncertainty=7071.0, generalised="Coordinates generalised to 10 km grid for species protection",
               issues=["COORDINATE_ROUNDED"], basis="PRESERVED_SPECIMEN"),
    occurrence(4022002, DS_MAMMALS, 2433753, "Lutra lutra (Linnaeus, 1758)", "2026-03-02",
               withheld="Coordinates withheld for species protection", basis="HUMAN_OBSERVATION"),
]
SPARROW_QUERY = {"geometry": BERLIN_WKT, "limit": "20", "offset": "0", "taxonKey": "5231190"}
OTTER_QUERY = {"country": "DE", "limit": "20", "offset": "0", "taxonKey": "2433753"}
DOWNLOAD = {"key": "0012345-260901000000000", "doi": "10.5555/gbif.fixture.dl1",
            "license": "http://creativecommons.org/licenses/by-nc/4.0/legalcode", "created": "2026-09-01T10:00:00.000+00:00",
            "totalRecords": 5, "status": "SUCCEEDED"}


def gbif_pages(sparrows=None):
    pages = [{"request": request(f"/v1/species/{key}"), "status": 200, "body": body} for key, body in SPECIES.items()]
    pages += [{"request": request(f"/v1/dataset/{key}"), "status": 200, "body": body} for key, body in DATASETS.items()]
    pages.append({"request": request("/v1/occurrence/download/" + DOWNLOAD["key"]), "status": 200, "body": DOWNLOAD})
    rows = SPARROWS if sparrows is None else sparrows
    pages.append({"request": request("/v1/occurrence/search", SPARROW_QUERY), "status": 200,
                  "body": {"offset": 0, "limit": 20, "endOfRecords": True, "count": len(rows), "results": rows}})
    pages.append({"request": request("/v1/occurrence/search", OTTER_QUERY), "status": 200,
                  "body": {"offset": 0, "limit": 20, "endOfRecords": True, "count": len(OTTERS), "results": OTTERS}})
    return pages


GBIF_SELECTION = ([{"kind": "species", "key": key} for key in SPECIES]
                  + [{"kind": "dataset", "key": key} for key in DATASETS]
                  + [{"kind": "download", "key": DOWNLOAD["key"], "label": "an existing download, cited by its DOI"},
                     {"kind": "occurrences", "taxon_key": "5231190", "geometry": BERLIN_WKT, "limit": 20,
                      "label": "house sparrow within the Berlin bounding polygon"},
                     {"kind": "occurrences", "taxon_key": "2433753", "country": "DE", "limit": 20,
                      "label": "Eurasian otter in DE (generalised and withheld records)"}])

# ------------------------------------------------------------------ IUCN

WITHHELD = {"documentation": {"rationale": "Narrative rationale (withheld under the reference-only decision)."},
            "threats": [{"code": "1.1", "description": {"en": "Housing & urban areas"}}],
            "habitats": [{"code": "5.1", "description": {"en": "Wetlands"}}],
            "population_trend": {"description": {"en": "Decreasing"}},
            "locations": [{"code": "DE", "description": {"en": "Germany"}}]}


def listed(aid, year, category, latest, scope="Global", code="1", sis=12419):
    return {"assessment_id": aid, "sis_taxon_id": sis, "year_published": year, "latest": latest,
            "possibly_extinct": False, "possibly_extinct_in_the_wild": False, "red_list_category_code": category,
            "url": f"https://www.iucnredlist.org/species/{sis}/{aid}",
            "scopes": [{"description": {"en": scope}, "code": code}]}


def detail(item, criteria, date, name, reason=None):
    body = {**item, "assessment_date": date, "criteria": criteria,
            "red_list_category": {"code": item["red_list_category_code"],
                                  "description": {"en": {"NT": "Near Threatened", "VU": "Vulnerable",
                                                         "LC": "Least Concern"}[item["red_list_category_code"]]}},
            "citation": f"Fixture Assessors. {item['year_published']}. {name}. The IUCN Red List of Threatened "
                        f"Species {item['year_published']}: e.T{item['sis_taxon_id']}A{item['assessment_id']}. "
                        f"https://dx.doi.org/10.5555/iucn.fixture.{item['assessment_id']}",
            "taxon": {"sis_id": item["sis_taxon_id"], "scientific_name": name}, **WITHHELD}
    if reason:
        body["reason_for_change"] = {"code": reason[0], "description": {"en": reason[1]}}
    return body


OTTER_LIST = [listed(900003, "2021", "NT", True), listed(900002, "2015", "NT", False),
              listed(900001, "1996", "VU", False)]
SPARROW_LIST = [listed(800002, "2018", "LC", True, sis=103818789),
                listed(800001, "2021", "LC", True, scope="Europe", code="2", sis=103818789)]


def iucn_pages(otters=None, extra=()):
    otter_list = OTTER_LIST if otters is None else otters
    pages = [{"request": request("/api/v4/taxa/sis/12419"), "status": 200,
              "body": {"taxon": {"sis_id": 12419, "scientific_name": "Lutra lutra", "authority": "(Linnaeus, 1758)"},
                       "assessments": otter_list}},
             {"request": request("/api/v4/taxa/sis/103818789"), "status": 200,
              "body": {"taxon": {"sis_id": 103818789, "scientific_name": "Passer domesticus",
                                 "authority": "(Linnaeus, 1758)"}, "assessments": SPARROW_LIST}},
             {"request": request("/api/v4/assessment/900003"), "status": 200,
              "body": detail(OTTER_LIST[0], None, "2020-10-15", "Lutra lutra")},
             {"request": request("/api/v4/assessment/900001"), "status": 200,
              "body": detail(OTTER_LIST[2], "A2ace", "1996-06-30", "Lutra lutra")},
             {"request": request("/api/v4/assessment/800002"), "status": 200,
              "body": detail(SPARROW_LIST[0], None, "2018-08-07", "Passer domesticus")},
             {"request": request("/api/v4/assessment/800001"), "status": 200,
              "body": detail(SPARROW_LIST[1], None, "2021-02-01", "Passer domesticus")}]
    # 900002 (2015) has no assessment document here: it stays at listing level with criteria unknown.
    return pages + list(extra)


IUCN_SELECTION = [{"kind": "taxon", "sis_id": "12419", "max_assessments": 10, "label": "Lutra lutra assessment history"},
                  {"kind": "taxon", "sis_id": "103818789", "max_assessments": 10,
                   "label": "Passer domesticus global and European assessments"}]


def later_payloads():
    """A later acquisition: a sparrow record changed, one removed, one new; a new otter assessment."""
    changed = copy.deepcopy(SPARROWS[1])
    changed["coordinateUncertaintyInMeters"] = 2000.0
    changed["modified"] = "2026-09-10T08:00:00.000+00:00"
    new = occurrence(4011004, DS_BIRDS, 5231190, "Passer domesticus (Linnaeus, 1758)", "2026-09-05", lat=52.521,
                     lon=13.41, uncertainty=15.0)
    sparrows = [SPARROWS[0], changed, new]
    new_otter = listed(900004, "2026", "VU", True)
    otters = [new_otter, {**OTTER_LIST[0], "latest": False}] + OTTER_LIST[1:]
    gbif = {p["request"]: {"body": p["body"]} for p in gbif_pages(sparrows)
            if p["request"] == request("/v1/occurrence/search", SPARROW_QUERY)}
    iucn = {p["request"]: {"body": p["body"]} for p in iucn_pages(
        otters, [{"request": request("/api/v4/assessment/900004"), "status": 200,
                  "body": detail(new_otter, "A2c", "2026-03-01", "Lutra lutra",
                                 reason=("genuine", "Genuine change: population decline"))}])
        if p["request"] in {request("/api/v4/taxa/sis/12419"), request("/api/v4/assessment/900004"),
                            request("/api/v4/assessment/900003")}}
    iucn[request("/api/v4/assessment/900003")]["body"] = {**iucn[request("/api/v4/assessment/900003")]["body"],
                                                          "latest": False}
    return {"description": NOTE + " A later acquisition of the same selections.",
            "gbif": gbif, "iucn": iucn}


# ------------------------------------------------------------------ manifest

DEFAULTS = {
    "connector": "biodiversity",
    "update_cadence": "weekly re-check of the explicit selection; CoL publishes monthly releases, GBIF re-publishes "
                      "occurrences continuously and IUCN publishes assessments by year",
    "temporal_semantics": "checklist usages belong to a named release (version and issue date); occurrences and "
                          "dataset metadata are revisions by retrieval time with event dates as published; IUCN "
                          "assessments carry assessment date and year published; every distinct payload is an "
                          "immutable revision and absence from a later complete page of the same selection is a "
                          "dated tombstone",
    "mapping": {"target_schema": "noesis-biodiversity-record-v1", "version": "1.0.0"},
    "extractor_versions": ["biodiversity-sources:1.0.0"],
    "operations": ["biodiversity"],
    "schedule": {"kind": "interval", "interval_s": 604800},
    "auth": {"kind": "none"},
    "health": {"required": False, "max_staleness_s": 2592000},
    "budgets": {"timeout_ms": 30000, "max_results": 200, "max_bytes": 2000000, "max_pages": 20},
    "policy": {"excluded": ["species distribution modelling", "abundance or presence/absence estimation",
                            "locations more precise than the publisher released",
                            "Noesis-derived threat status or trend"],
               "coordinates": "as published, never de-generalised"},
}
SOURCES = [
    {"source_id": "col-checklist-releases", "endpoint": "https://api.checklistbank.org",
     "publisher": "Catalogue of Life (ChecklistBank)",
     "scope": "Two named Catalogue of Life monthly releases (dataset keys pinned) and the name usages of the bounded "
              "taxa in each: accepted names, synonyms with their accepted name, and higher classification",
     "license": {"id": "cc-by-4.0", "terms_url": "https://www.catalogueoflife.org/about/citing",
                 "redistribution": "permitted with attribution; cite the release with its version and DOI"},
     "biodiversity": {"provider": "col", "namespace": "environment", "live_verification": "unverified-live",
                      "coverage_decision": "bounded taxa (Passer domesticus, Lutra lutra, Corvus corone, Corvus cornix) "
                                           "in two named releases", "selection": COL_SELECTION},
     "fixture": {"path": "tests/fixtures/source_packs/biodiversity-col.json"}},
    {"source_id": "gbif-species-occurrences", "endpoint": "https://api.gbif.org",
     "publisher": "Global Biodiversity Information Facility (GBIF)",
     "scope": "Backbone species for the bounded taxa, one small occurrence page per taxon and place (Berlin polygon, "
              "country DE), the publishing datasets' metadata and one existing download's DOI; coordinates as "
              "published with uncertainty and generalisation flags",
     "license": {"id": "gbif-per-dataset-cc0-cc-by-cc-by-nc", "terms_url": "https://www.gbif.org/terms",
                 "redistribution": "by each record's dataset licence (CC0, CC BY 4.0 or CC BY-NC 4.0) with attribution "
                                   "to the publishing dataset; cite download DOIs"},
     "biodiversity": {"provider": "gbif", "namespace": "environment", "live_verification": "unverified-live",
                      "coverage_decision": "four backbone species, two occurrence pages of at most 20 records, three "
                                           "publishing datasets and one download", "selection": GBIF_SELECTION},
     "fixture": {"path": "tests/fixtures/source_packs/biodiversity-gbif.json"}},
    {"source_id": "iucn-red-list-reference", "endpoint": "https://api.iucnredlist.org",
     "publisher": "IUCN Red List of Threatened Species",
     "scope": "Assessment history of two bounded taxa at citation level (reference-only licence decision): category, "
              "criteria, dates, scope, the assessor's latest designation, citation and URL; narrative, threats, "
              "habitats, population and spatial data are never stored",
     "license": {"id": "iucn-red-list-terms-non-commercial", "terms_url": "https://www.iucnredlist.org/terms/terms-of-use",
                 "redistribution": "not permitted beyond citation-level facts; non-commercial use; cite each assessment"},
     "auth": {"kind": "required-secret", "secret_ref": "NOESIS_IUCN_API_TOKEN"},
     "biodiversity": {"provider": "iucn", "namespace": "environment", "live_verification": "unverified-live",
                      "coverage_decision": "Lutra lutra and Passer domesticus, at most 10 assessments each",
                      "selection": IUCN_SELECTION},
     "fixture": {"path": "tests/fixtures/source_packs/biodiversity-iucn.json"}},
]
FIXTURES = {
    "col-checklist-releases": ("Catalogue of Life (ChecklistBank) release metadata and name usages", col_pages,
                               ["two releases", "Corvus cornix accepted then synonym of Corvus corone",
                                "classification per release"]),
    "gbif-species-occurrences": ("GBIF species, occurrence search pages, dataset metadata and a download", gbif_pages,
                                 ["records from three datasets under CC BY, CC0 and CC BY-NC",
                                  "a generalised sensitive-species record", "withheld coordinates",
                                  "missing coordinate uncertainty", "a download DOI"]),
    "iucn-red-list-reference": ("IUCN Red List API v4 taxon assessment lists and assessment documents", iucn_pages,
                                ["global history with a category change", "a regional (Europe) assessment",
                                 "an assessment without its document (criteria unknown)",
                                 "narrative fields dropped under the reference-only decision"]),
}


def build(write=True):
    """Regenerate fixtures, later payloads and the pinned manifest; returns {path: text}."""
    from src.ingestion.source_packs import SourcePackConformance, validate_source_pack

    outputs, hashes = {}, {}
    for source_id, (description, pages, scenarios) in FIXTURES.items():
        payload = {"description": f"{description}. {NOTE}", "scenarios": scenarios, "native_pages": pages()}
        text = json.dumps(payload, indent=1, ensure_ascii=False) + "\n"
        path = OUT / f"biodiversity-{source_id.split('-')[0]}.json"
        outputs[path] = text
        hashes[source_id] = hashlib.sha256(text.encode()).hexdigest()
        if write:
            path.write_text(text, encoding="utf-8")
    later = json.dumps(later_payloads(), indent=1, ensure_ascii=False) + "\n"
    outputs[LATER] = later
    manifest = {"pack_id": "climate-environment-biodiversity", "version": "1.0.0",
                "description": "Climate and Environment biodiversity sources for the optional biodiversity feature: "
                               "Catalogue of Life releases via ChecklistBank, GBIF species, occurrences, datasets and "
                               "download DOIs, and IUCN Red List assessments at citation level (reference-only). "
                               "Every source is unverified-live until a dated run (#2533); no distribution "
                               "modelling, abundance or de-generalised locations.",
                "domains": ["environment"], "defaults": DEFAULTS, "sources": copy.deepcopy(SOURCES)}
    for source in manifest["sources"]:
        source["fixture"]["sha256"] = hashes[source["source_id"]]
        source["fixture"]["expected_output_hash"] = "0" * 64
    if write:
        LATER.parent.mkdir(parents=True, exist_ok=True)
        LATER.write_text(later, encoding="utf-8")
        result = SourcePackConformance(ROOT).offline(validate_source_pack(manifest))
        outputs_by_id = {item["source_id"]: item["output_hash"] for item in result["sources"]}
        for source in manifest["sources"]:
            source["fixture"]["expected_output_hash"] = outputs_by_id[source["source_id"]]
    else:
        pinned = json.loads(PACK.read_text())
        for source, pinned_source in zip(manifest["sources"], pinned["sources"], strict=True):
            source["fixture"]["expected_output_hash"] = pinned_source["fixture"]["expected_output_hash"]
    text = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
    outputs[PACK] = text
    if write:
        PACK.write_text(text, encoding="utf-8")
    return outputs


if __name__ == "__main__":
    for written in build():
        print(written.relative_to(ROOT))
