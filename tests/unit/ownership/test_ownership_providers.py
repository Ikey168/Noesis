"""Source contracts (O01), GLEIF Level 2 (O03), Companies House (O04), EDGAR (O05), BODS (O06), registers (O07)."""

import copy
import json
from pathlib import Path

import pytest

from src.ingestion.ownership_providers import (
    ADAPTERS,
    FIXTURE_SECRET,
    IDENTIFIER_OVERLAP,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    PROVIDER_HOSTS,
    REGISTRATION_AUTHORITIES,
    ch_nature,
    fixture_transport,
    load_bods,
    parse_bods,
    parse_ch_psc,
    parse_schedule_13dg,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from src.kb.ownership_bundle import load_source_pack
from src.kb.ownership_store import OwnershipError, OwnershipStore
from tests.unit.ownership import harness

ROOT = Path(__file__).resolve().parents[3]
REQUIRED = {"status", "documentation", "access", "authentication"}


def source(source_id):
    return copy.deepcopy(next(s for s in load_source_pack()["sources"] if s["source_id"] == source_id))


def adapter(source_id, *, secret=FIXTURE_SECRET, pages=None, fixture=None):
    item = source(source_id)
    fixture = fixture or json.loads((ROOT / item["fixture"]["path"]).read_text())
    return ADAPTERS[item["connector"]](item, transport=fixture_transport(pages or fixture["native_pages"]), secret=secret)


def drain(ad, operation="ownership"):
    records, cursor, receipts = [], None, []
    for _ in range(100):
        page = ad.fetch_page({"operation": operation}, cursor=cursor)
        records.extend(page.records)
        receipts.append(page.receipt)
        cursor = page.next_cursor
        if cursor is None:
            return records, receipts
    raise AssertionError("adapter did not terminate")


# ------------------------------------------------------------------ O01


def test_every_provider_has_a_recorded_contract_and_no_invented_access():
    assert set(PROVIDER_CONTRACTS) == {"gleif", "companies-house", "sec-edgar", "open-ownership", "opencorporates",
                                       "handelsregister", "unternehmensregister", "bris"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert REQUIRED <= set(contract), provider
        if contract["status"] == "implemented":
            assert {"rate_limits", "pagination", "cadence", "terms", "retained_evidence", "identifiers", "coverage",
                    "unavailable_fallback"} <= set(contract), provider
            assert PROVIDER_HOSTS[provider]
            assert "comprehensive" not in contract["coverage"].lower() and "selected" in contract["coverage"]
    for register in ("handelsregister", "unternehmensregister", "bris"):
        assert PROVIDER_CONTRACTS[register]["status"] == "not-implemented"
        assert PROVIDER_CONTRACTS[register]["reason"] and "cost" in PROVIDER_CONTRACTS[register]
        assert PROVIDER_HOSTS[register] == set()  # no scraper can even be configured
        assert LIVE_VERIFICATION[register]["status"] == "not-implemented"
    assert "aggregator" in PROVIDER_CONTRACTS["opencorporates"]["role"]
    assert "never an authoritative substitute" in PROVIDER_CONTRACTS["opencorporates"]["role"]


def test_identifier_overlap_names_the_existing_owners():
    assert set(IDENTIFIER_OVERLAP) == {"lei", "register_number", "cik", "ticker"}
    assert "src/kb/lei.py" in IDENTIFIER_OVERLAP["lei"]["owner"]
    assert "opencorporates" in " ".join(IDENTIFIER_OVERLAP["register_number"]["carried_by"])
    assert "market" in IDENTIFIER_OVERLAP["cik"]["owner"]


def test_live_verification_is_honest_and_the_pack_declares_it_per_source():
    for provider in ("gleif", "companies-house", "sec-edgar", "open-ownership"):
        assert LIVE_VERIFICATION[provider]["status"] == "unverified-live"
    pack = load_source_pack()
    # The optional competition feature's five sources (#2217) state theirs under "competition".
    assert {s["source_id"]: (s.get("ownership") or s["competition"])["live_verification"]
            for s in pack["sources"]} == {
        "gleif-level2": "unverified-live", "companies-house": "unverified-live",
        "sec-edgar-ownership": "unverified-live", "open-ownership-bods": "unverified-live",
        "ec-competition-cases": "unverified-live", "eu-state-aid-tam": "unverified-live",
        "uk-cma-cases": "unverified-live", "us-ftc-cases": "unverified-live", "us-doj-atr-cases": "unverified-live"}


def test_offline_conformance_of_every_pinned_fixture():
    result = SourcePackConformance(ROOT).offline(load_source_pack())
    # Four registry sources plus the competition feature's five (#2217).
    assert result["valid"] and result["coverage"] == {"configured": 9, "verified": 9}
    for name in ("gleif-level2.json", "companies-house.json", "sec-edgar.json", "open-ownership-bods.json"):
        assert harness.load(name)["authored"] is True


# ------------------------------------------------------------------ O03


def test_gleif_level2_projects_parents_exceptions_and_successor_from_the_lei_record():
    env = harness.Env().ready()
    before = env.conn.execute("SELECT count(*) FROM lei_revisions").fetchone()[0]
    store = OwnershipStore(env.conn)
    views = {v["record"]["record_key"]: v["record"] for v in store.records(harness.NS, principal_id="a", scopes=harness.SCOPES)}
    direct = views[f"gleif:parent:direct:{harness.UK}:{harness.HOLD}"]
    assert direct["assertion_kind"] == "direct_parent" and direct["relationship_status"] == "ACTIVE"
    assert direct["validity"] == {"from": "2016-04-06", "to": None, "from_status": "stated", "to_status": "open"}
    assert direct["source"]["provider"] == "gleif" and direct["source"]["revision"].startswith("lei-revision:")
    exceptions = {(r["reporting_exception"]["level"], r["reporting_exception"]["category"])
                  for r in views.values() if r["source"]["provider"] == "gleif" and r.get("assertion_kind") == "reporting_exception"}
    assert exceptions == {("direct", "NO_KNOWN_PERSON"), ("ultimate", "NO_KNOWN_PERSON")}
    succession = views[f"gleif:succession:{harness.TRADE}"]
    assert succession["related_entity_key"] == f"gleif:lei:{harness.UK}" and succession["date_status"] == "unknown"
    registration = views[f"gleif:registration:{harness.UK}"]
    assert registration["register"] == REGISTRATION_AUTHORITIES["RA000585"][2]
    assert "not a register extract" in registration["native"]["note"]
    # Level 1 records are reused from src.kb.lei, never copied into a second LEI store.
    again = store.project_gleif(harness.NS, [harness.UK, harness.HOLD], lei_namespace=harness.NS, run_id="again",
                                principal_id="a")
    assert again["counts"]["inserted"] == 0 and again["counts"]["revised"] == 0
    assert env.conn.execute("SELECT count(*) FROM lei_revisions").fetchone()[0] == before
    tables = {r[0] for r in env.conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}
    assert not {t for t in tables if t.startswith("ownership_") and "lei" in t}
    missing = store.project_gleif(harness.NS, ["5299000000000000000X"], lei_namespace=harness.NS, run_id="m", principal_id="a")
    assert missing["not_acquired"] == ["5299000000000000000X"]


def test_gleif_acquisition_runs_through_the_runtime_with_cursor_budget_and_receipt():
    env = harness.Env()
    env.install()
    result = env.acquire("gleif-only", source_ids=["gleif-level2"])
    receipt = result["receipts"]["entities"]
    assert receipt["status"] == "complete" and receipt["mode"]
    run = env.conn.execute("SELECT pages, cursor_end FROM source_pack_source_runs WHERE run_id=?", [receipt["run_id"]]).fetchone()
    assert run[0] == 20  # four LEIs x five parts, one runtime page each
    assert env.conn.execute("SELECT count(*) FROM source_pack_checkpoints WHERE pack_id='corporate-ownership'").fetchone()[0] == 1
    assert result["gleif_projection"]["projected"] == 15


# ------------------------------------------------------------------ O04


def test_companies_house_requires_a_key_and_retains_each_response():
    with pytest.raises(SourcePackError) as caught:
        adapter("companies-house", secret=None).fetch_page({"operation": "ownership"}, cursor=None)
    assert caught.value.code == "authentication_failed"  # checked before any request is sent
    records, receipts = drain(adapter("companies-house"))
    parts = [(r["ownership_part"]["subject"], r["ownership_part"]["part"]) for r in records]
    assert parts.count(("09990002", "filing-history")) == 2  # start_index pagination followed
    assert all(len(r["ownership_part"]["raw_sha256"]) == 64 for r in records)
    profile = next(r for r in records if r["ownership_part"]["part"] == "profile")
    assert profile["ownership_part"]["native"]["company_number"] == "09990002"  # the API response is the evidence
    assert {r["outcome"] for r in receipts} == {"returned", "none_reported"}
    unauthenticated = fixture_transport(harness.load("companies-house.json")["native_pages"])
    assert unauthenticated(url="https://api.company-information.service.gov.uk/company/09990002", params={},
                           headers={}, timeout=1)["status"] == 401
    with pytest.raises(SourcePackError) as caught:
        adapter("companies-house").fetch_page({"operation": "ownership", "parameters": {"company": "x"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    with pytest.raises(SourcePackError) as caught:
        adapter("companies-house").fetch_page({"operation": "ownership"}, cursor=json.dumps({"i": 0, "scope": "other"}))
    assert caught.value.code == "cursor_drift"


def test_psc_natures_map_to_kinds_with_original_text_and_bands():
    assert ch_nature("ownership-of-shares-25-to-50-percent") == ("shareholding", {
        "band": {"min": "25", "max": "50", "min_inclusive": False, "max_inclusive": True},
        "native": "ownership-of-shares-25-to-50-percent"})
    assert ch_nature("voting-rights-75-to-100-percent-as-trust")[0] == "voting_rights"
    assert ch_nature("right-to-appoint-and-remove-directors-as-firm") == ("appoint_directors", None)
    assert ch_nature("significant-influence-or-control") == ("significant_influence", None)
    assert ch_nature("part-right-to-share-surplus-assets-25-to-50-percent")[0] == "other_control"
    records, _ = drain(adapter("companies-house"))
    flat = [x for r in records for x in r["ownership_part"]["records"]]
    psc = [x for x in flat if x["record_key"].startswith("companies-house:psc:09990002:FIXPSC001:")]
    assert {x["native"]["nature_of_control"] for x in psc} == {
        "ownership-of-shares-75-to-100-percent", "voting-rights-75-to-100-percent", "right-to-appoint-and-remove-directors"}
    assert all(x["validity"] == {"from": "2016-04-06", "to": "2024-12-31", "from_status": "stated", "to_status": "stated"}
               for x in psc)
    statements = [x for x in flat if x["record_key"].startswith(("companies-house:psc-statement", "companies-house:exemption"))]
    assert {x["reporting_exception"]["category"] for x in statements} == {
        "no-individual-or-entity-with-signficant-control", "psc-exempt-as-trading-on-regulated-market"}
    officers = {x["officer"]["name"]: x for x in flat if x["kind"] == "officer_role"}
    assert officers["DOE, Alex"]["resigned_on"] == "2021-05-31"
    assert officers["POE, Kim"]["appointed_status"] == "unknown" and officers["POE, Kim"]["appointed_on"] is None
    foreign = next(x for x in flat if x["record_key"] == "companies-house:psc:09990002:FIXPSC002")
    assert foreign["jurisdiction"] == "NL" and foreign["identifiers"][0]["value"] == "99990003"
    with pytest.raises(SourcePackError):
        parse_ch_psc({"items": [{"name": "x"}]}, company="09990002", raw_sha="0" * 64)


# ------------------------------------------------------------------ O05


def test_edgar_requires_a_declared_user_agent_and_keeps_filing_references():
    with pytest.raises(SourcePackError) as caught:
        adapter("sec-edgar-ownership", secret="no contact").fetch_page({"operation": "ownership"}, cursor=None)
    assert caught.value.code == "authentication_failed"
    records, _ = drain(adapter("sec-edgar-ownership"))
    flat = [x for r in records for x in r["ownership_part"]["records"]]
    refs = {x["record_key"]: x for x in flat if x["kind"] == "filing_reference"}
    thirteen_g = refs["sec-edgar:filing:0001888001-25-000003"]
    assert thirteen_g["form_type"] == "SCHEDULE 13G" and thirteen_g["filing_date"] == "2025-04-10"
    assert thirteen_g["document_url"] == "https://www.sec.gov/Archives/edgar/data/9999101/000188800125000003/primary_doc.xml"
    assert "beneficial-ownership form" in thirteen_g["description"] and thirteen_g["parsed"] is False
    assert refs["sec-edgar:document:0001888001-25-000003"]["parsed"] is True
    assert refs["sec-edgar:document:0001888001-23-000002"]["parsed"] is False  # HTML cover page: reference only
    assert refs["sec-edgar:facts-filing:0009999101-25-000010"]["native"]["value"] == "50000000"
    entity = next(x for x in flat if x["record_key"] == "sec-edgar:cik:0009999101")
    assert entity["jurisdiction"] is None and entity["native"]["state_of_incorporation"] == "X0"
    assert {i["scheme"] for i in entity["identifiers"]} == {"sec-cik", "ticker"}


def test_schedule_13g_cover_page_becomes_assertions_with_the_filing_as_source():
    records, _ = drain(adapter("sec-edgar-ownership"))
    flat = [x for r in records for x in r["ownership_part"]["records"]]
    stakes = [x for x in flat if x["kind"] == "ownership_assertion"]
    assert {(x["holder"]["name"], x["share"]["exact"], x["share"]["shares"]) for x in stakes} == {
        ("Northwind Capital LP (fictional)", "8.2", "4100000"), ("Northwind GP LLC (fictional)", "8.2", "4100000")}
    assert all(x["source"]["statement_id"] == "0001888001-25-000003" and x["validity"]["from"] == "2025-03-31"
               and x["validity"]["to_status"] == "unknown" and "not a Noesis determination" in x["native"]["note"]
               for x in stakes)
    with pytest.raises(SourcePackError):
        parse_schedule_13dg(b"<edgarSubmission><issuerCik>0000000001</issuerCik></edgarSubmission>", cik="0009999101",
                            accession="0001888001-25-000003", form="SCHEDULE 13G", document_url="https://www.sec.gov/x",
                            raw_sha="0" * 64)
    unparsed = parse_schedule_13dg(b"%PDF-1.4 not xml", cik="0009999101", accession="0001888001-25-000009",
                                   form="SC 13D", document_url="https://www.sec.gov/x", raw_sha="0" * 64)
    assert len(unparsed) == 1 and unparsed[0]["parsed"] is False


# ------------------------------------------------------------------ O06


def test_bods_statements_keep_ids_publisher_source_type_and_annotations():
    statements = load_bods(json.dumps(harness.load("open-ownership-bods.json")["native_pages"][0]["body"]).encode())
    records, coverage = parse_bods(statements, url="https://bods-data.openownership.org/f.json", raw_sha="a" * 64)
    assert coverage["publishers"] == {"Open Ownership Register": {"GB Persons Of Significant Control Register": 8}}
    assert coverage["not_implemented"] == {"Denmark Central Business Register (Centrale Virksomhedsregister [CVR])": 1}
    assert coverage["unsupported_version"] == 1
    by_key = {r["record_key"]: r for r in records}
    stake = by_key["open-ownership:statement:oo-fixture-ooc-1:interest:0"]
    assert stake["source"]["statement_id"] == "oo-fixture-ooc-1" and stake["source"]["publisher"] == "Open Ownership Register"
    assert stake["source"]["source_type"] == "officialRegister"
    assert stake["native"]["annotations"][0]["motivation"] == "commenting"
    assert stake["share"]["band"] == {"min": "75", "max": "100", "min_inclusive": True, "max_inclusive": True}
    person = by_key["open-ownership:statement:oo-fixture-per-1"]
    assert person["kind"] == "person" and person["owner_scoped"] is True
    assert by_key["open-ownership:statement:oo-fixture-ooc-2:interest:0"]["owner_scoped"] is True
    assert by_key["open-ownership:statement:oo-fixture-ooc-2:interest:0"]["native"][
        "beneficial_ownership_or_control_as_stated"] is True  # as the source states; never inferred
    unspecified = by_key["open-ownership:statement:oo-fixture-ooc-3:unspecified"]
    assert unspecified["reporting_exception"]["category"] == "interested-party-exempt-from-disclosure"
    lines = "\n".join(json.dumps(s) for s in statements).encode()
    assert len(load_bods(lines)) == len(statements)
    with pytest.raises(SourcePackError):
        load_bods(b"{not json")


def test_bods_publisher_coverage_is_on_the_contract_and_person_statements_stay_owner_scoped():
    coverage = PROVIDER_CONTRACTS["open-ownership"]["publisher_coverage"]
    assert coverage["GB Persons Of Significant Control Register"]["status"] == "implemented"
    assert {v["status"] for k, v in coverage.items() if k != "GB Persons Of Significant Control Register"} == {"not-implemented"}
    assert all("update_cadence" in v and "license" in v for v in coverage.values() if v["status"] == "implemented")
    env = harness.Env().ready()
    store = OwnershipStore(env.conn)
    mine = store.records(harness.NS, principal_id=harness.PRINCIPAL, scopes=harness.SCOPES, kinds=("person",))
    theirs = store.records(harness.NS, principal_id="someone-else", scopes=harness.SCOPES, kinds=("person",))
    assert mine[0]["record"]["name"] == "Jordan Example (fictional)"
    assert theirs[0]["redacted"] is True and "name" not in theirs[0]["record"]
    reviewer = store.records(harness.NS, principal_id="someone-else", scopes=harness.REVIEW_SCOPES, kinds=("person",))
    assert reviewer[0]["record"]["name"] == "Jordan Example (fictional)"


# ------------------------------------------------------------------ O07


def test_register_documents_are_recorded_with_their_official_reference_never_fetched():
    env = harness.Env()
    store = OwnershipStore(env.conn)
    document = {"kind": "AD extract (Aktueller Abdruck)", "reference": "Amtsgericht Charlottenburg HRB 999999 B, AD 2026-09-01",
                "sha256": "b" * 64, "retrieved_on": "2026-09-01"}
    result = store.record_register_document(harness.NS, provider="handelsregister", register="Amtsgericht Charlottenburg",
                                            number="HRB 999999 B", jurisdiction="DE-BE", document=document,
                                            name="Beispiel GmbH (fictional)", principal_id=harness.PRINCIPAL,
                                            scopes=harness.SCOPES)
    registration = next(r["record"] for r in result["records"] if r["record"]["kind"] == "registration")
    assert registration["document_reference"]["official"] is True
    assert registration["document_reference"]["reference"].startswith("Amtsgericht Charlottenburg")
    assert registration["source"]["source_type"] == "user-supplied official document"
    with pytest.raises(OwnershipError):
        store.record_register_document(harness.NS, provider="opencorporates", register="x", number="1", jurisdiction="DE",
                                       document=document, principal_id=harness.PRINCIPAL, scopes=harness.SCOPES)
    with pytest.raises(OwnershipError):
        store.record_register_document(harness.NS, provider="bris", register="x", number="1", jurisdiction="DE",
                                       document={"reference": "no digest"}, principal_id=harness.PRINCIPAL,
                                       scopes=harness.SCOPES)
    with pytest.raises(OwnershipError):
        store.record_register_document(harness.NS, provider="bris", register="x", number="1", jurisdiction="DE",
                                       document=document, principal_id=harness.PRINCIPAL, scopes=harness.READ_ONLY)
