"""Provider contracts and bounded acquisition (#1762, #1765–#1768).

All payloads are authored fixtures served through the real DurableHTTP path.
"""

import json

import pytest

from src.ingestion.funding_providers import (
    LIVE_VERIFICATION,
    PROVIDER_CONTRACTS,
    PROVIDER_HOSTS,
    parse_eu_search,
    parse_exist_programme,
    parse_foerderdatenbank_programme,
    parse_foerderdatenbank_results,
    parse_nlnet_propose,
)
from src.ingestion.provider_execution import ProviderError
from src.kb.funding_opportunities import FundingOpportunityStore
from tests.unit.funding.harness import (
    EXIST_TRANSFER_URL,
    EXIST_URL,
    FDB_EXIST_URL,
    FDB_STALE_URL,
    FIXTURES,
    NS,
    SCOPES,
    Env,
    load_json,
)


def _by_provider_id(env, provider_id):
    store = FundingOpportunityStore(env.conn, now=env.now)
    return next(o for o in store.list(NS, scopes=SCOPES) if o["record"]["provider_id"] == provider_id)


def test_every_provider_has_an_explicit_access_contract_without_invented_apis():
    for provider, contract in PROVIDER_CONTRACTS.items():
        assert set(contract) >= {"access", "entry_points", "authentication", "pagination", "cadence", "terms",
                                 "retained_evidence", "coverage", "distinguishes", "unavailable_fallback"}
        assert set(contract["distinguishes"]) == {"programme", "call", "award"}
        assert LIVE_VERIFICATION[provider]["status"] == "unverified"
        assert all(url.startswith("https://") for url in contract["entry_points"])
        assert provider in PROVIDER_HOSTS
    assert PROVIDER_CONTRACTS["nlnet"]["access"] == "web"
    assert "not a versioned public API" in PROVIDER_CONTRACTS["eu-ft"]["terms"]


def test_nlnet_calls_keep_fund_round_deadline_amount_and_licence_rule():
    parsed = parse_nlnet_propose((FIXTURES / "nlnet_propose.html").read_bytes())
    [call] = parsed["records"]  # the disabled (closed) fund option is not an open call
    assert (call["provider_id"], call["round_id"]) == ("commonsfund", "2026-10-01")
    deadline = call["deadlines"][0]
    assert deadline["text"] == "October 1st 2026 12:00 CEST" and deadline["instant"] == "2026-10-01T12:00:00+02:00"
    assert call["financial_terms"]["award_range"] == {**call["financial_terms"]["award_range"], "min": "5000", "max": "50000", "basis": "per-project"}
    licence = next(r for r in call["requirements"] if r["category"] == "licensing")
    assert licence["hard"] is True and licence["machine_rule"] == {"fact": "project.open_source", "op": "is_true"}
    assert "free and open source licence" in licence["locator"]["quote"]


def test_nlnet_refresh_is_idempotent_and_amendments_create_revisions():
    env = Env()
    first = env.acquire("nlnet", "nlnet_calls", observation="r1")
    assert first["ok"] and len(first["created"]) == 1
    again = env.acquire("nlnet", "nlnet_calls", observation="r2")
    assert again["unchanged"] == first["created"] and not again["revised"]
    env.web.routes["https://nlnet.nl/propose/"] = "nlnet_propose_amended.html"
    amended = env.acquire("nlnet", "nlnet_calls", observation="r3")
    # A new deadline is a new round: rounds are never merged.
    assert amended["created"] and amended["created"] != first["created"]
    kinds = {a["kind"] for a in amended["amendments"][amended["created"][0]]}
    assert kinds == {"new"}


def test_failed_or_partial_refresh_never_marks_a_call_closed():
    env = Env()
    env.acquire("nlnet", "nlnet_calls", observation="r1")
    env.web.routes["https://nlnet.nl/propose/"] = "nlnet_propose_broken.html"
    partial = env.acquire("nlnet", "nlnet_calls", observation="r2")
    assert not partial["ok"] and partial["failure"]["failure_code"] == "schema_drift"
    env.web.routes["https://nlnet.nl/propose/"] = None
    down = env.acquire("nlnet", "nlnet_calls", observation="r3")
    assert not down["ok"] and down["failure"]["failure_code"] == "http_404"
    call = next(o for o in FundingOpportunityStore(env.conn, now=env.now).list(NS, scopes=SCOPES)
                if o["record"]["record_kind"] == "call")
    assert call["status"]["state"] == "open" and call["status"]["source_stale"]
    assert call["source_freshness"]["stale"] and "refresh failed" in " ".join(call["status"]["reasons"])


def test_eu_pagination_budget_meaning_stages_and_tender_separation():
    env = Env()
    result = env.acquire("eu-ft", "eu_search_all", observation="r1", text="fixture", page_size=2)
    assert result["ok"] and result["coverage"]["complete"] and result["coverage"]["pages"] == 2
    assert result["tenders_excluded"] == 1 and len(result["created"]) == 3
    assert [c["params"]["pageNumber"] for c in env.web.calls] == ["1", "2"]
    # The portal key travels as a credential slot: its value never enters durable receipts.
    assert all("SEDIA" not in json.dumps(r["request"]) for r in [
        json.loads(row[0]) for row in env.conn.execute("SELECT receipt_json FROM provider_execution_requests").fetchall()])
    open_topic = _by_provider_id(env, "HORIZON-FIXTURE-2026-01-01")
    terms = open_topic["record"]["financial_terms"]
    assert terms["programme_budget"] == {**terms["programme_budget"], "amount": "30000000", "basis": "topic-total", "expected_awards": 6}
    assert terms["award_range"]["max"] == "5000000" and terms["award_range"]["basis"] == "per-project"
    two_stage = _by_provider_id(env, "HORIZON-FIXTURE-2026-01-02")
    assert two_stage["status"]["state"] == "forthcoming" and two_stage["status"]["stages"] == ["stage-1", "stage-2"]
    assert _by_provider_id(env, "HORIZON-FIXTURE-2025-02-03")["status"]["state"] == "closed"
    page = parse_eu_search(load_json("eu_search_page1.json"))
    assert not page["coverage"]["complete"] and page["page"]["has_more"]


def test_eu_topic_details_add_authoritative_conditions_and_amendments():
    env = Env()
    env.acquire("eu-ft", "eu_search_all", observation="r1", text="fixture", page_size=2)
    detail = env.acquire("eu-ft", "eu_topic", "HORIZON-FIXTURE-2026-01-01", observation="t1")
    [identity] = detail["revised"]
    topic = _by_provider_id(env, "HORIZON-FIXTURE-2026-01-01")
    assert topic["revision"] == 2 and any(r["category"] == "consortium" for r in topic["record"]["requirements"])
    assert "rule_change" in {a["kind"] for a in detail["amendments"][identity]}
    env.web.routes["https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/horizon-fixture-2026-01-01.json"] = "eu_topic_open_amended.json"
    amended = env.acquire("eu-ft", "eu_topic", "HORIZON-FIXTURE-2026-01-01", observation="t2")
    assert "deadline_shift" in {a["kind"] for a in amended["amendments"][identity]}
    topic = _by_provider_id(env, "HORIZON-FIXTURE-2026-01-01")
    # The search listing still states the old deadline: the conflict is retained, not merged away.
    assert [c["field"] for c in topic["conflicts"]] == ["deadlines"]
    assert topic["status"]["next_deadline"]["instant"] == "2026-12-02T16:00:00+00:00"


def test_foerderdatenbank_directory_entries_references_duplicates_and_instruments():
    env = Env()
    env.acquire("exist", "exist_programme", EXIST_URL, observation="e1")
    env.acquire("foerderdatenbank", "foerderdatenbank_programme", FDB_EXIST_URL, observation="f1")
    entry = _by_provider_id(env, "Bund/BMWi/exist-gruendungsstipendium")
    assert entry["record"]["record_kind"] == "directory_entry" and entry["status"]["state"] == "not_an_opportunity"
    assert entry["record"]["instrument"]["kinds"] == ["grant"]
    exist = _by_provider_id(env, "EN/Start-up-grant/start-up-grant")
    assert entry["links"] and entry["links"][0]["to"] == exist["opportunity_id"]
    assert exist["linked_from"] == [entry["opportunity_id"]]
    loan = parse_foerderdatenbank_programme((FIXTURES / "fdb_startgeld_loan.html").read_bytes(),
                                            source_url="https://www.foerderdatenbank.de/FDB/Content/DE/Foerderprogramm/Bund/KfW/startgeld-fixture.html")
    assert loan["records"][0]["instrument"]["kinds"] == ["loan"] and loan["records"][0]["references"] == []
    listing = parse_foerderdatenbank_results((FIXTURES / "fdb_results.html").read_bytes(),
                                             source_url="https://www.foerderdatenbank.de/FDB/SiteGlobals/FDB/Forms/Suche/x.html")
    assert listing["duplicates_removed"] == 1 and len(listing["programmes"]) == 3
    stale = env.acquire("foerderdatenbank", "foerderdatenbank_programme", FDB_STALE_URL, observation="f2")
    assert not stale["ok"] and stale["failure"]["failure_code"] == "http_404"


def test_exist_variants_keep_sourced_rules_without_inferring_affiliation():
    grant = parse_exist_programme((FIXTURES / "exist_gruendungsstipendium.html").read_bytes(), source_url=EXIST_URL)["records"][0]
    assert grant["instrument"]["kinds"] == ["stipend"] and grant["status"]["asserted"] == "rolling"
    assert grant["financial_terms"]["award_range"]["basis"] == "per-person-month"
    rules = {r["requirement_id"]: r for r in grant["requirements"]}
    assert rules["exist:university-affiliation"]["machine_rule"]["fact"] == "applicant.university_affiliation"
    assert rules["exist:team-size"]["machine_rule"] == {"fact": "applicant.team_size", "op": "lte", "value": 3}
    assert "machine_rule" not in rules["exist:submission-route"]
    assert grant["documents"][0]["kind"] == "guideline"
    transfer = parse_exist_programme((FIXTURES / "exist_forschungstransfer.html").read_bytes(), source_url=EXIST_TRANSFER_URL)["records"][0]
    # "31 January and 31 July" without a year is not converted into deadlines.
    assert transfer["status"]["asserted"] == "unknown" and "deadlines" not in transfer or not transfer.get("deadlines")
    assert "instrument.kinds" in transfer["unknowns"]


def test_hosts_are_exact_and_records_must_come_from_declared_origins():
    with pytest.raises(ProviderError):
        parse_nlnet_propose((FIXTURES / "nlnet_propose.html").read_bytes(), source_url="https://evil.example/propose/")
    env = Env()
    with pytest.raises(ProviderError) as exc:
        env.client("exist").exist_programme("https://www.exist.de.evil.example/x", "o")
    assert exc.value.code == "source_identity"
