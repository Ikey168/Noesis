"""Offline Funding & Grants acceptance journey (#1776).

Real local adapters, DurableHTTP, DocumentStore, research projects, authored
reports, quantitative receipts and subscriptions run against pinned *authored
provider fixtures* (see tests/fixtures/funding/README.md). This is offline
evidence only; it is not live provider coverage.
"""

import json

import duckdb
import pytest

from src.kb.funding_bundle import readiness, set_enabled
from src.kb.funding_eligibility import EligibilityService
from src.kb.funding_monitoring import FundingMonitor
from src.kb.funding_profiles import FundingProfileError, FundingProfileStore
from src.kb.funding_ranking import ShortlistService
from src.kb.funding_workspaces import FundingDraftService, FundingWorkspaceStore
from tests.unit.funding import harness
from tests.unit.funding.harness import NS, SCOPES, founder_profile

EU_TOPIC = "https://ec.europa.eu/info/funding-tenders/opportunities/data/topicDetails/horizon-fixture-2026-01-01.json"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import httpx

    def refuse(*args, **kwargs):
        raise AssertionError("offline acceptance must not open network connections")

    monkeypatch.setattr(httpx, "Client", refuse)


def _file_env(path):
    env = harness.Env()
    env.conn.close()
    env.conn = duckdb.connect(path)
    env.store = harness.FundingEvidenceStore(env.conn, now=env.now)
    return env


def _bucket(shortlist, title):
    return next(i for i in shortlist["items"] if title in i["title"])


def test_profile_to_application_journey_with_changes_restart_and_isolation(tmp_path):
    path = str(tmp_path / "journey.duckdb")
    env = _file_env(path)
    receipt = {"evidence": "offline-authored-fixtures", "not_live_coverage": True, "steps": []}

    # 1. Discovery across all four providers through the real adapters.
    acquired = env.acquire_all()
    assert all(r["ok"] for r in acquired)
    assert {r["execution"] for r in acquired} == {"injected"}
    receipt["steps"].append({"step": "discovery", "observations": [r["observation_id"] for r in acquired]})

    # 2. Profiles: eligible, ineligible and unknown applicants (clearly synthetic).
    founder = founder_profile(env)
    incorporated = founder_profile(env, key="company", overrides={"applicant.kind": {"value": "company"},
                                                                  "applicant.incorporated": {"value": True}})
    sparse = FundingProfileStore(env.conn, now=env.now).create(NS, "sparse", label="Unknown applicant", principal_id="alice", scopes=SCOPES)
    service = ShortlistService(env.conn, now=env.now)
    shortlists = {name: service.build(NS, profile["profile_id"], principal_id="alice", scopes=SCOPES)
                  for name, profile in (("founder", founder), ("company", incorporated), ("sparse", sparse))}
    exist = "EXIST Business Start-up"
    assert _bucket(shortlists["founder"], exist)["verdict"] == "eligible"
    assert _bucket(shortlists["founder"], exist)["bucket"] == "apply_now"
    assert _bucket(shortlists["company"], exist)["verdict"] == "ineligible"
    assert _bucket(shortlists["company"], exist)["bucket"] == "excluded"
    assert _bucket(shortlists["sparse"], exist)["verdict"] == "needs_clarification"
    assert not shortlists["sparse"]["buckets"]["apply_now"]

    # 3. Programme vs call, award vs opportunity, budget vs grant size, stages, duplicates.
    founder_list = shortlists["founder"]
    directory = _bucket(founder_list, "EXIST-Gründungsstipendium")
    assert directory["record_kind"] == "directory_entry" and directory["bucket"] == "watch"
    transfer = _bucket(founder_list, "Transfer of Research")
    assert transfer["state"] == "programme" and transfer["bucket"] != "apply_now"
    eu = _bucket(founder_list, "Open-source internet infrastructure")
    assert "30000000" not in json.dumps(eu["criteria"]["usable_funding"])
    two_stage = _bucket(founder_list, "two-stage")
    assert two_stage["state"] == "forthcoming" and two_stage["bucket"] == "watch"
    assert len({i["opportunity_id"] for i in founder_list["items"]}) == len(founder_list["items"]) == 9
    assert _bucket(founder_list, "Quantum")["bucket"] == "excluded"
    assert all("TENDER" not in i["title"] and "procurement" not in i["title"].lower() for i in founder_list["items"])

    # 4. Workspace, checklist and cited draft for the eligible call.
    workspaces = FundingWorkspaceStore(env.conn, now=env.now)
    exist_item = _bucket(founder_list, exist)
    workspace = workspaces.create(NS, "exist", shortlist_id=founder_list["shortlist_id"], opportunity_id=exist_item["opportunity_id"],
                                  principal_id="alice", scopes=SCOPES)
    draft = FundingDraftService(env.conn, now=env.now).generate(NS, workspace["workspace_id"], "draft", principal_id="alice", scopes=SCOPES)
    assert draft["budget"]["calculation_id"] and draft["unanswered"] == ["project.achievements"]
    eu_workspace = workspaces.create(NS, "eu", shortlist_id=founder_list["shortlist_id"], opportunity_id=eu["opportunity_id"],
                                     principal_id="alice", scopes=SCOPES)
    receipt["steps"].append({"step": "preparation", "workspaces": [workspace["workspace_id"], eu_workspace["workspace_id"]],
                             "draft": draft["draft_id"], "calculation": draft["budget"]["calculation_id"]})

    # 5. Monitoring picks up an amendment (deadline shift across timezones) and flags the workspace.
    monitor = FundingMonitor(env.conn, now=env.now)
    subscription = monitor.create(NS, founder["profile_id"], "watch", providers=["nlnet", "eu-ft", "exist", "foerderdatenbank"],
                                  principal_id="alice", scopes=SCOPES)
    monitor.run(subscription["subscription_id"], 1, principal_id="alice", scopes=SCOPES)
    env.web.routes[EU_TOPIC] = "eu_topic_open_amended.json"
    amended = env.acquire("eu-ft", "eu_topic", "HORIZON-FIXTURE-2026-01-01", observation="amendment")
    assert amended["ok"] and amended["revised"]
    changes = monitor.run(subscription["subscription_id"], 2, principal_id="alice", scopes=SCOPES)
    assert "deadline_shift" in {n["kind"] for n in changes["notifications"]}
    assert changes["workspaces_to_reassess"] == [eu_workspace["workspace_id"]]
    assert all(n["timezone"] == "Europe/Berlin" for n in changes["notifications"])
    receipt["steps"].append({"step": "monitoring", "notifications": [n["notification_id"] for n in changes["notifications"]]})
    env.conn.close()

    # 6. Restart: reopen the warehouse; replays are idempotent and state is intact.
    env = _file_env(path)
    replayed = env.acquire_all(round_key="after-restart")
    assert all(r["ok"] and not r["created"] for r in replayed)
    assert FundingMonitor(env.conn, now=env.now).run(subscription["subscription_id"], 2, principal_id="alice",
                                                     scopes=SCOPES)["status"] == "replayed"
    view = FundingWorkspaceStore(env.conn, now=env.now).inspect(NS, eu_workspace["workspace_id"], principal_id="alice", scopes=SCOPES)
    assert not view["freshness"]["current"]
    status = readiness(env.conn, NS, scopes=SCOPES)
    assert status["entry_points"]["discovery"] == "fixture-only"

    # 7. Isolation and revocation.
    eligibility = EligibilityService(env.conn, now=env.now)
    with pytest.raises(FundingProfileError):
        eligibility.assess(NS, founder["profile_id"], exist_item["opportunity_id"], principal_id="mallory", scopes=SCOPES)
    with pytest.raises(Exception):
        FundingWorkspaceStore(env.conn).inspect(NS, workspace["workspace_id"], principal_id="alice",
                                                scopes=SCOPES - {"namespace:grants:read", "namespace:grants:write"})
    public_rows = env.conn.execute("SELECT record_json FROM funding_provider_assertions").fetchall()
    assert not any("Fixture University Berlin" in row[0] or founder["profile_id"] in row[0] for row in public_rows)
    documents = env.conn.execute("SELECT content FROM documents").fetchall() if env.conn.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name='documents'").fetchone() else []
    assert not any("Fixture University Berlin" in (row[0] or "") for row in documents)

    # 8. Shared-provider disablement leaves other consumers working.
    set_enabled(env.conn, NS, False, principal_id="root", scopes={"operator"})
    assert env.acquire("nlnet", "nlnet_calls", observation="shared-after-disable")["ok"]
    receipt["steps"].append({"step": "restart-isolation-disablement", "verified": True})
    env.conn.close()

    receipt["limitations"] = [
        "Provider payloads are authored fixtures mirroring response shapes; they are not live captures.",
        "Recording of this receipt is not execution against live providers; see scripts/funding_live_check.py.",
    ]
    (tmp_path / "funding-offline-receipt.json").write_text(json.dumps(receipt, indent=2))
    assert json.loads((tmp_path / "funding-offline-receipt.json").read_text())["not_live_coverage"] is True
