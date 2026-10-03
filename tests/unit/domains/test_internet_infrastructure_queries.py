"""An ASN's, prefix's or domain's records as of a date, side by side and cited (II09, #2791 under #2743)."""

from __future__ import annotations

import pytest

from src.kb.internet_infrastructure_identity import InfrastructureIdentity
from src.kb.internet_infrastructure_queries import InfrastructureQueries
from src.kb.internet_infrastructure_records import InfrastructureRecordError, forbidden_paths, personal_data_paths
from tests.unit import internet_infrastructure_harness as h


@pytest.fixture()
def conn():
    value = h.connection()
    h.load_all(value, revisions=True)
    yield value
    value.close()


def _rows(answer, section):
    return answer["sections"][section]


def test_as_of_answers_select_the_revision_or_observation_current_at_the_date(conn):
    queries = InfrastructureQueries(conn)
    early = queries.records_as_of(h.NS, "AS64500", scopes=h.READ_ONLY, as_of="2096-01-01")
    late = queries.records_as_of(h.NS, {"asn": "AS64500"}, scopes=h.READ_ONLY, as_of="2097-12-31")
    net_early = next(s for s in _rows(early, "interconnection") if s["object_kind"] == "net")
    net_late = next(s for s in _rows(late, "interconnection") if s["object_kind"] == "net")
    assert (net_early["statement"]["policy_general"], net_late["statement"]["policy_general"]) == ("Open",
                                                                                                    "Selective")
    org_late = next(s for s in _rows(late, "interconnection") if s["object_kind"] == "org")
    assert org_late["state"] == "removed_by_source"
    routing_early = next(s for s in _rows(early, "routing") if s["data_call"] == "routing-status")
    routing_late = next(s for s in _rows(late, "routing") if s["data_call"] == "routing-status")
    assert routing_early["observed_at"] == "2095-05-31T12:00:00Z" and routing_late["observed_at"].startswith("2097")
    registration = _rows(late, "registration")
    assert [(s["object_kind"], s["statement"]["rir"]) for s in registration] == [("autnum", "ARIN")]
    assert _rows(early, "registration")[0]["statement"]["rir"] == "RIPE NCC"
    before_anything = queries.records_as_of(h.NS, "AS64500", scopes=h.READ_ONLY, as_of="2094-01-01")
    assert before_anything["status"] == "none_on_record" and before_anything["not_yet_recorded"]


def test_each_source_is_shown_separately_and_cited(conn):
    InfrastructureIdentity(conn).propose(h.NS, principal_id="op", scopes=h.SCOPES)
    answer = InfrastructureQueries(conn).records_as_of(h.NS, "AS64500", scopes=h.READ_ONLY, as_of="2096-01-01")
    providers = {s["provider"] for rows in answer["sections"].values() for s in rows}
    assert providers == {"ripestat", "peeringdb", "rdap"}
    for rows in answer["sections"].values():
        for statement in rows:
            citation = statement["citation"]
            assert citation["provider"] == statement["provider"] and citation["record_id"]
            assert citation["as_of"] and citation["retrieved_at"] and citation["source_id"]
            assert citation["live_verification"] == "unverified-live" and citation["evidence_origin"] == "fixture"
    # Proposed (unreviewed) matches are not used; nothing is merged.
    assert answer["identity_matches"] == []
    assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
    domain = InfrastructureQueries(conn).records_as_of(h.NS, "example.org", scopes=h.READ_ONLY, as_of="2096-01-01")
    assert [s["native_id"] for s in _rows(domain, "certificates")] == ["70000001", "70000002", "70000003"]
    assert _rows(domain, "registration")[0]["statement"]["holder_organisation"] == "Example Networks Ltd (fictional)"


@pytest.mark.parametrize(("resource", "code"), [
    ("192.0.2.1", "ip_lookup_refused"),
    ("2001:db8::1", "ip_lookup_refused"),
    ("192.0.2.1/32", "ip_lookup_refused"),
    ({"prefix": "2001:db8::1/128"}, "ip_lookup_refused"),
    ("hostmaster@example.org", "person_identifier_refused"),
    ("JD1-RIPE", "person_identifier_refused"),
    ("Jane Example", "person_identifier_refused"),
    ("*.example.org", "wildcard_refused"),
    ("%.example.org", "wildcard_refused"),
    ("AS*", "wildcard_refused"),
    ("AS64501", "undeclared_resource"),
    ("198.51.100.0/24", "undeclared_resource"),
    ("www2.example.org", "undeclared_resource"),
])
def test_ip_keyed_person_keyed_wildcard_and_undeclared_lookups_are_refused(conn, resource, code):
    with pytest.raises(InfrastructureRecordError) as caught:
        InfrastructureQueries(conn).records_as_of(h.NS, resource, scopes=h.READ_ONLY, as_of="2096-01-01")
    assert caught.value.code == code


def test_reads_need_the_read_scope(conn):
    with pytest.raises(InfrastructureRecordError) as caught:
        InfrastructureQueries(conn).records_as_of(h.NS, "AS64500", scopes={"namespace:global:read"})
    assert caught.value.code == "unauthorized"


def test_evidence_bundle_cites_every_item_with_source_revision_and_as_of_time(conn):
    queries = InfrastructureQueries(conn)
    answer = queries.records_as_of(h.NS, "AS64500", scopes=h.READ_ONLY, as_of="2097-12-31")
    bundle = queries.export_bundle(h.NS, answer, scopes=h.READ_ONLY)["bundle"]
    evidence = [o for o in bundle["objects"] if o.get("payload", {}).get("kind") == "internet-infrastructure-statement"]
    statements = sum(len(rows) for rows in answer["sections"].values())
    assert len(evidence) == statements > 0
    for item in evidence:
        payload = item["payload"]
        assert payload["provider"] and payload["record_id"] and payload["as_of"] and payload["source_id"]
        assert payload["revision_basis"] and payload["retrieved_at"]
