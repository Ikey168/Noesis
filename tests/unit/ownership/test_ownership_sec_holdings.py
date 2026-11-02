"""noesis-ownership-record-v2 and the sec-edgar-holdings parsers and adapter (#2883). Authored fixtures only."""

import copy
import json
from pathlib import Path

import duckdb
import jsonschema
import pytest

from src.ingestion.ownership_providers import PROVIDER_CONTRACTS
from src.ingestion.ownership_sec_holdings import (
    ADAPTERS,
    FIXTURE_SECRET,
    SEC_HOLDINGS_CONTRACT,
    fixture_transport,
    parse_13dg,
    parse_13f_cover,
    parse_13f_table,
    records_13dg,
    records_13f,
)
from src.ingestion.source_packs import SourcePackError
from src.kb.ownership_bundle import load_source_pack
from src.kb.ownership_records import (
    CONTRACT,
    CONTRACT_V2,
    OwnershipRecordError,
    record,
    record_v2,
    register_schemas,
    schema,
    validate_record,
)
from tests.unit.ownership import harness

ROOT = Path(__file__).resolve().parents[3]
SOURCE = {"provider": "sec-edgar", "provider_record_id": "0009999501-25-000001:information_table"}
FILING = {"accession_number": "0009999501-25-000001", "form_type": "13F-HR", "period_of_report": "2025-03-31",
          "amendment": {"is_amendment": False, "chain_key": "13f:manager-cik:0009999501:period:2025-03-31"}}
POSITION = {"cusip": "99990A101", "amount": "120000", "amount_type": "SH", "value": "3600000", "value_unit": "USD",
            "investment_discretion": "SOLE", "voting_authority": {"sole": "120000", "shared": "0", "none": "0"}}


def holding(**overrides):
    fields = {"subject_key": "issuer", "assertion_kind": "reported_holding",
              "holder": {"key": "manager", "name": "Manager", "kind": "entity"}, "filing": FILING, "position": POSITION}
    fields.update(overrides)
    return record_v2("ownership_assertion", "sec-edgar:13f:x:row:0", SOURCE, **fields)


def pages():
    return harness.load("sec-edgar-holdings.json")["native_pages"]


def body(fragment):
    return next(p["body"] for p in pages() if p["request"].endswith(fragment)).encode()


def source():
    return copy.deepcopy(next(s for s in load_source_pack()["sources"] if s["source_id"] == "sec-edgar-holdings"))


def drain(adapter):
    records, cursor = [], None
    for _ in range(100):
        page = adapter.fetch_page({"operation": "ownership", "limit": 2}, cursor=cursor)
        records.extend(page.records)
        cursor = page.next_cursor
        if cursor is None:
            return records
    raise AssertionError("adapter did not terminate")


# ------------------------------------------------------------- contract v2


def test_v2_is_a_new_contract_version_and_v1_is_unchanged():
    assert validate_record(holding())["contract"] == CONTRACT_V2
    assert holding()["unknowns"] == ["validity.from", "validity.to"]
    v1 = record("ownership_assertion", "k", SOURCE, subject_key="s", assertion_kind="shareholding",
                holder={"key": "h", "kind": "entity"}, share={"exact": "5"})
    assert v1["contract"] == CONTRACT
    with pytest.raises(OwnershipRecordError):  # v2 fields are never accepted on a v1 record
        validate_record({**v1, "filing": FILING})
    with pytest.raises(OwnershipRecordError):  # nor is the v2 assertion kind
        validate_record({**v1, "assertion_kind": "reported_holding", "share": None})
    v1_schema, v2_schema = schema("ownership-record"), schema("ownership-record", "2.0.0")
    assert v1_schema["properties"]["contract"]["const"] == CONTRACT
    assert "reported_holding" not in v1_schema["properties"]["assertion_kind"]["enum"]
    assert set(v1_schema["properties"]) < set(v2_schema["properties"])
    jsonschema.Draft7Validator(v2_schema).validate(holding())


@pytest.mark.parametrize(("change", "message"), [
    ({"position": None}, "carries the position"),
    ({"share": {"exact": "5"}}, "not a percentage"),
    ({"position": {**POSITION, "cusip": "short"}}, "CUSIP"),
    ({"position": {**POSITION, "amount": 120000}}, "decimal string"),
    ({"position": {**POSITION, "put_call": "Straddle"}}, "put_call"),
    ({"filing": {**FILING, "accession_number": "123"}}, "accession"),
    ({"filing": {**FILING, "amendment": {"is_amendment": False, "type": "restatement", "chain_key": "c"}}},
     "original filing"),
    ({"powers": {"sole_voting": "1"}}, "powers"),
])
def test_v2_assertions_fail_closed(change, message):
    with pytest.raises(OwnershipRecordError) as caught:
        holding(**change)
    assert message in str(caught.value)


def test_natural_person_policy_is_owner_scoped_unless_person_research_mode(monkeypatch):
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)
    person = {"name": "A Person", "identifiers": []}
    with pytest.raises(OwnershipRecordError) as caught:
        record_v2("person", "p", SOURCE, person_policy="ownership-person-owner-scoped-v1", **person)
    assert caught.value.code == "minimisation_violation"
    assert record_v2("person", "p", SOURCE, person_policy="ownership-person-owner-scoped-v1", owner_scoped=True,
                     **person)["owner_scoped"] is True
    with pytest.raises(OwnershipRecordError) as caught:
        record_v2("person", "p", SOURCE, person_policy="ownership-person-research-v1", **person)
    assert caught.value.code == "minimisation_violation"
    with pytest.raises(OwnershipRecordError):
        record_v2("person", "p", SOURCE, **person)  # a v2 person statement always states its policy
    named = {"member_of_group": "a", "members": [{"key": "k", "name": "A Person", "kind": "person", "withheld": False}]}
    stake = {"subject_key": "issuer", "assertion_kind": "shareholding", "share": {"exact": "7.4"},
             "holder": {"key": "fund", "name": "Fund", "kind": "entity"}, "group": named}
    with pytest.raises(OwnershipRecordError) as caught:
        record_v2("ownership_assertion", "s", SOURCE, **stake)
    assert caught.value.code == "minimisation_violation"
    monkeypatch.setenv("NOESIS_PERSON_RESEARCH", "1")
    assert record_v2("ownership_assertion", "s", SOURCE, **stake)["group"]["members"][0]["name"] == "A Person"
    assert record_v2("person", "p", SOURCE, person_policy="ownership-person-research-v1", **person)


def test_v2_schema_is_registered_beside_v1_and_records_validate_by_their_contract():
    conn = duckdb.connect()
    scopes = {"knowledge:schema:register", "knowledge:schema:read", "knowledge:schema:validate"}
    modules = register_schemas(conn, principal_id="ownership-service", scopes=scopes)
    assert sorted(m["semantic_version"] for m in modules if m["name"] == "ownership-record") == ["1.0.0", "2.0.0"]
    from src.kb.schema_registry import SchemaRegistry

    checked = SchemaRegistry(conn).validate_instance({"kind": "schema", "name": "ownership-record", "version": "2.0.0"},
                                                     holding(), scopes=scopes)
    assert checked["valid"], checked["errors"]


# ------------------------------------------------------------------ parsers


def test_13f_cover_and_table_parse_as_stated():
    cover = parse_13f_cover(body("000999950125000002/primary_doc.xml"), cik="0009999501",
                            accession="0009999501-25-000002", form="13F-HR/A")
    assert (cover["period"], cover["amendment_number"], cover["amendment_type"], cover["table_entry_total"]) == (
        "2025-03-31", "1", "restatement", "2")
    rows = parse_13f_table(body("000999950125000001/infotable.xml"))
    put = rows[1]
    assert (put["put_call"], put["investment_discretion"], put["other_managers"], put["voting_authority"]) == (
        "Put", "DFND", ["1"], {"sole": "0", "shared": "50000", "none": "0"})
    assert (rows[3]["amount_type"], rows[3]["cusip"]) == ("PRN", "99990BAA7")
    built = records_13f(cover, rows, cik="0009999501", accession="0009999501-25-000001", form="13F-HR",
                        primary_url="https://www.sec.gov/p", table_url="https://www.sec.gov/t", primary_sha="a" * 64,
                        table_sha="b" * 64, filing_date="2022-11-14")
    assert {r["position"]["value_unit"] for r in built if r["kind"] == "ownership_assertion"} == {"USD-thousands"}
    issuers = [r for r in built if r["record_key"].endswith(":issuer:99990A")]
    assert len(issuers) == 1 and issuers[0]["identifiers"] == [
        {"scheme": "cusip-issuer", "value": "99990A", "authority": "CUSIP issuer number as reported in Form 13F"}]
    undated = records_13f({**cover, "period": "2022-09-30"}, rows[:1], cik="0009999501", accession="0009999501-25-000001",
                          form="13F-HR", primary_url="https://www.sec.gov/p", table_url="https://www.sec.gov/t",
                          primary_sha="a" * 64, table_sha="b" * 64, filing_date=None)
    assert "position.value_unit" in undated[-1]["unknowns"]  # unit not inferable without the filing date


@pytest.mark.parametrize(("raw", "code"), [
    ((b"<informationTable><infoTable><cusip>99990A101</cusip><shrsOrPrnAmt><sshPrnamt>1</sshPrnamt>"
      b"<sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt><putCall>Straddle</putCall></infoTable></informationTable>"),
     "schema_drift"),
    (b"<informationTable><infoTable><cusip>bad</cusip></infoTable></informationTable>", "schema_drift"),
    (b'<!DOCTYPE x [<!ENTITY a "aaaa">]><informationTable/>', "schema_drift"),
    (b"<edgarSubmission/>", "schema_drift"),
])
def test_13f_tables_fail_closed(raw, code):
    with pytest.raises(SourcePackError) as caught:
        parse_13f_table(raw)
    assert caught.value.code == code


def test_13f_cover_contradictions_fail_closed():
    raw = body("000999950125000001/primary_doc.xml")
    with pytest.raises(SourcePackError):
        parse_13f_cover(raw, cik="0009999502", accession="0009999501-25-000001", form="13F-HR")  # another filer
    with pytest.raises(SourcePackError):
        parse_13f_cover(raw, cik="0009999501", accession="0009999501-25-000001", form="13F-HR/A")  # form mismatch


def test_13d_cover_pages_with_group_members_and_13g_amendment(monkeypatch):
    monkeypatch.delenv("NOESIS_PERSON_RESEARCH", raising=False)
    parsed = parse_13dg(body("000999960125000001/primary_doc.xml"), form="SCHEDULE 13D")
    assert parsed["issuer_cik"] == "0009999402" and parsed["event_date"] == "2025-03-28"
    assert [p["member_of_group"] for p in parsed["persons"]] == ["a", "a", "a", "a"]
    assert [p["natural_person"] for p in parsed["persons"]] == [False, False, True, False]
    built = records_13dg(parsed, accession="0009999601-25-000001", form="SCHEDULE 13D", url="https://www.sec.gov/d",
                         raw_sha="c" * 64, filing_date="2025-04-07")
    person = next(r for r in built if r["kind"] == "person")
    assert person["owner_scoped"] is True and person["person_policy"] == "ownership-person-owner-scoped-v1"
    reference = built[-1]
    assert reference["kind"] == "filing_reference" and "Dana" not in json.dumps(reference)
    trust = next(r for r in built if r["record_key"].endswith("reporting-person:3:stake"))
    assert trust["filing"]["amendment"]["chain_basis"].startswith("issuer CIK and reporting-person name")
    amended = parse_13dg(body("000999970125000002/primary_doc.xml"), form="SCHEDULE 13G/A")
    assert (amended["amendment_number"], amended["amends_accession"], amended["event_date"],
            amended["five_percent_or_less"]) == ("1", "0009999701-25-000001", "2025-06-30", True)
    assert amended["persons"][0]["powers"]["sole_voting"] == "2100000"  # nested 13G power block
    with pytest.raises(SourcePackError):
        parse_13dg(body("000999970125000002/primary_doc.xml"), form="SCHEDULE 13D/A")
    assert parse_13dg(b"<html><body>SC 13D</body></html>", form="SCHEDULE 13D") is None
    unparsed = records_13dg(None, accession="0009999601-25-000009", form="SCHEDULE 13D", url="https://www.sec.gov/x",
                            raw_sha="d" * 64, filing_date=None)
    assert len(unparsed) == 1 and unparsed[0]["parsed"] is False
    with pytest.raises(SourcePackError):
        parse_13dg(b"<edgarSubmission><reportingPersonInfo><reportingPersonName>x</reportingPersonName>"
                   b"</reportingPersonInfo></edgarSubmission>", form="SCHEDULE 13D")  # no issuer CIK


# ------------------------------------------------------------------ adapter


def test_contract_is_recorded_and_linked_from_the_sec_provider():
    assert SEC_HOLDINGS_CONTRACT["status"] == "implemented" and "selected" in SEC_HOLDINGS_CONTRACT["coverage"]
    assert "sec-edgar-holdings" in PROVIDER_CONTRACTS["sec-edgar"]["structured_filings"]


def test_adapter_requires_a_declared_user_agent_and_bounded_selection():
    item = source()
    with pytest.raises(SourcePackError) as caught:
        ADAPTERS["sec-edgar-holdings"](item, transport=fixture_transport(pages()), secret="no contact").fetch_page(
            {"operation": "ownership"}, cursor=None)
    assert caught.value.code == "authentication_failed"
    for broken in ({"information_table": None}, {"form": "SC 13D"}, {"accession": "1"}, {"filing_date": "05/12/2025"}):
        bad = copy.deepcopy(item)
        bad["ownership"]["filings"][0].update(broken)
        with pytest.raises(SourcePackError):
            ADAPTERS["sec-edgar-holdings"](bad, transport=fixture_transport(pages()), secret=FIXTURE_SECRET)
    bad = copy.deepcopy(item)
    bad["ownership"]["filings"] = bad["ownership"]["filings"] * 4
    with pytest.raises(SourcePackError):
        ADAPTERS["sec-edgar-holdings"](bad, transport=fixture_transport(pages()), secret=FIXTURE_SECRET)


def test_live_requests_are_paced_identified_and_each_document_fetched_once():
    calls, sleeps = [], []
    replay = fixture_transport(pages())

    def transport(*, url, params, headers, timeout):
        calls.append((url, headers.get("User-Agent")))
        return replay(url=url, params=params, headers=headers, timeout=timeout)

    adapter = ADAPTERS["sec-edgar-holdings"](source(), transport=transport, secret="research@example.org",
                                             sleep=sleeps.append)
    adapter.live = True  # exercise the fair-access pacing without a network
    envelopes = drain(adapter)
    urls = [u for u, _ in calls]
    assert len(urls) == len(set(urls)) == 11  # 4 x (cover + table) + 3 schedules, each once although tables page
    assert all(agent == "Noesis corporate-ownership research research@example.org" for _, agent in calls)
    assert sleeps and all(0 < s <= 0.12 for s in sleeps)
    first = [e for e in envelopes if e["ownership_part"]["subject"] == "0009999501:0009999501-25-000001"]
    assert [e["ownership_part"]["coverage"]["offset"] for e in first] == [0, 2]  # four rows, two per page
    rows = [r for e in first for r in e["ownership_part"]["records"] if r["kind"] == "ownership_assertion"]
    assert [r["record_key"].rsplit(":", 1)[1] for r in rows] == ["0", "1", "2", "3"]


def test_row_count_that_differs_from_the_stated_total_fails_the_source():
    broken = [dict(p) for p in pages()]
    for page in broken:
        if page["request"].endswith("000999950125000003/primary_doc.xml"):
            page["body"] = page["body"].replace("<tableEntryTotal>1<", "<tableEntryTotal>2<")
    adapter = ADAPTERS["sec-edgar-holdings"](source(), transport=fixture_transport(broken), secret=FIXTURE_SECRET)
    with pytest.raises(SourcePackError) as caught:
        drain(adapter)
    assert caught.value.code == "schema_drift" and "states 2" in str(caught.value)
