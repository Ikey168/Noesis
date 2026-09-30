"""Space-object registration offline acceptance (#2224, SO13): object to cited registration, operator and re-entry.

Authored fixtures naming fictional objects (UNOOSA index, UN registration
documents, the permitted DISCOS subset and Aerospace re-entry pages) replay
through the real adapter and projector together with the Astronomy pack's
fictional SATCAT objects; identity matching and review, citation links, as-of
answers across every revision boundary, evidence export and monitor events
follow. Sockets are blocked for the whole journey. This is **offline evidence
only**; live coverage is SO14 (#2461) and is recorded separately under
``docs/development/astronomy-evidence/``.
"""

from __future__ import annotations

import json
import socket

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.ingestion.source_packs import SourcePackError
from src.kb.astronomy_registration import NO_UN_REGISTRATION, RegistrationCitations, project_reentry_locations
from src.kb.astronomy_registration_identity import RegistrationIdentity
from src.kb.astronomy_registration_monitoring import RegistrationMonitor
from src.kb.astronomy_registration_queries import RegistrationQueries
from src.kb.entities import ensure_entity_schema, register_canonical_entity
from src.kb.subscriptions import SubscriptionStore
from tests.unit.astronomy import harness as ah
from tests.unit.astronomy import registration_harness as h
from tests.unit.astronomy.test_astronomy_registration_citations import seed_legal, seed_paper

NS = ah.NS
SCOPES = ah.SCOPES | {"knowledge:legal:read", "knowledge:geospatial:write"}
COMPUTED = {"predicted_by_noesis", "collision_probability", "footprint", "military_operator", "true_operator",
            "noesis_window"}
EVIDENCE = "offline-fixture"  # never reported as live evidence


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the offline journey opened a network connection")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def keys(value):
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in keys(v)}
    return set()


def test_object_to_cited_registration_operator_and_reentry_with_identity_and_revisions():
    bundles = adapt_all()
    plan = resolve([{"pack": "astronomy", "version": bundles["astronomy"]["version"],
                     "features": ["astronomy-space-object-registration", "astronomy-discos"]}],
                   list(bundles.values()), provider_descriptors())
    assert plan.ok and "astronomy-space-object-registration" in plan.plan["features"]["astronomy"]

    conn = duckdb.connect(":memory:")
    ah.acquire(conn, "satcat", "2099-06-01")
    ah.acquire(conn, "satcat", "2099-07-01")
    ah.acquire(conn, "gcat_satcat", "2099-07-01")
    h.acquire_all(conn, until="2099-04-02")
    monitor = RegistrationMonitor(conn)
    watch = monitor.create(NS, "fictsat1", watch="object", target="2099-001A", principal_id=ah.PRINCIPAL,
                           scopes=SCOPES)["subscription_id"]
    SubscriptionStore(conn).commit_watermark(NS, 1, kind="ingestion")
    assert monitor.run(watch, principal_id=ah.PRINCIPAL, scopes=SCOPES)["baseline"]
    h.acquire_all(conn)

    # Identity: exact SATCAT links with evidence, the GCAT conflict and the name-only registration for review.
    identity = RegistrationIdentity(conn)
    objects = identity.match_objects(NS, principal_id="analyst", scopes=SCOPES)
    assert objects["linked"]
    assert {c["basis"] for c in objects["candidates"]} == {"registration-identifier-conflict"}
    assert any(u["object_name"] == "FICTCUBE" for u in objects["unmatched"])
    ensure_entity_schema(conn)
    for name, kind in (("Fictland", "country"), ("Republic of Examplia", "country"),
                       ("Examplia Orbital Services", "organization")):
        register_canonical_entity(conn, "ent-fict-" + name.lower().replace(" ", "-"), name, entity_type=kind)
    parties = identity.match_parties(NS, principal_id="analyst", scopes=SCOPES)["candidates"]
    operator = next(c for c in parties if "operator:examplia orbital services" in c["left_key"] + c["right_key"])
    identity.review_party(NS, operator["candidate_id"], "accept", "same operator as published",
                          principal_id="reviewer", scopes=SCOPES)

    # Citations: the Registration Convention and the transfer paper by the identifiers the documents state.
    seed_legal(conn)
    seed_paper(conn, "10.5555/fict.2099.reg1")
    linked = RegistrationCitations(conn).link(NS, principal_id="analyst", scopes=SCOPES, legal_namespace="global")
    assert {link["citation_value"] for link in linked["links"] if link["state"] == "linked"} == {
        "UNTS-1023-15", "10.5555/fict.2099.reg1"}

    queries = RegistrationQueries(conn)
    # Registration with locator, before the transfer.
    april = queries.object_registration_as_of(NS, "2099-001A", "2099-04-15", scopes=ah.READ_ONLY)
    assert april["registration"]["current"]["supervising_state"]["value"] == "Fictland"
    registration = april["registration"]["documents"][0]
    assert registration["un_document"] == "ST/SG/SER.E/9901" and registration["document_locator"] == {
        "paragraph": "2"} and registration["language"] == "en"
    assert registration["registered_orbit"]["apogee"] == {"value": "420", "unit": "kilometres"}
    # Transfer of supervision, the operator match, prediction-to-confirmed re-entry revisions.
    july = queries.object_registration_as_of(NS, "99901", "2099-07-10", scopes=ah.READ_ONLY)
    current = july["registration"]["current"]
    assert current["registering_state"]["value"] == "Fictland"
    assert current["supervising_state"]["value"] == "Republic of Examplia"
    examplia = next(o for o in july["operators"] if o["operator_name"] == "Examplia Orbital Services")
    assert examplia["identity"]["state"] == "matched"
    aerospace = next(r for r in july["reentry"] if r["provider"] == "aerospace-reentry")
    assert [p["issued_at"] for p in aerospace["predictions"]] == [
        "2099-06-18T06:00:00Z", "2099-06-19T06:00:00Z", "2099-06-20T18:00:00Z"]
    assert aerospace["confirmed"][0]["location"]["text"] == "South Pacific Ocean"
    assert july["catalogue"] and july["catalogue"][0]["decay_date"] == "2099-06-21"
    assert july["identity_matches"]["object_links"] and july["citations"]
    assert not keys(july) & COMPUTED
    places = project_reentry_locations(conn, NS, "geo", principal_id="analyst", scopes=SCOPES)
    assert len(places["projected"]) == 1 and places["text_only"] == []
    exported = queries.export_bundle(NS, july, scopes=ah.READ_ONLY)["bundle"]
    assert exported["roots"] and "Fictsat-1" not in json.dumps(exported)  # DISCOS values stay unexported

    # Monitor: the transfer, operator change and re-entry reports since the baseline, each cited.
    SubscriptionStore(conn).commit_watermark(NS, 2, kind="ingestion")
    events = {n["event"] for n in monitor.run(watch, principal_id=ah.PRINCIPAL, scopes=SCOPES)["notifications"]}
    assert {"supervision_transferred", "operator_changed", "reentry_confirmed"} <= events

    # Negative cases: no UN registration on record; name-only registration; DISCOS disabled.
    rocket = queries.object_registration_as_of(NS, "2099-001B", "2099-07-10", scopes=ah.READ_ONLY)
    assert rocket["registration"]["un_registration"]["state"] == NO_UN_REGISTRATION
    cube = queries.object_registration_as_of(NS, "FICTCUBE", "2099-07-10", scopes=ah.READ_ONLY)
    assert cube["status"] == "answered" and cube["catalogue"] == []
    assert cube["identity_matches"]["object_links"] == []
    with pytest.raises(SourcePackError) as caught:
        h.acquire(duckdb.connect(":memory:"), "discos_objects", "2099-06-01", secret=None)
    assert caught.value.code == "credential_missing"
    assert EVIDENCE == "offline-fixture"
