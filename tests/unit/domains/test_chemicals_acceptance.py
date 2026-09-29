"""Offline acceptance: a substance name or identifier to a cited regulatory dossier (#2212, CH12 #2316).

The journey runs the real source-pack runtime over the pinned PubChem, ECHA
CHEM (CLP, REACH lists) and CompTox fixtures, the Legal owner (an authored
CELLAR act with the Annex XVII entry text) and the Products safety owner
(authored Safety Gate alerts), with sockets blocked. It reproduces: identity
match across CAS, EC, InChIKey and DTXSID, status as of a date before and
after an ATP, revision history, regulation citation with its text, linked
product notices, a monitored later acquisition, and a substance with no
entries reported as having none on record. Live coverage (#2317) is reported
separately and stays ``unverified-live``.
"""

from __future__ import annotations

import json
import socket

import jsonschema
import pytest

from src.ingestion.substance_sources import LIVE_VERIFICATION
from src.kb.substances_bundle import readiness
from src.kb.substances_identity import SubstanceIdentity
from src.kb.substances_links import SubstanceLinks
from src.kb.substances_monitoring import SubstanceMonitor
from src.kb.substances_queries import SubstanceQueries
from tests.unit.chemicals import harness as h

NS = h.NS
DOSSIER_SCHEMA = json.loads((h.ROOT / "contracts/schemas/jsonschema/noesis-substance-dossier-v1.json").read_text())
ENTRY_66 = ("Annex XVII/entry 66", "66. Bisphenol A (CAS No 80-05-7, EC No 201-245-8). Shall not be placed on the "
                                   "market in thermal paper in a concentration equal to or greater than 0,02 % by "
                                   "weight after 2 January 2020.")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_substance_name_or_identifier_to_a_cited_regulatory_dossier():
    env = h.Env()
    # 1. Acquire every source of the bounded selection through the runtime, with receipts.
    run = env.run("acceptance-1")
    assert run["status"] == "complete"
    assert {s["source_id"]: s["status"] for s in run["sources"]} == {s: "complete" for s in h.SOURCES}
    assert {r["evidence_origin"] for r in env.store.runs(NS)} == {"fixture"}
    env.seed_legal_act("32016R2235", "Commission Regulation (EU) 2016/2235", [ENTRY_66])
    notices = env.seed_notices()

    # 2. Identity: candidates by exact identifier / InChIKey, reviewed; the group entry is never proposed.
    identity = SubstanceIdentity(env.conn, now=lambda: next(env.clock))
    proposed = identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    assert not [c for c in proposed["candidates"] if "echa:index:082-001-00-6" in (c["left_key"], c["right_key"])]
    for candidate in proposed["candidates"]:
        if {candidate["left_key"], candidate["right_key"]} & {"pubchem:cid:6623", "comptox:dtxsid:DTXSID7020182"}:
            identity.review(NS, candidate["candidate_id"], "accept", "CAS, EC and InChIKey agree",
                            principal_id="reviewer", scopes=h.REVIEW)
    for query in ("bisphenol A", "80-05-7", "201-245-8", "IISBACLAFKSPIT-UHFFFAOYSA-N", "DTXSID7020182"):
        resolved = identity.resolve(NS, query, scopes=h.READ)
        assert resolved["status"] == "resolved", query
        assert len(resolved["substances"][0]["members"]) == 3
        assert {m["basis"] for m in resolved["substances"][0]["matches"]} <= {"exact-identifier", "inchikey"}

    # 3. Citations: the cited act by exact CELEX, product notices by stated CAS or own name.
    links = SubstanceLinks(env.conn, now=lambda: next(env.clock))
    assert links.link_legal(NS, scopes=h.ALL, principal_id="linker")["linked"]
    assert links.link_product_notices(NS, scopes=h.ALL, principal_id="linker")["linked"]

    # 4. Status as of a date, before and after the ATP applies, with the act that set it.
    queries = SubstanceQueries(env.conn)
    before = queries.status_as_of(NS, scopes=h.READ, query="80-05-7", as_of="2017-06-01")
    after = queries.status_as_of(NS, scopes=h.READ, query="80-05-7", as_of="2026-09-01")
    (old,), (new,) = before["harmonised_classification"], after["harmonised_classification"]
    assert old["as_published"]["hazard_classes"][0]["hazard_class_category"] == "Repr. 2"
    assert (new["as_published"]["hazard_classes"][0]["hazard_class_category"], new["citation"]["legal_act"]["atp"]) \
        == ("Repr. 1B", "ATP 9")
    assert before["restriction"] == [] and after["restriction"][0]["state"] == "in force"
    assert [e["since"] for e in after["candidate_list"]] == ["2017-07-07"]

    # 5. The dossier: identity, history, regulation text by citation, linked notices, data points.
    dossier = queries.dossier(NS, scopes=h.ALL, query="bisphenol A", as_of="2026-09-01")
    jsonschema.validate(dossier, DOSSIER_SCHEMA)
    harmonised = [e for e in dossier["history"] if e["record_type"] == "classification"
                  and e["as_published"]["kind"] == "harmonised"]
    assert [(e["effective_from"], e["citation"]["legal_act"]["celex"]) for e in harmonised] == [
        ("2009-01-20", "32008R1272"), ("2018-03-01", "32016R1179")]
    (regulation,) = dossier["regulations"]
    assert regulation["cited_as"] == "celex:32016R2235" and regulation["entry"] == "Annex XVII entry 66"
    assert regulation["text"]["passages"][0]["text"] == ENTRY_66[1]
    (notice,) = dossier["linked_notices"]
    assert notice["notice_id"] == notices[0] and notice["notice"]["notice_number"] == "SR/07001/26"
    assert any(c["basis"] == "cited-identifier" and "80-05-7" in c["citing_text"] for c in notice["citations"])
    assert len(dossier["data_points"]) == 2
    assert all(p["label"].startswith("the source's published data point") for p in dossier["data_points"])
    assert not h.forbidden_keys(dossier)

    # 6. No entries: none on record, never safe or unregulated.
    sucrose = queries.dossier(NS, scopes=h.ALL, subject_key="echa:substance:100.000.304", as_of="2026-09-01")
    assert sucrose["status_as_of"]["harmonised_classification"] == []
    assert set(sucrose["status_as_of"]["none_on_record"]) >= {"harmonised_classification", "candidate_list",
                                                              "restriction", "authorisation"}
    assert all("not a statement that the substance is safe" in text
               for text in sucrose["status_as_of"]["none_on_record"].values())
    unknown = queries.dossier(NS, scopes=h.ALL, query="7732-18-5", as_of="2026-09-01")
    assert unknown["status"] == "not_found" and "not a statement" in unknown["statement"]

    # 7. A later acquisition (new ATP) reaches a monitor as a cited event with prior and new status.
    monitor = SubstanceMonitor(env.conn, now=lambda: next(env.clock))
    subscription = monitor.create(NS, "acceptance", substances=["80-05-7"], principal_id="analyst",
                                  scopes=h.ALL)["subscription_id"]
    assert monitor.run(subscription, principal_id="analyst", scopes=h.ALL)["baseline"]
    assert env.run("acceptance-2", source_ids=["echa-clp-classifications"],
                   overrides=h.LATER["second"])["status"] == "complete"
    (event,) = monitor.run(subscription, principal_id="analyst", scopes=h.ALL)["notifications"]
    assert event["kind"] == "classification_revision"
    assert (event["prior"]["citation"]["legal_act"]["celex"], event["new"]["citation"]["legal_act"]["celex"]) == (
        "32016R1179", "32026R0000")
    # The new ATP applies from 2028, so today's status is unchanged and the revision is scheduled.
    today = queries.status_as_of(NS, scopes=h.READ, query="80-05-7", as_of="2026-09-01")
    assert today["harmonised_classification"][0]["citation"]["legal_act"]["celex"] == "32016R1179"
    assert any(s["applies_from"] == "2028-03-01" for s in today["scheduled"])

    # 8. Replaying the whole acquisition adds nothing.
    generation = env.store.generation(NS)
    assert env.run("acceptance-3")["status"] == "complete"
    assert env.store.generation(NS) == generation
    env.conn.close()


def test_live_evidence_is_reported_separately_and_unverified():
    env = h.loaded_env(__import__("tempfile").mkdtemp())
    status = readiness(env.conn, NS, scopes=h.READ)
    assert {p["status"] for p in status["providers"].values()} == {"fixture-only"}
    assert {v["status"] for v in LIVE_VERIFICATION.values()} == {"unverified-live"}
    assert all(p["live_verification"]["status"] == "unverified-live" for p in status["providers"].values())
    env.conn.close()
