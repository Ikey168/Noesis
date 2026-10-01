"""Offline advertiser-to-political-ads acceptance for the OSINT platform-transparency features (SP13, #2646).

The pinned ``osint-platform-transparency`` 1.0.0 fixtures for the DSA
Transparency Database, Meta Ad Library, Google political ads and Lumen sources
replay through the real source-pack runtime (fixture adapters compiled from
the installed pack) with sockets blocked and the four platform-transparency
features selected. An advertiser and an election reach cited political-ad
records with ranges as published and reviewable advertiser matches, a platform
reaches cited moderation statements counted over stored records with the dump
versions stated, later publications become revisions (removals included), a
subject with no records is ``none_on_record``, and Lumen without a researcher
token is reported as unavailable. Every platform, advertiser and notice is
fictional; placeholder personal text exists only in the native responses, which
the parser discards. Nothing here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.platform_transparency_sources import (
    FIXTURE_SECRET,
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

PACK_ID = "osint-platform-transparency"
FEATURES = ["platform-transparency-dsa", "platform-transparency-meta", "platform-transparency-google",
            "platform-transparency-lumen"]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
# Placeholder personal or private text present only in the native fixture responses; none may survive acquisition.
PERSONAL = ("PLACEHOLDER", "placeholder_user", "Placeholder Notifier", "Placeholder notice body", "1 Placeholder",
            "exampla.example/post", "FIXTURE-TOKEN-NOT-REAL", "Placeholder creative", "Exampleshire",
            "Placeholder terms explanation")


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
        coordinator.select("osint", bundles["osint"]["version"], features=FEATURES)
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

    def run(self, source_ids, key, adapters=None, secret=FIXTURE_SECRET) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": "selection", "source_ids": list(source_ids),
             "max_results": 5000, "max_bytes": 100_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in source_ids},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: secret)


def v2_adapter(source_id: str) -> PlatformTransparencyAdapter:
    return PlatformTransparencyAdapter(h.source(source_id), transport=fixture_transport(h.native_pages(source_id,
                                                                                                       "v2")),
                                       secret=FIXTURE_SECRET)


def personal_data_anywhere(conn) -> list[str]:
    """Every text column of every table that still holds placeholder personal or private text."""
    found = []
    columns = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE data_type IN "
                           "('VARCHAR', 'JSON')").fetchall()
    for table, column in columns:
        for needle in PERSONAL:
            hit = conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE ?', [f"%{needle}%"]).fetchone()
            if hit[0]:
                found.append(f"{table}.{column}: {needle}")
    return found


def test_advertiser_and_election_to_cited_political_ads_and_platform_to_cited_moderation_statements():
    env = Env()
    assert all(feature_enabled(env.conn, f) for f in FEATURES)
    first = env.run(h.SOURCES, "platform-transparency")
    assert first["status"] == "complete", first
    store = PlatformTransparencyStore(env.conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 33
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)
    assert personal_data_anywhere(env.conn) == []  # SP01: nothing minimised reached documents, records or receipts
    h.load_other_packs(env.conn)
    ask = PlatformTransparencyQueries(env.conn)

    # --- reviewable advertiser matches: proposed, reviewed, nothing automatic --------------------------------
    identity = PlatformTransparencyIdentity(env.conn, now=env.now)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert proposed["unavailable"] == [] and {c["state"] for c in proposed["candidates"]} == {"proposed"}
    for candidate in proposed["candidates"]:
        if not any(":client:" in r for r in candidate["records"]):
            identity.review(h.NS, candidate["candidate_id"], "accept", "published id or name and country agree",
                            principal_id="reviewer", scopes=h.REVIEW_SCOPES)
    unmatched = {u["record_key"] for u in identity.unmatched(h.NS, scopes=h.SCOPES)["unmatched"]}
    assert "platform-transparency:meta:advertiser:999000003" in unmatched  # a person-named page is never matched

    # --- cross-pack links: elections, campaign finance, lobbying, ownership ----------------------------------
    links = PlatformTransparencyLinks(env.conn, now=env.now)
    linked = links.link_all(h.NS, principal_id="alice", scopes=h.SCOPES, ownership_namespace=h.OWN_NS)
    assert linked["unavailable"] == [] and all(v["status"] == "linked" for v in linked["kinds"].values())

    # --- advertiser -> cited ads, ranges as published -------------------------------------------------------
    committee = ask.ads_for_advertiser(h.NS, "C00999901", scopes=h.SCOPES)
    assert committee["subjects"][0]["method"] == "published-id"
    assert [a["impressions_as_published"] for a in committee["ads"]] == ["100k-1M", "10k-100k", "≤ 10k"]
    assert all(a["citation"]["source_id"] == "google-political-ads" and a["citation"]["data_as_of"]
               for a in committee["ads"])
    holdings = ask.ads_for_advertiser(h.NS, "gb-coh:09990002", scopes=h.SCOPES)
    assert {a["ad_id"] for a in holdings["ads"]} == {"880000000000001", "880000000000002", "880000000000003"}
    assert {a["spend_as_published"]["currency"] for a in holdings["ads"]} == {"GBP"}

    # --- election -> cited ads by advertiser -----------------------------------------------------------------
    election = ask.ads_for_election(h.NS, h.US_ELECTION, scopes=h.SCOPES)
    assert {g["advertiser_key"].rsplit(":", 1)[1] for g in election["advertisers"]} == {
        "AR10000000000000000001", "AR10000000000000000002", "999000002"}

    # --- platform -> cited moderation statements over stored records ---------------------------------------
    moderation = ask.moderation_statements(h.NS, "exampla-social", scopes=h.SCOPES, start="2099-05-01",
                                           end="2099-05-02")
    assert moderation["total"] == 6 and moderation["stored_window"]["days_without_dump"] == []
    assert moderation["automated_detection_as_published"] == {"No": 2, "Yes": 4}
    bundle = ask.evidence_bundle(moderation)
    assert {b["id"] for b in bundle["bibliography"]} == {v["revision_id"] for v in moderation["dump_versions"]}

    # --- monitors, then later publications become revisions (removals included) ------------------------------
    monitor = PlatformTransparencyMonitor(env.conn, now=env.now)
    watch = monitor.create(h.NS, "cmte", watch="advertiser", key="C00999901", principal_id="alice",
                           scopes=h.SCOPES)
    platform = monitor.create(h.NS, "exampla", watch="platform", key="exampla-social", principal_id="alice",
                              scopes=h.SCOPES)
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    monitor.run(platform["subscription_id"], principal_id="alice", scopes=h.SCOPES)
    before = max(r["observed_at_ms"] for r in store.history(
        h.NS, "platform-transparency:google:ad:CR10000000000000000001", scopes=h.SCOPES))
    later = ["dsa-sor-dumps", "meta-ad-library-political", "google-political-ads"]
    revised = env.run(later, "platform-transparency-v2", adapters={s: v2_adapter(s) for s in later})
    assert revised["status"] == "complete"
    SubscriptionStore(env.conn).commit_watermark(h.NS, 2)
    kinds = sorted(n["kind"] for n in monitor.run(watch["subscription_id"], principal_id="alice",
                                                  scopes=h.SCOPES)["notifications"])
    assert kinds == ["ad_not_returned", "ad_revised", "new_ad"]
    assert [n["kind"] for n in monitor.run(platform["subscription_id"], principal_id="alice",
                                           scopes=h.SCOPES)["notifications"]] == ["dump_republished"]
    now = ask.ads_for_advertiser(h.NS, "C00999901", scopes=h.SCOPES)
    removed = next(a for a in now["ads"] if a["ad_id"] == "CR10000000000000000002")
    assert removed["listing_status"] == "not-returned" and [r["change"] for r in removed["revisions"]] == [
        "new", "not-returned"]
    then = ask.ads_for_advertiser(h.NS, "C00999901", scopes=h.SCOPES, as_of=before)
    assert {a["ad_id"]: a["impressions_as_published"] for a in then["ads"]}["CR10000000000000000001"] == "100k-1M"
    assert len(then["ads"]) == 3 and len(now["ads"]) == 4

    # --- a subject with no records, exclusions and minimisation ---------------------------------------------
    none = ask.ads_for_advertiser(h.NS, "AR10000000000000000004", scopes=h.SCOPES)
    assert none["status"] == "none_on_record" and "not evidence" in none["note"]
    for answer in (committee, holdings, election, moderation, now, then, none):
        assert forbidden_keys(answer) == []
    assert ask.takedown_notices(h.NS, "Exampla Social", scopes=h.SCOPES)["status"] == "counted"
    assert personal_data_anywhere(env.conn) == []

    # --- gated Lumen without a researcher token is reported, never a silent success -------------------------
    tokenless = PlatformTransparencyAdapter(h.source("lumen-notices"),
                                            transport=fixture_transport(h.native_pages("lumen-notices")), secret=None)
    gated = env.run(["lumen-notices"], "lumen-without-token", adapters={"lumen-notices": tokenless}, secret=None)
    assert gated["status"] == "partial"  # degraded and reported, never a silent success
    assert gated["failures"] == [{"source_id": "lumen-notices", "classification": "preflight",
                                  "detail": ["credential_missing"]}]
    assert gated["coverage"]["unavailable"] == 1 and gated["coverage"]["completed"] == 0
    assert readiness(env.conn)["degraded"] == ["lumen"]

    # --- an idempotent replay adds nothing -------------------------------------------------------------------
    count = env.conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0]
    replay = env.run(later, "platform-transparency-v2-replay", adapters={s: v2_adapter(s) for s in later})
    assert replay["status"] == "complete"
    assert env.conn.execute("SELECT count(*) FROM platform_transparency_revisions").fetchone()[0] == count
