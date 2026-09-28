"""Offline subject-to-safety-dossier acceptance (ES16, #2077): no network, no credentials.

Pinned FAA, EASA, NTSB, PHMSA, CSB, NHTSA ODI and complaint, BFU and BEA fixtures replay through the runtime
(sockets blocked) with later publications served in later runs: the NTSB preliminary report becomes final, its
recommendation changes status twice, and an ODI Preliminary Evaluation is upgraded to an Engineering Analysis
citing a recall campaign. The journey goes ingestion -> identity review (one accepted, one rejected) ->
citation linking -> directives as of a date -> dossier -> subscription events, and a restart replay adds nothing.
"""

from __future__ import annotations

import socket

import pytest

from src.kb.engineering_safety_citations import link_citations
from src.kb.engineering_safety_identity import SubjectIdentity
from src.kb.engineering_safety_monitoring import EngineeringSafetyMonitor
from src.kb.engineering_safety_queries import EngineeringSafetyQueries, export_bundle
from tests.unit.engineering_safety import harness as h

MONITOR_SCOPES = h.READ | {
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    f"namespace:{h.NS}:write",
}
VEHICLE = {"kind": "vehicle", "make": "VELOMARK", "model": "CITYRUNNER"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("the acceptance journey must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def kinds(result):
    return sorted((n["kind"], n["cites"]["native_id"]) for n in result["notifications"])


def test_subject_to_cited_engineering_safety_dossier_with_reviews_citations_and_monitoring():
    env = h.Env()
    variants = h.variants()
    # 1. Ingestion: every source, with the ODI file before the upgrade.
    first = env.run(
        "run-1",
        overrides={"nhtsa-odi-investigations": h.odi_pages(variants["odi_open_file"])},
    )
    assert first["status"] == "complete"
    revisions = env.conn.execute("SELECT count(*) FROM es_revisions").fetchone()[0]
    monitor = EngineeringSafetyMonitor(env.conn, now=lambda: next(env.clock))
    subscription = monitor.create(
        h.NS,
        "journey",
        watch={"subjects": [h.EX100, VEHICLE], "recommendations": ["ntsb:A-26-015"]},
        principal_id="analyst",
        scopes=MONITOR_SCOPES,
    )["subscription_id"]
    baseline = monitor.run(subscription, principal_id="analyst", scopes=MONITOR_SCOPES)
    assert baseline["baseline"] and ("new_directive", "2026-04-12") in kinds(baseline)

    # 2. Later publications: final NTSB report, first status change, ODI upgrade citing a recall.
    second = env.run(
        "run-2",
        source_ids=["ntsb-investigations", "nhtsa-odi-investigations"],
        overrides={
            "ntsb-investigations": h.ntsb_pages(
                case=variants["ntsb_case_final"],
                statuses=variants["ntsb_recommendation_statuses"][0],
            )
        },
    )
    assert second["status"] == "complete"
    third = env.run(
        "run-3",
        source_ids=["ntsb-investigations"],
        overrides={
            "ntsb-investigations": h.ntsb_pages(
                case=variants["ntsb_case_final"],
                statuses=variants["ntsb_recommendation_statuses"][1],
            )
        },
    )
    assert third["status"] == "complete"

    # 3. Identity review: accept the vehicle match, reject the operator match.
    model_id = h.seed_products(env.conn)
    h.seed_entities(env.conn)
    identity = SubjectIdentity(env.conn, now=lambda: next(env.clock))
    proposed = identity.propose(
        h.NS,
        scopes=h.REVIEW | h.PRODUCTS,
        products_namespace=h.NS,
        principal_id="matcher",
    )
    vehicle = next(
        c
        for c in proposed["candidates"]
        if c["target_id"] == model_id
        and c["subject_key"] == "vehicle:velomark:cityrunner:2025"
    )
    operator = next(
        c for c in proposed["candidates"] if c["target_id"] == "ent-examplar-pipeline"
    )
    assert (
        operator["subject_key"] == "pipeline-operator:39999"
    )  # PHMSA incident with an operator candidate
    identity.review(
        h.NS,
        vehicle["match_id"],
        "accepted",
        "make and model agree",
        scopes=h.REVIEW,
        principal_id="reviewer",
    )
    identity.review(
        h.NS,
        operator["match_id"],
        "rejected",
        "name only; different registration",
        scopes=h.REVIEW,
        principal_id="reviewer",
    )

    # 4. Citation linking, including the recall owned by the Products safety feature.
    notice_id = h.seed_recall(env.conn)
    links = link_citations(
        env.conn, h.NS, scopes=h.ALL, principal_id="linker", products_namespace=h.NS
    )
    linked = {(c["raw"], c["target_kind"]) for c in links["linked"]}
    assert ("AD 2025-12-05", "engineering-safety-record") in linked
    assert ("A-26-015", "engineering-safety-record") in linked
    assert ("26V104000", "product-safety-notice") in linked
    assert "SAE ARP5089" in {c["raw"] for c in links["unresolved"]}

    q = EngineeringSafetyQueries(env.conn)
    # 5. Directives as of a date: supersession and revision, the EASA AD cross-referencing the FAA AD.
    answer = q.directives_as_of(h.NS, h.EX100, scopes=h.READ, as_of="2026-06-01")
    in_effect = {i["native_id"]: i for i in answer["in_effect"]}
    faa = in_effect["2026-04-12"]
    assert (
        faa["revision"]["revision_label"] == "correction"
        and faa["revision"]["revision_no"] == 2
    )
    assert [c["native_id"] for c in faa["chain"]["supersedes"]] == ["2025-12-05"]
    easa = in_effect["2026-0123"]
    assert easa["revision"]["revision_label"] == "R1"
    easa_record = env.store.parts(h.NS, easa["revision"]["revision_id"])
    assert [
        (r["relation"], r["target_native_id"]) for r in easa_record["relations"]
    ] == [("cross_reference", "2026-04-12")]
    assert answer["semantics"] == "as published; not a compliance determination"

    # 6. Dossiers: aircraft model by designation, vehicle through the reviewed Products match.
    aircraft = q.dossier(h.NS, h.EX100, scopes=h.READ, as_of="2027-06-01")
    ntsb = next(
        i
        for i in aircraft["sections"]["investigations"]
        if i["native_id"] == "ERA26FA101"
    )
    assert ntsb["report_status"] == "final" and ntsb["probable_cause"][0][
        "text"
    ].startswith("The fatigue fracture")
    assert ntsb["probable_cause"][0]["locator"]["json_pointer"] == "/ProbableCause"
    (recommendation,) = [
        r
        for r in aircraft["sections"]["recommendations"]
        if r["native_id"] == "A-26-015"
    ]
    assert [s["status"] for s in recommendation["response_history"]] == [
        "Open - Await Response",
        "Open - Acceptable Response",
        "Closed - Acceptable Action",
    ]
    assert recommendation["status_as_of"]["status"] == "Closed - Acceptable Action"
    earlier = q.recommendations(
        h.NS, scopes=h.READ, native_id="A-26-015", as_of="2026-12-01"
    )
    assert (
        earlier["recommendations"][0]["status_as_of"]["status"]
        == "Open - Acceptable Response"
    )
    assert aircraft["status"] == "records on file" and not h.forbidden_keys(aircraft)
    product = q.dossier(
        h.NS,
        {"product_model_id": model_id},
        scopes=h.READ | h.PRODUCTS,
        products_namespace=h.NS,
    )
    assert product["matches"][0]["match_id"] == vehicle["match_id"]
    defects = {d["native_id"]: d for d in product["sections"]["defect_investigations"]}
    assert defects["EA26002"]["upgrades"] == [
        {"relation": "upgraded_from", "native_id": "PE26003", "on_record": True}
    ]
    assert defects["EA26002"]["recalls"] == [
        {
            "campaign_number": "26V104000",
            "status": "linked",
            "owner": "products.safety",
            "notice_id": notice_id,
            "notice_revision_id": defects["EA26002"]["recalls"][0][
                "notice_revision_id"
            ],
            "basis": "campaign number as cited",
        }
    ]
    assert (
        len(env.store.revisions(h.NS, env.record("nhtsa-odi", "PE26003"))) == 2
    )  # open, then closed/upgraded
    operator_dossier = q.dossier(
        h.NS, {"entity_id": "ent-examplar-pipeline"}, scopes=h.READ
    )
    assert (
        operator_dossier["status"] == "none on record"
    )  # the rejected match connects nothing
    unmatched = q.dossier(
        h.NS, {"kind": "aircraft_model", "model": "EX-300"}, scopes=h.READ
    )
    assert (
        unmatched["status"] == "none on record"
        and "not a statement that it is safe" in unmatched["semantics"]
    )
    bundle = export_bundle(env.conn, aircraft, created_at_ms=1)
    assert bundle["contract"] == "noesis-evidence-bundle-v1"

    # 7. Subscription events for the later publications, each citing the revision and the previous one.
    events = monitor.run(subscription, principal_id="analyst", scopes=MONITOR_SCOPES)
    assert ("report_final", "ERA26FA101") in kinds(events)
    assert ("defect_investigation_upgraded", "PE26003") in kinds(events)
    assert ("new_defect_investigation", "EA26002") in kinds(events)
    statuses = [
        n
        for n in events["notifications"]
        if n["kind"] == "recommendation_status_changed"
    ]
    assert {n["status"] for n in statuses} == {
        "Open - Acceptable Response",
        "Closed - Acceptable Action",
    }
    assert all(n["cites"]["previous_revision_id"] for n in statuses)

    # 8. A restart replay of the same publications adds no record, revision or event.
    before = env.conn.execute("SELECT count(*) FROM es_revisions").fetchone()[0]
    assert before > revisions
    replay = env.run(
        "run-4",
        source_ids=["ntsb-investigations", "nhtsa-odi-investigations"],
        overrides={
            "ntsb-investigations": h.ntsb_pages(
                case=variants["ntsb_case_final"],
                statuses=variants["ntsb_recommendation_statuses"][1],
            )
        },
    )
    assert replay["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM es_revisions").fetchone()[0] == before
    assert (
        monitor.run(subscription, principal_id="analyst", scopes=MONITOR_SCOPES)[
            "notifications"
        ]
        == []
    )
