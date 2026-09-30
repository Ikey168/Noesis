"""Substances linked to regulations, product notices, materials and literature by citation (CH08, #2303)."""

from __future__ import annotations

import pytest

from src.kb.substances_identity import SubstanceIdentity, entity_id
from src.kb.substances_links import SubstanceLinks, cited_identifiers, name_mentioned
from src.kb.substances_records import SubstanceError, statement
from tests.unit.chemicals import harness as h

NS = h.NS
BPA = ["pubchem:cid:6623", "echa:substance:100.001.133", "comptox:dtxsid:DTXSID7020182"]
DEHP = ["pubchem:cid:8343", "echa:substance:100.003.829", "comptox:dtxsid:DTXSID5020607"]
ENTRY_66 = ("Annex XVII/entry 66", "66. Bisphenol A. Shall not be placed on the market in thermal paper in a "
                                   "concentration equal to or greater than 0,02 % by weight after 2 January 2020.")


@pytest.fixture()
def env(tmp_path):
    item = h.loaded_env(tmp_path)
    item.links = SubstanceLinks(item.conn, now=lambda: next(item.clock))
    yield item
    item.conn.close()


def test_identifier_extraction_verifies_check_digits():
    assert cited_identifiers("contains bisphenol A (CAS 80-05-7) and EC 201-245-8") == [("cas", "80-05-7"),
                                                                                      ("ec", "201-245-8")]
    assert cited_identifiers("lot 80-05-8 and 2020-01-02") == []
    assert name_mentioned("contains Bisphenol A.", "bisphenol A")
    assert not name_mentioned("bisphenol-like plasticisers", "bisphenol")


def test_restrictions_and_classifications_link_to_the_cited_act_with_its_text(env):
    work = env.seed_legal_act("32016R2235", "Commission Regulation (EU) 2016/2235", [ENTRY_66])
    result = env.links.link_legal(NS, scopes=h.ALL, principal_id="linker")
    (link,) = [x for x in result["linked"] if x["subject_key"] == "echa:substance:100.001.133"]
    assert link["target_id"] == work and link["basis"] == "cited-act" and link["matched"] == "celex:32016R2235"
    assert {u["celex"] for u in result["unresolved"]} >= {"32008R1272", "32016R1179", "32011R0143"}
    (stored,) = env.links.links(NS, BPA, scopes=h.READ, owner="legal")
    assert stored["locator"]["entry"] == "Annex XVII entry 66" and stored["locator"]["record_type"] == "restriction"
    assert stored["citing_text"].startswith("Commission Regulation (EU) 2016/2235")
    text = env.links.regulation_text(NS, stored, scopes=h.ALL)
    assert text["status"] == "found" and text["passages"][0]["text"] == ENTRY_66[1]
    again = env.links.link_legal(NS, scopes=h.ALL, principal_id="linker")
    assert again["linked"] == []  # idempotent
    with pytest.raises(SubstanceError):
        env.links.link_legal(NS, scopes=h.WRITE, principal_id="linker")  # needs knowledge:legal:read


def test_product_notices_link_by_stated_identifier_or_own_name_and_store_the_citing_text(env):
    rollex, dollyco, gel = env.seed_notices()
    result = env.links.link_product_notices(NS, scopes=h.ALL, principal_id="linker")
    assert result["notices_read"] == 3
    bpa = env.links.links(NS, BPA, scopes=h.READ, owner="products")
    assert {x["target_id"] for x in bpa} == {rollex}
    by_basis = {(x["subject_key"], x["basis"], x["matched"]) for x in bpa}
    assert ("echa:substance:100.001.133", "cited-identifier", "cas:80-05-7") in by_basis
    assert any(x["basis"] == "explicit-name-mention" for x in bpa)  # "bisphenol A" is PubChem's own title
    assert all("80-05-7" in x["citing_text"] or "bisphenol A" in x["citing_text"] for x in bpa)
    assert all(x["locator"]["part"] == "hazard" for x in bpa)
    dehp = env.links.links(NS, DEHP, scopes=h.READ, owner="products")
    assert {x["target_id"] for x in dehp} == {dollyco}
    assert {x["basis"] for x in dehp} == {"explicit-name-mention"}  # the notice names DEHP, never its CAS
    everything = env.links.links(NS, [s["subject_key"] for s in env.store.subjects(NS)], scopes=h.READ)
    assert gel not in {x["target_id"] for x in everything}  # "bisphenol-like" and methanol name nothing acquired


def test_materials_and_literature_link_by_identifier_or_reviewed_identity_only(env):
    records = [
        {"record_id": "material:1", "revision_id": "r1", "title": "Polycarbonate grade PC-1",
         "text": "Monomer: bisphenol A."},  # a name alone is never a link
        {"record_id": "material:2", "revision_id": "r1", "title": "Thermal paper coating",
         "identifiers": {"cas": ["80-05-7"]}},
        {"record_id": "material:3", "revision_id": "r1", "title": "Reviewed substance reference",
         "text": f"substance {entity_id('pubchem:cid:8343')}"},
    ]
    result = env.links.link_citing_records(NS, "materials", records, scopes=h.WRITE, principal_id="linker")
    targets = {(x["target_id"], x["basis"]) for x in result["linked"]}
    assert ("material:1", "cited-identifier") not in targets and not any(t == "material:1" for t, _ in targets)
    assert ("material:2", "cited-identifier") in targets
    assert ("material:3", "cited-reviewed-identity") in targets
    unavailable = env.links.link_materials(NS, None, scopes=h.WRITE, principal_id="linker")
    assert unavailable["status"] == "provider_unavailable"
    from src.ingestion.document_store import DocumentStore

    DocumentStore(env.conn).upsert([{
        "document_id": "paper:tox-1", "source_type": "paper", "language": "en", "ingested_at": 1, "created_at": 1,
        "source_id": "papers", "url": "https://example.org/paper/1", "title": "Authored toxicology abstract",
        "content": "We report outcomes for DEHP (CAS 117-81-7) in an authored fixture.", "authors": [], "metadata": {}}])
    literature = env.links.link_literature(NS, scopes=h.ALL, principal_id="linker")
    assert {x["subject_key"] for x in literature["linked"]} == set(DEHP)


def test_a_rejected_identity_never_carries_a_link_to_the_other_record(env):
    subject = {"key": "comptox:dtxsid:DTXSID9999994", "kind": "unknown", "name": "4,4'-isopropylidenediphenol"}
    src = {"url": "https://api-ccte.epa.gov/x", "locator": "/", "attribution": "authored test record"}
    env.store.observe(NS, [
        statement("substance", "comptox", subject, "chemical", {"preferred_name": subject["name"]}, source=src),
        statement("identifier", "comptox", subject, "preferred-name", {"scheme": "preferred-name",
                                                                       "value": subject["name"]}, source=src),
        statement("identifier", "comptox", subject, "cas:50-00-0", {"scheme": "cas", "value": "50-00-0"},
                  source=src)])
    identity = SubstanceIdentity(env.conn, now=lambda: next(env.clock))
    identity.propose(NS, principal_id="matcher", scopes=h.WRITE)
    candidate = next(c for c in identity.candidates(NS, scopes=h.READ)
                     if {c["left_key"], c["right_key"]} == {"pubchem:cid:6623", subject["key"]})
    identity.review(NS, candidate["candidate_id"], "reject", "different substance", principal_id="reviewer",
                    scopes=h.REVIEW)
    env.links.link_citing_records(NS, "clinical", [{"record_id": "paper:2", "text": "exposure to CAS 50-00-0"}],
                                  scopes=h.WRITE, principal_id="linker")
    members = identity.members(NS, "pubchem:cid:6623")
    assert subject["key"] not in members
    assert not env.links.links(NS, members, scopes=h.READ, owner="clinical")
    assert [x["target_id"] for x in env.links.links(NS, [subject["key"]], scopes=h.READ)] == ["paper:2"]
