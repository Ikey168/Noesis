"""Life-science records linked to Chemicals, Clinical, Biodiversity and literature (#2652, LS08 #2691)."""

from __future__ import annotations

from src.kb.lifesci_identity import LifeSciIdentity
from src.kb.lifesci_links import LifeSciLinks, owner_status
from src.kb.lifesci_records import record_id_for
from tests.unit import lifesci_harness as h

COMPOUND = record_id_for(h.NS, "chembl", "compound", "CHEMBL9900101")
TARGET = record_id_for(h.NS, "chembl", "target", "CHEMBL9900001")
TAXON = record_id_for(h.NS, "ncbi-taxonomy", "taxon", "99000001")


def accept_identifier_matches(conn):
    identity = LifeSciIdentity(conn, now=lambda: h.SECOND)
    for match in identity.propose(h.NS, scopes=h.SCOPES, principal_id="m")["matches"]:
        if match["method"] != "scientific-name":
            identity.review(h.NS, match["match_id"], "accepted", "published identifier", scopes=h.SCOPES,
                            principal_id="rev")


def test_links_record_their_basis_point_at_revisions_and_keep_missing_targets():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    h.seed_all(conn)
    links = LifeSciLinks(conn, now=lambda: h.SECOND)
    before = links.link_all(h.NS, scopes=h.SCOPES, principal_id="linker")
    # Without accepted matches only citations and shared identifiers link; nothing rests on a proposal.
    assert before["owners"]["chemicals"]["links"] == 0 and before["owners"]["biodiversity"]["links"] == 0
    accept_identifier_matches(conn)
    result = links.link_all(h.NS, scopes=h.SCOPES, principal_id="linker")
    by = {(x["owner"], x["record_id"], x["basis"]): x for x in result["linked"]}
    substance = by[("chemicals", COMPOUND, "accepted-match")]
    assert substance["target_id"] == "pubchem:cid:99000101" and substance["target_revision"]
    assert substance["basis_detail"]["decision_id"]
    medicine = by[("clinical", COMPOUND, "shared-identifier")]
    assert medicine["target_kind"] == "medicinal-product" and medicine["basis_detail"]["identifier"]["value"] == \
        "CHEMBL9900101"
    cited = by[("clinical", TARGET, "citation")]
    assert cited["target_kind"] == "label-revision" and cited["basis_detail"]["cited_identifiers"] == ["X9EXA1"]
    occurrence = by[("biodiversity", TAXON, "accepted-match")]
    assert occurrence["target_kind"] == "occurrence" and occurrence["status"] == "resolved"
    literature = [x for x in result["linked"] if x["owner"] == "literature"]
    assert {x["status"] for x in literature} == {"resolved", "target_missing"}
    missing = next(x for x in literature if x["status"] == "target_missing")
    assert missing["target_revision"] is None and missing["basis_detail"]["cited"]["kind"] == "doi"
    assert all(x["revision_id"].startswith("lifesci-revision:") for x in result["linked"])
    assert "no drug-target" in result["notice"]
    # Idempotent: a second run adds nothing.
    again = links.link_all(h.NS, scopes=h.SCOPES, principal_id="linker")
    assert len(links.links(h.NS, scopes=h.READ_ONLY)) == len({x["link_id"] for x in again["linked"]})


def test_absent_providers_are_reported_not_dropped():
    conn = h.connection()
    h.load_all(conn)
    assert set(owner_status(conn).values()) == {"provider_absent"}
    result = LifeSciLinks(conn).link_all(h.NS, scopes=h.SCOPES, principal_id="linker")
    assert {o: r["status"] for o, r in result["owners"].items()} == {
        "chemicals": "provider_absent", "clinical": "provider_absent", "biodiversity": "provider_absent",
        "literature": "provider_absent"}
    # Cited literature is still kept as target_missing with its identifier.
    assert result["owners"]["literature"]["target_missing"] == result["owners"]["literature"]["links"] > 0
