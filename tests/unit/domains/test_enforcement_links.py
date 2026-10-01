"""Links to other packs by citation, shared identifier and accepted match (#2651, EN08)."""

from __future__ import annotations

import json

from src.kb.enforcement_links import EnforcementLinks, parse_references
from tests.unit import enforcement_harness as h


def test_references_are_parsed_exactly():
    keys = {c["key"] for c in parse_references("Section 10(b) of the Securities Exchange Act of 1934")}
    assert keys == {"usc:15:78a"}
    assert {c["key"] for c in parse_references("15 U.S.C. § 78j(b)")} == {"usc:15:78j"}
    assert {c["key"] for c in parse_references("CAA 112(r)(1)")} == {"usc:42:7401"}
    assert {c["key"] for c in parse_references("Financial Services and Markets Act 2000")} == {"uk:ukpga/2000/8"}
    assert parse_references("Article 6 (Lawfulness of processing)") == []  # GDPR only in the Article 60 register
    assert {c["key"] for c in parse_references("Article 6 (Lawfulness of processing)", authority="eu-sa-ie")} == \
        {"celex:32016R0679"}
    assert [c["kind"] for c in parse_references("SYSC 6.1.1R")] == ["handbook_provision"]
    assert [c["key"] for c in parse_references("see Commission case M.99001")] == ["case:ec:M.99001"]


def test_links_record_basis_and_revisions_and_report_missing_targets():
    conn = h.connection()
    env = h.reviewed(conn)
    links = EnforcementLinks(conn, initialize=False).links(h.NS)
    assert {link["basis"] for link in links} == {"citation", "shared_identifier", "accepted_match"}
    assert all(link["citing_revision_id"].startswith("enf-rev:") for link in links)
    gdpr = [link for link in links if link["target_key"] == env["works"]["gdpr"]]
    assert gdpr and gdpr[0]["status"] == "resolved" and gdpr[0]["evidence"]["provision"] == "Article 6 GDPR"
    fsma = [link for link in links if link["target_key"] == env["works"]["fsma"]]
    assert fsma and fsma[0]["status"] == "resolved"
    handbook = next(link for link in links if link["raw"] == "SYSC 6.1.1R")
    assert handbook["status"] == "unresolved"
    dockets = [link for link in links if link["target_kind"] == "court_docket"]
    assert {d["status"] for d in dockets} == {"provider_unavailable"}
    assert {d["raw"] for d in dockets} == {"1:99-cv-00901", "1:99-cv-00501", "FS/2099/0007"}
    market = next(link for link in links if link["target_kind"] == "market_issuer")
    assert market["status"] == "provider_unavailable" and market["basis"] == "shared_identifier"
    accepted = [link for link in links if link["basis"] == "accepted_match"]
    assert accepted and all(link["target_revision_id"] and link["evidence"]["candidate_id"] for link in accepted)
    assert all(link["status"] != "dropped" for link in links)
    again = EnforcementLinks(conn).link(h.NS, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    assert again["created"] == 0


def test_targets_acquired_later_resolve_by_exact_identity():
    conn = h.connection()
    h.reviewed(conn)
    # The Legal courts feature acquires the related docket; the Market store holds the CIK (test data only).
    from src.kb.legal_dockets import LegalDocketStore

    LegalDocketStore(conn)
    conn.execute("INSERT INTO legal_docket_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ["docket-rev:fixture", "global", "legal-work:x", "legal-version:x", "courts:docket:courtlistener:1",
                  "fixture", "courtlistener", "nysd", 1, "1:99-cv-00901", "SEC v. Exampla Holdings plc", None, None,
                  None, "0" * 64, 1, "run", 1, "fixture", "https://www.courtlistener.com/docket/1/", "{}"])
    from src.domains.market.instruments import _DDL as MARKET_DDL

    conn.execute(MARKET_DDL)
    conn.execute("INSERT INTO market_instrument_alias_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 ["market", "issuer", "issuer:exampla", "market-rev:1", 1, "cik", "9999101", "0009999101", 0, None,
                  "accepted", 0, "sec", "public", 1])
    result = EnforcementLinks(conn).link(h.NS, ownership_namespace=h.OWN_NS, scopes=h.SCOPES)
    resolved = {link["raw"]: link for link in result["links"] if link["status"] == "resolved"}
    assert resolved["1:99-cv-00901"]["target_revision_id"] == "docket-rev:fixture"
    assert resolved["0009999101"]["target_key"] == "issuer:exampla"
    listed = EnforcementLinks(conn, initialize=False).list_links(h.NS, scopes=h.READ_ONLY)
    assert listed["not_resolved"] and "causal" in listed["notice"]
    assert json.dumps(listed)
