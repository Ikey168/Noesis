"""Source contracts (P01) and TED / UK OCDS / SAM.gov acquisition adapters (P04-P06)."""

import copy
import json

import pytest

from src.ingestion.procurement_providers import (
    EFORMS_MAPPING,
    FIXTURE_SECRET,
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    PROVIDER_HOSTS,
    OcdsAdapter,
    SamAdapter,
    TedAdapter,
    criterion_rule,
    deadline,
    fixture_transport,
    parse_ocds_package,
    parse_sam_search,
    parse_ted_search,
)
from src.ingestion.source_packs import SourcePackConformance, SourcePackError
from tests.unit.procurement.harness import FIXTURES, ROOT, Env, manifest

TED = json.loads((ROOT / "tests/fixtures/source_packs/procurement-ted.json").read_text())
FTS = json.loads((ROOT / "tests/fixtures/source_packs/procurement-uk-fts.json").read_text())
CF = json.loads((ROOT / "tests/fixtures/source_packs/procurement-uk-cf.json").read_text())
SAM = json.loads((ROOT / "tests/fixtures/source_packs/procurement-sam.json").read_text())
TED2 = json.loads((FIXTURES / "ted-round2.json").read_text())
FTS2 = json.loads((FIXTURES / "uk-fts-round2.json").read_text())


def source(source_id):
    return copy.deepcopy(next(s for s in manifest()["sources"] if s["source_id"] == source_id))


def ted_records(fixture=TED):
    return [r for page in fixture["native_pages"] for r in parse_ted_search(page["body"])["records"]]


# ------------------------------------------------------------------ P01


def test_every_provider_has_an_audited_access_contract_or_a_not_implemented_reason():
    assert set(PROVIDER_CONTRACTS) == {"ted", "uk-fts", "uk-cf", "sam-gov", "service-bund", "berlin-vergabe", "opentender"}
    for provider, contract in PROVIDER_CONTRACTS.items():
        if contract["status"] == "implemented":
            for field in ("documentation", "access", "authentication", "terms", "rate_limits", "pagination", "cadence",
                          "identifiers", "schema_versions", "cpv_version", "cross_references", "retained_evidence",
                          "coverage", "unavailable_fallback"):
                assert contract[field], (provider, field)
            assert PROVIDER_HOSTS[provider]
        else:
            assert contract["status"] == "not-implemented" and contract["reason"] and contract["fallback"]
    assert "API key" in PROVIDER_CONTRACTS["sam-gov"]["authentication"]
    assert "NAICS" in PROVIDER_CONTRACTS["sam-gov"]["cpv_version"]


def test_live_verification_never_claims_live_coverage():
    assert {v["status"] for p, v in LIVE_VERIFICATION.items() if PROVIDER_CONTRACTS[p]["status"] == "implemented"} == {"unverified-live"}
    assert {v["status"] for p, v in LIVE_VERIFICATION.items() if PROVIDER_CONTRACTS[p]["status"] != "implemented"} == {"not-implemented"}


def test_source_pack_is_valid_bounded_and_its_authored_fixtures_replay_deterministically():
    value = manifest()
    assert {s["connector"] for s in value["sources"]} == {"ted", "ocds", "sam-gov"}
    sam = next(s for s in value["sources"] if s["connector"] == "sam-gov")
    assert sam["auth"] == {"kind": "required-secret", "secret_ref": "NOESIS_SAM_API_KEY"}
    result = SourcePackConformance(ROOT).offline(value)
    assert result["valid"], result
    assert {s["source_id"]: s["records"] for s in result["sources"]} == {
        "ted-notices": 6, "uk-fts-ocds": 3, "uk-contracts-finder-ocds": 1, "sam-opportunities": 3}
    for fixture in (TED, FTS, CF, SAM, TED2, FTS2):
        assert fixture["authored"] is True


# ------------------------------------------------------------------ P04 TED


def test_ted_eforms_fields_map_to_records_with_language_tags_and_locators():
    records = {r["notice_id"]: r for r in ted_records()}
    notice = records["00612345-2026"]
    assert notice["stage"] == "contract-notice" and notice["procedure_id"].endswith("0001")
    assert notice["titles"] == {"en": "IT service desk and cloud migration for the district office",
                                "de": "IT-Servicedesk und Cloud-Migration für das Bezirksamt"}
    assert notice["buyer"]["names"]["de"] == "Bezirksamt Fixture-Mitte von Berlin" and notice["buyer"]["country"] == "DE"
    assert [lot["lot_id"] for lot in notice["lots"]] == ["LOT-0001", "LOT-0002"]
    assert notice["lots"][0]["estimated_value"]["vat"] == "excluded" and notice["lots"][0]["estimated_value"]["kind"] == "estimated"
    submission = next(d for d in notice["deadlines"] if d["kind"] == "submission" and d["lot_id"] == "LOT-0001")
    assert submission["text"] == "2026-11-03+01:00 12:00:00+01:00" and submission["instant"] == "2026-11-03T12:00:00+01:00"
    assert "+01:00" in submission["timezone"]
    criterion = next(r for r in notice["requirements"] if r["requirement_id"] == "ted:LOT-0001:criterion:1")
    assert criterion["locator"]["field"] == "BT-750-Lot" and criterion["language"] == "en" and criterion["lot_ids"] == ["LOT-0001"]
    assert criterion["machine_rule"] == {"fact": "supplier.annual_turnover.EUR", "op": "gte", "value": "800000"}
    unparsed = next(r for r in notice["requirements"] if r["requirement_id"] == "ted:LOT-0002:criterion:3")
    assert "machine_rule" not in unparsed  # security clearance: no rule is invented
    assert {"BT-04-procedure", "BT-758-notice", "BT-750-Lot"} <= set(EFORMS_MAPPING)


def test_ted_corrigendum_award_and_cancellation_attach_to_their_procedure():
    first = {r["notice_id"]: r for r in ted_records()}
    second = {r["notice_id"]: r for r in ted_records(TED2)}
    corrigendum = second["00650001-2026"]
    assert corrigendum["stage"] == "corrigendum" and corrigendum["procedure_id"] == first["00612345-2026"]["procedure_id"]
    assert corrigendum["changes"]["changes_notice_id"].endswith("b001-01") and corrigendum["changes"]["description"]["de"]
    award = first["00587654-2024"]
    assert award["stage"] == "award" and not award.get("deadlines")
    assert award["awards"][0]["awarded_value"] == {**award["awards"][0]["awarded_value"], "amount": "380000", "kind": "awarded", "vat": "excluded"}
    assert award["awards"][0]["suppliers"][0]["identifiers"] == [{"scheme": "national", "id": "HRB 000777 B"}]
    cancelled = second["00650002-2026"]
    assert cancelled["stage"] == "cancellation" and cancelled["procedure_id"] == first["00620000-2026"]["procedure_id"]
    assert first["00613000-2026"]["stage"] == "prior-information"


def test_ted_parser_fails_closed_on_shape_drift():
    body = copy.deepcopy(TED["native_pages"][0]["body"])
    body["notices"][0]["BT-27-Lot"] = ["400000"]  # no longer aligned with BT-137-Lot
    with pytest.raises(SourcePackError) as caught:
        parse_ted_search(body)
    assert caught.value.code == "schema_drift"
    with pytest.raises(SourcePackError):
        parse_ted_search({"results": []})
    body = copy.deepcopy(TED["native_pages"][0]["body"])
    body["timedOut"] = True
    with pytest.raises(SourcePackError) as caught:
        parse_ted_search(body)
    assert caught.value.code == "source_timeout"


def test_ted_adapter_pages_by_iteration_token_and_records_injected_execution():
    adapter = TedAdapter(source("ted-notices"), transport=fixture_transport(TED["native_pages"]))
    page = adapter.fetch_page({"operation": "notices", "parameters": {}}, cursor=None)
    assert len(page.records) == 3 and page.next_cursor and page.receipt["execution"] == "injected"
    assert page.receipt["response_sha256"] and page.records[0]["procurement_record"]["contract"] == "noesis-procurement-record-v1"
    second = adapter.fetch_page({"operation": "notices", "parameters": {}}, cursor=page.next_cursor)
    assert len(second.records) == 3 and second.next_cursor is None and second.receipt["complete"]
    with pytest.raises(SourcePackError) as caught:
        adapter.fetch_page({"operation": "notices", "parameters": {"query": "anything"}}, cursor=None)
    assert caught.value.code == "parameter_forbidden"
    bad = source("ted-notices")
    bad["endpoint"] = "https://example.org/v3"
    with pytest.raises(SourcePackError):
        TedAdapter(bad)


# ------------------------------------------------------------------ P05 UK OCDS


def test_ocds_releases_keep_the_ocid_across_stages_and_amendments_become_revisions():
    records = [r for page in FTS["native_pages"] for r in parse_ocds_package(page["body"], "uk-fts")["records"]]
    tender = next(r for r in records if r["stage"] == "contract-notice")
    assert tender["procedure_id"] == "ocds-h6vhtk-0fx001" and tender["estimated_value"]["vat"] == "excluded"
    assert {c["code"] for c in tender["classifications"]} == {"72253000", "72415000"}
    lot2 = [r for r in tender["requirements"] if r.get("lot_ids") == ["2"]]
    assert {r["machine_rule"]["fact"] for r in lot2} == {"supplier.annual_turnover.GBP", "supplier.certifications"}
    award = next(r for r in records if r["stage"] == "award")
    assert award["procedure_id"] == "ocds-h6vhtk-0fx900" and award["contracts"][0]["award_id"] == "AW-1"
    assert {"scheme": "LEI", "id": "213800FIXTUREDIGI007", "native_scheme": "XI-LEI"} in award["awards"][0]["suppliers"][0]["identifiers"]
    modification = next(r for r in records if r["stage"] == "modification")
    assert modification["contracts"][0]["amendments"][0]["rationale"] == "Additional service hours"
    amended = parse_ocds_package(FTS2["native_pages"][0]["body"], "uk-fts")["records"][0]
    assert amended["stage"] == "corrigendum" and amended["procedure_id"] == tender["procedure_id"]
    cf = parse_ocds_package(CF["native_pages"][0]["body"], "uk-cf")["records"][0]
    assert cf["estimated_value"]["vat"] == "unknown" and "estimated_value.vat" in cf["unknowns"]


def test_ocds_adapter_follows_links_next_only_on_the_declared_host():
    adapter = OcdsAdapter(source("uk-fts-ocds"), transport=fixture_transport(FTS["native_pages"]))
    page = adapter.fetch_page({"operation": "notices", "parameters": {}}, cursor=None)
    assert len(page.records) == 2 and page.next_cursor
    pages = copy.deepcopy(FTS["native_pages"])
    pages[0]["body"]["links"]["next"] = "https://evil.example/ocds?cursor=2"
    hostile = OcdsAdapter(source("uk-fts-ocds"), transport=fixture_transport(pages))
    cursor = hostile.fetch_page({"operation": "notices", "parameters": {}}, cursor=None).next_cursor
    with pytest.raises(SourcePackError) as caught:
        hostile.fetch_page({"operation": "notices", "parameters": {}}, cursor=cursor)
    assert caught.value.code == "network_policy"


# ------------------------------------------------------------------ P06 SAM.gov and German portals


def test_sam_opportunities_keep_naics_psc_and_set_aside_codes():
    records = parse_sam_search(SAM["native_pages"][0]["body"])["records"]
    solicitation = next(r for r in records if r["stage"] == "contract-notice")
    assert {(c["scheme"], c["code"]) for c in solicitation["classifications"]} == {("NAICS", "541513"), ("PSC", "D399")}
    assert solicitation["set_aside"]["code"] == "SBA" and solicitation["set_aside"]["statuses"] == ["small-business"]
    assert solicitation["deadlines"][0]["text"] == "2026-10-28T14:00:00-04:00"
    assert solicitation["deadlines"][0]["instant"] == "2026-10-28T14:00:00-04:00"
    assert next(r for r in records if r["stage"] == "prior-information")["notice_id"].startswith("fx0002")
    award = next(r for r in records if r["stage"] == "award")
    assert award["awards"][0]["awarded_value"]["vat"] == "unknown" and award["awards"][0]["suppliers"][0]["identifiers"][0]["scheme"] == "UEI"


def test_sam_requires_the_secret_and_never_records_the_key():
    with pytest.raises(SourcePackError) as caught:
        SamAdapter(source("sam-opportunities"), transport=fixture_transport(SAM["native_pages"])).fetch_page(
            {"operation": "notices", "parameters": {}}, cursor=None)
    assert caught.value.code == "credential_missing"
    adapter = SamAdapter(source("sam-opportunities"), transport=fixture_transport(SAM["native_pages"]), secret=FIXTURE_SECRET)
    page = adapter.fetch_page({"operation": "notices", "parameters": {}}, cursor=None)
    assert len(page.records) == 3 and page.next_cursor is None
    assert FIXTURE_SECRET not in json.dumps(page.receipt) and FIXTURE_SECRET not in json.dumps(adapter.describe())
    refused = SamAdapter(source("sam-opportunities"), transport=lambda **_: {"status": 403, "headers": {}, "content": b""},
                         secret=FIXTURE_SECRET)
    with pytest.raises(SourcePackError) as caught:
        refused.fetch_page({"operation": "notices", "parameters": {}}, cursor=None)
    assert caught.value.code == "authentication_failed"


def test_german_portals_are_not_implemented_and_never_scraped():
    for provider in ("service-bund", "berlin-vergabe"):
        assert PROVIDER_CONTRACTS[provider]["status"] == "not-implemented"
        assert "not scraped" in PROVIDER_CONTRACTS[provider]["reason"]
        assert provider not in PROVIDER_HOSTS
    assert {s["connector"] for s in manifest()["sources"]} <= {"ted", "ocds", "sam-gov"}


# ------------------------------------------------------------------ helpers


def test_criterion_rules_are_extracted_only_when_unambiguous():
    assert criterion_rule("economic-financial", "Minimum annual turnover of GBP 1,500,000.")["value"] == "1500000"
    assert criterion_rule("economic-financial", "Minimum turnover: 2 million EUR")["value"] == "2000000"
    assert criterion_rule("technical-professional", "At least three references.")["value"] == 3
    assert criterion_rule("certification", "ISO 9001 or equivalent") is None
    assert criterion_rule("technical-professional", "ISO 9001 and ISO/IEC 27001 are required.") is None
    assert criterion_rule("economic-financial", "Adequate financial standing.") is None


def test_deadlines_never_invent_an_offset():
    no_offset = deadline("submission", "2026-11-03", None)
    assert no_offset["instant"] is None and no_offset["date"] == "2026-11-03"
    with pytest.raises(SourcePackError):
        deadline("submission", "3 November 2026")


def test_runtime_run_projects_every_provider_and_leaves_evidence(tmp_path):
    env = Env()
    receipt = env.acquire()
    assert receipt["status"] == "complete"
    assert {s["source_id"]: s["counts"]["fetched"] for s in receipt["sources"]} == {
        "ted-notices": 6, "uk-fts-ocds": 3, "uk-contracts-finder-ocds": 1, "sam-opportunities": 3}
    assert env.conn.execute("SELECT count(*) FROM documents").fetchone()[0] >= 13
    assert env.conn.execute("SELECT count(*) FROM source_pack_watermarks WHERE pack_id='procurement'").fetchone()[0] == 1
