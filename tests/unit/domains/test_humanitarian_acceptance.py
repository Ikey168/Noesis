"""Offline place-to-humanitarian-dossier acceptance journey (HR13, #2288; tracking #2206).

Every acquired source (ReliefWeb reports and disasters, HDX, UCDP Candidate and
GED) replays its pinned fixture through the real ``humanitarian`` source-pack
connector and projector with sockets blocked; ACLED is the declined source. A
country and an admin place reach a cited dossier of reports, datasets and
conflict events as of a date, with the revision used per item, coding
precision and release history, coders side by side, reviewable identity
matches, a place with none on record and the declined source listed. Live
evidence is reported separately (``LIVE_VERIFICATION``; HR14 #2293) and is
unverified.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb.humanitarian_bundle import dossier, export_dossier
from src.kb.humanitarian_identity import HumanitarianIdentity
from src.kb.humanitarian_records import PERSONAL_DATA_FIELDS
from src.kb.humanitarian_store import HumanitarianStore
from tests.unit.humanitarian.harness import ACQUIRED, NS, REVIEWER_SCOPES, SCOPES, World

FORBIDDEN_KEYS = {"total", "merged", "true_count", "estimate", "forecast", "risk_score", "severity", "per_capita"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def keys_in(value, found=None):
    found = set() if found is None else found
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key).casefold())
            keys_in(item, found)
    elif isinstance(value, list):
        for item in value:
            keys_in(item, found)
    return found


def test_place_and_crisis_to_a_cited_dossier_with_precision_history_identity_and_gaps():
    world = World()
    world.install()
    receipts = [world.run(source_id) for source_id in ACQUIRED]
    assert all(r["status"] == "complete" for r in receipts), [r["status"] for r in receipts]
    acled = world.run("acled-events")
    assert acled["status"] == "complete" and acled["sources"][0]["counts"]["fetched"] == 0  # declined: nothing fetched
    world.run("reliefweb-reports-sdn", revised=True)
    world.boundaries()

    # Reviewable identity: proposed by one principal, accepted by another; nothing merged.
    identity = HumanitarianIdentity(world.conn)
    identity.propose(NS, principal_id="alice", scopes=SCOPES)
    for subject, kind in (("place-ref:iso3:SDN", "geospatial-place"), ("place-ref:name:admin1:khartoum", "geospatial-place"),
                          ("hdx:dataset:hdx-fixture-0001", "crisis")):
        for assertion in identity.assertions(NS, scopes=SCOPES, subject_key=subject, target_kind=kind):
            assert assertion["state"] == "proposed" and assertion["method"] and assertion["confidence"]
            identity.review(NS, assertion["assertion_id"], "accept", "checked against COD-AB and GLIDE",
                            principal_id="bob", scopes=REVIEWER_SCOPES)
    unmatched = {u["subject_key"] for u in identity.unmatched(NS, scopes=SCOPES)}
    assert "actor-ref:ucdp:fixture militia" in unmatched and "place-ref:name:admin1:north darfur" in unmatched

    # Country as of mid-2098: the first revision of the report, the candidate coding of events.
    early = dossier(world.conn, NS, as_of="2098-05-20", pcode="SDN", scopes=SCOPES,
                    events={"area": {"pcode": "SD01"}, "start": "2098-01-01", "end": "2098-12-31"})
    items = {i["record_key"]: i for i in early["published"]["items"]}
    assert items["reliefweb:situation_report:9900001"]["revision_used"]["seq"] == 1
    assert "reliefweb:situation_report:9900004" not in items
    assert early["conflict_events"]["by_coder"] == {}  # candidate release (2098-06-10) not yet published
    assert early["sources_declined"] == [{"source": "acled", "status": "declined", "reason": "not acquired (licence)",
                                          "reference": "docs/development/humanitarian-evidence/source-audit.md"
                                                       "#acled-licence-and-access-decision"}]

    # After the final GED release: corrected report, final codings with release history.
    late = dossier(world.conn, NS, as_of="2099-07-01", pcode="SDN", scopes=SCOPES,
                   events={"area": {"pcode": "SD01"}, "start": "2098-01-01", "end": "2098-12-31"})
    items = {i["record_key"]: i for i in late["published"]["items"]}
    assert items["reliefweb:situation_report:9900001"]["revision_used"]["seq"] == 2
    assert items["hdx:dataset:hdx-fixture-0002"]["metadata_only"] is True
    assert late["published"]["boundary"]["boundary_vintage"] == "COD-AB SDN fixture 2098-03-01"
    events = late["conflict_events"]["by_coder"]["ucdp"]
    first = next(e for e in events if e["record_key"] == "ucdp:conflict_event:990001")
    assert first["coding_status"] == "final" and first["counts"]["best"] == 4
    assert first["precision"]["where"] == {"code": 1, "scheme": "ucdp-where_prec", "label": "exact location"}
    assert [(h["coding_status"], h["dataset_version"]) for h in first["history"]] == [("candidate", "98.0.5"),
                                                                                     ("final", "99.1")]
    assert set(late["conflict_events"]["by_coder"]) == {"ucdp"}  # coders side by side; ACLED declined, never merged
    assert late["conflict_events"]["precision_policy"]["policy"] == "admin"

    # A dropped candidate is a revision, and admin-level events are included only under the stated policy.
    store = HumanitarianStore(world.conn, initialize=False)
    assert [h["content"]["coding_status"] for h in store.history(NS, "ucdp:conflict_event:990003", scopes=SCOPES)] == [
        "candidate", "dropped-in-release"]
    from src.kb.humanitarian_queries import HumanitarianQueries

    darfur = HumanitarianQueries(world.conn).conflict_events(
        NS, area={"bbox": [22.0, 12.0, 26.0, 16.5]}, start="2098-01-01", end="2098-12-31", scopes=SCOPES,
        imprecise="exact-only")
    assert darfur["by_coder"] == {} and darfur["precision_policy"]["excluded"]

    # A place with none on record.
    kassala = dossier(world.conn, NS, as_of="2099-07-01", pcode="SD03", scopes=SCOPES)
    assert kassala["published"]["items"] == [] and kassala["none_on_record"][0]["pcode"] == "SD03"

    # Crisis: source tags plus the accepted GLIDE assertion.
    crisis = dossier(world.conn, NS, as_of="2099-07-01", crisis_key="reliefweb:crisis:99001", scopes=SCOPES)
    via = {i["record_key"]: i["matched_via"]["basis"] for i in crisis["published"]["items"]}
    assert via["hdx:dataset:hdx-fixture-0001"] == "accepted crisis assertion"

    # Cited export: every record revision with source, revision and as-of; gaps and the declined source omitted.
    bundle = export_dossier(late)
    assert not verify_bundle(bundle).errors
    cited = [o["payload"]["citation"] for o in bundle["objects"] if o["payload"].get("kind") == "humanitarian-record-revision"]
    assert len(cited) == len(late["published"]["items"]) + len(events)
    assert all(c["source"] and c["source_id"] and c["revision"] and c["as_of"] for c in cited)
    assert any("acled" in o["reason"] for o in bundle["completeness"]["omissions"])

    # Exclusions: no merged counts, forecasts or personal-data fields anywhere in the dossier or the store.
    everything = keys_in(json.loads(json.dumps(late, default=str)))
    assert not everything & FORBIDDEN_KEYS
    stored = [json.loads(r[0]) for r in world.conn.execute("SELECT content_json FROM humanitarian_revisions").fetchall()]
    assert not keys_in(stored) & PERSONAL_DATA_FIELDS
    assert "fixture headline" not in json.dumps(stored)

    # Re-running every source adds nothing.
    before = world.conn.execute("SELECT count(*) FROM humanitarian_revisions").fetchone()[0]
    for source_id in ACQUIRED:
        world.run(source_id)
    world.run("reliefweb-reports-sdn", revised=True)
    assert world.conn.execute("SELECT count(*) FROM humanitarian_revisions").fetchone()[0] == before
