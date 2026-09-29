"""Medicines and active substances resolved through RxNorm and reviewable clinical term crosswalks (#2214, MR07)."""

from __future__ import annotations

import pytest

from src.kb.clinical_records import ClinicalRecordError
from tests.unit import medicines_fixture_builder as fb
from tests.unit.clinical.harness import NS
from tests.unit.clinical.medicines_harness import READ_ONLY, REVIEWER, SCOPES, Env


@pytest.fixture(scope="module")
def env():
    env = Env()
    env.acquire("r1")
    env.acquire_medicines("m1")
    env.proposed = env.propose()
    return env


def test_rxcui_is_stored_with_the_release_and_lookups_are_receipted(env):
    proposed = env.proposed
    assert proposed["rxnorm_release"] == fb.RXNAV_RELEASE
    assert all(m["rxnorm_release"] == fb.RXNAV_RELEASE for m in proposed["matches"])
    assert proposed["receipts"][0]["path"] == "/REST/version.json"
    assert all(len(r["sha256"]) == 64 for r in proposed["receipts"])
    assert env.identity().records.provider_state(NS, "rxnorm")["last_execution"] == "injected"


def test_ingredient_and_branded_product_match_equivalent_by_exact_rxnorm_name(env):
    ingredient = [m for m in env.proposed["matches"] if m["subject_key"] == "openfda:NDA299001"]
    by_tty = {m["target"]["tty"]: m for m in ingredient}
    assert by_tty["IN"]["target"]["rxcui"] == "9990001" and by_tty["IN"]["kind"] == "equivalent"
    brand = by_tty["BN"]
    assert brand["target"]["rxcui"] == "9990002" and brand["target"]["ingredients"] == [
        {"rxcui": "9990001", "name": "noetiglutide"}]
    assert brand["state"] == "accepted" and brand["basis"] == "rxnav-exact-name"
    assert brand["evidence"]["published_name"] == {"name": "NOETIGLU (FIXTURE)", "role": "brand name"}
    assert brand["decisions"][0]["principal_id"] == "rule:rxnav-exact-name"


def test_eu_only_product_matches_by_active_substance_only_after_review_and_unmatched_are_reported(env):
    eu = env.match("ema:EMEA/H/C/009001")
    assert eu["kind"] == "narrower" and eu["state"] == "proposed" and eu["basis"] == "reviewed-active-substance"
    service = env.service()
    before = service.status_as_of(NS, "noetiglutide", "2026-09-01", scopes=SCOPES)
    assert {a["jurisdiction"] for a in before["jurisdictions"]} == {"US"}
    assert [p["subject_key"] for p in before["identity"]["pending_review"]] == ["ema:EMEA/H/C/009001"]
    env.accept_eu()
    after = service.status_as_of(NS, "noetiglutide", "2026-09-01", scopes=SCOPES)
    eu_answer = next(a for a in after["jurisdictions"] if a["jurisdiction"] == "EU")
    assert eu_answer["identity_match"]["kind"] == "narrower" and eu_answer["identity_match"]["decided_by"] == "bob"
    (unmatched,) = env.proposed["unmatched"]
    assert unmatched["subject_key"] == "ema:EMEA/H/C/009002" and "left unmatched" in unmatched["reason"]
    assert service.status_as_of(NS, "fixturamab", "2025-01-01", scopes=SCOPES)["on_record"] is False
    by_number = service.status_as_of(NS, "EMEA/H/C/009002", "2025-01-01", scopes=SCOPES)
    (withdrawn,) = by_number["jurisdictions"]
    assert withdrawn["status"] == "withdrawn" and withdrawn["identity_match"]["basis"] == "exact-regulator-identifier"


def test_a_rejected_match_connects_nothing_and_revert_restores_it():
    env = Env()
    env.acquire_medicines("m1", ["medicines-fda-dsc", "medicines-drugsfda-submissions"])
    env.propose()
    identity = env.identity()
    dsc = next(m for m in identity.matches(NS, scopes=SCOPES) if m["subject_key"].startswith("fda-dsc:"))
    service = env.service()
    assert service.communications(NS, "noetiglutide", scopes=SCOPES)["on_record"] is True
    with pytest.raises(ClinicalRecordError):
        identity.review(NS, dsc["match_id"], "reject", "not the same", principal_id="eve", scopes=READ_ONLY)
    rejected = identity.review(NS, dsc["match_id"], "reject", "the communication names another salt (test)",
                               principal_id="bob", scopes=REVIEWER)
    assert rejected["state"] == "rejected"
    answer = service.communications(NS, "noetiglutide", scopes=SCOPES)
    assert answer["on_record"] is False
    assert [r["subject_key"] for r in answer["identity"]["rejected"]] == [dsc["subject_key"]]
    reverted = identity.revert(NS, dsc["match_id"], "rejected in error", principal_id="bob", scopes=REVIEWER)
    assert reverted["state"] == "accepted"
    assert service.communications(NS, "noetiglutide", scopes=SCOPES)["on_record"] is True
    actions = [d["action"] for d in env.match(dsc["subject_key"])["decisions"]]
    assert actions == ["propose", "reject", "revert"]
    with pytest.raises(ClinicalRecordError):
        identity.revert(NS, dsc["match_id"], "again", principal_id="bob", scopes=REVIEWER)


def test_resolution_reports_the_term_crosswalk_when_mesh_is_aligned(env):
    env.seed_publications() if not env.documents else None
    env.align_terms()
    resolved = env.identity().resolve(NS, "noetiglutide", scopes=SCOPES)
    assert {c["rxcui"] for c in resolved["concepts"]} >= {"9990001"}
    assert resolved["term_crosswalk"] is not None
    assert "accepted matches" in resolved["note"]
