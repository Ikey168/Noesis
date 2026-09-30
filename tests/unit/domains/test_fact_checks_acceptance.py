"""Offline claim-to-fact-checks acceptance for the News fact-checks provider (FC12, #2719).

The pinned ``bounded-public-osint`` 1.2.0 fixtures for the Google Fact Check
Tools claim search, the Data Commons ClaimReview feed and the IFCN signatories
listing replay through the real source-pack runtime (fixture adapters compiled
from the installed pack) with sockets blocked and the ``fact-checks-google``,
``fact-checks-datacommons`` and ``fact-checks-ifcn`` features selected. An
argument claim reaches cited fact-checks only through accepted claim matches,
with every rating as published side by side, the publishers' IFCN status at
review time and revision history as of a date; a news article reaches the
fact-checks citing it by the stated URL rules with an archived capture; links
point at revisions; a claimant with no records is ``none_on_record``. Every
publisher, claimant and claim is fictional; placeholder reviewer names, images
and job titles exist only in the native responses, which the parsers discard.
Nothing here is live evidence.
"""

from __future__ import annotations

import json
import socket

import pytest

from src.domains import registry as domain_registry
from src.ingestion.fact_checks_sources import (
    EXCLUSIONS,
    FIXTURE_SECRET,
    FactChecksAdapter,
    fixture_transport,
)
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.citation_preservation import CAPTURE_SCOPE, CitationPreservationStore
from src.kb.claim_timelines import READ_SCOPE as TIMELINE_READ
from src.kb.claim_timelines import WRITE_SCOPE as TIMELINE_WRITE
from src.kb.claim_timelines import ClaimTimelineStore
from src.kb.fact_checks_identity import FactCheckIdentity
from src.kb.fact_checks_links import FactCheckLinks
from src.kb.fact_checks_monitoring import FactChecksMonitor
from src.kb.fact_checks_queries import FactCheckQueries
from src.kb.fact_checks_records import (
    FactChecksStore,
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
# Placeholder personal data present only in the native fixture responses; none may survive acquisition.
PERSONAL = ("Mayor of Example Bay", "mayor.jpg", "Sam Placeholder", "Reviewer Placeholder", "reviewer-placeholder",
            "false.png", "logo.png", "/media/example-fact-check.png")
VERDICT_WORDS = ("noesis verdict", "normalised_rating", "normalized_rating", "truth_value")


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
        self.clock = 1_767_225_600_000  # 2026-01-01, after every fixture date
        _, coordinator, bundles, _ = _migrated(self.conn)
        coordinator.select("news", bundles["news"]["version"], features=FEATURES)
        coordinator.activate("fact-checks-acceptance")
        manifest = validate_source_pack(json.loads(h.PACK.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for item in manifest["sources"]:
            if item["source_id"] in h.SOURCES:
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


def v2_adapter(source_id: str) -> FactChecksAdapter:
    return FactChecksAdapter(h.source(source_id), transport=fixture_transport(h.native_pages(source_id, "v2")),
                             secret=FIXTURE_SECRET)


def text_anywhere(conn, needles) -> list[str]:
    found = []
    columns = conn.execute("SELECT table_name, column_name FROM information_schema.columns WHERE data_type IN "
                           "('VARCHAR', 'JSON')").fetchall()
    for table, column in columns:
        for needle in needles:
            hit = conn.execute(f'SELECT count(*) FROM "{table}" WHERE "{column}" LIKE ?', [f"%{needle}%"]).fetchone()
            if hit[0]:
                found.append(f"{table}.{column}: {needle}")
    return found


def test_claim_and_article_to_cited_fact_checks_with_revisions_reviewable_matches_and_ratings_as_published():
    env = Env()
    assert all(feature_enabled(env.conn, feature) for feature in FEATURES)
    first = env.run(h.SOURCES, "fact-checks")
    assert first["status"] == "complete", first
    store = FactChecksStore(env.conn)
    assert len(store.records(h.NS, scopes=h.SCOPES)) == 10
    receipts = store.receipts(h.NS, scopes=h.SCOPES)
    assert {r["source_id"] for r in receipts} == set(h.SOURCES)
    assert all(r["receipt"]["evidence_origin"] == "fixture" for r in receipts)
    assert text_anywhere(env.conn, PERSONAL) == []  # FC01: nothing withheld reached documents, records or receipts
    status = readiness(env.conn)
    assert status["store_ready"] and all(status["features"].values())
    assert {p: v["live"] for p, v in status["providers"].items() if p != "datacommons-research-dataset"} == dict.fromkeys(
        ("google-fact-check-tools", "datacommons", "ifcn"), "unverified-live")

    # --- revision history: a publisher update, a review dropped from a release, an IFCN status change --------
    second = env.run([h.GOOGLE, h.DATACOMMONS, h.IFCN], "fact-checks-v2",
                     adapters={s: v2_adapter(s) for s in h.SOURCES})
    assert second["status"] == "complete", second
    replay = env.run([h.GOOGLE, h.DATACOMMONS, h.IFCN], "fact-checks-v2-again",
                     adapters={s: v2_adapter(s) for s in h.SOURCES})
    assert replay["status"] == "complete"
    changes = [r["counts"] for r in store.receipts(h.NS, scopes=h.SCOPES) if r["run_id"] == replay["run_id"]]
    assert changes and all(c["new"] == c["revised"] == c["absent"] == 0 for c in changes)  # re-running adds nothing
    assert [r["status"] for r in store.history(h.NS, "fact-checks:ifcn:claimwatch-example", scopes=h.SCOPES)] == [
        "published", "absent-from-listing"]

    # --- reviewable identity: nothing is accepted without a reviewer -----------------------------------------
    h.load_news(env.conn)
    h.load_source_identity(env.conn)
    identity = FactCheckIdentity(env.conn)
    proposed = identity.propose(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES)
    assert all(m["state"] == "proposed" for m in proposed["matches"])
    ask = FactCheckQueries(env.conn)
    assert ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-seals")["status"] == "none_on_record"
    for match in proposed["matches"]:
        if match["method"] != "lexical-overlap":
            identity.review(h.NS, match["match_id"], "accept", "reviewed against the article", principal_id="rev",
                            scopes=h.REVIEW_SCOPES)

    # --- claim -> cited fact-checks as of a date, ratings verbatim and side by side ---------------------------
    early = ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-seals", as_of="2025-04-01")
    late = ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-seals", as_of="2025-12-31")
    for answer, expected in ((early, "False"), (late, "Mostly false")):
        (group,) = answer["ratings_side_by_side"]
        example = {r["source_id"]: r for r in group["ratings_as_published"]
                   if r["publisher_site"] == "factcheck.example.org"}
        assert {r["textual_rating"] for r in example.values()} == {expected}
        assert "Misleading" in group["distinct_textual_ratings"]
    (group,) = late["ratings_side_by_side"]
    scale = next(r for r in group["ratings_as_published"]
                 if r["publisher_site"] == "factcheck.example.org" and r["source_id"] == h.DATACOMMONS)
    assert (scale["rating_value"], scale["worst_rating"], scale["best_rating"]) == ("2", "1", "5")
    statuses = {i["publisher"]["site"]: i["publisher_status_at_review"]["signatories"][0]["status_as_published"]
                for i in late["fact_checks"]}
    assert statuses == {"factcheck.example.org": "Verified", "verifica.example.net": "Under renewal"}
    assert ask.for_claim(h.NS, scopes=h.SCOPES, claim_id="claim-array")["status"] == "none_on_record"  # lexical
    bundle = ask.evidence_bundle(late)
    cited = {a["citation"]["revision_id"] for i in late["fact_checks"] for a in i["source_assertions"]}
    assert {b["id"] for b in bundle["bibliography"]} == cited

    # --- cross-pack links point at revisions; absent targets are reported ------------------------------------
    ClaimTimelineStore(env.conn).capture_state(
        h.NS, "claim-seals", principal_id="analyst", scopes={TIMELINE_READ, TIMELINE_WRITE}, source_id="doc-seals",
        source_revision_id="sha256:doc-seals", evidence=[{"citation": "doc-seals"}])
    linked = FactCheckLinks(env.conn).link(h.NS, principal_id="alice", scopes=h.REVIEW_SCOPES)
    assert {k for k, v in linked["created"].items() if v} == {"news-article", "argument-claim", "osint-corroboration",
                                                              "claim-timeline"}
    assert linked["missing_targets"]
    assert all(link["revision_id"] and link["target_revision"]
               for link in FactCheckLinks(env.conn).links(h.NS, scopes=h.SCOPES))

    # --- news article -> fact-checks citing it, with an archived capture --------------------------------------
    CitationPreservationStore(env.conn).record_capture(h.NS, {
        "archive_id": "internet-archive", "archive_kind": "memento-archive", "resolver": "timetravel",
        "uri_r": h.APPEARANCE, "uri_m": "https://web.archive.org/web/20250301120000/" + h.APPEARANCE,
        "memento_datetime": "Sat, 01 Mar 2025 12:00:00 GMT", "status": 200, "mimetype": "text/html", "digests": [],
        "receipt": {"request_id": "req-1", "adapter": "test", "evidence_origin": "fixture"}},
        principal_id="curator", scopes={CAPTURE_SCOPE})
    article = ask.citing(h.NS, scopes=h.SCOPES, document_id="doc-seals", as_of="2025-12-31")
    assert {i["publisher"]["site"] for i in article["fact_checks"]} == {"factcheck.example.org",
                                                                        "verifica.example.net"}
    assert article["url_matching"]["version"] == "wa-canon-v1"
    assert article["archived_captures"]["status"] == "answered"

    # --- claimant as named, a subject with no records, monitors ----------------------------------------------
    claimant = ask.for_claimant(h.NS, "ent-alex-example", scopes=h.REVIEW_SCOPES, as_of="2025-12-31")
    assert len(claimant["fact_checks"]) == 2
    nobody = ask.for_claimant(h.NS, "Nobody Example", scopes=h.REVIEW_SCOPES)
    assert nobody["status"] == "none_on_record"
    monitor = FactChecksMonitor(env.conn)
    watch = monitor.create(h.NS, "verifica", watch="publisher", key="verifica.example.net", principal_id="alice",
                           scopes=h.SCOPES)
    SubscriptionStore(env.conn).commit_watermark(h.NS, 1)
    notes = monitor.run(watch["subscription_id"], principal_id="alice", scopes=h.SCOPES)["notifications"]
    assert notes and all(n["cites"]["revision_id"] for n in notes)

    # --- exclusions ------------------------------------------------------------------------------------------
    for answer in (early, late, article, claimant, nobody, bundle):
        assert forbidden_keys(answer) == []
        dumped = json.dumps(answer).casefold()
        assert not any(word in dumped for word in VERDICT_WORDS)
    assert late["exclusions"] == list(EXCLUSIONS)
    assert text_anywhere(env.conn, PERSONAL) == []
