"""Reviewable substance identity across CAS, EC, InChIKey and DTXSID (CH07, #2300)."""

from __future__ import annotations

import pytest

from src.kb.substances_identity import SubstanceIdentity, entity_id
from src.kb.substances_records import SubstanceError, statement
from tests.unit.chemicals import harness as h

NS = h.NS
SRC = {"url": "https://api-ccte.epa.gov/chemical/detail/search/by-dtxsid/x", "locator": "/",
       "attribution": "authored test record"}


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    item.identity = SubstanceIdentity(item.conn, now=lambda: next(item.clock))
    yield item
    item.conn.close()


def _pair(identity, a, b):
    return next(c for c in identity.candidates(NS, scopes=h.READ) if {c["left_key"], c["right_key"]} == {a, b})


def _seed(env, key, kind, name, **identifiers):
    subject = {"key": key, "kind": kind, "name": name}
    items = [statement("substance", "comptox", subject, "chemical", {"preferred_name": name}, source=SRC),
             statement("identifier", "comptox", subject, f"preferred-name:{name}",
                       {"scheme": "preferred-name", "value": name}, source=SRC)]
    items += [statement("identifier", "comptox", subject, f"{scheme}:{value}", {"scheme": scheme, "value": value},
                        source=SRC) for scheme, value in identifiers.items()]
    env.store.observe(NS, items)


def test_candidates_by_exact_identifier_and_inchikey_nothing_accepted(env):
    result = env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    candidates = result["candidates"]
    assert len(candidates) == 10 and {c["state"] for c in candidates} == {"proposed"}
    cas_to_ec = _pair(env.identity, "pubchem:cid:6623", "echa:substance:100.001.133")
    assert cas_to_ec["basis"] == "exact-identifier"
    shared = {(s["scheme"], s["value"]) for s in cas_to_ec["evidence"]["shared"]}
    assert {("cas", "80-05-7"), ("ec", "201-245-8")} <= shared
    assert any(s["conflicting_depositor_identifier"] for s in cas_to_ec["evidence"]["shared"])
    inchikey = _pair(env.identity, "pubchem:cid:6623", "comptox:dtxsid:DTXSID7020182")
    assert ("inchikey", "IISBACLAFKSPIT-UHFFFAOYSA-N") in {(s["scheme"], s["value"]) for s in inchikey["evidence"][
        "shared"]}
    # Nothing is grouped before review: a query reaches one record, with the open candidates shown.
    resolved = env.identity.resolve(NS, "80-05-7", scopes=h.READ)
    assert resolved["status"] == "ambiguous" and all(s["state"] == "unmatched" for s in resolved["substances"])
    again = env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    assert again["proposed"] == []  # re-proposing changes nothing


def test_accepted_matches_resolve_every_identifier_to_one_substance_with_the_match(env):
    env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    for a, b in (("pubchem:cid:6623", "echa:substance:100.001.133"),
                 ("comptox:dtxsid:DTXSID7020182", "echa:substance:100.001.133")):
        env.identity.review(NS, _pair(env.identity, a, b)["candidate_id"], "accept", "CAS and EC agree",
                            principal_id="reviewer", scopes=h.REVIEW)
    for query in ("80-05-7", "201-245-8", "IISBACLAFKSPIT-UHFFFAOYSA-N", "DTXSID7020182", "bisphenol A"):
        resolved = env.identity.resolve(NS, query, scopes=h.READ)
        assert resolved["status"] == "resolved", query
        (substance,) = resolved["substances"]
        assert {m["subject_key"] for m in substance["members"]} == {
            "pubchem:cid:6623", "echa:substance:100.001.133", "comptox:dtxsid:DTXSID7020182"}
        assert {m["basis"] for m in substance["matches"]} == {"exact-identifier"}
        assert all(m["decision_id"].startswith("entity-decision:") and m["reviewer"] == "reviewer"
                   for m in substance["matches"])
    decision = env.conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
                                [substance["matches"][0]["decision_id"]]).fetchone()
    assert decision == ("match",)
    row = env.conn.execute("SELECT entity_type FROM canonical_entities WHERE canonical_id=?",
                           [entity_id("pubchem:cid:6623")]).fetchone()
    assert row == ("chemical_substance",)


def test_reject_and_revert_are_recorded_and_unmatched_identifiers_reported(env):
    env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    candidate = _pair(env.identity, "pubchem:cid:702", "echa:substance:100.000.526")
    accepted = env.identity.review(NS, candidate["candidate_id"], "accept", "CAS agrees", principal_id="reviewer",
                                   scopes=h.REVIEW)
    assert env.identity.resolve(NS, "64-17-5", scopes=h.READ)["status"] == "ambiguous"  # CompTox still separate
    reverted = env.identity.revert(NS, candidate["candidate_id"], "reviewed in error", principal_id="reviewer",
                                   scopes=h.REVIEW)
    assert reverted["state"] == "reverted" and reverted["decision_id"] != accepted["decision_id"]
    assert env.identity.members(NS, "pubchem:cid:702") == ["pubchem:cid:702"]
    with pytest.raises(SubstanceError):
        env.identity.review(NS, candidate["candidate_id"], "accept", "again", principal_id="reviewer",
                            scopes=h.REVIEW)
    with pytest.raises(SubstanceError) as caught:
        env.identity.review(NS, candidate["candidate_id"], "accept", "x", principal_id="r", scopes=h.WRITE)
    assert caught.value.code == "unauthorized"
    report = env.identity.unmatched(NS, scopes=h.READ)
    keys = {i["subject_key"] for i in report["unmatched"]}
    assert "pubchem:cid:702" in keys and "echa:index:082-001-00-6" in keys and report["count"] == 12
    item = next(i for i in report["unmatched"] if i["subject_key"] == "pubchem:cid:702")
    assert "cas:64-17-5" in item["identifiers"]


def test_group_entry_is_never_proposed_and_only_joins_through_an_explicit_reviewed_match(env):
    _seed(env, "comptox:dtxsid:DTXSID9999998", "unknown", "authored lead compound")
    _seed(env, "comptox:dtxsid:DTXSID9999997", "mixture", "4,4'-isopropylidenediphenol")
    result = env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    group = "echa:index:082-001-00-6"
    assert not [c for c in result["candidates"] if group in (c["left_key"], c["right_key"])]
    assert len(result["withheld"]) == 2  # the mixture's name equals a name PubChem and ECHA publish for BPA
    assert all(w["composite"] == ["comptox:dtxsid:DTXSID9999997"] and w["basis"] == "synonym"
               for w in result["withheld"])
    manual = env.identity.propose_manual(NS, group, "comptox:dtxsid:DTXSID9999998",
                                         "authored: the reviewer checked the member list", principal_id="reviewer",
                                         scopes=h.REVIEW)
    assert manual["basis"] == "reviewed-manual" and manual["state"] == "proposed"
    assert env.identity.members(NS, group) == [group]  # proposed is not joined
    env.identity.review(NS, manual["candidate_id"], "accept", "member confirmed", principal_id="reviewer",
                        scopes=h.REVIEW)
    assert env.identity.members(NS, group) == ["comptox:dtxsid:DTXSID9999998", group]


def test_a_rejected_synonym_match_never_connects_the_records(env):
    _seed(env, "comptox:dtxsid:DTXSID9999996", "unknown", "BPA")
    env.identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    candidate = _pair(env.identity, "pubchem:cid:6623", "comptox:dtxsid:DTXSID9999996")
    assert candidate["basis"] == "synonym"
    assert candidate["evidence"]["shared"][0]["left_published"] in {"BPA"}
    rejected = env.identity.review(NS, candidate["candidate_id"], "reject", "abbreviation shared by other substances",
                                   principal_id="reviewer", scopes=h.REVIEW)
    assert rejected["state"] == "rejected"
    decision = env.conn.execute("SELECT decision_type FROM entity_identity_decisions WHERE decision_id=?",
                                [rejected["decision_id"]]).fetchone()
    assert decision == ("non-match",)
    resolved = env.identity.resolve(NS, "BPA", scopes=h.READ)
    assert resolved["status"] == "ambiguous"
    assert all(len(s["members"]) == 1 for s in resolved["substances"])
