"""Institution and country statistics as of a vintage with definitions and comparability notes (#2428)."""

from __future__ import annotations

import pytest

from src.kb.education_identity import EducationIdentity
from src.kb.education_statistics import EducationError, EducationLinks, EducationQueries, forbidden_keys
from tests.unit import education_harness as h

R1, R2 = "https://ror.org/0zfs01a23", "https://ror.org/0zmc02b34"


@pytest.fixture()
def env():
    conn = h.connection()
    h.load_all(conn, revisions=True)
    identity = EducationIdentity(conn)
    identity.record_ror(h.NS, h.ror_records(), principal_id="svc", scopes=h.SCOPES)
    identity.propose(h.NS, principal_id="svc", scopes=h.SCOPES)
    yield conn, identity, EducationQueries(conn)
    conn.close()


def figures(answer, concept):
    group = next(c for c in answer["concepts"] if c["concept"] == concept)
    return {(p["period"], f["provider"], f["indicator"]["code"]): f for p in group["periods"] for f in p["figures"]}


def test_as_of_selects_the_vintage_known_at_the_date_and_lists_every_vintage(env):
    conn, identity, queries = env
    match = next(m for m in identity.matches(h.NS, scopes=h.SCOPES) if m["ror_id"] == R1)
    identity.review(h.NS, match["match_id"], "accept", "same city", principal_id="r", scopes=h.SCOPES)
    early = queries.institution(h.NS, scopes=h.READ_ONLY, ror=R1, as_of="2099-06-01", concept="enrolment")
    figure = figures(early, "enrolment")[("2098", "ipeds", "EFTOTLT")]
    assert figure["value"] == "25000" and figure["release"]["release_stage"] == "provisional"
    assert [v["value"] for v in figure["vintages"]] == ["25000"]
    late = queries.institution(h.NS, scopes=h.READ_ONLY, ror=R1, as_of="2099-12-31", concept="enrolment")
    figure = figures(late, "enrolment")[("2098", "ipeds", "EFTOTLT")]
    assert figure["value"] == "25140" and figure["release"]["release_stage"] == "final"
    assert figure["release"]["revision_of"] is not None
    assert [(v["release_stage"], v["value"]) for v in figure["vintages"]] == [("provisional", "25000"),
                                                                             ("final", "25140")]
    assert figure["citation"]["attribution"].startswith("U.S. Department of Education")
    assert late["identity"]["basis"][0]["state"] == "accepted" and late["profiles"][0]["subject"]["city"] == \
        "Springfield"
    history = queries.value_vintages(h.NS, figure["series_id"], "2098", scopes=h.READ_ONLY)
    assert [v["value"] for v in history["vintages"]] == ["25000", "25140"]
    assert all(v["source_revision"]["file_sha256"] for v in history["vintages"])
    before = queries.institution(h.NS, scopes=h.READ_ONLY, ror=R1, as_of="2098-12-31")
    assert before["status"] == "none_on_record" and before["not_released_by_as_of"]


def test_sources_for_one_concept_stand_side_by_side_with_their_definitions(env):
    _, _, queries = env
    answer = queries.country(h.NS, scopes=h.READ_ONLY, country="DE", concept="education_expenditure",
                             as_of="2099-12-31")
    found = figures(answer, "education_expenditure")
    uis, oecd = found[("2096", "unesco-uis", "XGDP.5T8")], found[("2096", "oecd-eag", "EXP_INST_GDP")]
    assert (uis["value"], oecd["value"]) == ("1.2", "1.3")
    assert uis["definition"]["definition"] != oecd["definition"]["definition"]
    period = next(p for p in answer["concepts"][0]["periods"] if p["period"] == "2096")
    assert period["side_by_side"] is True and period["sources"] == ["oecd-eag", "unesco-uis"]
    assert uis["comparability_notes"][0]["code"] == "NAT_EST"
    assert answer["country"]["published_codes"] == ["DEU"]
    assert forbidden_keys(answer) == []
    # Two published values, no third (combined) one.
    assert [f["value"] for f in period["figures"]] == ["1.3", "1.2"]


def test_missing_suppressed_and_confidential_values_keep_their_codes_and_empty_subjects_are_none(env):
    _, _, queries = env
    eter = queries.institution(h.NS, scopes=h.READ_ONLY, scheme="eter-id", code="DE0002")
    withheld = {(w["indicator"]["code"], w["status"], w["special_code"]) for w in eter["withheld"]}
    assert withheld == {("STAFF.ACAD.FTE", "confidential", "c"), ("EXP.CURR.TOTAL", "missing", "m")}
    assert eter["identity"]["status"] == "confirmed" and eter["identity"]["ror_changes"][0]["kind"] == "successor"
    ipeds = queries.institution(h.NS, scopes=h.READ_ONLY, scheme="ipeds-unitid", code="100002", concept="finance")
    suppressed = figures(ipeds, "finance")[("FY2098", "ipeds", "F1D01")]
    assert (suppressed["status"], suppressed["special_code"], suppressed["value"]) == ("suppressed", "-2", None)
    usa = queries.country(h.NS, scopes=h.READ_ONLY, country="USA", concept="education_expenditure")
    missing = figures(usa, "education_expenditure")[("2096", "oecd-eag", "EXP_INST_GDP")]
    assert (missing["status"], missing["special_code"]) == ("missing", "M")
    nothing = queries.institution(h.NS, scopes=h.READ_ONLY, scheme="ipeds-unitid", code="999999")
    assert nothing["status"] == "none_on_record" and nothing["concepts"] == []
    unconfirmed = queries.institution(h.NS, scopes=h.READ_ONLY, ror=R1)
    assert unconfirmed["status"] == "none_on_record" and unconfirmed["subjects"] == []
    assert queries.country(h.NS, scopes=h.READ_ONLY, country="JP")["status"] == "none_on_record"


def test_answers_cite_every_source_record_and_show_linked_records(env):
    conn, _, queries = env
    ids = h.seed_science_and_funding(conn)
    EducationLinks(conn).link(h.NS, subject={"ror": R2}, target={"kind": "funding_opportunity",
                                                                 "record_id": ids["award"]},
                              citation={"source": "award register", "locator": "recipient",
                                        "identifier": {"scheme": "ror", "value": R2}},
                              principal_id="a", scopes=h.SCOPES)
    answer = queries.institution(h.NS, scopes=h.READ_ONLY, ror=R2)
    assert answer["status"] == "answered" and answer["subjects"] == [{"scheme": "eter-id", "code": "DE0001"}]
    cited = {c["release_id"] for c in answer["citations"]}
    for concept in answer["concepts"]:
        for period in concept["periods"]:
            for figure in period["figures"]:
                assert figure["citation"]["release_id"] in cited and figure["citation"]["file_sha256"]
    assert [link["target"]["record_id"] for link in answer["linked_records"]] == [ids["award"]]
    rd = queries.country(h.NS, scopes=h.READ_ONLY, country="FR", concept="rd_expenditure", as_of="2099-03-31")
    provisional = figures(rd, "rd_expenditure")[("2097", "eurostat-rd", "GERD_HES")]
    assert provisional["value"] == "12800.4" and provisional["comparability_notes"][0]["code"] == "p"
    updated = queries.country(h.NS, scopes=h.READ_ONLY, country="FR", concept="rd_expenditure")
    assert figures(updated, "rd_expenditure")[("2097", "eurostat-rd", "GERD_HES")]["value"] == "12850.1"
    with pytest.raises(EducationError):
        queries.institution(h.NS, scopes=set(), ror=R2)
