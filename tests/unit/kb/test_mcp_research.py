import json
from pathlib import Path

import duckdb
import pytest

from src.kb.federation import FederationError
from src.kb.mcp_research import GitHubResearchSource, InteractiveEvidenceSession


class GitHubClient:
    version = "native-envelope-fixture"

    def __init__(self):
        self.calls = []
        self.body = "Captured evidence"
        self.error = False

    def list_tools(self):
        return [{"name": "issue_read"}, {"name": "pull_request_read"}]

    def list_resources(self):
        return []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error:
            return {
                "isError": True,
                "content": [{"type": "text", "text": "unavailable"}],
            }
        value = {
            "id": 123,
            "number": 7,
            "title": "Fixture",
            "body": self.body,
            "html_url": "https://github.com/org/repo/issues/7",
            "updated_at": "2026-09-07T00:00:00Z",
        }
        if arguments["method"] == "get_comments":
            value = [
                {
                    **value,
                    "id": 456,
                    "html_url": value["html_url"] + "#issuecomment-456",
                }
            ]
        return {"content": [{"type": "text", "text": json.dumps(value)}]}


def test_official_remote_issue_without_rest_database_id_keeps_stable_identity():
    envelope = json.loads(
        (Path(__file__).parents[2] / "fixtures/github_mcp/issue-1463.json").read_text()
    )
    record = json.loads(envelope["content"][0]["text"])
    assert "id" not in record and "node_id" not in record

    class Client(GitHubClient):
        def call_tool(self, name, arguments):
            return {"content": [{"type": "text", "text": json.dumps(record)}]}

    conn = duckdb.connect()
    source = GitHubResearchSource(
        conn, Client(), repositories=["Ikey168/Noesis"], namespace="r", principal_id="a"
    )
    request = {
        "repository": "Ikey168/Noesis",
        "record_kind": "issue",
        "number": 1463,
        "observation_id": "one",
    }
    first = source.query(request, scopes={"operator"})
    record.update(id=12345, body="Edited source")
    second = source.query({**request, "observation_id": "two"}, scopes={"operator"})
    assert first["items"][0]["id"] == second["items"][0]["id"]
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    assert (
        conn.execute("SELECT count(*) FROM document_revision_records").fetchone()[0]
        == 2
    )
    record["number"] = 1464
    with pytest.raises(FederationError, match="different issue"):
        source.query({**request, "observation_id": "wrong"}, scopes={"operator"})
    conn.close()


def test_capture_cites_offline_replays_and_preserves_edited_revisions(tmp_path):
    path = str(tmp_path / "evidence.duckdb")
    conn = duckdb.connect(path)
    client = GitHubClient()
    source = GitHubResearchSource(
        conn, client, repositories=["org/repo"], namespace="r", principal_id="alice"
    )
    request = {
        "repository": "org/repo",
        "number": 7,
        "record_kind": "issue",
        "observation_id": "one",
    }
    result = source.query(request, scopes={"operator"})
    identity = result["items"][0]["id"]
    assert (
        source.query(request, scopes={"operator"})["replayed"]
        and len(client.calls) == 1
    )
    client.body = "Edited evidence"
    changed = source.query({**request, "observation_id": "two"}, scopes={"operator"})
    assert changed["items"][0]["id"] == identity
    assert (
        changed["items"][0]["value"]["revision"]
        != result["items"][0]["value"]["revision"]
    )
    conn.close()
    conn = duckdb.connect(path)
    client.error = True
    reopened = GitHubResearchSource(
        conn, client, repositories=["org/repo"], namespace="r", principal_id="alice"
    )
    assert (
        reopened.captured("one", principal_id="alice", scopes={"operator"})["items"][0][
            "snapshot"
        ]
        == result["items"][0]["snapshot"]
    )
    with pytest.raises(FederationError, match="access"):
        reopened.captured("one", principal_id="alice", scopes=set())
    with pytest.raises(FederationError):
        reopened.query({**request, "observation_id": "failure"}, scopes={"operator"})
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
    conn.close()


def test_comments_pagination_and_repository_limits():
    conn = duckdb.connect()
    client = GitHubClient()
    source = GitHubResearchSource(
        conn, client, repositories=["org/repo"], namespace="r", principal_id="alice"
    )
    request = {
        "repository": "org/repo",
        "number": 7,
        "record_kind": "issue_comments",
        "observation_id": "one",
        "limit": 1,
    }
    result = source.query(request, scopes={"operator"})
    assert result["cursor"] == "2" and client.calls[0][1]["perPage"] == 1
    with pytest.raises(ValueError):
        source.query({**request, "repository": "another/repo"}, scopes={"operator"})
    conn.close()


@pytest.mark.parametrize(
    "reason",
    ["rate limit exceeded", "record deleted or inaccessible", "access revoked"],
)
def test_remote_failure_preserves_prior_evidence_and_consumes_request_budget(reason):
    class Client(GitHubClient):
        def call_tool(self, name, arguments):
            if self.error:
                return {"isError": True, "content": [{"type": "text", "text": reason}]}
            return super().call_tool(name, arguments)

    conn = duckdb.connect()
    client = Client()
    source = GitHubResearchSource(
        conn,
        client,
        repositories=["org/repo"],
        namespace="r",
        principal_id="alice",
        max_requests=2,
    )
    request = {
        "repository": "org/repo",
        "number": 7,
        "record_kind": "issue",
        "observation_id": "first",
    }
    first = source.query(request, scopes={"operator"})
    client.error = True
    with pytest.raises(FederationError):
        source.query({**request, "observation_id": "failed"}, scopes={"operator"})
    assert (
        source.captured("first", principal_id="alice", scopes={"operator"})["items"]
        == first["items"]
    )
    assert (
        conn.execute("SELECT count(*) FROM document_revision_records").fetchone()[0]
        == 1
    )
    with pytest.raises(FederationError) as failure:
        source.query({**request, "observation_id": "over-budget"}, scopes={"operator"})
    assert failure.value.code == "request_budget"
    conn.close()


class BrowserClient:
    version = "browser-fixture"

    def __init__(self):
        self.calls = []
        self.url = "https://www.berlin.de/evidence"

    def list_tools(self):
        return []

    def list_resources(self):
        return []

    def call_tool(self, name, arguments):
        self.calls.append(name)
        text = "Page URL: " + self.url + "\nSnapshot: Captured public evidence"
        return {"content": [{"type": "text", "text": text}]}


def test_interactive_capture_and_unconditional_cleanup():
    conn = duckdb.connect()
    client = BrowserClient()
    source = InteractiveEvidenceSession(
        conn,
        client,
        domains=["www.berlin.de"],
        namespace="r",
        principal_id="a",
        network_guard_confirmed=True,
    )
    result = source.acquire(client.url, "one", scopes={"operator"})
    assert result["status"] == "captured" and not result["precise_source_offsets"]
    assert client.calls[-1] == "browser_close"
    client.url = "https://evil.example/wrong"
    with pytest.raises(FederationError):
        source.acquire("https://www.berlin.de/evidence", "two", scopes={"operator"})
    assert client.calls[-1] == "browser_close"
    with pytest.raises(FederationError, match="cancelled"):
        source.acquire(
            "https://www.berlin.de/evidence",
            "three",
            scopes={"operator"},
            cancelled=lambda: True,
        )
    assert client.calls[-1] == "browser_close"
    conn.close()


def test_browser_cancellation_reaches_inflight_transport_and_always_closes():
    class Client(BrowserClient):
        supports_cancellation = True

        def call_tool(self, name, arguments, *, cancelled=None):
            self.calls.append(name)
            if name == "browser_navigate":
                assert cancelled is not None
                raise FederationError("cancelled", "in-flight request cancelled")
            return super().call_tool(name, arguments)

    conn = duckdb.connect()
    client = Client()
    source = InteractiveEvidenceSession(
        conn,
        client,
        domains=["www.berlin.de"],
        namespace="r",
        principal_id="a",
        network_guard_confirmed=True,
    )
    with pytest.raises(FederationError, match="in-flight"):
        source.acquire(
            client.url, "cancel", scopes={"operator"}, cancelled=lambda: False
        )
    assert client.calls[-1] == "browser_close"
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    assert (
        conn.execute("SELECT state FROM mcp_research_captures").fetchone()[0]
        == "failed"
    )
    conn.close()


@pytest.mark.parametrize("target_key", ["ref", "target"])
def test_browser_click_uses_advertised_schema(target_key):
    class Client(BrowserClient):
        def list_tools(self):
            return [
                {
                    "name": "browser_click",
                    "inputSchema": {
                        "type": "object",
                        "properties": {target_key: {"type": "string"}},
                        "required": [target_key],
                        "additionalProperties": False,
                    },
                }
            ]

        def call_tool(self, name, arguments):
            if name == "browser_click":
                assert arguments == {target_key: "e12"}
                self.url = "https://www.berlin.de/next"
            return super().call_tool(name, arguments)

    conn = duckdb.connect()
    client = Client()
    source = InteractiveEvidenceSession(
        conn,
        client,
        domains=["www.berlin.de"],
        namespace="r",
        principal_id="a",
        network_guard_confirmed=True,
    )
    result = source.acquire(
        client.url,
        "one",
        scopes={"operator"},
        actions=[
            {
                "kind": "click",
                "target": "e12",
                "expected_url": "https://www.berlin.de/next",
            }
        ],
    )
    assert result["url"].endswith("/next")
    assert client.calls.count("browser_snapshot") == 2
    conn.close()


def test_unapproved_intermediate_navigation_stops_before_next_action():
    class Client(BrowserClient):
        def list_tools(self):
            return [
                {
                    "name": "browser_click",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"target": {"type": "string"}},
                        "required": ["target"],
                    },
                }
            ]

        def call_tool(self, name, arguments):
            if name == "browser_navigate":
                self.url = "https://forbidden.example/wrong"
            if name == "browser_click":
                self.url = "https://www.berlin.de/next"
            return super().call_tool(name, arguments)

    conn = duckdb.connect()
    client = Client()
    source = InteractiveEvidenceSession(
        conn,
        client,
        domains=["www.berlin.de"],
        namespace="r",
        principal_id="a",
        network_guard_confirmed=True,
    )
    with pytest.raises(FederationError):
        source.acquire(
            "https://www.berlin.de/evidence",
            "one",
            scopes={"operator"},
            actions=[
                {
                    "kind": "click",
                    "target": "e12",
                    "expected_url": "https://www.berlin.de/next",
                }
            ],
        )
    assert "browser_click" not in client.calls and client.calls[-1] == "browser_close"
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    conn.close()


def test_browser_native_error_cannot_be_turned_into_capture():
    class Client(BrowserClient):
        def call_tool(self, name, arguments):
            result = super().call_tool(name, arguments)
            if name == "browser_navigate":
                result["isError"] = True
            return result

    conn = duckdb.connect()
    client = Client()
    source = InteractiveEvidenceSession(
        conn,
        client,
        domains=["www.berlin.de"],
        namespace="r",
        principal_id="a",
        network_guard_confirmed=True,
    )
    with pytest.raises(FederationError):
        source.acquire(client.url, "one", scopes={"operator"})
    assert client.calls[-1] == "browser_close"
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
    conn.close()
