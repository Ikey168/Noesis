"""Offline committee-to-filings acceptance for the Political campaign-finance features (CF13, #2526).

The pinned ``official-political-records`` 1.4.0 fixtures for the six OpenFEC
and two Electoral Commission sources replay through the real source-pack
runtime (fixture adapters compiled from the installed pack) with sockets
blocked and the ``campaign-finance-us``, ``campaign-finance-uk``, ``lobbying``
and ``elections`` features selected. A committee reaches its cited filings with
amendment history and totals as of a date (version stated), an organisation
reaches its affiliates' donations through accepted identity matches and a cited
ownership relation, a contest reaches its committee filings and independent
expenditures, and a committee with no filing is ``none_on_record``. Every
committee, candidate and organisation is fictional; individual donors carry
placeholder names only in the native responses, which the parser discards.
Nothing here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.campaign_finance_sources import FIXTURE_SECRET, CampaignFinanceAdapter, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.campaign_finance_identity import CampaignFinanceIdentity
from src.kb.campaign_finance_links import CampaignFinanceLinks
from src.kb.campaign_finance_monitoring import CampaignFinanceMonitor
from src.kb.campaign_finance_queries import CampaignFinanceQueries
from src.kb.campaign_finance_records import CampaignFinanceStore, feature_enabled, forbidden_keys, readiness
from src.kb.subscriptions import SubscriptionStore
from tests.unit import campaign_finance_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "official-political-records"
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
# Placeholder personal data present only in the native fixture responses; none may survive acquisition.
PERSONAL = ("PLACEHOLDER, PAT", "Pat Placeholder", "MOCK, JORDAN", "SAMPLE, JO", "FICTIVE, JO", "2 SAMPLE ROAD",
            "FICTIONAL EMPLOYER", "FICTIONAL OCCUPATION", "EX2 2BB", "SAMPLE, TREASURER", "REATTRIBUTION TO SPOUSE")
EXCLUDED_WORDS = ("influence score", "dark money", "undisclosed funder", "quid pro quo", "donor profile")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("offline acceptance must not open sockets")

    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


@pytest.fixture(autouse=True)
def isolated_registry():
    saved = (dict(domain_registry._REGISTRY), set(domain_registry._ENABLED), domain_registry._AUTHORITY)
    yield
    domain_registry._REGISTRY.clear()
    domain_registry._REGISTRY.update(saved[0])
    domain_registry._ENABLED.clear()
    domain_registry._ENABLED.update(saved[1])
    domain_registry.set_authority(saved[2])


class Env:
    def __init__(self) -> None:
        self.conn = h.connection()
        self.clock = 4_102_444_800_000
        _, coordinator, bundles, _ = _migrated(self.conn)
        coordinator.select("political", bundles["political"]["version"],
                           features=["campaign-finance-us", "campaign-finance-uk", "lobbying", "elections"])
        coordinator.activate("campaign-finance-acceptance")
        manifest = validate_source_pack(json.loads(h.PACK.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for item in manifest["sources"]:
            runtime.accept_license(PACK_ID, item["source_id"], principal_id="operator")

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def run(self, source_ids, key, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": "selection", "source_ids": list(source_ids),
             "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in source_ids},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)


def v2_adapter(source_id: str) -> CampaignFinanceAdapter:
    return CampaignFinanceAdapter(h.source(source_id), transport=fixture_transport(h.native_pages(source_id, "v2")),
                                  secret=FIXTURE_SECRET)


def personal_data_anywhere(conn) -> list[str]:
    """Every text column of every table that still holds a placeholder individual's personal data."""
    found = []
    columns = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE data_type IN "
                           "('VARCHAR', 'JSON')").fetchall()
    for table, column in columns:
        for needle in PERSONAL:
            hit = conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE ?', [f"%{needle}%"]).fetchone()
            if hit[0]:
                found.append(f"{table}.{column}: {needle}")
    return found


def test_committee_and_organisation_to_cited_filings_with_amendment_history_and_reviewable_donor_matches():
    env = Env()
    assert feature_enabled(env.conn, "campaign-finance-us") and feature_enabled(env.conn, "campaign-finance-uk")
    first = env.run(h.SOURCES, "campaign-finance")
    assert first["status"] == "complete", first
    store = CampaignFinanceStore(env.conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 34
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)
    assert personal_data_anywhere(env.conn) == []  # CF01: nothing personal reached documents, records or receipts
    h.load_ownership(env.conn)
    h.load_lobbying(env.conn)
    h.load_elections(env.conn)
    ask = CampaignFinanceQueries(env.conn)

    # --- committee -> filings with amendment history, totals as of a date with the version stated -----------
    early = ask.reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-05-01")
    assert early["reports"][0]["version_used"]["file_number"] == 1500101
    q1, q2 = ask.reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-07-31")["reports"]
    assert q1["version_used"]["file_number"] == 1500150 and q1["version_used"]["amendment_indicator"] == "A"
    assert q1["totals_as_reported"]["total_receipts"] == 107400.0
    assert [(d["field"], d["before"], d["after"]) for d in q1["differences_between_versions"]][0] == (
        "cash_on_hand_end_period", 112500.0, 112400.0)
    assert q1["citation"]["source_id"] == "us-fec-filings" and q1["citation"]["observed_at"]
    derived = ask.reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-12-31", derive=True)["derived"]
    assert derived["label"] == "derived" and len(derived["filing_versions_used"]) == 2
    items = ask.filing_items(h.NS, "1500101", scopes=h.SCOPES)
    assert items["individual_items_withheld"] == 1 and all(i["citation"]["filing_revision_id"]
                                                           for i in items["items"])

    # --- reviewable donor and candidate matches (proposed, then reviewed; nothing automatic) -----------------
    identity = CampaignFinanceIdentity(env.conn, now=env.now)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert proposed["unavailable"] == [] and all(c["state"] == "proposed" for c in proposed["candidates"])
    wanted = {"lei:5299EXAMPLEENERGY001", "lei:5299EXAMPLEINDUSTR01", "campaign-finance:fec:committee:C00999902",
              "lobbying:us-lda:9002-8002", "gb-coh:09990002"}
    for candidate in proposed["candidates"]:
        records = set(candidate["records"])
        if records & wanted or ("campaign-finance:fec:candidate:P99000001" in records and
                                any(":99001:" in r for r in records)) or "campaign-finance:ukec:entity:9901" in records:
            identity.review(h.NS, candidate["candidate_id"], "accept", "identifiers and published address agree",
                            principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    unmatched = identity.unmatched(h.NS, scopes=h.SCOPES)
    assert "campaign-finance:fec:donor-committee:C00999909" in {u["record_key"] for u in unmatched["unmatched"]}
    assert unmatched["individual_items_never_matched"] == 6

    # --- links by citation, then affiliate expansion through accepted matches only ----------------------------
    links = CampaignFinanceLinks(env.conn, now=env.now)
    assert links.link_contests(h.NS, principal_id="alice", scopes=h.SCOPES)["status"] == "linked"
    lobbying = links.link_lobbying(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert lobbying["links"][0]["basis"]["register"] == "us-lda"
    assert links.link_ownership(h.NS, h.OWN_NS, principal_id="alice", scopes=h.SCOPES)["status"] == "linked"
    affiliates = ask.affiliate_donations(h.NS, "lei:5299EXAMPLEINDUSTR01", principal_id="alice", scopes=h.SCOPES,
                                         ownership_namespace=h.OWN_NS, as_of="2099-12-31")
    paths = {c["record_key"]: [s["step"] for s in c["path"]] for c in affiliates["contributions"]}
    assert paths == {
        "campaign-finance:fec:sa:1500101:4000001": ["organisation", "identity", "connected-organisation", "identity"],
        "campaign-finance:fec:sa:1500150:4000011": ["organisation", "identity", "connected-organisation", "identity"],
        "campaign-finance:fec:sa:1500310:4000101": ["organisation", "ownership", "identity"]}
    assert {c["reported_in"]["file_number"]: c["reported_in"]["selected_as_of"]
            for c in affiliates["contributions"]} == {1500101: False, 1500150: True, 1500310: True}

    # --- contest filings: candidate committee filings and independent expenditures as reported -------------
    contest = ask.contest_filings(h.NS, h.contest(env.conn, "us-fips-county", "99001"), scopes=h.SCOPES)
    assert {f["file_number"] for f in contest["candidate_committee_filings"]} == {1500101, 1500150, 1500201}
    assert sorted((i["support_oppose_indicator"], i["source_assertion"]) for i in
                  contest["independent_expenditures"]) == [("S", "24/48-hour notice"), ("S", "periodic report")]
    uk = ask.contest_filings(h.NS, h.contest(env.conn, "gb-ons-pcon", "E14099901"), scopes=h.SCOPES)
    assert len(uk["party_spending_returns"]) == 1

    # --- monitors: new, amended and unchanged -----------------------------------------------------------------
    monitor = CampaignFinanceMonitor(env.conn, now=env.now)
    watch = monitor.create(h.NS, "campaign", watch="committee", key=h.CAMPAIGN, principal_id="alice",
                           scopes=h.SCOPES)
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    revised = env.run(["us-fec-filings", "uk-ec-donations"], "campaign-finance-v2",
                      adapters={s: v2_adapter(s) for s in ("us-fec-filings", "uk-ec-donations")})
    assert revised["status"] == "complete"
    SubscriptionStore(env.conn).commit_watermark(h.NS, 2)
    kinds = {n["kind"] for n in monitor.run(watch["subscription_id"], principal_id="alice",
                                            scopes=h.SCOPES)["notifications"]}
    assert {"amendment_filed", "most_recent_flag_changed"} <= kinds
    after = ask.reported_totals(h.NS, h.CAMPAIGN, scopes=h.SCOPES, as_of="2099-09-01")["reports"][0]
    assert after["version_used"]["file_number"] == 1500170 and len(after["amendment_chain"]) == 3
    SubscriptionStore(env.conn).commit_watermark(h.NS, 3)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []
    corrected = store.history(h.NS, "campaign-finance:ukec:donation:C0990001", scopes=h.SCOPES)
    assert [e["change"] for e in corrected] == ["new", "revised"]

    # --- none on record, exclusions and minimisation, idempotent replay ---------------------------------------
    none = ask.reported_totals(h.NS, h.NOT_ACQUIRED, scopes=h.SCOPES)
    assert none["status"] == "none_on_record"
    for answer in (q1, affiliates, contest, uk, none, items):
        assert forbidden_keys(answer) == []
        text = json.dumps({k: v for k, v in answer.items() if k != "exclusions"}).lower()
        assert not any(word in text for word in EXCLUDED_WORDS)
        assert not [p for p in PERSONAL if p.lower() in text]
    assert set(affiliates["exclusions"]) == {"influence scoring", "dark-money or undisclosed-funding inference",
                                            "profiling of individual donors beyond the CF01 minimisation decision"}
    revisions = env.conn.execute("SELECT count(*) FROM campaign_finance_revisions").fetchone()[0]
    replay = env.run(h.SOURCES, "campaign-finance-replay")
    assert replay["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM campaign_finance_revisions").fetchone()[0] == revisions
    assert personal_data_anywhere(env.conn) == []


def test_offline_and_live_evidence_are_reported_separately():
    env = Env()
    env.run(["uk-ec-donations"], "uk-only")
    state = readiness(env.conn)
    assert state["enabled"] == {"US": True, "GB": True}
    assert all(p["live"] != "verified-live" for p in state["providers"].values())
    assert state["providers"]["uk-electoral-commission"]["records"] == 5
    assert state["minimisation"]["minimised_individual_item_revisions"] == 1
    evidence = (h.ROOT / "docs/development/campaign-finance-evidence/README.md").read_text()
    assert "Live evidence: none yet" in evidence


def test_with_the_features_disabled_the_political_bundle_is_unchanged():
    conn = h.connection()
    _, coordinator, _, _ = _migrated(conn)
    assert not feature_enabled(conn, "campaign-finance-us") and not feature_enabled(conn, "campaign-finance-uk")
    bound = {b["provider"] for b in coordinator.active()["plan"]["bindings"] if "political" in b["consumers"]}
    assert "political.campaign-finance" not in bound
