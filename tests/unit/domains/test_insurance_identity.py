"""Insurers matched to LEI and ownership entities through reviewable identity (#2230, IN07)."""

from __future__ import annotations

import pytest

import tests.unit.insurance_harness as h
from src.domains.market import insurance
from src.domains.market.insurance import CONTRACT, InsuranceQueries, InsuranceStore
from src.domains.market.insurance_identity import InsuranceIdentity, insurer_key
from src.kb.ownership_identity import FOREIGN_KEY_PREFIXES, OwnershipError

GROUP_KEY = f"insurance:insurer:lei:{h.GROUP_LEI}:group"
SOLO_KEY = f"insurance:insurer:lei:{h.SOLO_LEI}:solo"


@pytest.fixture
def conn():
    value = h.connection()
    h.acquire(value, "sfcr", "2025-05-02")
    h.lei_records(value)
    h.ownership_entities(value)
    return value


def test_published_leis_match_exactly_and_names_only_become_candidates(conn):
    identity = InsuranceIdentity(conn)
    exact = identity.exact_matches(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, lei_namespace=h.LEI_NS,
                                   ownership_namespace=h.OWN_NS)
    assert {(m["kind"], m["basis"]) for m in exact[GROUP_KEY]} == {
        ("lei-record", "exact-identifier"), ("ownership-entity", "exact-identifier")}
    assert [m["target"] for m in exact[SOLO_KEY]] == [f"lei:{h.SOLO_LEI}"]
    proposed = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    candidates = {c["records"][1] if c["records"][0].startswith("insurance:") else c["records"][0]: c
                  for c in proposed["candidates"]}
    # The subsidiary's German register record is a name + jurisdiction candidate, the record without a
    # jurisdiction a similar-name candidate, and the Austrian record (another country) no candidate at all.
    assert candidates["register:fiktiva-leben"]["basis"] == "name-jurisdiction"
    assert candidates["register:fiktiva-leben-unknown"]["basis"] == "similar-name"
    assert "register:fiktiva-leben-at" not in candidates
    assert all(c["state"] == "proposed" for c in candidates.values())
    # The group's exact ownership match is never re-offered as a name candidate.
    assert f"lei:{h.GROUP_LEI}" not in candidates
    # Idempotent.
    again = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert again["proposed"] == []
    assert "insurance:" in FOREIGN_KEY_PREFIXES


def test_accepted_rejected_and_unmatched_insurers(conn):
    identity = InsuranceIdentity(conn)
    proposed = identity.propose(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    by_target = {c["records"][1] if c["records"][0].startswith("insurance:") else c["records"][0]: c["candidate_id"]
                 for c in proposed["candidates"]}
    with pytest.raises(OwnershipError):
        identity.review(h.NS, by_target["register:fiktiva-leben-unknown"], "accept", "same name",
                        principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    identity.review(h.NS, by_target["register:fiktiva-leben-unknown"], "reject", "no identifier",
                    principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    identity.review(h.NS, by_target["register:fiktiva-leben"], "accept", "register extract states the LEI",
                    principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    # The accepted register record now resolves to the solo undertaking; the rejected one resolves nothing.
    accepted = identity.resolve(h.NS, "register:fiktiva-leben", scopes=h.SCOPES)
    assert accepted["status"] == "resolved" and accepted["record_keys"] == [SOLO_KEY]
    assert identity.resolve(h.NS, "register:fiktiva-leben-unknown", scopes=h.SCOPES)["status"] == "unmatched"
    # An insurer published without any identifier stays unmatched but queryable by its source key.
    InsuranceStore(conn, initialize=False).apply(h.NS, [{
        "contract": CONTRACT, "kind": "insurer_report",
        "source": {"provider": "insurer-sfcr", "source_id": "musterversicherung|sfcr|2024|solo",
                   "url": "https://www.allianz.com/fixture/muster.pdf"},
        "publication_date": "2025-04-28",
        "insurer": {"name": "Musterversicherung AG", "country": "DE", "reporting_level": "solo"},
        "report_type": "sfcr", "reporting_year": 2024,
        "document": {"url": "https://www.allianz.com/fixture/muster.pdf", "sha256": "c" * 64},
        "figures": [],
    }], run_id="manual", observed_at_ms=h.ms("2025-05-03"))
    unmatched = identity.unmatched(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES, lei_namespace=h.LEI_NS,
                                   ownership_namespace=h.OWN_NS)
    key = "insurance:insurer:name:musterversicherung ag|DE|solo"
    assert unmatched == [key]
    by_name = identity.resolve(h.NS, "Musterversicherung AG", scopes=h.SCOPES)
    assert by_name["status"] == "unmatched" and by_name["same_name_parties"] == [key]
    answer = InsuranceQueries(conn).insurer(h.NS, key, as_of="2025-05-31", scopes=h.SCOPES)
    assert answer["status"] == "on_record" and answer["insurer"]["basis"] == "source-key"
    # Reverting the accepted match stops it from resolving.
    identity.revert(h.NS, by_target["register:fiktiva-leben"], "register extract was for another entity",
                    principal_id=h.PRINCIPAL, scopes=h.SCOPES)
    assert identity.resolve(h.NS, "register:fiktiva-leben", scopes=h.SCOPES)["status"] == "unmatched"


def test_group_and_solo_reports_stay_distinct_without_and_with_a_recorded_ownership_link(conn):
    queries = InsuranceQueries(conn)
    solo = queries.insurer(h.NS, h.SOLO_LEI, as_of="2025-05-31", scopes=h.SCOPES)
    assert [r["record"]["insurer"]["reporting_level"] for r in solo["reports"]] == ["solo"]
    assert solo["group_reports_via_ownership_link"] == []  # no LEI namespace given: no link is assumed
    linked = queries.insurer(h.NS, h.SOLO_LEI, as_of="2025-05-31", scopes=h.SCOPES, lei_namespace=h.LEI_NS)
    [group] = linked["group_reports_via_ownership_link"]
    assert group["record"]["insurer"]["lei"] == h.GROUP_LEI
    assert group["ownership_link"]["basis"] == "gleif-parent-relationship"
    assert "never the undertaking's" in group["attribution"]
    assert [r["record"]["insurer"]["lei"] for r in linked["reports"]] == [h.SOLO_LEI]
    # The group query never pulls in the subsidiary's solo report.
    group_answer = queries.insurer(h.NS, h.GROUP_LEI, as_of="2025-05-31", scopes=h.SCOPES, lei_namespace=h.LEI_NS)
    assert [r["record"]["insurer"]["reporting_level"] for r in group_answer["reports"]] == ["group"]
    assert group_answer["group_reports_via_ownership_link"] == []


def test_without_a_parent_relationship_a_group_report_is_never_attributed():
    conn = h.connection()
    h.acquire(conn, "sfcr", "2025-05-02")
    h.lei_records(conn, parent=False)
    linked = InsuranceQueries(conn).insurer(h.NS, h.SOLO_LEI, as_of="2025-05-31", scopes=h.SCOPES,
                                             lei_namespace=h.LEI_NS)
    assert linked["group_reports_via_ownership_link"] == []


def test_naic_codes_match_exactly_when_a_decision_permits_naic_rows(monkeypatch):
    from src.kb.ownership_records import record
    from src.kb.ownership_store import OwnershipStore

    monkeypatch.setitem(insurance.LICENCE_DECISIONS, "naic-public", {
        **insurance.LICENCE_DECISIONS["naic-public"], "decision": "in-scope", "kinds": ("supervisory_indicator",)})
    conn = h.connection()
    InsuranceStore(conn).apply(h.NS, [{
        "contract": CONTRACT, "kind": "supervisory_indicator",
        "source": {"provider": "naic-public", "source_id": "ms|12345", "url": "https://content.naic.org/x.csv"},
        "publication_date": "2025-05-20", "publisher": "NAIC", "dataset": "Market share (fixture)",
        "indicator": "direct_written_premiums", "indicator_label": "Direct written premium",
        "dimensions": {"country": "US"}, "period": {"reference_year": 2024}, "value": "2500000", "unit": "USD",
        "currency": "USD", "release": "2024",
        "insurer": {"name": "Fiktiva Casualty Company", "naic_company_code": "12345", "naic_group_code": "4321",
                    "country": "US", "reporting_level": "solo"},
    }], run_id="naic", observed_at_ms=h.ms("2025-06-01"))
    OwnershipStore(conn, now=lambda: h.ms("2025-01-01")).apply(h.OWN_NS, [
        record("legal_entity", "register:fiktiva-casualty", {"provider": "opencorporates",
                                                             "provider_record_id": "fiktiva-casualty"},
               name="Fiktiva Casualty Co.", jurisdiction="US-FL", identifiers=[{"scheme": "naic", "value": "12345"}]),
    ], run_id="register", observed_at_ms=h.ms("2025-01-01"), principal_id=h.PRINCIPAL)
    exact = InsuranceIdentity(conn).exact_matches(h.NS, principal_id=h.PRINCIPAL, scopes=h.SCOPES,
                                                  ownership_namespace=h.OWN_NS)
    key = insurer_key({"naic_company_code": "12345", "reporting_level": "solo"})
    assert [m["identifier"] for m in exact[key]] == [{"scheme": "naic", "value": "12345"}]
    assert InsuranceIdentity(conn).resolve(h.NS, "12345", scopes=h.SCOPES)["record_keys"] == [key]
