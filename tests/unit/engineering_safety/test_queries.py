"""Directives as of a date, recommendation status over time and subject dossiers (ES12 #2073, ES13 #2074)."""

from __future__ import annotations

import pytest

from src.kb.engineering_safety_identity import SubjectIdentity
from src.kb.engineering_safety_queries import EngineeringSafetyQueries, export_bundle
from src.kb.engineering_safety_records import EngineeringSafetyError
from tests.unit.engineering_safety import harness as h


@pytest.fixture(scope="module")
def env():
    env = h.Env()
    assert env.run("r1")["status"] == "complete"
    return env


def queries(env):
    return EngineeringSafetyQueries(env.conn)


def ids(items):
    return [(i["provider"], i["native_id"]) for i in items]


def test_directives_in_effect_with_the_supersession_chain_and_revision_used(env):
    answer = queries(env).directives_as_of(h.NS, h.EX100, scopes=h.READ, as_of="2026-06-01")
    assert answer["semantics"] == "as published; not a compliance determination"
    assert ids(answer["in_effect"]) == [("easa-ad", "2026-0123"), ("faa-ad", "2026-04-12")]
    faa = answer["in_effect"][1]
    assert faa["revision"]["revision_label"] == "correction"  # the corrected text is the one in effect
    assert faa["effective_date"] == "2026-04-08"
    assert [(c["native_id"], c["relation"]) for c in faa["chain"]["supersedes"]] == [("2025-12-05", "supersedes")]
    assert faa["applicability"][0]["locator"]["paragraph"] == "(c)"
    assert "Within 50 hours" in faa["required_actions"][0]["text"]
    assert ("faa-ad", "2025-12-05") not in ids(answer["in_effect"] + answer["possibly_applicable"])
    (possible,) = answer["possibly_applicable"]
    assert possible["native_id"] == "2026-08-02" and possible["applicability_state"] == "possibly applicable — see text"
    assert "(1) Model EX-100" in possible["clauses"][0]["text"]
    easa = answer["in_effect"][0]
    assert easa["revision"]["revision_label"] == "R1"


def test_superseded_and_future_directives_by_date(env):
    q = queries(env)
    before = q.directives_as_of(h.NS, h.EX100, scopes=h.READ, as_of="2025-12-01")
    assert ids(before["in_effect"]) == [("faa-ad", "2025-12-05")]
    # The successor was published after the date, so the chain as of the date does not know it yet.
    assert before["in_effect"][0]["chain"]["superseded_by"] == []
    after = q.directives_as_of(h.NS, h.EX100, scopes=h.READ, as_of="2026-03-25")
    old = next(c for c in after["in_effect"] if c["native_id"] == "2025-12-05")
    assert [(c["native_id"], c["in_effect_by_as_of"]) for c in old["chain"]["superseded_by"]] == [
        ("2026-04-12", False)]
    # Published but not yet effective on the date: excluded from in effect, listed separately.
    between = q.directives_as_of(h.NS, h.EX100, scopes=h.READ, as_of="2026-03-25")
    assert ("faa-ad", "2025-12-05") in ids(between["in_effect"])  # its successor is not effective yet
    assert ("faa-ad", "2026-04-12") in ids(between["not_yet_effective"])
    easa_first = next(i for i in between["in_effect"] if i["provider"] == "easa-ad")
    assert "revision_label" not in easa_first["revision"]  # R1 was issued later
    # Serial numbers outside the published range are shown as such, never silently dropped.
    serial = q.directives_as_of(h.NS, h.EX100, scopes=h.READ, as_of="2026-06-01", serial="1500")
    assert ("faa-ad", "2026-04-12") in ids(serial["outside_serial_range"])
    assert ("easa-ad", "2026-0123") in ids(serial["in_effect"])  # all serial numbers


def test_an_acquisition_cutoff_replays_only_what_was_acquired(env):
    first = env.conn.execute("SELECT min(observed_at_ms) FROM es_revisions").fetchone()[0]
    answer = queries(env).directives_as_of(h.NS, h.EX100, scopes=h.READ, as_of="2026-06-01",
                                           acquired_by_ms=first - 1)
    assert answer["status"] == "none on record"
    faa = env.store.revisions(h.NS, env.record("faa-ad", "2026-04-12"))
    partial = queries(env).directives_as_of(h.NS, h.EX100, scopes=h.READ, as_of="2026-06-01",
                                            acquired_by_ms=faa[0]["observed_at_ms"])
    in_effect = {i["native_id"]: i for i in partial["in_effect"]}
    assert "revision_label" not in in_effect["2026-04-12"]["revision"]  # the correction was acquired later


def test_recommendations_by_addressee_and_status_with_dated_history(env):
    q = queries(env)
    answer = q.recommendations(h.NS, scopes=h.READ, addressee="examplar aircraft company", as_of="2026-09-01")
    (rec,) = answer["recommendations"]
    assert rec["native_id"] == "A-26-015" and rec["status_as_of"]["status"] == "Open - Await Response"
    assert rec["investigations"] == ["ERA26FA101"]
    early = q.recommendations(h.NS, scopes=h.READ, native_id="A-26-015", as_of="2026-01-01")
    assert early["n"] == 0  # not yet issued
    csb = q.recommendations(h.NS, scopes=h.READ, authority="us-csb", status="acceptable")
    assert [r["native_id"] for r in csb["recommendations"]] == ["2026-02-I-TX-R2"]


def test_dossiers_are_cited_and_a_subject_without_records_is_none_on_record(env):
    q = queries(env)
    dossier = q.dossier(h.NS, h.EX100, scopes=h.READ, as_of="2026-09-01")
    assert dossier["status"] == "records on file" and dossier["semantics"].startswith("as published")
    sections = dossier["sections"]
    assert ids(sections["investigations"]) == [("bea", "BEA2026-0042"), ("ntsb", "ERA26FA101")]
    assert sections["investigations"][0]["access"] == "link-only"
    assert sections["investigations"][1]["report_status"] == "preliminary"
    assert all(dossier["pins"].values())
    assert not h.forbidden_keys(dossier)
    empty = q.dossier(h.NS, {"kind": "aircraft_model", "model": "EX-999"}, scopes=h.READ)
    assert empty["status"] == "none on record" and "not a statement that it is safe" in empty["semantics"]
    vehicle = q.dossier(h.NS, {"kind": "vehicle", "make": "VELOMARK", "model": "CITYRUNNER"}, scopes=h.READ)
    defects = {d["native_id"]: d for d in vehicle["sections"]["defect_investigations"]}
    assert defects["EA26002"]["upgrades"] == [{"relation": "upgraded_from", "native_id": "PE26003",
                                                "on_record": True}]
    assert defects["EA26002"]["recalls"][0]["status"] == "unresolved identifier"
    assert vehicle["sections"]["complaints"]["n"] == 2
    operator = q.dossier(h.NS, {"kind": "pipeline_operator", "operator_id": "39999"}, scopes=h.READ)
    (incident,) = operator["sections"]["occurrences"]
    assert incident["quantities"][0]["value"] == "125.5" and incident["quantities"][0]["unit"] == "bbl"
    assert incident["quantities"][0]["normalized"]["status"] in {"converted", "not_converted"}
    bundle = export_bundle(env.conn, dossier, created_at_ms=1)
    assert bundle["contract"] == "noesis-evidence-bundle-v1"


def test_recalls_link_when_the_products_namespace_is_named_with_its_scope(env):
    h.seed_recall(env.conn)
    q = queries(env)
    subject = {"kind": "vehicle", "make": "VELOMARK", "model": "CITYRUNNER"}
    with pytest.raises(EngineeringSafetyError) as denied:
        q.dossier(h.NS, subject, scopes=h.READ, products_namespace=h.NS)
    assert denied.value.code == "unauthorized"
    dossier = q.dossier(h.NS, subject, scopes=h.READ | {"knowledge:products:read"}, products_namespace=h.NS)
    ea = next(d for d in dossier["sections"]["defect_investigations"] if d["native_id"] == "EA26002")
    assert ea["recalls"][0]["status"] == "linked" and ea["recalls"][0]["owner"] == "products.safety"


def test_a_products_id_reaches_records_only_through_an_accepted_match(env):
    model = h.seed_products(env.conn)
    identity = SubjectIdentity(env.conn)
    q = queries(env)
    before = q.dossier(h.NS, {"product_model_id": model}, scopes=h.READ)
    assert before["status"] == "none on record" and before["unknowns"][-1]["kind"] == "identity"
    proposed = identity.propose(h.NS, scopes=h.REVIEW, principal_id="m")
    match = next(c for c in proposed["candidates"]
                 if c["target_id"] == model and c["subject_key"] == "vehicle:velomark:cityrunner:2025")
    identity.review(h.NS, match["match_id"], "accepted", "make and model agree", scopes=h.REVIEW, principal_id="r")
    after = q.dossier(h.NS, {"product_model_id": model}, scopes=h.READ)
    assert after["status"] == "records on file" and after["matches"][0]["match_id"] == match["match_id"]
    assert {d["native_id"] for d in after["sections"]["defect_investigations"]} == {"EA26002", "PE26003"}
    identity.revert(h.NS, match["match_id"], "wrong market", scopes=h.REVIEW, principal_id="r")
    assert q.dossier(h.NS, {"product_model_id": model}, scopes=h.READ)["status"] == "none on record"


def test_every_query_is_not_ready_before_a_source_ran_and_dates_are_checked():
    import duckdb

    q = EngineeringSafetyQueries(duckdb.connect(":memory:"))
    for call in (lambda: q.directives_as_of(h.NS, h.EX100, scopes=h.READ, as_of="2026-01-01"),
                 lambda: q.dossier(h.NS, h.EX100, scopes=h.READ),
                 lambda: q.recommendations(h.NS, scopes=h.READ),
                 lambda: q.search(h.NS, scopes=h.READ, kinds=["directive"])):
        with pytest.raises(EngineeringSafetyError) as caught:
            call()
        assert caught.value.code == "not_ready"
