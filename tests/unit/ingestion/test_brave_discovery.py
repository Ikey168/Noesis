import json
import pytest
from src.ingestion.brave_discovery import BraveDiscovery


def test_pages_candidates_not_evidence_and_budget_before_request():
    calls = []

    def transport(**kw):
        calls.append(kw)
        page = kw["params"]["offset"]
        return {
            "content": json.dumps(
                {
                    "query": {"more_results_available": page == 0},
                    "web": {
                        "results": [
                            {
                                "url": f"https://example.org/{page}",
                                "title": "Title",
                                "description": "Unverified snippet",
                            }
                        ]
                    },
                }
            )
        }

    client = BraveDiscovery(
        api_key="private",
        transport=transport,
        per_request_cost_micros=5,
        budget_micros=10,
    )
    refs = list(client.discover("question", max_pages=2, freshness="pw"))
    assert len(refs) == 2 and all(
        ref.metadata["requires_source_acquisition"] for ref in refs
    )
    assert "private" not in json.dumps(client.receipts)
    with pytest.raises(ValueError, match="budget"):
        list(client.discover("question"))
    assert len(calls) == 2


def test_project_reservation_and_local_replay():
    import duckdb
    from src.kb.research_projects import ResearchProjectStore, READ_SCOPE, WRITE_SCOPE

    scopes = {
        READ_SCOPE,
        WRITE_SCOPE,
        "namespace:research:write",
        "domain:research:read",
    }
    auth = {"principal_id": "alice", "scopes": scopes}
    projects = ResearchProjectStore(duckdb.connect())
    project = projects.create(
        "research",
        "brave-test",
        questions=["Question"],
        success_criteria=["Sources"],
        scope={"domains": ["research"], "namespaces": ["research"]},
        budget={"requests": 1, "usd_micros": 5},
        **auth,
    )
    calls = []

    def transport(**kw):
        calls.append(1)
        return {"content": '{"web":{"results":[]}}'}

    client = BraveDiscovery(
        api_key="private",
        transport=transport,
        per_request_cost_micros=5,
        budget_micros=5,
    )
    first = client.discover_for_project(
        projects, "research", project["project_id"], "request", "question", **auth
    )
    second = client.discover_for_project(
        projects, "research", project["project_id"], "request", "question", **auth
    )
    assert first == second and len(calls) == 1
    assert (
        projects.inspect("research", project["project_id"], **auth)["spent"][
            "usd_micros"
        ]
        == 5
    )
