"""Institution statistics linked to Science and Funding records by citation (#2420)."""

from __future__ import annotations

import pytest

from src.kb.education_identity import EducationIdentity
from src.kb.education_statistics import EducationError, EducationLinks
from tests.unit import education_harness as h

R1, R2 = "https://ror.org/0zfs01a23", "https://ror.org/0zmc02b34"
CITE = {"source": "Fictional award register", "locator": "award FICT-AWARD-1, recipient",
        "identifier": {"scheme": "ror", "value": R2}}


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn)
    identity = EducationIdentity(conn)
    identity.record_ror(h.NS, h.ror_records(), principal_id="svc", scopes=h.SCOPES)
    identity.propose(h.NS, principal_id="svc", scopes=h.SCOPES)
    ids = h.seed_science_and_funding(conn)
    yield conn, identity, EducationLinks(conn), ids
    conn.close()


def test_a_confirmed_ror_match_links_statistics_to_science_and_funding_records(env):
    _, _, links, ids = env
    award = links.link(h.NS, subject={"ror": R2}, target={"kind": "funding_opportunity", "record_id": ids["award"]},
                       citation=CITE, principal_id="alice", scopes=h.SCOPES)
    assert award["target_status"] == "resolved" and award["side"] == "funding"
    assert award["basis"]["method"] == "confirmed-ror-match" and award["basis"]["identifier_in_record"] is True
    assert award["subject"]["institutions"] == [{"scheme": "eter-id", "code": "DE0001"}]
    paper = links.link(h.NS, subject={"scheme": "eter-id", "code": "DE0001"},
                       target={"kind": "scholarly_work", "record_id": ids["paper"]},
                       citation={**CITE, "source": "document store", "locator": "metadata.affiliations[0].ror"},
                       principal_id="alice", scopes=h.SCOPES)
    assert paper["basis"]["method"] == "confirmed-ror-match" and paper["subject"]["ror"] == R2
    again = links.link(h.NS, subject={"ror": R2}, target={"kind": "funding_opportunity", "record_id": ids["award"]},
                       citation=CITE, principal_id="alice", scopes=h.SCOPES)
    assert again["link_id"] == award["link_id"]
    assert {v["target_kind"] for v in links.links(h.NS, scopes=h.READ_ONLY)} == {"funding_opportunity",
                                                                                "scholarly_work"}
    # The funding record is linked, not recomputed: the link holds no amount.
    assert "1000000" not in str(award["basis"]) + str(award["target"])


def test_no_name_or_keyword_joins_and_unconfirmed_candidates_do_not_link(env):
    _, identity, links, ids = env
    target = {"kind": "funding_opportunity", "record_id": ids["award"]}
    with pytest.raises(EducationError) as caught:  # 100001's candidates are not reviewed yet
        links.link(h.NS, subject={"ror": R1}, target=target,
                   citation={**CITE, "identifier": {"scheme": "ror", "value": R1}}, principal_id="a", scopes=h.SCOPES)
    assert caught.value.code == "no_confirmed_match"
    with pytest.raises(EducationError) as caught:
        links.link(h.NS, subject={"scheme": "eter-id", "code": "DE0001"}, target=target,
                   citation={**CITE, "identifier": {"scheme": "name", "value": "Universitaet Beispielstadt"}},
                   principal_id="a", scopes=h.SCOPES)
    assert caught.value.code == "citation_required"
    # A held record that does not state the identifier is refused.
    match = next(m for m in identity.matches(h.NS, scopes=h.SCOPES) if m["ror_id"] == R1)
    identity.review(h.NS, match["match_id"], "accept", "same city", principal_id="r", scopes=h.SCOPES)
    with pytest.raises(EducationError) as caught:
        links.link(h.NS, subject={"ror": R1}, target=target,
                   citation={**CITE, "identifier": {"scheme": "ror", "value": R1}}, principal_id="a", scopes=h.SCOPES)
    assert caught.value.code == "identifier_not_in_record"
    missing = links.link(h.NS, subject={"ror": R1}, target={"kind": "methodology_study", "record_id": "study:x"},
                         citation={**CITE, "identifier": {"scheme": "ror", "value": R1}}, principal_id="a",
                         scopes=h.SCOPES)
    assert missing["target_status"] == "provider_absent"


def test_an_institution_without_science_or_funding_records_has_no_links(env):
    _, _, links, _ = env
    assert links.links(h.NS, scopes=h.READ_ONLY, subject={"scheme": "eter-id", "code": "FR0001"}) == []
    assert links.links(h.NS, scopes=h.READ_ONLY, ror=R1) == []
    with pytest.raises(EducationError):
        links.link(h.NS, subject={"ror": R2}, target={"kind": "funding_opportunity", "record_id": "x"},
                   citation=CITE, principal_id="a", scopes=h.READ_ONLY)
