"""Offline claim-to-fact-checks acceptance for the News pack's fact-checks provider (FC12, #2719).

The pinned ``bounded-public-osint`` 1.2.0 fixtures for the Google Fact Check
Tools, Data Commons ClaimReview and IFCN sources replay through the real
source-pack runtime (fixture adapters compiled from the installed pack) with
sockets blocked and the ``fact-checks-google``, ``fact-checks-datacommons`` and
``fact-checks-ifcn`` features selected; a later acquisition replays the v2
responses (a revised review, a later release and a later listing). A claim and a
news article reach cited fact-checks with ratings as published, publisher status
at review time and reviewable claim matches; a subject with no records is
``none_on_record``. Every publisher, claimant and claim is fictional; personal
fields exist only in the native responses, which the parser discards. Nothing
here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.fact_checks_sources import (
    FIXTURE_SECRET,
    FactChecksAdapter,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.fact_checks_identity import FactCheckIdentity
from src.kb.fact_checks_links import FactCheckLinks
from src.kb.fact_checks_monitoring import FactCheckMonitor
from src.kb.fact_checks_queries import FactCheckQueries
from src.kb.fact_checks_records import (
    FactCheckStore,
    feature_enabled,
    forbidden_keys,
    readiness,
)
from src.kb.subscriptions import SubscriptionStore
from tests.unit import fact_checks_harness as h
from tests.unit.composition.test_migration import _migrated

PACK_ID = "bounded-public-osint"
FEATURES = ["fact-checks-google", "fact-checks-datacommons", "fact-checks-ifcn"]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]
# Personal fields present only in the native fixture responses; none may survive acquisition (FC01).
PERSONAL = ("Chief Spokesperson", "images.example/robin", "robinsample_fake", "Pat Reviewer", "staff/pat")
VERDICT_WORDS = ("truth_verdict", "normalized_rating", "normalised_rating", "noesis_verdict")


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
        self.clock = h.OBSERVED["v1"]
        _, coordinator, bundles, _ = _migrated(self.conn)
        coordinator.select("news", bundles["news"]["version"], features=FEATURES)
        coordinator.activate("fact-checks-acceptance")
        manifest = validate_source_pack(json.loads(h.PACK.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for source_id in h.SOURCES:
            runtime.accept_license(PACK_ID, source_id, principal_id="operator")

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def run(self, key, adapters=None) -> dict:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, h.ROOT)
        return runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": "selection", "source_ids": list(h.SOURCES),
             "max_results": 5000, "max_bytes": 60_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters={s: (adapters or {}).get(s) or fixtures[s] for s in h.SOURCES},
            dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)


def v2_adapter(source_id: str) -> FactChecksAdapter:
    return FactChecksAdapter(h.source(source_id), transport=fixture_transport(h.native_pages(source_id, "v2")),
                             secret=FIXTURE_SECRET)


def personal_data_anywhere(conn) -> list[str]:
    """Every text column of every table that still holds a withheld personal field."""
    found = []
    columns = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE data_type IN "
                           "('VARCHAR', 'JSON')").fetchall()
    for table, column in columns:
        for needle in PERSONAL:
            hit = conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE ?', [f"%{needle}%"]).fetchone()
            if hit[0]:
                found.append(f"{table}.{column}: {needle}")
    return found


def test_claim_or_article_to_cited_fact_checks_with_ratings_as_published_and_reviewable_matches():
    env = Env()
    assert all(feature_enabled(env.conn, f) for f in FEATURES)
    first = env.run("fact-checks")
    assert first["status"] == "complete", first
    store = FactCheckStore(env.conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 9
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)
    assert personal_data_anywhere(env.conn) == []  # FC01: nothing personal reached documents, records or receipts
    state = readiness(env.conn)
    assert all(state["enabled"].values()) and all(p["live"] == "unverified-live" for p in state["providers"].values())

    # --- a later acquisition: a revised review, a later release and a later listing --------------------------
    env.clock = h.OBSERVED["v2"]
    second = env.run("fact-checks-v2", adapters={s: v2_adapter(s) for s in h.SOURCES})
    assert second["status"] == "complete", second
    replay = env.run("fact-checks-v2-replay", adapters={s: v2_adapter(s) for s in h.SOURCES})
    assert replay["status"] == "complete"
    assert {r["change"] for r in store.records(h.NS, scopes=h.SCOPES)} >= {"new", "revised", "absent"}
    factdesk = next(v for v in store.records(h.NS, scopes=h.SCOPES, kinds=["fact-check"])
                    if v["source_id"] == h.GOOGLE and v["publisher_key"].endswith("factdesk.example"))
    chain = store.history(h.NS, factdesk["record_key"], scopes=h.SCOPES, source_id=h.GOOGLE)
    assert [v["change"] for v in chain] == ["new", "revised"]  # the replay added nothing
    solar = [v for v in store.records(h.NS, scopes=h.SCOPES) if v["status"] == "absent-from-release"]
    assert len(solar) == 1 and store.history(h.NS, solar[0]["record_key"], scopes=h.SCOPES)[0]["status"] == \
        "published"  # a removal is a revision; the earlier revision stays

    # --- reviewable identity: proposed, then reviewed; nothing automatic ------------------------------------
    h.load_news(env.conn)
    h.load_source_identity(env.conn)
    identity = FactCheckIdentity(env.conn, now=env.now)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert proposed["unavailable"] == [] and {c["state"] for c in proposed["candidates"]} == {"proposed"}
    ask = FactCheckQueries(env.conn)
    before_review = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-widgets")
    assert before_review["status"] == "none_on_record"  # no claim match is used before review
    assert before_review["identity"]["unreviewed_candidates_not_used"]
    for candidate in proposed["candidates"]:
        if candidate["method"] == "name-as-published":
            continue
        decision = "reject" if candidate["right_key"] == "claim-weather" else "accept"
        identity.review(h.NS, candidate["candidate_id"], decision, "acceptance review", principal_id="rev",
                        scopes=h.REVIEW_SCOPES)
    unmatched = identity.unmatched(h.NS, scopes=h.SCOPES)
    assert "fact-check:claimant:name:robin-sample" in {u["subject_key"] for u in unmatched["claimant"]}

    # --- cross-pack links: news article by citation, claim timeline, OSINT corroboration, source identity ----
    linked = FactCheckLinks(env.conn, now=env.now).link(h.NS, principal_id="alice", scopes=h.SCOPES)
    assert {link["link_kind"] for link in linked["links"]} == {"news-article", "claim-timeline",
                                                              "osint-corroboration", "source-identity"}
    assert all(link["record_revision_id"] and link["target_revision"] for link in linked["links"])
    assert linked["missing_targets"]  # the uncited bus-fares article is reported, not dropped

    # --- claim -> cited fact-checks as of a date, ratings verbatim, side by side, publisher status -----------
    answer = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-widgets", as_of="2099-08-31")
    assert answer["status"] == "answered" and forbidden_keys(answer) == []
    ratings = {f["publisher"]["domain"]: sorted(s["rating_as_published"]["text"] for s in f["as_published_by_source"])
               for f in answer["fact_checks"]}
    assert ratings == {"factdesk.example": ["False", "False (updated with the company's statement)"],
                       "northwind-verify.example": ["Mostly accurate"]}
    assert answer["side_by_side"]["groups"][0]["rating_texts_differ"] is True
    status = {f["publisher"]["domain"]: f["publisher_status_at_review"]["status"] for f in answer["fact_checks"]}
    assert status == {"factdesk.example": "verified", "northwind-verify.example": "expired"}
    known_then = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claim_id="claim-widgets", as_of="2099-06-05",
                                           known_at="2099-07-31")
    assert {f["publisher"]["domain"]: f["publisher_status_at_review"]["status"]
            for f in known_then["fact_checks"]}["northwind-verify.example"] == "verified"
    for item in answer["fact_checks"]:
        for entry in item["as_published_by_source"]:
            assert entry["citation"]["source_id"] in h.SOURCES and entry["citation"]["revision_id"]
            assert entry["citation"]["observed_at"] and entry["citation"]["evidence_origin"] == "fixture"
    bundle = ask.evidence_bundle(answer)
    assert {c for a in bundle["sections"][0]["assertions"] for c in a["citations"]} == {
        b["id"] for b in bundle["bibliography"]}

    # --- claimant through an accepted published-identifier match ---------------------------------------------
    claimant = ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claimant="ent-wikidata-q99999901")
    assert [f["publisher"]["domain"] for f in claimant["fact_checks"]] == ["factdesk.example"]

    # --- news article -> fact-checks citing it (stated URL rule, archived capture) -------------------------
    citing = ask.citing(h.NS, scopes=h.SCOPES, document_id="doc-widgets")
    (item,) = citing["fact_checks"]
    assert citing["subject"]["url_rule"]["rules"] == "wa-canon-v1"
    assert item["archived_captures"]["status"] == "answered"
    assert ask.citing(h.NS, scopes=h.SCOPES, url=h.SOCIAL_POST)["fact_checks"][0]["cites_target_as"][0]["match"] \
        == "url-digest"

    # --- a subject with no records ----------------------------------------------------------------------------
    assert ask.for_claim_or_claimant(h.NS, scopes=h.SCOPES, claimant="Nobody Anywhere")["status"] == "none_on_record"
    assert ask.citing(h.NS, scopes=h.SCOPES, url="https://news.example/unrelated")["status"] == "none_on_record"

    # --- monitoring through subscriptions ---------------------------------------------------------------------
    monitor = FactCheckMonitor(env.conn, now=env.now)
    watch = monitor.create(h.NS, "northwind", watch="publisher", key="northwind-verify.example", principal_id="alice",
                           scopes=h.SCOPES)
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1)
    notices = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert {n["kind"] for n in notices} == {"publisher_listed", "fact_check_published"}
    assert all(n["cites"]["revision_id"] for n in notices)

    # --- exclusions and minimisation hold everywhere ----------------------------------------------------------
    text = json.dumps([answer, known_then, claimant, citing, bundle, linked, notices]).casefold()
    assert not [w for w in VERDICT_WORDS if w in text]
    assert "no truth verdicts by noesis" in text
    assert personal_data_anywhere(env.conn) == []
