"""Authored offline humanitarian fixtures (synthetic; never live coverage).

Identifiers are in synthetic ranges (ReliefWeb 99xxxxx, UCDP 99xxxx, HDX
``hdx-fixture-*``), dates are in 2098-2099 and actors are fixture labels, so no
fixture can be mistaken for a real report or event. The shapes follow the
providers' documented responses as recorded in the HR01 audit.

``python -m tests.unit.humanitarian.fixture_builder`` rewrites the pinned
source-pack fixtures (``tests/fixtures/source_packs/humanitarian-*.json``) and
prints each file's SHA-256 and expected output hash for
``config/source_packs/humanitarian.json``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
RELIEFWEB = "https://api.reliefweb.int/v2"
HDX = "https://data.humdata.org/api/3/action"
UCDP = "https://ucdpapi.pcr.uu.se/api"
FCO = {"id": 99901, "name": "Fixture Coordination Office", "shortname": "FCO"}
SDN = {"id": 220, "name": "Sudan", "iso3": "sdn", "primary": True}
DISASTER = {"id": 99001, "name": "Sudan: Fixture Conflict - Apr 2098", "glide": "CE-2098-000001-SDN"}


def reliefweb_report(report_id, title, fmt, created, changed=None, *, original=None, disasters=(DISASTER,)):
    return {"id": report_id, "score": 1, "href": f"{RELIEFWEB}/reports/{report_id}", "fields": {
        "id": report_id, "title": title, "url": f"https://reliefweb.int/node/{report_id}",
        "url_alias": f"https://reliefweb.int/report/sudan/fixture-{report_id}",
        "date": {"created": created, "changed": changed or created, **({"original": original} if original else {})},
        "source": [FCO], "country": [dict(SDN, iso3="SDN")], "primary_country": dict(SDN, iso3="SDN"),
        "disaster": [dict(d) for d in disasters], "format": [{"id": 10, "name": fmt}], "language": [{"code": "en"}]}}


def reports_payload(revised=False):
    first = reliefweb_report(9900001, "Sudan: fixture situation report No. 1", "Situation Report",
                             "2098-05-02T10:00:00+00:00", original="2098-05-01T00:00:00+00:00")
    if revised:
        first["fields"]["title"] = "Sudan: fixture situation report No. 1 (corrected)"
        first["fields"]["date"]["changed"] = "2098-06-01T09:00:00+00:00"
    data = [first,
            reliefweb_report(9900002, "Sudan: fixture flash appeal 2098", "Flash Appeal", "2098-05-10T08:00:00+00:00"),
            reliefweb_report(9900003, "Sudan: fixture access map", "Map", "2098-05-11T08:00:00+00:00")]
    if revised:
        data.append(reliefweb_report(9900004, "Sudan: fixture situation report No. 2", "Situation Report",
                                     "2098-06-02T10:00:00+00:00", original="2098-06-02T00:00:00+00:00"))
    return {"time": 1, "href": f"{RELIEFWEB}/reports", "totalCount": len(data), "count": len(data), "data": data}


def disasters_payload():
    return {"time": 1, "href": f"{RELIEFWEB}/disasters", "totalCount": 1, "count": 1, "data": [{
        "id": DISASTER["id"], "score": 1, "fields": {
            "id": DISASTER["id"], "name": DISASTER["name"], "glide": DISASTER["glide"], "status": "current",
            "date": {"event": "2098-04-15T00:00:00+00:00", "created": "2098-04-16T00:00:00+00:00",
                     "changed": "2098-04-20T00:00:00+00:00"},
            "country": [dict(SDN, iso3="SDN")], "primary_country": dict(SDN, iso3="SDN"),
            "type": [{"id": 41764, "name": "Complex Emergency"}],
            "url": f"https://reliefweb.int/node/{DISASTER['id']}",
            "url_alias": "https://reliefweb.int/disaster/ce-2098-000001-sdn"}}]}


def _resource(dataset_id, resource_id, name, last_modified, digest):
    return {"id": resource_id, "name": name, "format": "CSV", "last_modified": last_modified, "hash": digest,
            "size": 2048, "url": f"https://data.humdata.org/dataset/{dataset_id}/resource/{resource_id}/download/{name}"}


def hdx_packages(revised=False):
    sites = {"id": "hdx-fixture-0001", "name": "fixture-sdn-displacement-sites",
             "title": "Sudan: fixture displacement sites (admin 1)", "license_id": "cc-by",
             "license_title": "Creative Commons Attribution International", "private": False,
             "is_requestdata_type": False, "metadata_modified": "2098-05-05T12:00:00.000000",
             "dataset_date": "[2098-05-01T00:00:00 TO 2098-05-05T23:59:59]",
             "organization": {"id": "org-fixture-fco", "name": "fixture-fco", "title": "Fixture Coordination Office"},
             "groups": [{"name": "sdn", "title": "Sudan"}],
             "tags": [{"name": "hxl"}, {"name": "internally displaced persons-idp"}, {"name": "ce-2098-000001-sdn"}],
             "resources": [_resource("hdx-fixture-0001", "r-fixture-0001", "sites.csv", "2098-05-05T11:00:00",
                                     "md5:1a2b3c4d")]}
    if revised:
        sites["metadata_modified"] = "2098-06-03T12:00:00.000000"
        sites["resources"][0].update(last_modified="2098-06-03T11:00:00", hash="md5:9f8e7d6c")
    restricted = {"id": "hdx-fixture-0002", "name": "fixture-sdn-household-survey",
                  "title": "Sudan: fixture household survey (HDX Connect)", "license_id": "other-pd-nr",
                  "license_title": "Other (non-redistributable)", "private": False, "is_requestdata_type": True,
                  "metadata_modified": "2098-05-06T09:00:00.000000", "dataset_date": "[2098-04-01T00:00:00 TO 2098-04-30T23:59:59]",
                  "organization": {"id": "org-fixture-fco", "name": "fixture-fco", "title": "Fixture Coordination Office"},
                  "groups": [{"name": "sdn", "title": "Sudan"}], "tags": [{"name": "hxl"}, {"name": "households"}],
                  "resources": [_resource("hdx-fixture-0002", "r-fixture-0002", "survey.csv", "2098-05-06T09:00:00", "")]}
    boundaries = {"id": "hdx-fixture-0003", "name": "fixture-cod-ab-sdn", "title": "Sudan: fixture COD-AB admin 1 p-codes",
                  "license_id": "cc-by-igo", "license_title": "Creative Commons Attribution for Intergovernmental Organisations",
                  "private": False, "is_requestdata_type": False, "metadata_modified": "2098-03-01T00:00:00.000000",
                  "dataset_date": "[2098-03-01T00:00:00 TO 2098-03-01T23:59:59]",
                  "organization": {"id": "org-fixture-ocha", "name": "fixture-ocha", "title": "Fixture OCHA Office"},
                  "groups": [{"name": "sdn", "title": "Sudan"}], "tags": [{"name": "hxl"}, {"name": "administrative boundaries-divisions"}],
                  "resources": [_resource("hdx-fixture-0003", "r-fixture-0003", "sdn_adm1_pcodes.csv", "2098-03-01T00:00:00",
                                          "md5:0c0d0e0f")]}
    return [sites, restricted, boundaries]


HXL_HEADERS = {
    "r-fixture-0001": "Admin 1,Admin 1 p-code,Site,Individuals,Focal point e-mail\n"
                      "#adm1+name,#adm1+code,,#affected+idps+ind,#contact+email\n",
    "r-fixture-0003": "ADM1_EN,ADM1_PCODE\n#adm1+name,#adm1+code\n",
}


def hdx_payload(revised=False):
    packages = hdx_packages(revised)
    return {"help": f"{HDX}/help_show?name=package_search", "success": True,
            "result": {"count": len(packages), "results": packages}}


def ucdp_event(event_id, **fields):
    base = {"id": event_id, "relid": f"SDN-2098-1-{event_id}", "year": 2098, "active_year": True, "code_status": "Clear",
            "type_of_violence": 1, "conflict_new_id": 99100, "conflict_name": "Sudan: Government (fixture)",
            "dyad_new_id": 99200, "dyad_name": "Fixture Armed Forces - Fixture Militia", "side_a_new_id": 99301,
            "side_a": "Fixture Armed Forces", "side_b_new_id": 99302, "side_b": "Fixture Militia",
            "number_of_sources": 2, "source_article": "Fixture Wire, 2098-05-11, 'fixture headline'",
            "source_office": "Fixture Wire", "source_date": "2098-05-11", "source_headline": "fixture headline",
            "source_original": "fixture wire service", "where_prec": 1, "where_coordinates": "Khartoum town",
            "where_description": "fixture free text", "adm_1": "Khartoum state", "adm_2": "Khartoum locality",
            "latitude": 15.55, "longitude": 32.53, "geom_wkt": "POINT (32.53 15.55)", "priogrid_gid": 999001,
            "country": "Sudan", "country_id": 625, "region": "Africa", "event_clarity": 1, "date_prec": 1,
            "date_start": "2098-05-10 00:00:00.000", "date_end": "2098-05-10 00:00:00.000",
            "deaths_a": 1, "deaths_b": 2, "deaths_civilians": 0, "deaths_unknown": 0, "best": 3, "high": 5, "low": 2,
            "gwnoa": "625", "gwnob": None}
    base.update(fields)
    return base


def candidate_events():
    return [
        ucdp_event(990001),
        ucdp_event(990002, type_of_violence=3, where_prec=4, adm_1="North Darfur state", adm_2=None, latitude=15.8,
                   longitude=25.0, where_coordinates="North Darfur state", date_prec=3, side_b="Civilians",
                   side_b_new_id=1, dyad_name="Fixture Militia - civilians", side_a="Fixture Militia",
                   date_start="2098-05-14 00:00:00.000", date_end="2098-05-20 00:00:00.000", best=7, low=7, high=12),
        ucdp_event(990003, type_of_violence=2, where_prec=6, adm_1=None, adm_2=None, latitude=15.0, longitude=30.0,
                   where_coordinates="Sudan", date_start="2098-05-20 00:00:00.000", date_end="2098-05-20 00:00:00.000",
                   best=1, low=1, high=1),
    ]


def ged_events():
    return [
        ucdp_event(990001, best=4, high=5, low=2, deaths_b=3),
        ucdp_event(990002, type_of_violence=3, where_prec=3, adm_1="North Darfur state", adm_2="El Fasher locality",
                   latitude=13.63, longitude=25.35, where_coordinates="El Fasher locality", date_prec=3,
                   side_b="Civilians", side_b_new_id=1, dyad_name="Fixture Militia - civilians", side_a="Fixture Militia",
                   date_start="2098-05-14 00:00:00.000", date_end="2098-05-20 00:00:00.000", best=7, low=7, high=12),
        ucdp_event(990010, latitude=15.6, longitude=32.5, date_start="2098-07-02 00:00:00.000",
                   date_end="2098-07-02 00:00:00.000", best=2, low=2, high=2),
    ]


def ucdp_payload(events):
    return {"TotalCount": len(events), "TotalPages": 1, "PreviousPageUrl": "", "NextPageUrl": "", "Result": events}


SELECTIONS = {
    "reliefweb-reports-sdn": {"resource": "reports", "countries": ["SDN"], "disaster_ids": [99001], "limit": 50,
                              "date_from": "2098-04-15", "date_to": "2099-04-14"},
    "reliefweb-disasters-sdn": {"resource": "disasters", "countries": ["SDN"], "limit": 20},
    "hdx-sdn-datasets": {"groups": ["sdn"], "rows": 25, "max_hxl_resources": 10},
    "ucdp-candidate-sdn": {"version": "98.0.5", "release": {"version": "98.0.5", "published_on": "2098-06-10"},
                           "country": "625", "start_date": "2098-01-01", "end_date": "2098-12-31", "pagesize": 500},
    "ucdp-ged-sdn": {"version": "99.1", "release": {"version": "99.1", "published_on": "2099-06-01"},
                     "country": "625", "start_date": "2098-01-01", "end_date": "2098-12-31", "pagesize": 500},
}


def reliefweb_params(selection, offset=0):
    params = {"offset": offset, "limit": selection["limit"],
              "filter[conditions][0][value]": ",".join(selection["countries"])}
    return params


def native_pages(source_id, *, revised=False):
    selection = SELECTIONS[source_id]
    if source_id == "reliefweb-reports-sdn":
        return [{"url": f"{RELIEFWEB}/reports", "params": reliefweb_params(selection), "json": reports_payload(revised)}]
    if source_id == "reliefweb-disasters-sdn":
        return [{"url": f"{RELIEFWEB}/disasters", "params": reliefweb_params(selection), "json": disasters_payload()}]
    if source_id == "hdx-sdn-datasets":
        pages = [{"url": f"{HDX}/package_search", "params": {"start": 0}, "json": hdx_payload(revised)}]
        for package in hdx_packages(revised):
            for resource in package["resources"]:
                if resource["id"] in HXL_HEADERS:
                    pages.append({"url": resource["url"], "text": HXL_HEADERS[resource["id"]]})
        return pages
    version = selection["version"]
    events = candidate_events() if source_id == "ucdp-candidate-sdn" else ged_events()
    return [{"url": f"{UCDP}/gedevents/{version}", "params": {"page": 0}, "json": ucdp_payload(events)}]


SCENARIOS = {
    "reliefweb-reports-sdn": ["situation report with publishers, dates and country/disaster tags",
                              "flash appeal classified as appeal", "out-of-scope map format skipped and counted"],
    "reliefweb-disasters-sdn": ["crisis with GLIDE number, status and type"],
    "hdx-sdn-datasets": ["HXL header with an untagged column and a contact tag flagged as personal data",
                         "HDX Connect dataset recorded as metadata only; no resource read",
                         "admin-1 p-code dataset with HXL tags"],
    "ucdp-candidate-sdn": ["candidate events with where/date/type precision, actor labels and best/low/high",
                           "free-text headline, article and where_description dropped",
                           "admin-level and country-level precision kept"],
    "ucdp-ged-sdn": ["final release revising a candidate's counts and location precision",
                     "complete release record listing its event ids (drops candidate 990003)"],
    "acled-events": ["licence declined: no request, no record; queries report not acquired (licence)"],
}


def fixture_document(source_id):
    if source_id == "acled-events":
        return {"scenarios": SCENARIOS[source_id], "normalized": []}
    return {"scenarios": SCENARIOS[source_id], "native_pages": native_pages(source_id)}


def write_fixtures():
    from src.ingestion.humanitarian_sources import replay_native_fixture
    from src.ingestion.source_packs import _digest, validate_source_pack

    pack_path = ROOT / "config/source_packs/humanitarian.json"
    pack = json.loads(pack_path.read_text())
    for source in pack["sources"]:
        path = ROOT / f"tests/fixtures/source_packs/humanitarian-{source['source_id']}.json"
        raw = json.dumps(fixture_document(source["source_id"]), sort_keys=True, separators=(",", ":")).encode()
        path.write_bytes(raw)
        source["fixture"] = {"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(raw).hexdigest(),
                             "expected_output_hash": "0" * 64}
    validated = {s["source_id"]: s for s in validate_source_pack(copy.deepcopy(pack))["sources"]}
    for source in pack["sources"]:
        document = fixture_document(source["source_id"])
        output = replay_native_fixture(validated[source["source_id"]], document) if document.get("native_pages") else []
        source["fixture"]["expected_output_hash"] = _digest(output)
    pack_path.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n")
    return {s["source_id"]: s["fixture"] for s in pack["sources"]}


if __name__ == "__main__":
    for source_id, fixture in write_fixtures().items():
        print(source_id, fixture)
