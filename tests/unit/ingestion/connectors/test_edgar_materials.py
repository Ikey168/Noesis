"""Offline checks for SEC company-materials acquisition (#1672).

Shapes mirror live SEC responses observed on 2026-09-24: submissions columns,
``-index.htm`` document tables, EX-99 press releases, Form 4 XML under an
``xslF345X05/`` rendering path, and Inline XBRL segment facts.
"""

from __future__ import annotations

import json

import duckdb
import pytest

from src.domains.market.research import MarketResearchStore
from src.ingestion.connectors.edgar import EdgarClient
from src.ingestion.connectors.edgar_materials import (
    document_text,
    extract_guidance_statements,
    filing_documents,
    harvest_sec_company_materials,
    parse_insider_transactions,
)
from src.ingestion.connectors.filings_connector import FilingsConnector
from tests.unit.domains.test_market_research import NS, SCOPES

CIK = "0000123456"
BASE = "https://www.sec.gov/Archives/edgar/data/123456/"

SUBMISSIONS = {
    "name": "Acme Software",
    "filings": {"recent": {
        "accessionNumber": ["0000123456-26-000010", "0000123456-26-000009", "0000123456-26-000008", "0000123456-26-000007", "0000123456-26-000006", "0000123456-26-000005"],
        "form": ["8-K/A", "4", "SCHEDULE 13G", "10-Q", "8-K", "8-K"],
        "items": ["2.02,9.01", "", "", "", "2.02,9.01", "5.02"],
        "reportDate": ["2026-05-20", "2026-05-01", "", "2026-03-31", "2026-05-20", "2026-04-01"],
        "filingDate": ["2026-05-22", "2026-05-02", "2026-04-15", "2026-04-30", "2026-05-20", "2026-04-02"],
        "acceptanceDateTime": ["2026-05-22T21:00:00.000Z", "2026-05-02T20:00:00.000Z", "2026-04-15T12:00:00.000Z", "2026-04-30T20:00:00.000Z", "2026-05-20T20:05:00.000Z", "2026-04-02T12:00:00.000Z"],
        "primaryDocument": ["acme-8ka.htm", "xslF345X05/form4.xml", "primary_doc.xml", "acme-20260331.htm", "acme-8k.htm", "acme-502.htm"],
        "isInlineXBRL": [1, 0, 0, 1, 1, 0],
    }},
}


def index_html(*rows):
    body = "".join(
        f'<tr><td scope="row">{seq}</td><td scope="row">{desc}</td>'
        f'<td scope="row"><a href="/Archives/edgar/data/123456/x/{doc}">{doc}</a></td>'
        f'<td scope="row">{kind}</td><td scope="row">100</td></tr>'
        for seq, desc, doc, kind in rows
    )
    return f'<html><body><table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>{body}</table></body></html>'


RELEASE = """<?xml version="1.0" encoding="utf-8"?><html><body>
<h1>Acme Reports First Quarter Results</h1>
<ul><li>Revenue of $1.25 billion, up 12% Y/Y</li>
<li>Raises full year FY27 revenue guidance to $5.1 billion to $5.2 billion, up 10% - 11% Y/Y</li></ul>
<p>Outlook: Acme expects second quarter revenue of approximately $1.3 billion.</p>
<p>This release contains forward-looking statements, including expected revenue of $5 billion, subject to risks and uncertainties.</p>
</body></html>"""

FORM4 = """<?xml version="1.0"?><ownershipDocument><documentType>4</documentType><periodOfReport>2026-05-01</periodOfReport>
<reportingOwner><reportingOwnerId><rptOwnerCik>0000999999</rptOwnerCik><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
<reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>CFO</officerTitle></reportingOwnerRelationship></reportingOwner>
<nonDerivativeTable><nonDerivativeTransaction><securityTitle><value>Common Stock</value></securityTitle>
<transactionDate><value>2026-05-01</value></transactionDate><transactionCoding><transactionCode>S</transactionCode></transactionCoding>
<transactionAmounts><transactionShares><value>1000</value></transactionShares><transactionPricePerShare><value>250.5</value></transactionPricePerShare>
<transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode></transactionAmounts>
<postTransactionAmounts><sharesOwnedFollowingTransaction><value>5000</value></sharesOwnedFollowingTransaction></postTransactionAmounts>
</nonDerivativeTransaction></nonDerivativeTable></ownershipDocument>"""

TENQ = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi"><body>
<ix:header><ix:resources>
<xbrli:context id="q-cloud"><xbrli:entity><xbrli:identifier>0000123456</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">acme:CloudMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
<xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:context id="q-us"><xbrli:entity><xbrli:identifier>0000123456</xbrli:identifier><xbrli:segment><xbrldi:explicitMember dimension="srt:StatementGeographicalAxis">country:US</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
<xbrli:period><xbrli:startDate>2026-01-01</xbrli:startDate><xbrli:endDate>2026-03-31</xbrli:endDate></xbrli:period></xbrli:context>
<xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
</ix:resources></ix:header>
<ix:nonFraction name="us-gaap:Revenues" contextRef="q-cloud" unitRef="usd" scale="6" decimals="-6" format="ixt:num-dot-decimal">800</ix:nonFraction>
<ix:nonFraction name="us-gaap:Revenues" contextRef="q-us" unitRef="usd" scale="6" decimals="-6" format="ixt:num-dot-decimal">700</ix:nonFraction>
</body></html>"""


def fake_get(url, _user_agent):
    if "submissions" in url:
        return json.dumps(SUBMISSIONS)
    routes = {
        "000012345626000006/0000123456-26-000006-index.htm": index_html((1, "8-K", "acme-8k.htm", "8-K"), (2, "PRESS RELEASE", "ex99-1.htm", "EX-99.1"), (3, "SLIDES", "ex99-2.htm", "EX-99.2")),
        "000012345626000010/0000123456-26-000010-index.htm": index_html((1, "8-K/A", "acme-8ka.htm", "8-K/A"), (2, "PRESS RELEASE", "ex99-1a.htm", "EX-99.1")),
        "000012345626000006/ex99-1.htm": RELEASE,
        "000012345626000006/ex99-2.htm": "<html><body><p>Investor presentation.</p></body></html>",
        "000012345626000010/ex99-1a.htm": RELEASE,  # re-filed identical exhibit
        "000012345626000009/form4.xml": FORM4,
        "000012345626000007/acme-20260331.htm": TENQ,
    }
    for suffix, body in routes.items():
        if url == BASE + suffix:
            return body
    raise AssertionError(f"unexpected SEC request {url}")


@pytest.fixture
def harvested():
    client = EdgarClient("Noesis test bot", http_get=fake_get)
    return harvest_sec_company_materials(CIK, issuer_id="issuer:acme", client=client, request_pause_s=0)


def test_earnings_exhibits_link_period_accession_and_hash(harvested):
    releases = [row for row in harvested["materials"] if row["kind"] == "earnings_release"]
    original = next(row for row in releases if row["form"] == "8-K")
    amended = next(row for row in releases if row["form"] == "8-K/A")

    assert original["reporting_period"] == "2026-05-20"
    assert original["document_locator"] == BASE + "000012345626000006/ex99-1.htm"
    assert original["source_revision_id"].startswith("sec:0000123456-26-000006:ex99-1.htm@sha256:")
    assert original["licensing_status"] == "public"
    assert amended["corrects_material_id"] == original["material_id"]
    assert amended["duplicate_of"] == original["material_id"]
    assert harvested["coverage"]["earnings_presentation"] == 1
    # The unrelated Item 5.02 8-K is not an earnings material.
    assert all("0000123456-26-000005" not in row["material_id"] for row in harvested["materials"])


def test_guidance_is_a_span_linked_management_claim(harvested):
    guidance = next(row for row in harvested["materials"] if row["kind"] == "guidance" and row["form"] == "8-K")
    texts = [item["text"] for item in guidance["statements"]]
    assert "Raises full year FY27 revenue guidance to $5.1 billion to $5.2 billion, up 10% - 11% Y/Y" in texts
    assert "Outlook: Acme expects second quarter revenue of approximately $1.3 billion." in texts
    assert not any("forward-looking statements" in text for text in texts)
    assert not any(text.startswith("Revenue of $1.25 billion") for text in texts)
    source_text = document_text(RELEASE)
    for item in guidance["statements"]:
        assert source_text[item["start"]:item["end"]] == item["text"]
        assert item["claim_status"] == "management_claim"


def test_ownership_segments_and_unavailable_sources_are_explicit(harvested):
    insider = next(row for row in harvested["materials"] if row["kind"] == "insider_transaction")
    assert insider["owners"][0] == {"name": "Doe Jane", "owner_cik": "0000999999", "roles": ["officer"], "officer_title": "CFO"}
    assert insider["transactions"][0]["code"] == "S" and insider["transactions"][0]["shares_owned_after"] == "5000"
    assert insider["document_locator"].endswith("/form4.xml")

    ownership = next(row for row in harvested["materials"] if row["kind"] == "beneficial_ownership")
    assert ownership["form"] == "SCHEDULE 13G" and ownership["parse_status"] == "metadata_only"

    segments = next(row for row in harvested["materials"] if row["kind"] == "segment_disclosure")
    assert {(item["segment"], item["breakdown"], item["value_lexical"]) for item in segments["segments"]} == {
        ("acme:CloudMember", "operating_segment", "800000000"),
        ("country:US", "revenue_by_geography", "700000000"),
    }

    unavailable = {row["kind"]: row for row in harvested["materials"] if row["licensing_status"] != "public"}
    assert set(unavailable) == {"earnings_call_transcript", "analyst_consensus", "institutional_holdings_13f"}
    assert unavailable["analyst_consensus"]["licensing_status"] == "unlicensed"
    assert harvested["diagnostics"] == []


def test_connector_saves_versioned_materials_with_guidance_history():
    conn = duckdb.connect(":memory:")
    store = MarketResearchStore(conn, now=lambda: 1_900_000_000_000)
    connector = FilingsConnector(client=EdgarClient("Noesis test bot", http_get=fake_get))

    result = connector.ingest_market_materials(
        CIK, issuer_id="issuer:acme", namespace=NS, artifact_id="materials:acme", version=1,
        cutoff_ms=1_900_000_000_000, store=store, principal_id="alice", scopes=SCOPES,
        request_pause_s=0,
    )

    artifact = result["artifact"]
    assert artifact["contract"] == "noesis-market-materials-v1"
    assert [item["reporting_period"] for item in artifact["guidance_history"]] == ["2026-05-20", "2026-05-20"]
    assert artifact["duplicates"] and artifact["corrections"]
    assert artifact["coverage"]["unavailable"] == 3
    assert store.inspect(NS, "materials:acme", 1, principal_id="alice", scopes=SCOPES)["record_hash"] == artifact["record_hash"]


def test_parsers_are_bounded_and_do_not_resolve_entities():
    hostile = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><ownershipDocument><documentType>&e;</documentType></ownershipDocument>'
    assert parse_insider_transactions(hostile)["document_type"] is None
    assert filing_documents("<html><table><tr><td>1</td></tr></table></html>") == []
    assert extract_guidance_statements("Revenue grew 10%.") == []
