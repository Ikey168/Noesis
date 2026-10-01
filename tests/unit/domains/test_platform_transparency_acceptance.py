"""Offline advertiser-to-political-ads acceptance for the OSINT platform-transparency features (SP13, #2646).

The pinned ``bounded-public-osint`` 1.2.0 fixtures for the DSA Transparency
Database, Meta Ad Library and Google political-ads sources replay through the
real source-pack runtime (fixture adapters compiled from the installed pack)
with sockets blocked and the ``platform-transparency-*`` features selected,
beside the Political campaign-finance, elections and lobbying fixtures and a
Corporate Ownership record. An advertiser and an election reach cited
political-ad records with ranges as published and reviewable advertiser
matches; a platform reaches cited moderation statements with the stored window
and dump versions; a later acquisition revises ranges, removes an ad (a
revision, never a deletion) and republishes a dump; monitors notify each; a
subject with no records is ``none_on_record``. Every platform, page,
advertiser and funder is fictional; withheld fields exist only in the native
responses, which the parser discards. Nothing here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.platform_transparency_sources import (
    FIXTURE_SECRET,
    PROVIDER_CONTRACTS,
    PlatformTransparencyAdapter,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.platform_transparency_identity import PlatformTransparencyIdentity
from src.kb.platform_transparency_links import PlatformTransparencyLinks
from src.kb.platform_transparency_monitoring import PlatformTransparencyMonitor
from src.kb.platform_transparency_queries import PlatformTransparencyQueries
from src.kb.platform_transparency_records import (
    PlatformTransparencyStore,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from src.kb.subscriptions import SubscriptionStore
from tests.unit import platform_transparency_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "bounded-public-osint"
FEATURES = ["platform-transparency-dsa", "platform-transparency-meta", "platform-transparency-google"]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
# Withheld values present only in the native fixture responses; none may survive acquisition.
WITHHELD = ("synthetic-content-id", "Example Flagger Association", "free text a notifier wrote", "access_token=",
            "render_ad", "25-34")
EXCLUDED_WORDS = ("midpoint", "point estimate", "coordinated behaviour detected", "user profile", "cohort")


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
    def __init__(self, features=FEATURES) -> None:
        self.conn = h.connection()
        self.clock = 4_102_444_800_000
        _, coordinator, bundles, _ = _migrated(self.conn)
        coordinator.select("osint", bundles["osint"]["version"], features=list(features))
        coordinator.activate("platform-transparency-acceptance")
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


def v2_adapter(source_id: str) -> PlatformTransparencyAdapter:
    return PlatformTransparencyAdapter(h.source(source_id), transport=fixture_transport(h.native_pages(source_id,
                                                                                                        "v2")),
                                       secret=FIXTURE_SECRET)


def withheld_anywhere(conn) -> list[str]:
    """Every text column of every table that still holds a withheld native value."""
    found = []
    columns = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE data_type IN "
                           "('VARCHAR', 'JSON')").fetchall()
    for table, column in columns:
        for needle in WITHHELD:
            hit = conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE ?', [f"%{needle}%"]).fetchone()
            if hit[0]:
                found.append(f"{table}.{column}: {needle}")
    return found


def clean(answer) -> None:
    assert forbidden_keys(answer) == []
    text = json.dumps({k: v for k, v in answer.items()
                       if k not in {"exclusions", "ranges_notice", "removal_notice", "count_notice"}}).lower()
    assert not [w for w in EXCLUDED_WORDS if w in text]
    assert not [w for w in WITHHELD if w.lower() in text]


def test_advertiser_and_election_to_cited_political_ads_and_platform_to_cited_moderation_statements():
    env = Env()
    assert all(feature_enabled(env.conn, f) for f in FEATURES)
    assert not feature_enabled(env.conn, "platform-transparency-lumen")
    first = env.run(h.SOURCES, "platform-transparency")
    assert first["status"] == "complete", first
    store = PlatformTransparencyStore(env.conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 28
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)
    assert withheld_anywhere(env.conn) == []  # SP01: nothing withheld reached documents, records or receipts
    h.load_campaign_finance(env.conn)
    h.load_ownership(env.conn)
    h.load_lobbying(env.conn)
    h.load_elections(env.conn)
    h.link_campaign_finance_contests(env.conn)
    ask = PlatformTransparencyQueries(env.conn)

    # --- reviewable identity: proposed, reviewed (one rejected), nothing automatic ------------------------
    identity = PlatformTransparencyIdentity(env.conn, now=env.now)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS,
                                campaign_finance_namespace=h.CF_NS, lobbying_namespace=h.CF_NS,
                                elections_namespace=h.CF_NS)
    assert proposed["unavailable"] == [] and all(c["state"] == "proposed" for c in proposed["candidates"])
    for candidate in proposed["candidates"]:
        decision = "reject" if "gb-coh:09990002" in candidate["records"] else "accept"
        identity.review(h.NS, candidate["candidate_id"], decision, "reviewed against the published disclosures",
                        principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    unmatched = {u["record_key"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)["unmatched"]}
    assert f"platform-transparency:google:advertiser:{h.CIVIC}" in unmatched
    assert f"platform-transparency:meta:advertiser:{h.HOLDINGS_PAGE}" in unmatched  # the rejected match
    assert len(identity.register_platforms(h.NS, principal_id="alice", scopes=h.SCOPES)) == 4

    # --- cross-pack links by citation and accepted matches ---------------------------------------------------
    links = PlatformTransparencyLinks(env.conn, now=env.now)
    assert links.link_campaign_finance(h.NS, principal_id="alice", scopes=h.SCOPES,
                                       campaign_finance_namespace=h.CF_NS)["status"] == "linked"
    elections = links.link_elections(h.NS, principal_id="alice", scopes=h.SCOPES, elections_namespace=h.CF_NS,
                                     campaign_finance_namespace=h.CF_NS)
    assert elections["status"] == "linked" and elections["missing_targets"]  # reported, never dropped
    assert links.link_lobbying(h.NS, principal_id="alice", scopes=h.SCOPES,
                               lobbying_namespace=h.CF_NS)["status"] == "linked"

    # --- advertiser -> cited ads with ranges as published ---------------------------------------------------
    by_committee = ask.ads_by_advertiser(h.NS, "C00999903", scopes=h.SCOPES)
    (fund,) = by_committee["advertisers"]
    assert fund["path"][1]["method"] == "published-id" and len(fund["ads"]) == 3
    spend = {a["ad_id"]: a["spend_range_as_published"] for a in fund["ads"]}
    assert spend["CR00000000000000000103"]["as_published"] == {"spend_range_min_usd": "0",
                                                               "spend_range_max_usd": "100"}
    assert all(a["citation"]["source_id"] == h.GOOGLE and a["citation"]["observed_at"] for a in fund["ads"])
    party = ask.ads_by_advertiser(h.NS, h.PARTY_PAGE, scopes=h.SCOPES)
    assert party["advertisers"][0]["identity"]["state"] == "matched"
    first_party = {a["ad_id"]: a for a in party["advertisers"][0]["ads"]}
    as_of_first = first_party["990000000000102"]["citation"]["observed_at_ms"]

    # --- election -> cited ads ------------------------------------------------------------------------------
    uk = ask.ads_for_election(h.NS, "gb-general:2099-05-07", scopes=h.SCOPES)
    assert uk["status"] == "answered" and uk["election"] == "UK Parliamentary General Election 2099"
    us = ask.ads_for_election(h.NS, "us-us-president:2099", scopes=h.SCOPES)
    assert all(a["link"]["basis"]["via"] == "campaign-finance contest link"
               for g in us["advertisers"] for a in g["ads"])

    # --- platform -> cited moderation statements ------------------------------------------------------------
    moderation = ask.moderation_statements(h.NS, "example-video", scopes=h.SCOPES, start="2099-05-01",
                                           end="2099-05-02")
    assert moderation["statements_counted"] == 6 and moderation["stored_window"]["complete"] is True
    assert [d["date"] for d in moderation["dump_versions"]] == ["2099-05-01", "2099-05-02"]

    # --- monitors, then a later acquisition: revised ranges, a removal revision, a republished dump --------
    monitor = PlatformTransparencyMonitor(env.conn, now=env.now)
    watch_party = monitor.create(h.NS, "party", watch="advertiser", key=h.PARTY_PAGE, principal_id="alice",
                                 scopes=h.SCOPES)
    watch_holdings = monitor.create(h.NS, "holdings", watch="advertiser", key=h.HOLDINGS_PAGE,
                                    principal_id="alice", scopes=h.SCOPES)
    watch_video = monitor.create(h.NS, "video", watch="platform", key="example-video", principal_id="alice",
                                 scopes=h.SCOPES)
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1)
    for watch in (watch_party, watch_holdings, watch_video):
        assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    revised = env.run(h.SOURCES, "platform-transparency-v2", adapters={s: v2_adapter(s) for s in h.SOURCES})
    assert revised["status"] == "complete"
    SubscriptionStore(env.conn).commit_watermark(h.NS, 2)
    notes = {w: {n["kind"] for n in monitor.run(watch["subscription_id"], principal_id="alice",
                                                  scopes=h.SCOPES)["notifications"]}
             for w, watch in (("party", watch_party), ("holdings", watch_holdings), ("video", watch_video))}
    assert notes == {"party": {"ad_published", "ad_ranges_revised"}, "holdings": {"ad_not_returned"},
                     "video": {"dump_republished"}}
    SubscriptionStore(env.conn).commit_watermark(h.NS, 3)
    assert monitor.run(watch_party["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"] == []

    # --- revision history and as-of answers -----------------------------------------------------------------
    holdings = ask.ads_by_advertiser(h.NS, h.HOLDINGS_PAGE, scopes=h.SCOPES)["advertisers"][0]["ads"][0]
    assert holdings["listing_state"] == "not-returned" and holdings["removal"]["revision_id"]
    assert [r["change"] for r in holdings["revisions"]] == ["new", "not-returned"]
    now = {a["ad_id"]: a for a in ask.ads_by_advertiser(h.NS, h.PARTY_PAGE, scopes=h.SCOPES)["advertisers"][0]["ads"]}
    then = {a["ad_id"]: a for a in ask.ads_by_advertiser(h.NS, h.PARTY_PAGE, scopes=h.SCOPES,
                                                         as_of=as_of_first)["advertisers"][0]["ads"]}
    assert now["990000000000102"]["spend_range_as_published"]["lower_bound"] == "1500"
    assert then["990000000000102"]["spend_range_as_published"]["lower_bound"] == "1000"
    assert "990000000000103" in now and "990000000000103" not in then
    later = ask.moderation_statements(h.NS, "example-video", scopes=h.SCOPES, start="2099-05-01", end="2099-05-01")
    assert later["dump_versions"][0]["revision_no"] == 2

    # --- a subject with no records, exclusions and minimisation, evidence, idempotent replay ---------------
    none = ask.ads_by_advertiser(h.NS, "999999999999999", scopes=h.SCOPES)
    assert none["status"] == "none_on_record"
    for answer in (by_committee, party, uk, us, moderation, later, none):
        clean(answer)
    assert set(party["exclusions"]) == {"user-level profiling", "collection of private content",
                                        "inference of coordinated behaviour",
                                        "conversion of spend or impression ranges into point estimates"}
    bundle = ask.evidence_bundle(by_committee)
    assert len(bundle["bibliography"]) == 3 and all("source platform-transparency-google-political-ads" in b["text"]
                                                    for b in bundle["bibliography"])
    revisions = env.conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0]
    replay = env.run(h.SOURCES, "platform-transparency-replay", adapters={s: v2_adapter(s) for s in h.SOURCES})
    assert replay["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0] == revisions
    assert withheld_anywhere(env.conn) == []


def test_offline_and_live_evidence_are_reported_separately_and_lumen_is_not_implemented():
    env = Env()
    env.run([h.DSA], "dsa-only")
    state = readiness(env.conn)
    assert state["enabled"] == {"dsa-transparency-db": True, "meta-ad-library": True, "google-political-ads": True,
                                "lumen": False}
    assert all(p["live"] != "verified-live" for p in state["providers"].values())
    assert state["providers"]["dsa-transparency-db"]["records"] == 10
    assert PROVIDER_CONTRACTS["lumen"]["access_decision"] == "not-implemented"
    evidence = (h.ROOT / "docs/development/platform-transparency-evidence/README.md").read_text()
    assert "Live evidence: none yet" in evidence


def test_with_the_features_disabled_the_osint_bundle_is_unchanged():
    conn = h.connection()
    _, coordinator, _, _ = _migrated(conn)
    assert not any(feature_enabled(conn, f) for f in FEATURES)
    bound = {b["provider"] for b in coordinator.active()["plan"]["bindings"] if "osint" in b["consumers"]}
    assert "osint.platform-transparency" not in bound
