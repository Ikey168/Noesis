"""Offline ASN-and-domain acceptance for the Technology internet-infrastructure features (II12, #2807 under #2743).

The pinned RIPEstat, PeeringDB, RDAP, crt.sh and CT log list fixtures (authored in the publishers' documented shapes:
documentation ASN AS64500, documentation prefixes, ``example.org``, fictional organisations, dates in 2094-2099) replay
through the real ``internet-infrastructure`` adapter and the runtime's projector with every internet-infrastructure
feature selected in the composition plan and sockets blocked. The journey takes a declared ASN and a declared domain to
cited routing, interconnection, registration and certificate records with revisions, side by side and never merged.
Offline evidence only, never live coverage (``docs/development/internet-infrastructure-evidence/``).
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion import internet_infrastructure_sources as ii
from src.ingestion.source_pack_runtime import PROJECTORS
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.internet_infrastructure_identity import InfrastructureIdentity
from src.kb.internet_infrastructure_links import InfrastructureLinks, summarise
from src.kb.internet_infrastructure_monitoring import InfrastructureMonitor
from src.kb.internet_infrastructure_queries import InfrastructureQueries
from src.kb.internet_infrastructure_records import (
    FEATURES,
    InfrastructureRecordError,
    feature_enabled,
    forbidden_paths,
    personal_data_paths,
    readiness,
)
from tests.unit import internet_infrastructure_harness as h
from tests.unit.composition.test_migration import _migrated

PERSON_MARKERS = ("Jane Example", "John Example", "jane@", "john@", "hostmaster@", "abuse@", "noc@", "tel:",
                  "Example Street", "JD1-RIPE", "+00 0000")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


def test_asn_and_domain_to_cited_routing_interconnection_registration_and_certificate_records_with_revisions():
    conn, coordinator, bundles, _ = _migrated()
    coordinator.select("technology", bundles["technology"]["version"], features=list(FEATURES))
    assert coordinator.activate("technology-internet-infrastructure-acceptance")["status"] == "published"
    assert feature_enabled(conn) is True
    assert "noesis-internet-infrastructure-record-v2" in PROJECTORS

    # The pinned fixtures are valid and replay deterministically through the real adapter.
    gate = SourcePackConformance(h.ROOT).offline(json.loads(h.PACK.read_text()))
    assert gate["valid"] and gate["coverage"]["verified"] == 5

    # First acquisition (2095), then revisions (2097): PeeringDB update and removals, an RIR transfer, changed
    # routing, a CT log retired.
    monitor = InfrastructureMonitor(conn, now=lambda: h.FIRST_RETRIEVAL)
    h.load_all(conn)
    watch_asn = monitor.create(h.NS, "asn", target={"resource": "AS64500"}, principal_id="alice", scopes=h.SCOPES)
    watch_domain = monitor.create(h.NS, "domain", target={"resource": "example.org"}, principal_id="alice",
                                  scopes=h.SCOPES)
    monitor.run(watch_asn["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    monitor.run(watch_domain["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    # Identity is proposed by stated identifiers while the PeeringDB organisation is still published.
    identity = InfrastructureIdentity(conn)
    proposed = identity.propose(h.NS, principal_id="op", scopes=h.SCOPES)["proposed"]
    for name in ("ripestat", "peeringdb", "rdap", "ct"):
        h.apply(conn, name, revision=name, retrieved_at_ms=h.SECOND_RETRIEVAL)
    store = h.store(conn)

    # Routing observations dated by RIPEstat, never rewritten.
    routing = next(o for o in store.objects(h.NS, provider="ripestat") if o["native_id"] == "routing-status:AS64500")
    observed = store.observations(h.NS, routing["object_id"])
    assert [o["stated_time"] for o in observed] == ["2095-05-31T12:00:00Z", "2097-05-31T12:00:00Z"]
    assert observed[1]["previous_observation_id"] == observed[0]["observation_id"]

    # Revision history: PeeringDB updated, a removed PeeringDB object, an RIR transfer.
    net = store.revisions(h.NS, h.one(conn, "peeringdb", "net")["object_id"])
    assert [r["source_revision"] for r in net] == ["2095-03-01T00:00:00Z", "2096-05-01T00:00:00Z"]
    org = store.revisions(h.NS, h.one(conn, "peeringdb", "org")["object_id"])
    assert org[-1]["state"] == "removed_by_source" and org[0]["state"] == "published"
    autnum = store.revisions(h.NS, h.one(conn, "rdap", "autnum")["object_id"])
    assert [r["content"]["rir"] for r in autnum] == ["RIPE NCC", "ARIN"]

    queries = InfrastructureQueries(conn)
    before = queries.records_as_of(h.NS, "AS64500", scopes=h.READ_ONLY, as_of="2096-01-01")
    after = queries.records_as_of(h.NS, "AS64500", scopes=h.READ_ONLY, as_of="2097-12-31")
    for answer in (before, after):
        assert {s["provider"] for rows in answer["sections"].values() for s in rows} == {"ripestat", "peeringdb",
                                                                                         "rdap"}
        for rows in answer["sections"].values():
            for statement in rows:
                assert statement["citation"]["record_id"] and statement["citation"]["as_of"]
                assert statement["citation"]["evidence_origin"] == "fixture"
    assert next(s for s in before["sections"]["interconnection"] if s["object_kind"] == "org")["state"] == \
        "published"
    assert next(s for s in after["sections"]["interconnection"] if s["object_kind"] == "org")["state"] == \
        "removed_by_source"
    assert before["sections"]["registration"][0]["statement"]["rir"] == "RIPE NCC"
    assert after["sections"]["registration"][0]["statement"]["rir"] == "ARIN"

    # The domain: an RDAP registration and the crt.sh certificates, side by side.
    domain = queries.records_as_of(h.NS, "example.org", scopes=h.READ_ONLY, as_of="2096-01-01")
    assert [s["native_id"] for s in domain["sections"]["certificates"]] == ["70000001", "70000002", "70000003"]
    assert domain["sections"]["registration"][0]["statement"]["nameservers"] == ["ns1.example.org",
                                                                                  "ns2.example.org"]

    # An over-budget crt.sh answer is refused, never truncated, and changes nothing.
    rows = [{"id": 73000000 + i, "issuer_name": "C=ZZ, O=Fictional Trust Services", "name_value": "example.org",
             "not_before": "2098-01-01T00:00:00", "not_after": "2098-04-01T00:00:00",
             "entry_timestamp": "2098-01-01T00:00:00"} for i in range(201)]
    counted = conn.execute("SELECT count(*) FROM ii_revisions").fetchone()[0]
    with pytest.raises(SourcePackError) as caught:
        h.fetch("crtsh", transport=ii.fixture_transport([{"request": "crt.sh/?output=json&q=example.org",
                                                          "body": rows}]))
    assert caught.value.code == "budget_exhausted"
    assert conn.execute("SELECT count(*) FROM ii_revisions").fetchone()[0] == counted

    # Identity review: accepted by a reviewer, shown beside the statements (never merged); a removed organisation is
    # not proposed again.
    assert not [a for a in identity.propose(h.NS, principal_id="op", scopes=h.SCOPES)["proposed"]
                if a["kind"] == "organisation"]
    organisation = next(a for a in proposed if a["kind"] == "organisation")
    identity.review(h.NS, organisation["assertion_id"], "accepted", "both tied to AS64500 by stated identifiers",
                    principal_id="rev", scopes=h.SCOPES)
    same_asn = next(a for a in proposed if a["kind"] == "same-asn")
    identity.review(h.NS, same_asn["assertion_id"], "accepted", "AS64500 stated by both", principal_id="rev",
                    scopes=h.SCOPES)
    reviewed = queries.records_as_of(h.NS, "AS64500", scopes=h.READ_ONLY, as_of="2097-12-31")
    assert {m["assertion_id"] for m in reviewed["identity_matches"]} >= {same_asn["assertion_id"]}
    assert len({s["object_id"] for rows in reviewed["sections"].values() for s in rows}) == \
        sum(len(rows) for rows in reviewed["sections"].values())
    assert identity.unmatched(h.NS, scopes=h.READ_ONLY)  # unmatched records stay visible

    # OSINT links by the stated domain of an existing source identity; Vulnerabilities none (no stated CVE id).
    h.register_source_identity(conn)
    links = InfrastructureLinks(conn)
    assert summarise(links.link_osint(h.NS, principal_id="op", scopes=h.SCOPES)["links"]) == {"linked": 4}
    assert links.link_vulnerabilities(h.NS, principal_id="op", scopes=h.SCOPES)["links"] == []

    # Refused IP-keyed, person-keyed and wildcard queries, and a declared resource with no records.
    for resource, code in (("192.0.2.1", "ip_lookup_refused"), ("hostmaster@example.org",
                                                                 "person_identifier_refused"),
                           ("JD1-RIPE", "person_identifier_refused"), ("*.example.org", "wildcard_refused"),
                           ("AS64511", "undeclared_resource")):
        with pytest.raises(InfrastructureRecordError) as refused:
            queries.records_as_of(h.NS, resource, scopes=h.READ_ONLY)
        assert refused.value.code == code
    empty = queries.records_as_of(h.NS, "192.0.2.0/24", scopes=h.READ_ONLY, as_of="2094-06-01")
    assert empty["status"] == "none_on_record" and set(empty["coverage"].values()) == {"none_on_record"}

    # Monitors report the changes with citations and replay nothing on restart.
    later = InfrastructureMonitor(conn, now=lambda: h.SECOND_RETRIEVAL)
    notices = later.run(watch_asn["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {"peeringdb_update", "registration_change", "removed_by_source",
            "routing_observation_changed"} <= {n["kind"] for n in notices}
    assert later.run(watch_asn["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # The evidence bundle cites every item; exclusions and the II01 minimisation decision hold everywhere.
    bundle = queries.export_bundle(h.NS, reviewed, scopes=h.READ_ONLY)
    payloads = [o["payload"] for o in bundle["bundle"]["objects"]
                if o.get("payload", {}).get("kind") == "internet-infrastructure-statement"]
    assert payloads and all(p["record_id"] and p["as_of"] and p["provider"] for p in payloads)
    everything = json.dumps([conn.execute(f"SELECT * FROM {t}").fetchall() for t in (
        "ii_revisions", "ii_observations", "ii_objects", "ii_units", "ii_links", "ii_identity_assertions")],
        default=str)
    for marker in PERSON_MARKERS + (ii.FIXTURE_SECRET,):
        assert marker not in everything, marker
    for answer in (before, after, domain, reviewed):
        assert forbidden_paths(answer) == [] and personal_data_paths(answer) == []
        assert answer["exclusions"] == list(ii.EXCLUSIONS)
    report = readiness(conn)
    assert report["minimisation"] == ii.MINIMISATION["decision"]
    assert {p["live_verification"] for p in report["providers"].values()} == {"unverified-live"}
    # Re-acquisition adds nothing.
    counts = conn.execute("SELECT (SELECT count(*) FROM ii_revisions), (SELECT count(*) FROM ii_observations)"
                          ).fetchone()
    for name in ("ripestat", "peeringdb", "rdap", "ct"):
        h.apply(conn, name, revision=name, retrieved_at_ms=h.SECOND_RETRIEVAL + 1)
    assert conn.execute("SELECT (SELECT count(*) FROM ii_revisions), (SELECT count(*) FROM ii_observations)"
                        ).fetchone() == counts
