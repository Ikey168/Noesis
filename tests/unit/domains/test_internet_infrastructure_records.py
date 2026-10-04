"""Network registry records and routing observations with revisions (II02, #2752 under #2743).

Authored fixtures only (documentation ASN AS64500, 192.0.2.0/24, example.org, fictional organisations, 2094-2099).
"""

from __future__ import annotations

import copy
import json

import pytest
from jsonschema import Draft7Validator

from src.kb.internet_infrastructure_records import (
    CONTRACT,
    InfrastructureRecordError,
    check_item,
    personal_data_paths,
    readiness,
)
from src.kb.internet_infrastructure_store import InternetInfrastructureStore
from tests.unit import internet_infrastructure_harness as h


@pytest.fixture()
def conn():
    value = h.connection()
    yield value
    value.close()


def _items(name, **kwargs):
    return [r["ii_item"] for page in h.fetch(name, **kwargs) for r in page]


def test_every_item_validates_against_the_record_contract_and_the_minimisation_rules():
    schema = json.loads((h.ROOT / f"contracts/schemas/jsonschema/{CONTRACT}.json").read_text())
    Draft7Validator.check_schema(schema)
    for name in h.SOURCES:
        for item in _items(name):
            assert not list(Draft7Validator(schema).iter_errors(item)), (name, item)
            check_item(item)
            assert personal_data_paths(item) == []


def test_registry_records_carry_source_revision_and_as_of_time_and_chain_their_revisions(conn):
    h.load_all(conn, revisions=True)
    store = h.store(conn)
    net = h.one(conn, "peeringdb", "net")
    chain = store.revisions(h.NS, net["object_id"])
    assert [r["revision_no"] for r in chain] == [1, 2]
    assert chain[1]["previous_revision_id"] == chain[0]["revision_id"]
    assert [(r["basis"], r["source_revision"]) for r in chain] == [
        ("source_updated", "2095-03-01T00:00:00Z"), ("source_updated", "2096-05-01T00:00:00Z")]
    assert chain[1]["changes"]["fields"]["policy_general"] == {"before": "Open", "after": "Selective"}
    assert chain[0]["retrieved_at"].startswith("2095-06-01") and chain[1]["retrieved_at"].startswith("2097-06-01")
    # As-of lookup selects the revision in force at the date; before the first one nothing is recorded.
    assert store.revision_as_of(h.NS, net["object_id"], h.ms("2096-01-01"))["revision_no"] == 1
    assert store.revision_as_of(h.NS, net["object_id"], h.ms("2096-06-01"))["revision_no"] == 2
    assert store.revision_as_of(h.NS, net["object_id"], h.ms("2094-12-31")) is None
    autnum = store.revisions(h.NS, h.one(conn, "rdap", "autnum")["object_id"])
    assert [(r["basis"], r["source_revision"]) for r in autnum] == [
        ("last_changed", "2095-01-10T00:00:00Z"), ("last_changed", "2097-02-01T00:00:00Z")]
    log_a = store.revisions(h.NS, h.one(conn, "ct-log-list", "ct-log", "RklDVElPTkFMLUxPRy1BLTIwOTQ=")["object_id"])
    assert [(r["source_revision"], r["content"]["state"]["name"]) for r in log_a] == [("2094.1", "usable"),
                                                                                    ("2097.1", "retired")]
    # A log whose statement is unchanged in a new list version is not a new revision.
    log_b = h.one(conn, "ct-log-list", "ct-log", "RklDVElPTkFMLUxPRy1CLTIwOTQ=")
    assert len(store.revisions(h.NS, log_b["object_id"])) == 1


def test_404_status_deleted_and_absence_from_a_complete_listing_are_removed_by_source_revisions(conn):
    h.load_all(conn, revisions=True)
    store = h.store(conn)
    fac = store.revisions(h.NS, h.one(conn, "peeringdb", "fac")["object_id"])
    org = store.revisions(h.NS, h.one(conn, "peeringdb", "org")["object_id"])
    gone = store.revisions(h.NS, h.one(conn, "peeringdb", "netixlan", "8002")["object_id"])
    assert (fac[-1]["state"], fac[-1]["basis"], fac[-1]["valid_from_basis"]) == (
        "removed_by_source", "not_found", "retrieval_time")
    assert (org[-1]["state"], org[-1]["basis"], org[-1]["source_revision"]) == (
        "removed_by_source", "source_updated", "2096-04-01T00:00:00Z")
    assert (gone[-1]["state"], gone[-1]["basis"]) == ("removed_by_source", "absent_from_complete_listing")
    # Removals are revisions, never deletions: the earlier revision is still there and still answers its date.
    assert fac[0]["state"] == "published" and len(fac) == 2
    assert store.revision_as_of(h.NS, h.one(conn, "peeringdb", "fac")["object_id"],
                                h.ms("2096-01-01"))["state"] == "published"


def test_ripestat_answers_are_observations_dated_by_ripestat_and_never_rewritten(conn):
    h.load_all(conn, revisions=True)
    store = h.store(conn)
    routing = next(o for o in store.objects(h.NS, provider="ripestat")
                   if o["native_id"] == "routing-status:AS64500")
    assert routing["shape"] == "observations"
    first, second = store.observations(h.NS, routing["object_id"])
    assert (first["stated_time"], first["time_basis"], first["data_call_version"]) == (
        "2095-05-31T12:00:00Z", "query_time", "2.1")
    assert second["previous_observation_id"] == first["observation_id"] and second["changed"] is True
    assert store.observation_as_of(h.NS, routing["object_id"], h.ms("2096-01-01"))["observation_id"] == \
        first["observation_id"]
    overview = next(o for o in store.objects(h.NS, provider="ripestat") if o["native_id"] == "as-overview:AS64500")
    unchanged = store.observations(h.NS, overview["object_id"])
    assert len(unchanged) == 2 and unchanged[1]["changed"] is False  # a later answer is a later observation
    before = conn.execute("SELECT observation_id, content_json FROM ii_observations ORDER BY 1").fetchall()
    h.apply(conn, "ripestat", revision="ripestat", retrieved_at_ms=h.SECOND_RETRIEVAL + 1)
    assert conn.execute("SELECT observation_id, content_json FROM ii_observations ORDER BY 1").fetchall() == before


def test_statements_of_different_sources_about_one_asn_are_never_merged(conn):
    h.load_all(conn)
    store = h.store(conn)
    about = store.objects(h.NS, resource={"kind": "asn", "value": h.ASN})
    providers = {o["provider"] for o in about}
    assert providers == {"ripestat", "peeringdb", "rdap"}
    # Each object is keyed by its own source; no row combines providers.
    assert all(len({o["provider"]}) == 1 for o in about)
    assert len({o["object_id"] for o in about}) == len(about)


def test_rdap_individuals_contacts_and_certificate_subjects_are_refused_at_write_time(conn):
    rdap = _items("rdap")[0]
    assert rdap["content"]["redaction"]["individual_entities_dropped"] == 1
    for poison in (
        {"entities": [{"roles": ["administrative"], "vcardArray": ["vcard", [["kind", {}, "text", "individual"]]]}]},
        {"holder_contact": {"kind": "individual", "fn": "Jane Example"}},
        {"email": "hostmaster@example.org"},
        {"tel": "tel:+00-0000-0000"},
        {"adr": ["", "", "1 Example Street"]},
        {"poc_set": [{"role": "NOC"}]},
    ):
        bad = copy.deepcopy(rdap)
        bad["content"].update(poison)
        with pytest.raises(InfrastructureRecordError) as caught:
            check_item(bad)
        assert caught.value.code == "personal_data", poison
    cert = _items("crtsh")[0]
    for key, value in (("common_name", "example.org"), ("subject", "CN=example.org, O=Example"),
                       ("revoked", True)):
        bad = copy.deepcopy(cert)
        bad["content"][key] = value
        with pytest.raises(InfrastructureRecordError) as caught:
            check_item(bad)
        assert caught.value.code == "certificate_minimisation"
    bad = copy.deepcopy(cert)
    bad["content"]["dns_sans"].append("hostmaster@example.org")
    with pytest.raises(InfrastructureRecordError):
        check_item(bad)
    for verdict in ({"hijack_verdict": "suspected"}, {"risk_score": 3}, {"open_ports": [22]}, {"as_rank": 1}):
        bad = copy.deepcopy(rdap)
        bad["content"].update(verdict)
        with pytest.raises(InfrastructureRecordError) as caught:
            check_item(bad)
        assert caught.value.code == "excluded_field"
    # The store refuses the whole unit at write time; nothing is written.
    store = InternetInfrastructureStore(conn)
    page = h.fetch("rdap")[0]
    header = page[0]["ii_unit"]
    bad = copy.deepcopy(page[0]["ii_item"])
    bad["content"]["email"] = "hostmaster@example.org"
    with pytest.raises(InfrastructureRecordError):
        store.apply_unit(h.NS, header, [bad], source_id="rdap-registrations", run_id="r", principal_id="svc",
                         scopes=h.SCOPES)
    assert conn.execute("SELECT count(*) FROM ii_revisions").fetchone()[0] == 0


def test_ip_keyed_and_whois_items_are_refused():
    item = copy.deepcopy(_items("ripestat")[0])
    item["data_call"] = "whois"
    with pytest.raises(InfrastructureRecordError) as caught:
        check_item(item)
    assert caught.value.code == "call_forbidden"
    item = copy.deepcopy(_items("rdap")[1])
    item["resource"] = {"kind": "prefix", "value": "192.0.2.1/32"}
    with pytest.raises(InfrastructureRecordError) as caught:
        check_item(item)
    assert caught.value.code == "ip_keyed"


def test_a_failed_unit_records_a_receipt_and_never_a_removal(conn):
    h.load_all(conn)
    store = h.store(conn)
    before = conn.execute("SELECT count(*) FROM ii_revisions").fetchone()[0]
    failure = store.record_failure(h.NS, "peeringdb", code="rate_limited", run_id="r2",
                                   source_id="peeringdb-network", scopes=h.SCOPES)
    assert failure["outcome"] == "failed"
    assert conn.execute("SELECT count(*) FROM ii_revisions").fetchone()[0] == before
    assert store.provider_state(h.NS, "peeringdb") == {"stale": True, "reason": "last run failed"}
    assert readiness(conn)["providers"]["peeringdb"]["stale"] is True


def test_re_applying_a_unit_is_idempotent(conn):
    h.load_all(conn)
    counts = conn.execute("SELECT (SELECT count(*) FROM ii_revisions), (SELECT count(*) FROM ii_observations)"
                          ).fetchone()
    results = [r for name in h.SOURCES for r in h.apply(conn, name, retrieved_at_ms=h.FIRST_RETRIEVAL + 5)]
    assert {r["status"] for r in results} == {"unchanged"}
    assert conn.execute("SELECT (SELECT count(*) FROM ii_revisions), (SELECT count(*) FROM ii_observations)"
                        ).fetchone() == counts


def test_writes_and_reads_need_the_declared_scopes(conn):
    page = h.fetch("crtsh")[0]
    store = InternetInfrastructureStore(conn)
    with pytest.raises(InfrastructureRecordError) as caught:
        store.apply_unit(h.NS, page[0]["ii_unit"], [r["ii_item"] for r in page], source_id="crtsh-certificates",
                         run_id="r", principal_id="svc", scopes=h.READ_ONLY)
    assert caught.value.code == "unauthorized"
    store.apply_unit(h.NS, page[0]["ii_unit"], [r["ii_item"] for r in page], source_id="crtsh-certificates",
                     run_id="r", principal_id="svc", scopes=h.SCOPES)
    cert = h.one(conn, "crtsh", "certificate", "70000001")
    history = store.history(h.NS, cert["object_id"], scopes=h.READ_ONLY)
    assert history["revisions"][0]["citation"]["revision_basis"] == "entry_timestamp"
    with pytest.raises(InfrastructureRecordError):
        store.history(h.NS, cert["object_id"], scopes=set())
