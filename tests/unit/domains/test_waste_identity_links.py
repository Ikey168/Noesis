"""WC06 (#2778) and WC07 (#2783): reviewable place, facility and indicator identity; Chemicals and Products links."""

from __future__ import annotations

import pytest

from src.kb.waste_identity import WasteIdentity
from src.kb.waste_links import WasteLinks
from src.kb.waste_records import WasteError
from src.kb.waste_store import WasteStore
from tests.unit import waste_harness as h


@pytest.fixture(scope="module")
def loaded(tmp_path_factory):
    import duckdb

    path = str(tmp_path_factory.mktemp("waste") / "identity.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn, revisions=True)
    conn.close()
    return path


@pytest.fixture()
def conn(loaded, tmp_path):
    import shutil

    import duckdb

    copy = str(tmp_path / "copy.duckdb")
    shutil.copy(loaded, copy)
    return duckdb.connect(copy)


def _accept_all(identity, kind, state="proposed"):
    for a in identity.assertions(h.NS, scopes=h.SCOPES, kind=kind, state=state):
        identity.review(h.NS, a["assertion_id"], "accept", "published code checked", principal_id=h.REVIEWER,
                        scopes=h.SCOPES)


def test_places_match_by_published_code_with_method_evidence_and_confidence_and_nothing_is_auto_merged(conn):
    h.register_places(conn)
    identity = WasteIdentity(conn)
    proposed = identity.propose_places(h.NS, principal_id="svc", scopes=h.SCOPES)
    by_code = {(a["subject"]["scheme"], a["subject"]["code"]): a for a in proposed["assertions"]}
    assert set(by_code) == {("eurostat-geo", "DE"), ("eurostat-geo", "FR"), ("iso3166-1-alpha3", "DEU"),
                            ("iso3166-1-alpha3", "FRA")}
    de = by_code[("eurostat-geo", "DE")]
    assert de["state"] == "proposed" and de["method"] == "published-code" and de["confidence"] == "high"
    assert de["evidence"]["candidates"][0]["evidence"]["source_id_key"] == "nuts" and de["merged"] is False
    assert identity.area_codes_for_place(h.NS, de["target"]["place_id"]) == []  # nothing used until reviewed
    _accept_all(identity, "area")
    place = de["target"]["place_id"]
    assert {(c["scheme"], c["code"]) for c in identity.area_codes_for_place(h.NS, place)} == {
        ("eurostat-geo", "DE"), ("iso3166-1-alpha3", "DEU")}
    reverted = identity.revert(h.NS, de["assertion_id"], "re-check", principal_id=h.REVIEWER, scopes=h.SCOPES)
    assert reverted["state"] == "reverted" and [h["state"] for h in reverted["history"]] == [
        "proposed", "accepted", "reverted"]
    assert {(c["scheme"], c["code"]) for c in identity.area_codes_for_place(h.NS, place)} == {
        ("iso3166-1-alpha3", "DEU")}
    with pytest.raises(WasteError):
        identity.review(h.NS, de["assertion_id"], "accept", "again", principal_id=h.REVIEWER, scopes=h.SCOPES)
    unknown = WasteIdentity(h.connection())
    assert unknown.assertions(h.NS, scopes=h.SCOPES) == []


def test_transfer_rows_attach_to_environment_core_facilities_and_unknown_ids_stay_unmatched(conn):
    identity = WasteIdentity(conn)
    facilities_before = conn.execute("SELECT count(*) FROM environment_records WHERE record_type='facility'").fetchone()
    result = identity.propose_facilities(h.NS, principal_id="svc", scopes=h.SCOPES)
    states = {a["subject"]["inspire_id"]: a for a in result["assertions"]}
    assert states[h.FACILITY_1]["state"] == "proposed" and states[h.FACILITY_1]["method"] == "published-code (INSPIRE id)"
    assert states[h.FACILITY_1]["target"]["provider_id"] == "environment.core"
    assert states[h.UNKNOWN_FACILITY]["state"] == "unmatched" and "no facility is created" in states[
        h.UNKNOWN_FACILITY]["reason"]
    assert result["unmatched_inspire_ids"] == [h.UNKNOWN_FACILITY]
    assert conn.execute("SELECT count(*) FROM environment_records WHERE record_type='facility'").fetchone() == \
        facilities_before
    # The unmatched rows stay visible.
    assert WasteStore(conn).transfer_rows(h.NS, inspire_id=h.UNKNOWN_FACILITY)
    _accept_all(identity, "facility")
    assert identity.facility_for(h.NS, h.FACILITY_1)["target"]["native_id"] == h.FACILITY_1
    assert identity.facility_for(h.NS, h.UNKNOWN_FACILITY)["target"] is None


def test_without_environment_core_the_facility_identity_is_provider_absent():
    conn = h.connection()
    h.apply(conn, "eea-industry-waste-transfers", retrieved_at_ms=h.FIRST_RETRIEVAL)
    conn.execute("DROP TABLE environment_records")
    result = WasteIdentity(conn).propose_facilities(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert {a["state"] for a in result["assertions"]} == {"provider_absent"}


def test_the_same_or_a_related_indicator_across_eurostat_and_oecd_is_recorded_as_related_never_merged(conn):
    h.register_places(conn)
    identity = WasteIdentity(conn)
    assert identity.propose_related_indicators(h.NS, principal_id="svc", scopes=h.SCOPES)["assertions"] == []
    identity.propose_places(h.NS, principal_id="svc", scopes=h.SCOPES)
    _accept_all(identity, "area")
    related = identity.propose_related_indicators(h.NS, principal_id="svc", scopes=h.SCOPES)["assertions"]
    assert related and {a["relation"] for a in related} == {"related-different-scope"}
    assert all(a["target"]["merge"] is False for a in related)
    oecd = h.series_by(conn, "oecd-municipal-waste", area="DEU")
    eurostat = h.series_by(conn, "eurostat-waste", area="DE", dataset="env_wasgen", hazard="HAZ_NHAZ")
    pair = next(a for a in related if oecd["series_id"] in a["subject"]["pair"])
    assert eurostat["series_id"] in pair["subject"]["pair"]
    _accept_all(identity, "indicator")
    assert identity.related(h.NS, oecd["series_id"])[0]["series_id"] == eurostat["series_id"]
    store = WasteStore(conn)
    # Both series keep their own vintages (the OECD DEU series was unchanged by the revision; Eurostat DE was not).
    assert len(store.vintage_rows(h.NS, oecd["series_id"])) == 1
    assert len(store.vintage_rows(h.NS, eurostat["series_id"])) == 2


def test_chemicals_link_only_by_published_cas_and_products_only_by_citation(conn):
    links = WasteLinks(conn)
    absent = links.link_chemicals(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert absent["missing"] and not absent["linked"]
    assert {link["state"] for link in links.links(h.NS, scopes=h.SCOPES, kind="chemicals")} == {"provider_absent"}
    products_absent = links.link_products(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert products_absent["missing"]
    subject_key = h.seed_chemicals(conn)
    h.seed_products(conn)
    chemicals = links.link_chemicals(h.NS, principal_id="svc", scopes=h.SCOPES)
    linked = [links.link(h.NS, i) for i in chemicals["linked"]]
    assert linked and {link["subject_kind"] for link in linked} == {"transfer_row"}
    for link in linked:
        assert link["basis"] == "shared-identifier" and link["target"]["subject_key"] == subject_key
        assert link["vintage_id"] and link["target"]["revision_ids"]  # both sides pinned
        assert link["reference"]["identifier"] == h.MERCURY_CAS
    products = [links.link(h.NS, i) for i in links.link_products(h.NS, principal_id="svc", scopes=h.SCOPES)["linked"]]
    assert products and {p["basis"] for p in products} == {"citation"}
    assert {p["target"]["url"] for p in products} == {h.PACKAGING_URL}
    weee = links.links(h.NS, scopes=h.SCOPES, kind="products", state="target_not_held")
    assert {link["reference"]["identifier"] for link in weee} == {"env_waselee"}  # reported, not dropped
    with pytest.raises(WasteError):
        links.link_chemicals(h.NS, principal_id="svc", scopes=h.SCOPES - {"knowledge:substances:read"})


def test_a_substance_with_only_the_same_name_is_never_linked(conn):
    from src.kb.substances_records import statement
    from src.kb.substances_store import SubstanceStore

    subject = {"key": "pubchem:cid:99000202", "kind": "unknown", "name": "mercury"}
    src = {"url": "https://echa.europa.eu/", "locator": "/", "attribution": "authored test record (fictional)"}
    SubstanceStore(conn).observe(h.NS, [statement("substance", "pubchem", subject, "element",
                                                  {"preferred_name": "mercury"}, source=src)])
    result = WasteLinks(conn).link_chemicals(h.NS, principal_id="svc", scopes=h.SCOPES)
    assert not result["linked"]
    assert {link["state"] for link in WasteLinks(conn).links(h.NS, scopes=h.SCOPES, kind="chemicals")} == {
        "target_not_held"}


def test_facility_links_pin_the_environment_core_record_revision_and_report_unmatched_rows(conn):
    identity = WasteIdentity(conn)
    identity.propose_facilities(h.NS, principal_id="svc", scopes=h.SCOPES)
    _accept_all(identity, "facility")
    links = WasteLinks(conn)
    result = links.link_facilities(h.NS, principal_id="svc", scopes=h.SCOPES)
    linked = [links.link(h.NS, i) for i in result["linked"]]
    assert linked and all(link["target"]["revision_id"] and link["vintage_id"] for link in linked)
    missing = [links.link(h.NS, i) for i in result["missing"]]
    assert {m["reference"]["identifier"] for m in missing} == {h.UNKNOWN_FACILITY}
    assert {m["state"] for m in missing} == {"target_not_held"}
