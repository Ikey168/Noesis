"""Offline Funding & Grants harness: real adapters, pinned authored fixtures.

Every network response is served from ``tests/fixtures/funding`` through an
injected ``DurableHTTP`` transport, so the provider client, durable request
receipts, document store and normalization all run for real while nothing
leaves the process. Fixture provenance is recorded in each receipt
(``execution: injected``) and must never be reported as live coverage.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb

from src.ingestion.funding_providers import PROVIDER_HOSTS, FundingClient, FundingEvidenceStore, acquire
from src.ingestion.provider_execution import DurableHTTP

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "funding"
NS = "grants"
NOTICE = "Authored offline fixture; not a live capture"
SCOPES = {
    "knowledge:funding:read", "knowledge:funding:write", "knowledge:ingestion:execute",
    "namespace:grants:read", "namespace:grants:write",
    "knowledge:projects:read", "knowledge:projects:write",
    "knowledge:reports:read", "knowledge:reports:write",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write",
}
REVIEWER_SCOPES = SCOPES | {"knowledge:funding:review"}
EXIST_URL = "https://www.exist.de/EXIST/Navigation/EN/Start-up-grant/start-up-grant.html"
EXIST_TRANSFER_URL = "https://www.exist.de/EXIST/Navigation/EN/Transfer-of-research/transfer-of-research.html"
FDB_EXIST_URL = "https://www.foerderdatenbank.de/FDB/Content/DE/Foerderprogramm/Bund/BMWi/exist-gruendungsstipendium.html"
FDB_LOAN_URL = "https://www.foerderdatenbank.de/FDB/Content/DE/Foerderprogramm/Bund/KfW/startgeld-fixture.html"
FDB_STALE_URL = "https://www.foerderdatenbank.de/FDB/Content/DE/Foerderprogramm/Land/Berlin/removed-programme.html"
NLNET_FUND_URL = "https://nlnet.nl/commonsfund/"


def ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


class FixtureWeb:
    """URL -> fixture file routing; swap entries to simulate amendments/outages."""

    def __init__(self):
        self.routes = {
            "https://nlnet.nl/propose/": "nlnet_propose.html",
            NLNET_FUND_URL: "nlnet_commonsfund.html",
            "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/horizon-fixture-2026-01-01.json": "eu_topic_open.json",
            FDB_EXIST_URL: "fdb_exist_gruendungsstipendium.html",
            FDB_LOAN_URL: "fdb_startgeld_loan.html",
            EXIST_URL: "exist_gruendungsstipendium.html",
            EXIST_TRANSFER_URL: "exist_forschungstransfer.html",
        }
        self.calls = []

    def transport(self, *, method, url, params, body, headers, timeout_s, max_bytes):
        self.calls.append({"url": url, "params": dict(params)})
        if url.endswith("/search-api/prod/rest/search"):
            name = f"eu_search_page{params['pageNumber']}.json"
        else:
            name = self.routes.get(url)
        if name is None:
            return {"status": 404, "headers": {}, "content": b"not found"}
        kind = "application/json" if name.endswith(".json") else "text/html"
        return {"status": 200, "headers": {"Content-Type": kind}, "content": (FIXTURES / name).read_bytes()}


class Env:
    def __init__(self, now_iso="2026-09-25T09:00:00+00:00"):
        self.conn = duckdb.connect()
        self.web = FixtureWeb()
        self.clock = ms(now_iso)
        self.store = FundingEvidenceStore(self.conn, now=self.now)

    def now(self):
        return self.clock

    def client(self, provider, budget="b1"):
        http = DurableHTTP(self.conn, budget_id=f"{provider}-{budget}", provider=provider, principal_id="ingest",
                           allowed_hosts=PROVIDER_HOSTS[provider], reuse_notice=NOTICE, max_requests=200, max_bytes=1_000_000_000,
                           transport=self.web.transport, now=self.now)
        return FundingClient(http, principal_id="ingest")

    def acquire(self, provider, method, *args, observation, budget="b1", **kwargs):
        client = self.client(provider, budget)
        return acquire(client, provider, lambda: getattr(client, method)(*args, observation, **kwargs),
                       namespace=NS, scopes=SCOPES, reuse_notice=NOTICE, observation=observation, store=self.store)

    def acquire_all(self, round_key="r1"):
        results = [
            self.acquire("nlnet", "nlnet_calls", observation=f"{round_key}:nlnet"),
            self.acquire("nlnet", "nlnet_fund", NLNET_FUND_URL, observation=f"{round_key}:nlnet-fund"),
        ]
        results.append(self.acquire("eu-ft", "eu_search_all", observation=f"{round_key}:eu", text="fixture", page_size=2))
        results.append(self.acquire("eu-ft", "eu_topic", "HORIZON-FIXTURE-2026-01-01", observation=f"{round_key}:eu-topic"))
        for url in (EXIST_URL, EXIST_TRANSFER_URL):
            results.append(self.acquire("exist", "exist_programme", url, observation=f"{round_key}:exist:{url}"))
        for url in (FDB_EXIST_URL, FDB_LOAN_URL):
            results.append(self.acquire("foerderdatenbank", "foerderdatenbank_programme", url, observation=f"{round_key}:fdb:{url}"))
        return results


def founder_profile(env, *, owner="alice", scopes=SCOPES, key="founder", overrides=None):
    """A clearly synthetic applicant: a German graduate team building open search tooling."""
    from src.kb.funding_profiles import FundingProfileStore

    store = FundingProfileStore(env.conn, now=env.now)
    profile = store.create(NS, key, label="Synthetic founder team (fixture)", principal_id=owner, scopes=scopes)
    facts = {
        "applicant.kind": {"value": "informal-team"},
        "applicant.residence_country": {"value": "DE"},
        "applicant.incorporated": {"value": False},
        "applicant.university_affiliation": {"value": True, "evidence": [{"kind": "note", "id": "letter-of-support-draft"}]},
        "applicant.university_name": {"value": "Fixture University Berlin"},
        "applicant.academic_status": {"value": "graduate"},
        "applicant.team_size": {"value": 3},
        "project.title": {"value": "OpenSeek"},
        "project.summary": {"value": "An open-source, privacy-preserving search index for public-interest archives."},
        "project.stage": {"value": "prototype"},
        "project.themes": {"value": ["open source search", "privacy-enhancing technologies", "internet infrastructure"]},
        "project.licence": {"value": "AGPL-3.0-or-later"},
        "project.open_source": {"value": True},
        "project.funding_need": {"value": {"amount": "60000", "currency": "EUR"}},
        "project.duration_months": {"value": 12},
        "project.milestones": {"value": [{"title": "Indexer prototype", "month": 4}, {"title": "Public beta", "month": 10, "deliverable": "beta release"}]},
        "project.budget_lines": {"value": [
            {"category": "Personal living expenses", "amount": "45000", "currency": "EUR"},
            {"category": "Material expenses", "amount": "9000", "currency": "EUR"},
            {"category": "Coaching", "amount": "4000", "currency": "EUR"},
            {"category": "Office rent", "amount": "2000", "currency": "EUR"},
        ]},
        "preferences.timezone": {"value": "Europe/Berlin"},
        "preferences.max_effort": {"value": "medium"},
        "preferences.accept_co_financing": {"value": True},
    }
    facts.update(overrides or {})
    return store.update(NS, profile["profile_id"], "initial", 1, set_facts=facts, principal_id=owner, scopes=scopes)


def load_json(name):
    return json.loads((FIXTURES / name).read_text())
