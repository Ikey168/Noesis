"""Offline CompanyFacts normalization checks for the market fact contract."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
import pytest

from jsonschema import Draft7Validator, FormatChecker
import duckdb

from src.domains.market.financial_facts import MarketFinancialFactStore
from src.domains.market.instruments import MarketInstrumentStore
from src.ingestion.connectors.edgar import (
    EdgarClient,
    companyfacts_to_market_facts,
    harvest_market_financial_facts,
    parse_inline_xbrl_facts,
    primary_document_for_accession,
    reconcile_market_facts_to_filing,
    submissions_acceptance_times,
)
from src.ingestion.connectors.filings_connector import FilingsConnector
from tests.unit.domains.market_entitlement_fixtures import register_market_entitlement

CIK = "0000123456"
ISSUER_ID = "issuer:acme"
NAMESPACE = "market:facts-test"
RETRIEVED_AT = 1_800_000_000_000
ACCESSION_Q1 = "0000123456-25-000001"
ACCESSION_Q2 = "0000123456-25-000002"
ACCESSION_ANNUAL = "0000123456-26-000003"


def ms(day: str) -> int:
    return int(
        datetime.fromisoformat(day).replace(tzinfo=timezone.utc).timestamp() * 1000
    )


def entry(accession, form, filed, fy, fp, value, *, start=None, end=None, frame=None):
    result = {
        "accn": accession,
        "form": form,
        "filed": filed,
        "fy": fy,
        "fp": fp,
        "val": Decimal(str(value)),
    }
    if start is not None:
        result["start"] = start
    if end is not None:
        result["end"] = end
    if frame is not None:
        result["frame"] = frame
    return result


def companyfacts():
    return {
        "cik": int(CIK),
        "entityName": "Acme Corporation",
        "facts": {
            "us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {
                        "USD": [
                            entry(
                                ACCESSION_Q1,
                                "10-Q",
                                "2025-05-08",
                                2025,
                                "Q1",
                                "100.25",
                                start="2025-01-01",
                                end="2025-03-31",
                                frame="CY2025Q1",
                            ),
                            entry(
                                ACCESSION_Q2,
                                "10-Q",
                                "2025-08-07",
                                2025,
                                "Q2",
                                "125.50",
                                start="2025-04-01",
                                end="2025-06-30",
                                frame="CY2025Q2",
                            ),
                            entry(
                                ACCESSION_Q2,
                                "10-Q",
                                "2025-08-07",
                                2025,
                                "Q2",
                                "225.75",
                                start="2025-01-01",
                                end="2025-06-30",
                                frame="CY2025Q2YTD",
                            ),
                            # A second context with a different value is kept and flagged.
                            entry(
                                ACCESSION_Q2,
                                "10-Q",
                                "2025-08-07",
                                2025,
                                "Q2",
                                "226.75",
                                start="2025-01-01",
                                end="2025-06-30",
                                frame="CY2025Q2YTD-alt",
                            ),
                        ]
                    }
                },
                "NetIncomeLoss": {
                    "units": {
                        "USD": [
                            entry(
                                ACCESSION_ANNUAL,
                                "10-K",
                                "2026-02-25",
                                2025,
                                "FY",
                                "42.00",
                                start="2025-01-01",
                                end="2026-01-03",
                            )
                        ]
                    }
                },
                "Assets": {
                    "units": {
                        "USD": [
                            entry(
                                ACCESSION_Q2,
                                "10-Q",
                                "2025-08-07",
                                2025,
                                "Q2",
                                "900.00",
                                end="2025-06-30",
                            )
                        ]
                    }
                },
                "NetCashProvidedByUsedInOperatingActivities": {
                    "units": {
                        "USD": [
                            entry(
                                ACCESSION_Q2,
                                "10-Q",
                                "2025-08-07",
                                2025,
                                "Q2",
                                "70.00",
                                start="2025-01-01",
                                end="2025-06-30",
                            )
                        ]
                    }
                },
                "UnmappedCustomLikeStandardTag": {
                    "units": {
                        "USD-per-shares": [
                            entry(
                                ACCESSION_Q2,
                                "10-Q",
                                "2025-08-07",
                                2025,
                                "Q2",
                                "1.25",
                                start="2025-04-01",
                                end="2025-06-30",
                            )
                        ]
                    }
                },
            }
        },
    }


def test_companyfacts_normalization_preserves_statement_contexts_and_periods():
    payload = companyfacts()
    batch = companyfacts_to_market_facts(
        payload,
        cik=CIK,
        issuer_id=ISSUER_ID,
        namespace=NAMESPACE,
        retrieved_at_ms=RETRIEVED_AT,
        accepted_at_by_accession={ACCESSION_Q1: ms("2025-05-08T17:30:00")},
    )
    facts = batch["facts"]
    schema = json.loads(
        __import__("pathlib")
        .Path("contracts/schemas/jsonschema/noesis-market-financial-fact-v1.json")
        .read_text()
    )
    validator = Draft7Validator(schema, format_checker=FormatChecker())
    for fact in facts:
        validator.validate(fact)

    revenue = [fact for fact in facts if fact["concept"].startswith("Revenue")]
    q1 = next(fact for fact in revenue if fact["filing_accession"] == ACCESSION_Q1)
    q2 = [fact for fact in revenue if fact["filing_accession"] == ACCESSION_Q2]
    q2_periods = {fact["period_class"] for fact in q2}
    annual = next(fact for fact in facts if fact["concept"] == "NetIncomeLoss")
    instant = next(fact for fact in facts if fact["concept"] == "Assets")
    unmapped = next(
        fact for fact in facts if fact["concept"] == "UnmappedCustomLikeStandardTag"
    )

    assert q1["value_lexical"] == "100.25"
    assert q1["accepted_at_ms"] == ms("2025-05-08T17:30:00")
    assert q1["public_at_ms"] == q1["accepted_at_ms"]
    assert q1["statement"] == "income_statement"
    assert q1["canonical_concept"] == "revenue"
    assert q1["context_id_kind"] == "companyfacts_composite_key"
    assert q2_periods == {"quarter", "year_to_date"}
    assert len(q2) == 3  # contexts remain separate; conflicting values are flagged
    assert annual["period_class"] == "annual"  # actual 53-week dates are retained
    assert annual["period"]["end_date"] == "2026-01-03"
    assert instant["period"] == {"kind": "instant", "instant_date": "2025-06-30"}
    assert unmapped["unit"] == "USD-per-shares"
    assert unmapped["mapping_status"] == "unmapped"
    assert any(item["code"] == "conflicting_contexts" for item in batch["diagnostics"])
    assert any(item["code"] == "missing_core_concept" for item in batch["diagnostics"])
    assert any(
        item["code"] == "unmapped_accounting_concept"
        and item["concept"] == "UnmappedCustomLikeStandardTag"
        for item in batch["diagnostics"]
    )


def test_reconciliation_matches_context_independent_filing_facts_and_flags_limits():
    batch = companyfacts_to_market_facts(
        companyfacts(),
        cik=CIK,
        issuer_id=ISSUER_ID,
        namespace=NAMESPACE,
        retrieved_at_ms=RETRIEVED_AT,
    )
    filing_facts = [
        {
            "filing_accession": fact["filing_accession"],
            "taxonomy": fact["taxonomy"],
            "concept": fact["concept"],
            "unit": fact["unit"],
            "period": fact["period"],
            "value_lexical": fact["value_lexical"],
            "context_id": f"native:{fact['context_id']}",
        }
        for fact in batch["facts"]
    ]

    result = reconcile_market_facts_to_filing(batch, filing_facts)

    assert result["matched"] == result["normalized_fact_count"]
    assert result["mismatched"] == 0
    assert result["unsupported_mappings"] == 1
    assert any(
        item["code"] == "unsupported_accounting_mapping"
        for item in result["diagnostics"]
    )
    assert any(
        item["code"] == "conflicting_contexts" for item in result["diagnostics"]
    )
    assert result["readiness"] == "partial"


def test_reconciliation_reports_filing_value_mismatch_and_missing_normalized_fact():
    normalized = {
        "facts": [
            {
                "filing_accession": ACCESSION_Q1,
                "taxonomy": "us-gaap",
                "concept": "Assets",
                "unit": "USD",
                "period": {"kind": "instant", "instant_date": "2025-03-31"},
                "value_lexical": "100",
                "context_id": "companyfacts-context",
                "mapping_status": "mapped",
            }
        ],
        "diagnostics": [],
    }
    filing_facts = [
        {
            "filing_accession": ACCESSION_Q1,
            "taxonomy": "us-gaap",
            "concept": "Assets",
            "unit": "USD",
            "period": {"kind": "instant", "instant_date": "2025-03-31"},
            "value_lexical": "101",
        },
        {
            "filing_accession": ACCESSION_Q1,
            "taxonomy": "us-gaap",
            "concept": "Liabilities",
            "unit": "USD",
            "period": {"kind": "instant", "instant_date": "2025-03-31"},
            "value_lexical": "20",
        },
    ]

    result = reconcile_market_facts_to_filing(normalized, filing_facts)

    assert result["mismatched"] == 1
    assert result["missing_normalized"] == 1
    assert result["unmatched_normalized"] == 0
    codes = {item["code"] for item in result["diagnostics"]}
    assert {"filing_value_mismatch", "missing_normalized_fact"} <= codes
    assert result["readiness"] == "partial"


def test_reconciliation_flags_conflicting_native_filing_contexts():
    period = {"kind": "instant", "instant_date": "2025-03-31"}
    normalized = {
        "facts": [
            {
                "filing_accession": ACCESSION_Q1,
                "taxonomy": "us-gaap",
                "concept": "Assets",
                "unit": "USD",
                "period": period,
                "value_lexical": "100",
                "context_id": "companyfacts:instant",
                "mapping_status": "mapped",
            }
        ],
        "diagnostics": [],
    }
    filing_facts = [
        {
            "filing_accession": ACCESSION_Q1,
            "taxonomy": "us-gaap",
            "concept": "Assets",
            "unit": "USD",
            "period": period,
            "value_lexical": value,
            "native_context_id": context,
        }
        for context, value in (("consolidated", "100"), ("segment", "90"))
    ]

    result = reconcile_market_facts_to_filing(normalized, filing_facts)

    conflicts = [
        item for item in result["diagnostics"]
        if item["code"] == "conflicting_contexts"
    ]
    assert len(conflicts) == 1
    assert conflicts[0]["source"] == "inline_xbrl"
    assert conflicts[0]["context_ids"] == ["consolidated", "segment"]
    assert result["mismatched"] == 1


def test_inline_xbrl_parser_preserves_native_contexts_scale_and_dimensions():
    filing = """
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
          xmlns:xbrli="http://www.xbrl.org/2003/instance"
          xmlns:us-gaap="http://fasb.org/us-gaap/2024"
          xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      <body><ix:header><ix:resources>
        <xbrli:context id="ctx-duration">
          <xbrli:entity><xbrli:identifier scheme="https://www.sec.gov/CIK">0000123456</xbrli:identifier>
            <xbrli:segment><xbrli:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">us-gaap:ServicesMember</xbrli:explicitMember></xbrli:segment>
          </xbrli:entity>
          <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period>
        </xbrli:context>
        <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      </ix:resources></ix:header>
      <ix:nonFraction name="us-gaap:Revenues" contextRef="ctx-duration" unitRef="usd" scale="6" decimals="-6">100.25</ix:nonFraction>
    </body></html>
    """
    result = parse_inline_xbrl_facts(filing, accession=ACCESSION_Q1)
    fact = result["facts"][0]
    assert fact["value_lexical"] == "100250000"
    assert fact["native_context_id"] == "ctx-duration"
    assert fact["context_id_kind"] == "inline_xbrl_native"
    assert fact["dimensions"][0]["member"] == "us-gaap:ServicesMember"
    assert fact["period"] == {
        "kind": "duration",
        "start_date": "2025-01-01",
        "end_date": "2025-03-31",
    }


def test_inline_xbrl_parser_diagnoses_unrecognized_transform_without_guessing():
    filing = """
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
          xmlns:xbrli="http://www.xbrl.org/2003/instance">
      <xbrli:context id="instant"><xbrli:entity><xbrli:identifier>0000123456</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period>
      </xbrli:context>
      <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <ix:nonFraction name="us-gaap:Assets" contextRef="instant" unitRef="usd" format="ixt:unsupported">1,234</ix:nonFraction>
    </html>
    """

    result = parse_inline_xbrl_facts(filing, accession=ACCESSION_Q1)

    assert result["facts"] == []
    assert result["counts"]["skipped"] == 1
    assert result["diagnostics"][0]["code"] == "unsupported_numeric_transform"


def test_sec_accession_reconciliation_fetches_filing_and_flags_actual_disagreement():
    submissions = {
        "name": "Acme Corporation",
        "filings": {
            "recent": {
                "accessionNumber": [ACCESSION_Q1],
                "acceptanceDateTime": ["2025-05-08T17:30:00Z"],
                "primaryDocument": ["acme-20250331.htm"],
            }
        },
    }
    filing = """
    <html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
          xmlns:xbrli="http://www.xbrl.org/2003/instance"
          xmlns:us-gaap="http://fasb.org/us-gaap/2024"
          xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
      <xbrli:context id="ctx-revenue"><xbrli:entity><xbrli:identifier>0000123456</xbrli:identifier></xbrli:entity>
        <xbrli:period><xbrli:startDate>2025-01-01</xbrli:startDate><xbrli:endDate>2025-03-31</xbrli:endDate></xbrli:period>
      </xbrli:context>
      <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
      <ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" contextRef="ctx-revenue" unitRef="usd">101.25</ix:nonFraction>
    </html>
    """

    def fake_get(url, _user_agent):
        if "submissions" in url:
            return json.dumps(submissions)
        if "companyfacts" in url:
            return json.dumps(companyfacts(), default=str)
        if url.endswith("acme-20250331.htm"):
            return filing
        raise AssertionError(url)

    client = EdgarClient("Noesis test test@example.org", http_get=fake_get)
    connector = FilingsConnector(client=client)
    result = connector.reconcile_market_financial_facts(
        CIK,
        issuer_id=ISSUER_ID,
        namespace=NAMESPACE,
        accession=ACCESSION_Q1,
        retrieved_at_ms=RETRIEVED_AT,
    )
    assert primary_document_for_accession(submissions, ACCESSION_Q1) == "acme-20250331.htm"
    assert result["filing_url"].endswith("acme-20250331.htm")
    assert result["inline_xbrl"]["native_contexts"] is True
    assert result["reconciliation"]["mismatched"] == 1
    assert any(
        item["code"] == "filing_value_mismatch"
        for item in result["reconciliation"]["diagnostics"]
    )


def test_filing_document_path_validation_blocks_path_traversal():
    client = EdgarClient("Noesis test test@example.org", http_get=lambda _url, _ua: "body")
    with pytest.raises(ValueError, match="accession"):
        client.filing_document(CIK, "../0000123456-25-000001", "filing.htm")
    with pytest.raises(ValueError, match="basename"):
        client.filing_document(CIK, ACCESSION_Q1, "../filing.htm")


def test_amendments_and_accession_revisions_are_not_collapsed():
    payload = companyfacts()
    facts = payload["facts"]["us-gaap"][
        "RevenueFromContractWithCustomerExcludingAssessedTax"
    ]["units"]["USD"]
    facts.extend(
        [
            entry(
                "0000123456-26-000004",
                "10-K/A",
                "2026-03-15",
                2025,
                "FY",
                "500",
                start="2025-01-01",
                end="2026-01-03",
            ),
            entry(
                "0000123456-26-000003",
                "10-K",
                "2026-02-25",
                2025,
                "FY",
                "490",
                start="2025-01-01",
                end="2026-01-03",
            ),
        ]
    )
    batch = companyfacts_to_market_facts(
        payload,
        cik=CIK,
        issuer_id=ISSUER_ID,
        namespace=NAMESPACE,
        retrieved_at_ms=RETRIEVED_AT,
    )
    annual_revenue = [
        fact
        for fact in batch["facts"]
        if fact["concept"].startswith("Revenue") and fact["period_class"] == "annual"
    ]

    assert {fact["filing_accession"] for fact in annual_revenue} == {
        "0000123456-26-000003",
        "0000123456-26-000004",
    }
    assert {fact["value_lexical"] for fact in annual_revenue} == {"490", "500"}


def test_missing_filing_metadata_is_diagnostic_not_silently_invented():
    payload = {
        "facts": {
            "us-gaap": {
                "Assets": {
                    "units": {
                        "USD": [
                            {
                                "form": "10-Q",
                                "filed": "not-a-date",
                                "val": Decimal("10"),
                                "end": "2025-06-30",
                            },
                            {
                                "accn": "0000123456-25-000001",
                                "form": "10-Q",
                                "filed": "not-a-date",
                                "val": Decimal("11"),
                                "end": "2025-06-30",
                            },
                        ]
                    }
                }
            }
        }
    }
    batch = companyfacts_to_market_facts(
        payload,
        cik=CIK,
        issuer_id=ISSUER_ID,
        namespace=NAMESPACE,
        retrieved_at_ms=RETRIEVED_AT,
    )

    assert len(batch["facts"]) == 1
    fact = batch["facts"][0]
    assert fact["filed_at_ms"] is None
    assert fact["public_at_ms"] is None
    assert any(item["code"] == "missing_accession" for item in batch["diagnostics"])
    assert any(item["code"] == "public_time_unknown" for item in batch["diagnostics"])


def test_companyfacts_http_decoder_preserves_decimal_lexical_digits():
    body = (
        '{"facts":{"us-gaap":{"Revenues":{"units":{"USD":['
        '{"accn":"0000123456-25-000001","form":"10-Q",'
        '"filed":"2025-05-08","fy":2025,"fp":"Q1",'
        '"start":"2025-01-01","end":"2025-03-31","val":100.2500}'
        "]}}}}}"
    )
    client = EdgarClient(
        "Noesis test test@example.org",
        http_get=lambda _url, _agent: body,
    )
    decoded = client.company_facts(CIK)
    batch = companyfacts_to_market_facts(
        decoded,
        cik=CIK,
        issuer_id=ISSUER_ID,
        namespace=NAMESPACE,
        retrieved_at_ms=RETRIEVED_AT,
    )

    assert batch["facts"][0]["value_lexical"] == "100.2500"


def test_submission_acceptance_time_lookup_and_new_harvest_entrypoint():
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": [ACCESSION_Q1, ACCESSION_Q2],
                "acceptanceDateTime": ["2025-05-08T17:30:00.000Z", ""],
            }
        }
    }
    assert submissions_acceptance_times(submissions) == {
        ACCESSION_Q1: ms("2025-05-08T17:30:00")
    }

    def fake_get(url, user_agent):
        assert user_agent == "Noesis test test@example.org"
        if "companyfacts" in url:
            return json.dumps(companyfacts(), default=str)
        if "submissions" in url:
            return json.dumps(
                {
                    "name": "Acme Corporation",
                    "filings": {
                        "recent": {
                            "accessionNumber": [ACCESSION_Q1],
                            "acceptanceDateTime": ["2025-05-08T17:30:00Z"],
                        }
                    },
                }
            )
        raise AssertionError(url)

    client = EdgarClient("Noesis test test@example.org", http_get=fake_get)
    batch = harvest_market_financial_facts(
        CIK,
        issuer_id=ISSUER_ID,
        namespace=NAMESPACE,
        retrieved_at_ms=RETRIEVED_AT,
        client=client,
    )

    assert batch is not None
    assert batch["cik"] == CIK
    assert batch["entity_name"] == "Acme Corporation"
    assert batch["counts"]["facts"] > 0


def test_filings_connector_indexes_full_facts_without_changing_note_harvest():
    def fake_get(url, _user_agent):
        if "companyfacts" in url:
            return json.dumps(companyfacts(), default=str)
        if "submissions" in url:
            return json.dumps({"name": "Acme Corporation", "filings": {"recent": {}}})
        raise AssertionError(url)

    connection = duckdb.connect(":memory:")
    register_market_entitlement(
        connection,
        NAMESPACE,
        "sec-edgar-public",
        "sec-edgar",
        "sec-public",
        now_ms=RETRIEVED_AT,
    )
    instruments = MarketInstrumentStore(connection, now=lambda: RETRIEVED_AT)
    instruments.put_issuer(
        NAMESPACE,
        issuer_id=ISSUER_ID,
        kg_entity_id="kg:acme",
        display_name="Acme Corporation",
        source_refs=[
            {
                "source_ref_id": "src:acme-issuer",
                "provider": "sec-edgar",
                "provider_object_id": CIK,
                "source_revision_id": CIK,
                "public_at_ms": RETRIEVED_AT,
                "source_snapshot_id": "snapshot:acme-issuer",
                "source_url": "https://www.sec.gov/Archives/edgar/data/123456/",
                "retrieved_at_ms": RETRIEVED_AT,
                "content_hash": "a" * 64,
                "license_id": "sec-public",
                "entitlement_id": "sec-edgar-public",
            }
        ],
        principal_id="market-ingest:operator",
        scopes={"operator"},
    )
    store = MarketFinancialFactStore(connection, now=lambda: RETRIEVED_AT)
    connector = FilingsConnector(
        client=EdgarClient("Noesis test test@example.org", http_get=fake_get)
    )

    result = connector.ingest_market_financial_facts(
        CIK,
        issuer_id=ISSUER_ID,
        namespace=NAMESPACE,
        store=store,
        principal_id="market-ingest:operator",
        scopes={"operator"},
        retrieved_at_ms=RETRIEVED_AT,
    )

    assert result is not None
    assert result["count"] == result["counts"]["facts"]
    assert result["readiness"] == "partial"
    stored = store.get_facts(
        NAMESPACE,
        ISSUER_ID,
        acquired_by_ms=RETRIEVED_AT,
        publicly_available_by_ms=RETRIEVED_AT,
        principal_id="market-ingest:operator",
        scopes={"operator"},
    )
    assert len(stored) == result["count"]
    assert all(fact["issuer_id"] == ISSUER_ID for fact in stored)


# The following cases reproduce behaviour observed in live SEC Inline XBRL
# filings (2026-09-24 reconciliation of MSFT/ORCL/CRM/ADBE/NOW 10-K and 10-Q).
LIVE_SHAPED_FILING = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
      xmlns:ixt="http://www.xbrl.org/inlineXBRL/transformation/2022-02-16"
      xmlns:ixt-sec="http://www.sec.gov/inlineXBRL/transformation/2015-08-31"
      xmlns:xbrli="http://www.xbrl.org/2003/instance"
      xmlns:xbrldi="http://xbrl.org/2006/xbrldi"
      xmlns:us-gaap="http://fasb.org/us-gaap/2025"
      xmlns:acme="http://acme.example/20250331">
  <body><ix:header><ix:resources>
    <xbrli:context id="c-i"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000123456</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period></xbrli:context>
    <xbrli:context id="c-seg"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000123456</xbrli:identifier>
      <xbrli:segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">acme:CloudMember</xbrldi:explicitMember></xbrli:segment></xbrli:entity>
      <xbrli:period><xbrli:instant>2025-03-31</xbrli:instant></xbrli:period></xbrli:context>
    <xbrli:context id="c-zero"><xbrli:entity><xbrli:identifier scheme="http://www.sec.gov/CIK">0000123456</xbrli:identifier></xbrli:entity>
      <xbrli:period><xbrli:startDate>2025-02-01</xbrli:startDate><xbrli:endDate>2025-02-01</xbrli:endDate></xbrli:period></xbrli:context>
    <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
    <xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>
  </ix:resources></ix:header>
  <ix:nonFraction name="us-gaap:Assets" contextRef="c-i" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">1,234</ix:nonFraction>
  <ix:nonFraction name="us-gaap:Assets" contextRef="c-seg" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">400</ix:nonFraction>
  <ix:nonFraction name="us-gaap:UnrecognizedTaxBenefits" contextRef="c-i" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">22,760</ix:nonFraction>
  <ix:nonFraction name="us-gaap:UnrecognizedTaxBenefits" contextRef="c-i" unitRef="usd" decimals="-8" scale="9" format="ixt:num-dot-decimal">22.8</ix:nonFraction>
  <ix:nonFraction name="us-gaap:GoodwillImpairmentLoss" contextRef="c-zero" unitRef="usd" decimals="INF" format="ixt:fixed-zero">No</ix:nonFraction>
  <ix:nonFraction name="us-gaap:StockIssuedDuringPeriodSharesNewIssues" contextRef="c-i" unitRef="shares" decimals="INF" format="ixt-sec:numwordsen">twenty-two</ix:nonFraction>
  <ix:nonFraction name="acme:CustomBacklog" contextRef="c-i" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">77</ix:nonFraction>
  <ix:nonFraction name="us-gaap:Liabilities" contextRef="c-i" unitRef="usd" decimals="-6" scale="6" format="ixt:num-dot-decimal">(5)</ix:nonFraction>
</body></html>
"""


def _live_shaped_facts():
    return parse_inline_xbrl_facts(LIVE_SHAPED_FILING, accession=ACCESSION_Q1)


def test_inline_parser_accepts_xml_declaration_and_registry_4_transforms():
    result = _live_shaped_facts()

    assert result["diagnostics"] == []
    values = {
        (fact["concept"], fact["native_context_id"], fact["decimals"]): fact["value_lexical"]
        for fact in result["facts"]
    }
    assert values[("Assets", "c-i", "-6")] == "1234000000"
    assert values[("Assets", "c-seg", "-6")] == "400000000"
    assert values[("UnrecognizedTaxBenefits", "c-i", "-6")] == "22760000000"
    assert values[("UnrecognizedTaxBenefits", "c-i", "-8")] == "22800000000"
    assert values[("GoodwillImpairmentLoss", "c-zero", "INF")] == "0"
    assert values[("StockIssuedDuringPeriodSharesNewIssues", "c-i", "INF")] == "22"
    assert values[("Liabilities", "c-i", "-6")] == "-5000000"


def test_inline_parser_rejects_unparseable_number_words():
    filing = LIVE_SHAPED_FILING.replace("twenty-two", "a great many")

    result = parse_inline_xbrl_facts(filing, accession=ACCESSION_Q1)

    assert {item["code"] for item in result["diagnostics"]} == {
        "unsupported_numeric_transform"
    }


def _normalized(concept, period, value, unit="USD"):
    return {
        "filing_accession": ACCESSION_Q1,
        "taxonomy": "us-gaap",
        "concept": concept,
        "unit": unit,
        "period": period,
        "value_lexical": value,
        "context_id": f"companyfacts:{concept}",
        "mapping_status": "mapped",
    }


def test_reconciliation_matches_live_shaped_filing_without_false_disagreement():
    instant = {"kind": "instant", "instant_date": "2025-03-31"}
    normalized = {
        "facts": [
            _normalized("Assets", instant, "1234000000"),
            _normalized("Liabilities", instant, "-5000000"),
            # CompanyFacts keeps the precise duplicate; the filing also tags a
            # rounded "$22.8 billion" in prose.
            _normalized("UnrecognizedTaxBenefits", instant, "22760000000"),
            # CompanyFacts renders a zero-length duration as an instant.
            _normalized(
                "GoodwillImpairmentLoss",
                {"kind": "instant", "instant_date": "2025-02-01"},
                "0",
            ),
            _normalized(
                "StockIssuedDuringPeriodSharesNewIssues", instant, "22.0", unit="shares"
            ),
        ],
        "diagnostics": [],
    }

    result = reconcile_market_facts_to_filing(normalized, _live_shaped_facts()["facts"])

    assert result["matched"] == 5
    assert result["mismatched"] == 0
    assert result["missing_normalized"] == 0
    assert result["conflicting_contexts"] == 0
    assert result["value_status"] == "consistent"
    assert result["coverage"]["dimensional_filing_facts_not_compared"] == 1
    assert result["coverage"]["dimensional_concepts"] == {"us-gaap:Assets": 1}
    assert result["coverage"]["filing_facts_outside_companyfacts_taxonomies"] == {
        "acme": 1
    }
    assert result["coverage"]["consistent_duplicate_filing_facts"] == 1


def test_reconciliation_keeps_inconsistent_duplicates_as_conflicts():
    instant = {"kind": "instant", "instant_date": "2025-03-31"}
    # 23.1 billion at decimals=-8 is outside the rounding interval of 22,760m.
    filing = LIVE_SHAPED_FILING.replace(">22.8<", ">23.1<")
    normalized = {
        "facts": [_normalized("UnrecognizedTaxBenefits", instant, "22760000000")],
        "diagnostics": [],
    }

    result = reconcile_market_facts_to_filing(
        normalized,
        [
            fact
            for fact in parse_inline_xbrl_facts(filing, accession=ACCESSION_Q1)["facts"]
            if fact["concept"] == "UnrecognizedTaxBenefits"
        ],
    )

    assert result["mismatched"] == 1
    assert result["conflicting_contexts"] == 1
    assert result["value_status"] == "review_required"
    assert result["coverage"]["consistent_duplicate_filing_facts"] == 0


def test_reconciliation_reports_unsupported_mapping_once_per_concept():
    periods = [
        {"kind": "instant", "instant_date": f"2025-0{month}-28"} for month in (1, 2, 3)
    ]
    normalized = {
        "facts": [
            {**_normalized("CustomLikeTag", period, "1"), "mapping_status": "unmapped"}
            for period in periods
        ],
        "diagnostics": [],
    }

    result = reconcile_market_facts_to_filing(normalized, [])

    assert result["unsupported_mappings"] == 3
    assert [
        item["concept"]
        for item in result["diagnostics"]
        if item["code"] == "unsupported_accounting_mapping"
    ] == ["CustomLikeTag"]
