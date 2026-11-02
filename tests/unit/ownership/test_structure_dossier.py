"""The holding-structure dossier: existing record owners composed, each section cited (#2890)."""

from __future__ import annotations

import copy
import sys
import types

import pytest

from src.evidence_bundle.verifier import verify_bundle
from src.kb import ownership_structure_dossier as dossiers
from src.kb.ownership_identity import OwnershipIdentityService
from src.kb.ownership_store import OwnershipError
from src.kb.ownership_structure_dossier import (
    CONTRACT,
    SECTIONS,
    SectionSpec,
    build_structure_dossier,
    export_structure_bundle,
    forbidden_keys,
    register_section,
)
from tests.unit.ownership import structure_harness as h

LINKED = ("lobbying", "political_donations", "public_payments")


@pytest.fixture(scope="module")
def world():
    return h.world()


@pytest.fixture()
def restore_sections():
    saved = dict(SECTIONS)
    yield
    SECTIONS.clear()
    SECTIONS.update(saved)


def build(conn, **kwargs):
    kwargs.setdefault("sections", h.SECTIONS)
    return build_structure_dossier(conn, h.OWN_NS, "gb-coh", "09990002", principal_id=h.ANALYST, scopes=h.SCOPES,
                                   as_of="2099-06-01", **kwargs)


def test_acceptance_every_section_is_cited_and_the_bundle_verifies(world, monkeypatch):
    h.enable(monkeypatch)
    dossier = build(world, evidence_kind="offline-fixture")
    assert dossier["contract"] == CONTRACT and dossier["status"] == "assembled"
    assert dossier["structure"] == {"status": "classified", "structure_types": ["holding_company"], "differ": False}
    assert dossier["coverage"] == {
        "classification": "assembled", "ownership": "assembled", "sec_holdings": "none_on_record",
        "bafin_holdings": "inert", "lobbying": "assembled", "political_donations": "assembled",
        "public_payments": "assembled", "competition": "inert", "procurement": "not_requested",
        "sanctions": "inert", "offshore_leaks": "not_requested"}
    sections = dossier["sections"]
    for name in ("classification", "ownership") + LINKED:
        section = sections[name]
        assert section["lines"] and section["citations"] == sorted({line["citation"]["id"] for line in section["lines"]})
        assert all(line["citation"]["record"] and line["citation"]["revision"] is not None for line in section["lines"])
    # Linked sections are reached only through accepted identity decisions, each named.
    accepted = {c["candidate_id"] for c in OwnershipIdentityService(world, initialize=False).candidates(
        h.NS, scopes=h.SCOPES, state="accepted")}
    for name in LINKED:
        assert all(line["reached_by"]["basis"] == "accepted-identity-decision"
                   and line["reached_by"]["candidate_id"] in accepted for line in sections[name]["lines"])
    ownership = sections["ownership"]["lines"]
    assert {(line["data"].get("direction"), line["data"]["relation"]) for line in ownership} >= {
        ("up", "direct parent or controller"), ("down", "subsidiary or controlled entity")}
    up = next(line for line in ownership if line["data"].get("direction") == "up")
    assert up["data"]["holder_entity"] == h.TRUST
    assert [line["data"]["amount_text"] for line in sections["public_payments"]["lines"]] == [
        "300,000.00", "120,000.00"]  # side by side, as published
    assert sections["political_donations"]["lines"][0]["data"]["amount_as_reported"] == "25000.00"
    assert sections["lobbying"]["lines"][0]["data"]["role"] == "registrant"
    for name in ("bafin_holdings", "competition", "sanctions"):
        assert sections[name]["status"] == "inert" and "not the same as empty" in sections[name]["reason"]
    assert forbidden_keys(dossier) == [] and "never summed" in dossier["notice"]

    bundle = export_structure_bundle(world, dossier, principal_id=h.ANALYST, scopes=h.SCOPES, created_at_ms=1)
    result = verify_bundle(bundle)
    assert result.errors == [] and result.status == "incomplete"  # inert sections stay explicit omissions
    omitted = {o["object_id"] for o in bundle["completeness"]["omissions"]}
    assert omitted == {f"structure-section:{n}" for n in ("bafin_holdings", "competition", "procurement",
                                                          "sanctions", "offshore_leaks")}
    evidence = {o["payload"]["citation"]["id"] for o in bundle["objects"] if o["type"] == "evidence"}
    assert evidence == {c for name in sections for c in sections[name]["citations"]}
    root = next(o for o in bundle["objects"] if o["id"] in bundle["roots"])
    assert root["payload"]["dossier_hash"] == dossier["dossier_hash"]

    live = build(world, include=("classification", "ownership", "sec_holdings") + LINKED)
    complete = verify_bundle(export_structure_bundle(world, live, principal_id=h.ANALYST, scopes=h.SCOPES,
                                                     created_at_ms=1))
    assert complete.valid and complete.errors == []


def test_sections_whose_feature_is_off_report_inert_not_empty(world):
    dossier = build(world)  # no optional feature selected
    for name in LINKED + ("bafin_holdings", "competition", "sanctions"):
        assert dossier["sections"][name]["status"] == "inert" and dossier["sections"][name]["lines"] == []
    assert dossier["sections"]["ownership"]["status"] == "assembled"


def test_only_accepted_identity_decisions_join_proposed_ones_are_listed_apart(monkeypatch):
    h.enable(monkeypatch)
    conn = h.connection()
    h.load_ownership(conn)
    h.load_donations(conn, accept=False)
    h.load_lobbying(conn, accept=False)
    h.load_payments(conn, accept=False)
    dossier = build(conn)
    for name in LINKED:
        assert dossier["sections"][name]["status"] == "none_on_record" and not dossier["sections"][name]["lines"]
    for name in ("lobbying", "public_payments"):
        [pending] = dossier["sections"][name]["pending"]
        assert pending["state"] == "proposed" and "never joined" in pending["note"]
    # Accept, then revert: the section follows the decision, records are untouched.
    service = OwnershipIdentityService(conn)
    candidate = dossier["sections"]["lobbying"]["pending"][0]["candidate_id"]
    service.review(h.NS, candidate, "accept", "registrant states the company number", principal_id=h.REVIEWER,
                   scopes=h.SCOPES)
    assert build(conn)["sections"]["lobbying"]["status"] == "assembled"
    service.revert(h.NS, candidate, "wrong register entry", principal_id=h.REVIEWER, scopes=h.SCOPES)
    assert build(conn)["sections"]["lobbying"]["status"] == "none_on_record"


def test_an_unclassified_structure_stays_unclassified(monkeypatch):
    h.enable(monkeypatch)
    conn = h.world(classify_structure=False)
    dossier = build(conn)
    assert dossier["structure"]["status"] == "unclassified" and dossier["structure"]["structure_types"] == []
    assert dossier["sections"]["classification"]["status"] == "none_on_record"
    office = build_structure_dossier(conn, h.OWN_NS, "gb-coh", "09990302", principal_id=h.ANALYST, scopes=h.SCOPES)
    assert office["structure"]["status"] == "unclassified"  # "Example Family Office Ltd" states no legal form


def test_offshore_leaks_extension_point(world, restore_sections, monkeypatch):
    dossier = build(world, include=("offshore_leaks",))
    section = dossier["sections"]["offshore_leaks"]
    # #2888 installed the provider, so without a namespace the section is not requested (not "not_available").
    assert section["status"] == "not_requested" and SECTIONS["offshore_leaks"].provider == \
        dossiers.OFFSHORE_LEAKS_PROVIDER

    # #2888 only has to provide the function at the declared path; it is picked up lazily.
    def structure_section(ctx):
        assert ctx.root == h.STRUCTURE and ctx.section_namespace == "offshore"
        return {"lines": [{"statement": "Offshore Leaks node 99000001 names an officer of the entity",
                           "reached_by": {"basis": "accepted-identity-decision", "candidate_id": "own-idc:fixture"},
                           "citation": {"id": "offshore_leaks:icij:node:99000001@release-2099",
                                        "namespace": "offshore", "record": "icij:node:99000001",
                                        "revision": "release-2099", "provider": "icij-offshore-leaks"}}],
                "notice": "a leaked record is what the leak states"}

    module = types.ModuleType("src.kb.offshore_leaks_links")
    module.structure_section = structure_section
    monkeypatch.setitem(sys.modules, "src.kb.offshore_leaks_links", module)
    wired = build(world, include=("offshore_leaks",), sections={"offshore_leaks": {"namespace": "offshore"}})
    assert wired["sections"]["offshore_leaks"]["status"] == "assembled"
    assert wired["sections"]["offshore_leaks"]["citations"] == ["offshore_leaks:icij:node:99000001@release-2099"]

    # A provider may also be registered explicitly; uncited lines and excluded keys are refused.
    register_section(SectionSpec("offshore_leaks", "Offshore Leaks (ICIJ)", "fixture", lambda ctx: {
        "lines": [{"statement": "uncited", "reached_by": {"basis": "fixture"}, "citation": {}}]},
        needs_namespace=False))
    with pytest.raises(OwnershipError) as refused:
        build(world, include=("offshore_leaks",))
    assert refused.value.code == "uncited_line"
    register_section(SectionSpec("offshore_leaks", "Offshore Leaks (ICIJ)", "fixture", lambda ctx: {
        "lines": [], "influence_score": 0.9}, needs_namespace=False))
    with pytest.raises(OwnershipError) as refused:
        build(world, include=("offshore_leaks",))
    assert refused.value.code == "forbidden_output"


def test_export_refuses_a_tampered_dossier(world, monkeypatch):
    h.enable(monkeypatch)
    dossier = build(world, include=("classification", "ownership", "public_payments"))
    forged = copy.deepcopy(dossier)
    forged["sections"]["public_payments"]["lines"][0]["data"]["amount_text"] = "9,999,999.00"
    with pytest.raises(OwnershipError) as refused:
        export_structure_bundle(world, forged, principal_id=h.ANALYST, scopes=h.SCOPES)
    assert refused.value.code == "integrity_failed"
    with pytest.raises(OwnershipError):
        export_structure_bundle(world, {**dossier, "status": "needs_selection"}, principal_id=h.ANALYST,
                                scopes=h.SCOPES)


def test_selection_persons_and_unknown_sections(world, monkeypatch):
    candidates = build_structure_dossier(world, h.OWN_NS, "name", "Example Holdings Ltd|GB", principal_id=h.ANALYST,
                                         scopes=h.SCOPES)
    assert candidates["status"] == "needs_selection" and "never picks by name" in candidates["note"]
    with pytest.raises(OwnershipError) as refused:
        build(world, sections={"influence": {"namespace": h.NS}})
    assert refused.value.code == "invalid_request"
    with pytest.raises(OwnershipError) as refused:
        build_structure_dossier(world, h.OWN_NS, "lei", "NOTACQUIRED000000000", principal_id=h.ANALYST,
                                scopes=h.SCOPES)
    assert refused.value.code == "not_found"

    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)
    monkeypatch.delenv("NEURONEWS_PERSON_RESEARCH", raising=False)
    conn = h.connection()
    OwnershipStore(conn).apply(h.OWN_NS, [record("person", "fixture:person:settlor", {
        "provider": "open-ownership", "provider_record_id": "settlor"}, name="A Fictional Settlor",
        identifiers=[{"scheme": "fixture-person", "value": "P1"}])], run_id="p", observed_at_ms=1,
        principal_id="ownership-loader")
    with pytest.raises(OwnershipError) as refused:
        build_structure_dossier(conn, h.OWN_NS, "fixture-person", "P1", principal_id=h.ANALYST, scopes=h.SCOPES)
    assert refused.value.code == "person_research_required"
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    assert build_structure_dossier(conn, h.OWN_NS, "fixture-person", "P1", principal_id=h.ANALYST,
                                   scopes=h.SCOPES)["person"] is True


def test_a_section_owner_refusal_is_reported_not_hidden(world, monkeypatch):
    h.enable(monkeypatch)
    scopes = h.SCOPES - {"knowledge:political:lobbying:read"}
    dossier = build_structure_dossier(world, h.OWN_NS, "gb-coh", "09990002", principal_id=h.ANALYST, scopes=scopes,
                                      sections=h.SECTIONS)
    assert dossier["sections"]["lobbying"]["status"] == "unauthorized" and not dossier["sections"]["lobbying"]["lines"]
    assert dossier["sections"]["public_payments"]["status"] == "assembled"
