"""Offline bill-to-dossier acceptance for the Political legislation features (LT12, #2451).

The pinned ``official-political-records`` 1.3.0 fixtures for the nine
legislation sources and the US LDA register replay through the real
source-pack runtime (fixture adapters compiled from the installed pack) with
sockets blocked and the ``legislation-us``, ``legislation-uk`` and ``lobbying``
features selected. A US and a UK bill each reach a cited dossier in the
existing legislative dossier store with stages, text versions, votes with
reviewable member matches, a congress.gov / BILLSTATUS disagreement, lobbying
and enactment links, and a bill with none on record. Every bill, member and
disclosure is fictional; nothing here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.domains.political.legislation_queries import LegislationQueries
from src.ingestion.legislation_sources import FIXTURE_SECRET, LegislationAdapter, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.legislation import LegislationDossiers, LegislationStore, feature_enabled, forbidden_keys, readiness
from src.kb.legislation_identity import LegislationIdentity
from src.kb.legislation_links import LegislationLinks
from src.kb.legislation_monitoring import LegislationMonitor
from src.kb.subscriptions import SubscriptionStore
from tests.unit import elections_harness as eh
from tests.unit import legislation_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "official-political-records"
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
EXCLUDED_WORDS = ("probability", "predict", "ideology", "score", "legal effect of the bill")


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
                           features=["legislation-us", "legislation-uk", "lobbying"])
        coordinator.activate("legislation-acceptance")
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

    def run(self, source_ids, key, operation, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": operation, "source_ids": list(source_ids),
             "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in source_ids},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)


def v2_adapter(source_id: str) -> LegislationAdapter:
    item = next(s for s in validate_source_pack(json.loads(h.PACK.read_text()))["sources"]
                if s["source_id"] == source_id)
    return LegislationAdapter(item, transport=fixture_transport(h.native_pages(source_id, h.V2[source_id])),
                              secret=FIXTURE_SECRET)


def ask(env, bill, as_of, **kw):
    return LegislationQueries(env.conn).bill_as_of(h.NS, bill, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES,
                                                   as_of=as_of, **kw)


def build(env, bill):
    return LegislationDossiers(env.conn, now=env.now).build(h.NS, bill, h.DOSSIER_NS, principal_id="alice",
                                                            scopes=h.SCOPES)


def test_us_and_uk_bills_to_cited_dossiers_with_stages_versions_votes_and_linked_disclosures():
    env = Env()
    assert feature_enabled(env.conn, "legislation-us") and feature_enabled(env.conn, "legislation-uk")
    first = env.run(h.SOURCES, "legislation", "selection")
    lda = env.run(["us-senate-lda"], "lda", "export")
    assert first["status"] == lda["status"] == "complete", (first, lda)
    store = LegislationStore(env.conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 18
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)

    # --- US: dossier, as-of stage and version, votes, disagreement, lobbying ---------------------------------
    us = build(env, h.US_BILL)
    assert us["jurisdiction"] == "US" and us["evidence_origin"] == "fixture"
    assert env.conn.execute("SELECT count(*) FROM legislative_dossiers").fetchone()[0] == 1  # existing store
    introduced = ask(env, h.US_BILL, "2099-02-10")
    assert introduced["text_version"]["version_code"] == "ih" and introduced["votes"]["held"] == []
    passed = ask(env, h.US_BILL, "2099-03-10")
    assert passed["stage"]["latest_action"]["action_code"] == "H38310"
    assert passed["text_version"]["version_code"] == "rh" and passed["text_version"]["used"]["revision_id"]
    (house,) = passed["votes"]["held"]
    assert {p["member_id"]: p["position"] for p in house["positions"]}["P009903"] == "Nay"
    assert {d["field"] for d in ask(env, h.US_BILL, "2099-03-20")["source_disagreements"]} == {
        "latest_action", "cosponsors"}
    linked = LegislationLinks(env.conn, now=env.now).link_lobbying(h.NS, h.US_BILL, h.DOSSIER_NS, h.NS,
                                                                   principal_id="alice", scopes=h.SCOPES)
    assert linked["status"] == "linked" and linked["missing_targets"][0]["reference"] == "us-bill:156-s-9950"
    with_lobbying = ask(env, h.US_BILL, "2099-06-01", lobbying_namespace=h.NS)
    (disclosure,) = with_lobbying["lobbying"]["disclosures"]
    assert disclosure["register"] == "us-lda" and disclosure["link_kind"] == "explicit-field"
    assert with_lobbying["lobbying"]["notice"] == "a disclosure naming a bill is not evidence of influence"

    # --- UK: candidates reviewed, member matched through reviewable identity ------------------------------------
    eh.apply(env.conn, "gb", eh.UK)
    uk = build(env, h.UK_BILL)
    assert len(uk["review_candidates"]) == 3
    store.review_link(h.NS, "uk-division:commons-1701", "uk-commons-divisions", h.UK_BILL, "accept",
                      "the division title names the bill's second reading", principal_id="rev",
                      scopes=h.REVIEW_SCOPES)
    identity = LegislationIdentity(env.conn, now=env.now)
    (candidate,) = [c for c in identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)["candidates"]
                    if "legislation:member:uk-parliament:4002" in c["records"]]
    assert candidate["state"] == "proposed" and candidate["method"] == "name-jurisdiction"
    identity.review(h.NS, candidate["candidate_id"], "accept", "constituency and party agree", principal_id="rev",
                    scopes=h.REVIEW_SCOPES)
    build(env, h.UK_BILL)
    committee = ask(env, h.UK_BILL, "2099-02-20")
    assert committee["stage"]["description"] == "Committee stage"
    assert committee["text_version"]["publication_type"] == "Bill"
    (division,) = committee["votes"]["held"]
    matched = {p["member_id"]: p["identity"]["state"] for p in division["positions"]}
    assert matched["4002"] == "matched" and matched["4003"] == "unmatched"
    assert committee["royal_assent"] == "not on record"

    # --- provider revisions: the bill becomes law, Royal Assent, enactment links ---------------------------------
    monitor = LegislationMonitor(env.conn, now=env.now)
    watch = monitor.create(h.NS, "hr9901", watch="bill", key=h.US_BILL, principal_id="alice", scopes=h.SCOPES)
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1)
    assert monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    revised = env.run(["us-congress-gov-bills", "uk-parliament-bills"], "legislation-v2", "selection",
                      adapters={s: v2_adapter(s) for s in ("us-congress-gov-bills", "uk-parliament-bills")})
    assert revised["status"] == "complete"
    SubscriptionStore(env.conn).commit_watermark(h.NS, 2)
    kinds = {n["kind"] for n in monitor.run(watch["subscription_id"], principal_id="alice",
                                            scopes=h.SCOPES)["notifications"]}
    assert {"actions_recorded", "law_cited"} <= kinds
    build(env, h.US_BILL)
    build(env, h.UK_BILL)
    h.seed_uk_act(env.conn)
    links = LegislationLinks(env.conn, now=env.now)
    (uk_act,) = links.link_enactment(h.NS, h.UK_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)["links"]
    assert uk_act["status"] == "linked" and uk_act["citation"] == "ukpga/2099/5"
    (us_law,) = links.link_enactment(h.NS, h.US_BILL, h.DOSSIER_NS, principal_id="alice", scopes=h.SCOPES)["links"]
    assert us_law["status"] == "missing_target" and us_law["citation"] == "Pub. L. 156-12"
    enacted = ask(env, h.UK_BILL, "2099-06-01")
    assert enacted["royal_assent"] == "published" and enacted["enactment"][0]["relation"] == "enacted_as"
    before_law = ask(env, h.US_BILL, "2099-04-01")
    assert before_law["stage"]["category"] is None  # the law citation is not applied before its date

    # --- none on record, exclusions, idempotent replay ---------------------------------------------------------
    assert ask(env, "uk-bill:9999", "2099-06-01")["status"] == "none_on_record"
    for answer in (passed, committee, enacted, with_lobbying):
        assert forbidden_keys(answer) == []
        assert {"passage prediction", "member scoring or ideology rating",
                "summaries presented as legal effect"} == set(answer["exclusions"])
        stated = {k: v for k, v in answer.items() if k not in {"exclusions", "review_boundary"}}
        text = json.dumps(stated).lower()
        assert not any(word in text for word in EXCLUDED_WORDS), [w for w in EXCLUDED_WORDS if w in text]
    crs = next(s for s in build(env, h.US_BILL)["stages"] if s["legislation"]["record_kind"] == "us-bill")
    assert crs["legislation"]["fields"]["crs_summaries"][0]["label"] == "CRS summary"
    replay = env.run(h.SOURCES, "legislation-replay", "selection")
    assert replay["status"] == "complete"
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 20  # 18 + the Royal Assent stage and the Act publication
    # the replayed first-run responses are older provider revisions: logged, never current, nothing rebuilt
    assert build(env, h.US_BILL)["idempotent"] is True
    history = store.history(h.NS, h.US_BILL, "us-congress-gov-bills", scopes=h.SCOPES)
    assert [e["change"] for e in history] == ["new", "revised", "older-observation"]


def test_offline_and_live_evidence_are_reported_separately():
    env = Env()
    env.run(["uk-parliament-bills"], "uk-only", "selection")
    state = readiness(env.conn)
    assert state["enabled"] == {"US": True, "GB": True}
    assert all(p["live"] == "unverified-live" for p in state["providers"].values())
    assert state["providers"]["uk-bills"]["records"] == 8
    evidence = (h.ROOT / "docs/development/legislation-evidence/README.md").read_text()
    assert "Live evidence: none yet" in evidence


def test_with_the_features_disabled_the_political_bundle_is_unchanged():
    conn = h.connection()
    _, coordinator, _, _ = _migrated(conn)
    assert not feature_enabled(conn, "legislation-us") and not feature_enabled(conn, "legislation-uk")
    bound = {b["provider"] for b in coordinator.active()["plan"]["bindings"] if "political" in b["consumers"]}
    assert "political.legislation" not in bound
