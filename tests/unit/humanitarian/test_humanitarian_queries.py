"""HR09/HR10 (#2270, #2274): cited as-of answers and bounded conflict-event queries with precision and history."""

from __future__ import annotations

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.humanitarian_identity import HumanitarianIdentity
from src.kb.humanitarian_queries import HumanitarianQueries, to_evidence_bundle
from src.kb.humanitarian_records import HumanitarianError
from tests.unit.humanitarian.harness import NS, REVIEWER_SCOPES, SCOPES, world


@pytest.fixture(scope="module")
def reviewed():
    value = world()
    value.run("reliefweb-reports-sdn", revised=True)
    identity = HumanitarianIdentity(value.conn)
    identity.propose(NS, principal_id="alice", scopes=SCOPES)
    for subject, kind in (("place-ref:iso3:SDN", "geospatial-place"), ("place-ref:name:admin1:khartoum", "geospatial-place"),
                          ("hdx:dataset:hdx-fixture-0001", "crisis")):
        for assertion in identity.assertions(NS, scopes=SCOPES, subject_key=subject, target_kind=kind):
            identity.review(NS, assertion["assertion_id"], "accept", "reviewed against the source", principal_id="bob",
                            scopes=REVIEWER_SCOPES)
    return value, HumanitarianQueries(value.conn)


def test_as_of_answer_uses_the_revision_available_then_and_says_which(reviewed):
    _, queries = reviewed
    early = queries.published_about(NS, pcode="SDN", as_of="2098-05-20", scopes=SCOPES)
    report = next(i for i in early["items"] if i["record_key"] == "reliefweb:situation_report:9900001")
    assert report["title"] == "Sudan: fixture situation report No. 1" and report["revision_used"]["seq"] == 1
    assert report["citation"]["source"] == "reliefweb" and report["citation"]["as_of"].startswith("2098-05-02")
    assert "reliefweb:situation_report:9900004" not in {i["record_key"] for i in early["items"]}
    later = queries.published_about(NS, pcode="SDN", as_of="2098-07-01", scopes=SCOPES)
    report = next(i for i in later["items"] if i["record_key"] == "reliefweb:situation_report:9900001")
    assert report["title"].endswith("(corrected)") and report["revision_used"]["seq"] == 2
    assert "reliefweb:situation_report:9900004" in {i["record_key"] for i in later["items"]}
    before = queries.published_about(NS, pcode="SDN", as_of="2098-01-01", scopes=SCOPES)
    assert before["items"] == [] and {n.get("record_type") for n in before["none_on_record"]} >= {"situation_report", "appeal"}


def test_place_answer_reports_boundary_vintage_sources_consulted_and_declined(reviewed):
    _, queries = reviewed
    answer = queries.published_about(NS, pcode="SDN", as_of="2099-07-01", scopes=SCOPES)
    assert answer["boundary"]["boundary_vintage"] == "COD-AB SDN fixture 2098-03-01"
    assert answer["sources_declined"] == [{"source": "acled", "status": "declined", "reason": "not acquired (licence)",
                                           "reference": "docs/development/humanitarian-evidence/source-audit.md"
                                                        "#acled-licence-and-access-decision"}]
    assert {s["source"] for s in answer["sources_consulted"]} == {"reliefweb", "hdx", "ucdp-ged", "ucdp-candidate"}
    restricted = next(i for i in answer["items"] if i["record_key"] == "hdx:dataset:hdx-fixture-0002")
    assert restricted["metadata_only"] and restricted["access"] == "hdx-connect"
    assert all(i["citation"]["revision_id"] and i["citation"]["as_of"] for i in answer["items"])
    bundle = to_evidence_bundle(answer)
    verified = verify_bundle(bundle)
    assert not verified.errors and verified.status == "incomplete"  # well-formed; partial by declared omissions
    assert bundle["completeness"]["status"] == "partial"  # ACLED declined is an explicit omission
    assert len([o for o in bundle["objects"] if o["type"] == "evidence"]) == len(answer["items"])


def test_admin_hierarchy_only_through_accepted_matches_and_none_on_record(reviewed):
    _, queries = reviewed
    kassala = queries.published_about(NS, pcode="SD03", as_of="2099-07-01", scopes=SCOPES)
    assert kassala["items"] == [] and kassala["none_on_record"][0]["pcode"] == "SD03"
    darfur = queries.published_about(NS, pcode="SD02", as_of="2099-07-01", scopes=SCOPES)
    assert darfur["items"] == []  # North Darfur names were proposed but never accepted


def test_crisis_answer_uses_source_tags_and_accepted_crisis_assertions(reviewed):
    _, queries = reviewed
    answer = queries.published_about(NS, crisis_key="reliefweb:crisis:99001", as_of="2098-07-01", scopes=SCOPES)
    via = {i["record_key"]: i["matched_via"]["basis"] for i in answer["items"]}
    assert via["reliefweb:situation_report:9900001"] == "the source tags the disaster"
    assert via["hdx:dataset:hdx-fixture-0001"] == "accepted crisis assertion"
    assert "hdx:dataset:hdx-fixture-0003" not in via


def test_conflict_events_side_by_side_with_precision_policy(reviewed):
    _, queries = reviewed
    area = {"bbox": [22.0, 12.0, 26.0, 16.5]}
    admin = queries.conflict_events(NS, area=area, start="2098-01-01", end="2098-12-31", as_of="2098-12-01", scopes=SCOPES)
    events = admin["by_coder"]["ucdp"]
    assert [e["record_key"] for e in events] == ["ucdp:conflict_event:990002"]
    assert events[0]["precision_class"] == "admin" and events[0]["location_note"]
    assert events[0]["counts"]["best"] == 7 and events[0]["coding_status"] == "candidate"
    exact = queries.conflict_events(NS, area=area, start="2098-01-01", end="2098-12-31", as_of="2098-12-01",
                                    imprecise="exact-only", scopes=SCOPES)
    assert exact["by_coder"] == {} and exact["precision_policy"]["excluded"][0]["precision_class"] == "admin"
    assert exact["sources_declined"][0]["reason"] == "not acquired (licence)"
    assert "total" not in str(admin["by_coder"]) and "merged" not in admin["by_coder"]


def test_conflict_events_in_an_admin_place_as_of_the_final_release(reviewed):
    _, queries = reviewed
    result = queries.conflict_events(NS, area={"pcode": "SD01"}, start="2098-01-01", end="2098-12-31", scopes=SCOPES)
    assert result["area"]["boundary_vintage"] == "COD-AB SDN fixture 2098-03-01"
    assert [(e["record_key"], e["coding_status"]) for e in result["by_coder"]["ucdp"]] == [
        ("ucdp:conflict_event:990001", "final"), ("ucdp:conflict_event:990010", "final")]
    wide = queries.conflict_events(NS, area={"bbox": [28.0, 13.0, 31.0, 17.0]}, start="2098-01-01", end="2098-12-31",
                                   as_of="2098-12-01", imprecise="all", scopes=SCOPES)
    assert [e["precision_class"] for e in wide["by_coder"]["ucdp"]] == ["wide"]  # country-level, included on request


def test_event_history_returns_every_release_with_changed_fields(reviewed):
    _, queries = reviewed
    history = queries.event_history(NS, "ucdp:conflict_event:990001", scopes=SCOPES)
    assert [(r["coding_status"], r["dataset_version"]) for r in history["revisions"]] == [("candidate", "98.0.5"),
                                                                                          ("final", "99.1")]
    changed = {c["field"] for c in history["revisions"][1]["changed_fields"]}
    assert {"counts", "coding_status", "dataset_version"} <= changed
    dropped = queries.event_history(NS, "ucdp:conflict_event:990003", scopes=SCOPES)
    assert dropped["revisions"][-1]["coding_status"] == "dropped-in-release"


@pytest.mark.parametrize("kwargs,code", [
    ({"area": {"bbox": [20.0, 10.0, 30.0, 20.0]}, "start": "2098-01-01", "end": "2098-02-01"}, "area_too_large"),
    ({"area": {"bbox": [22.0, 12.0, 26.0, 16.0]}, "start": "2097-01-01", "end": "2098-12-31"}, "window_too_long"),
    ({"area": {"pcode": "SDN"}, "start": "2098-01-01", "end": "2098-02-01"}, "area_too_large"),
])
def test_bounds_are_enforced(reviewed, kwargs, code):
    _, queries = reviewed
    with pytest.raises(HumanitarianError) as caught:
        queries.conflict_events(NS, scopes=SCOPES, **kwargs)
    assert caught.value.code == code
