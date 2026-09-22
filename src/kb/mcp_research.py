"""GitHub and interactive browser evidence through existing MCP federation.

Captured tool responses are labelled MCP representations, not upstream HTTP
bytes. Local citations remain usable without the remote server, under current
Noesis access controls. Failed remote reads never fabricate deletion events.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

from services.ingest.common.document_model import Document
from src.ingestion.document_store import DocumentStore
from src.ingestion.snapshots import SnapshotStore
from src.kb.federation import (
    FederationError,
    RemoteMCPAdapter,
    _envelope,
    source_definition,
)
from src.kb.mcp_transport import BROWSER_TOOLS, GITHUB_TOOLS


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _authorize(namespace, principal_id, scopes, *, write=False):
    if (
        not principal_id
        or "operator" not in scopes
        and (
            "knowledge:federation:read" not in scopes
            or f"namespace:{namespace}:{'write' if write else 'read'}" not in scopes
        )
    ):
        raise FederationError(
            "unauthorized", "current federation and namespace access required"
        )
    if (
        write
        and "operator" not in scopes
        and "knowledge:ingestion:execute" not in scopes
    ):
        raise FederationError(
            "unauthorized", "capture requires explicit local ingestion permission"
        )


def decode_tool(value):
    if not isinstance(value, dict) or value.get("isError"):
        raise FederationError(
            "remote_tool_error", "MCP returned invalid/error evidence"
        )
    if isinstance(value.get("structuredContent"), (dict, list)):
        return value["structuredContent"]
    parts = value.get("content", [])
    texts = [part["text"] for part in parts if part.get("type") == "text"]
    if len(texts) != 1:
        raise FederationError("schema_drift", "expected one native JSON text result")
    try:
        return json.loads(texts[0])
    except ValueError:
        raise FederationError(
            "schema_drift", "GitHub response was not structured JSON"
        ) from None


class MCPCaptureStore:
    def __init__(self, conn):
        self.conn, self.documents, self.snapshots = (
            conn,
            DocumentStore(conn),
            SnapshotStore(conn),
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS mcp_research_captures(namespace TEXT,source TEXT,observation TEXT,request_hash TEXT,owner TEXT,state TEXT,result_json TEXT,PRIMARY KEY(namespace,source,observation))"
        )

    def replay(self, namespace, source, observation, request, *, principal_id, scopes):
        _authorize(namespace, principal_id, scopes)
        row = self.conn.execute(
            "SELECT request_hash,owner,state,result_json FROM mcp_research_captures WHERE namespace=? AND source=? AND observation=?",
            [namespace, source, observation],
        ).fetchone()
        if not row:
            return None
        if row[1] != principal_id and "operator" not in scopes:
            raise FederationError(
                "unauthorized", "capture belongs to another principal"
            )
        if request is not None and row[0] != _hash(request):
            raise FederationError(
                "capture_conflict", "observation key is bound to another request"
            )
        if row[2] != "completed":
            raise FederationError(
                "capture_unavailable", "previous capture failed or is indeterminate"
            )
        return {**json.loads(row[3]), "replayed": True}

    def reserve(self, namespace, source, observation, request, *, principal_id, scopes):
        _authorize(namespace, principal_id, scopes, write=True)
        if not isinstance(observation, str) or not 1 <= len(observation) <= 256:
            raise ValueError("explicit bounded observation key required")
        self.conn.execute(
            "INSERT INTO mcp_research_captures VALUES (?,?,?,?,?,'reserved',NULL)",
            [namespace, source, observation, _hash(request), principal_id],
        )

    def finish(self, namespace, source, observation, result):
        self.conn.execute(
            "UPDATE mcp_research_captures SET state='completed',result_json=? WHERE namespace=? AND source=? AND observation=? AND state='reserved'",
            [_json(result), namespace, source, observation],
        )

    def fail(self, namespace, source, observation):
        self.conn.execute(
            "UPDATE mcp_research_captures SET state='failed' WHERE namespace=? AND source=? AND observation=? AND state='reserved'",
            [namespace, source, observation],
        )


class GitHubResearchSource:
    def __init__(
        self, conn, client, *, repositories, namespace, principal_id, max_requests=100
    ):
        if (
            not repositories
            or len(repositories) > 50
            or any(
                not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value)
                for value in repositories
            )
        ):
            raise ValueError("explicit repository allowlist required")
        if type(max_requests) is not int or not 1 <= max_requests <= 1000:
            raise ValueError("invalid GitHub request budget")
        self.store = MCPCaptureStore(conn)
        self.namespace, self.principal_id = namespace, principal_id
        self.repositories, self.max_requests = set(repositories), max_requests
        self.remote = RemoteMCPAdapter(
            "github-research-remote",
            client,
            tools=sorted(GITHUB_TOOLS),
            limits={"max_results": 100, "max_bytes": 4_000_000, "timeout_ms": 60000},
        )
        self.definition = source_definition(
            "github-research",
            "mcp",
            capabilities=["github-read"],
            schemas={"record": "issue|pull_request|comment"},
            limits={"max_results": 100, "max_bytes": 4_000_000, "timeout_ms": 60000},
            temporal_support="captured-revisions",
        )

    def describe(self):
        return self.definition

    def query(self, request, *, scopes):
        _authorize(self.namespace, self.principal_id, scopes, write=True)
        started = time.monotonic()
        repository, kind, number = (
            request.get("repository"),
            request.get("record_kind"),
            request.get("number"),
        )
        if (
            repository not in self.repositories
            or kind
            not in {"issue", "pull_request", "issue_comments", "pull_request_comments"}
            or type(number) is not int
            or number < 1
        ):
            raise ValueError("allowlisted repository, record kind and number required")
        page, limit = request.get("page", 1), request.get("limit", 50)
        if (
            type(page) is not int
            or not 1 <= page <= 100
            or type(limit) is not int
            or not 1 <= limit <= 100
        ):
            raise ValueError("bounded explicit pagination required")
        observation = request.get("observation_id")
        previous = self.store.replay(
            self.namespace,
            "github",
            observation,
            request,
            principal_id=self.principal_id,
            scopes=scopes,
        )
        if previous:
            return previous
        count = self.store.conn.execute(
            "SELECT count(*) FROM mcp_research_captures WHERE namespace=? AND source='github' AND owner=?",
            [self.namespace, self.principal_id],
        ).fetchone()[0]
        if count >= self.max_requests:
            raise FederationError(
                "request_budget", "GitHub capture request ceiling reached"
            )
        self.store.reserve(
            self.namespace,
            "github",
            observation,
            request,
            principal_id=self.principal_id,
            scopes=scopes,
        )
        owner, repo = repository.split("/")
        pr = kind.startswith("pull_request")
        tool = "pull_request_read" if pr else "issue_read"
        args = {
            "owner": owner,
            "repo": repo,
            "method": "get_comments" if kind.endswith("comments") else "get",
            "pullNumber" if pr else "issue_number": number,
        }
        if kind.endswith("comments"):
            args.update(page=page, perPage=limit)
        try:
            response = self.remote.query(
                {"kind": "tool", "name": tool, "arguments": args}, scopes=scopes
            )
            native_result = response["items"][0]["value"]
            payload = decode_tool(native_result)
            records = (
                payload.get("comments")
                if isinstance(payload, dict) and kind.endswith("comments")
                else payload
            )
            if not kind.endswith("comments"):
                records = [records]
            if not isinstance(records, list) or len(records) > limit:
                raise FederationError(
                    "schema_drift", "unexpected GitHub record envelope/count"
                )
            url = (
                f"https://github.com/{repository}/{'pull' if pr else 'issues'}/{number}"
            )
            stamp = response["provenance"]["observed_at_ms"]
            snapshot = self.store.snapshots.snapshot_bytes(
                url,
                _json(native_result).encode(),
                stamp,
                content_type="application/json",
                final_url=url,
            )
            docs, items = [], []
            for record in records:
                if not isinstance(record, dict):
                    raise FederationError(
                        "schema_drift", "native GitHub record must be an object"
                    )
                # The official MCP issue projection omits REST id/node_id.
                # Repository + kind + number is the stable public identity for
                # issues/PRs, regardless of which optional database ID is exposed.
                identity = (
                    record.get("id") or record.get("node_id")
                    if kind.endswith("comments")
                    else url
                )
                if identity is None or not record.get("updated_at"):
                    raise FederationError(
                        "schema_drift", "GitHub identity and revision timestamp missing"
                    )
                record_url = record.get("html_url") or record.get("url") or url
                # Some native GraphQL adapters return an API URL instead of HTML;
                # only repository-bound GitHub URLs are accepted as citation IDs.
                parsed = urlsplit(record_url)
                if parsed.hostname == "api.github.com" and parsed.path.startswith(
                    f"/repos/{repository}/"
                ):
                    record_url = url
                elif (
                    parsed.scheme != "https"
                    or parsed.hostname != "github.com"
                    or not parsed.path.startswith(f"/{repository}/")
                ):
                    raise FederationError(
                        "source_identity", "GitHub record belongs to another repository"
                    )
                if not kind.endswith("comments") and record.get("number") != number:
                    raise FederationError(
                        "source_identity", "GitHub returned a different issue or PR"
                    )
                document_id = "github:" + _hash([repository, kind, str(identity)])[:32]
                body = record.get("body") or ""
                if not isinstance(body, str) or len(body) > 1000000:
                    raise FederationError(
                        "result_too_large", "GitHub source body exceeds budget"
                    )
                title = (
                    record.get("title") or f"{repository} #{number} {kind} {identity}"
                )
                metadata = {
                    "namespace": self.namespace,
                    "provider": "github-mcp",
                    "native_id": str(identity),
                    "repository": repository,
                    "record_kind": "comment" if kind.endswith("comments") else kind,
                    "updated_at": record["updated_at"],
                    "captured_revision_sha256": _hash(record),
                    "native_capture_sha256": snapshot["digest"],
                    "representation": "mcp-tool-result",
                    "merged": record.get("merged"),
                    "merged_at": record.get("merged_at"),
                    "source_state": record.get("state"),
                    "proposal_is_merged_code": False,
                    "native_record_json": _json(record),
                }
                docs.append(
                    Document(
                        document_id=document_id,
                        source_type="web",
                        source_id="github:" + str(identity),
                        language="und",
                        ingested_at=stamp,
                        url=record_url,
                        title=title,
                        content=body or title,
                        metadata=metadata,
                    )
                )
                items.append(
                    {
                        "id": document_id,
                        "type": kind,
                        "value": {
                            "url": record_url,
                            "revision": _hash(record),
                            "native_id": str(identity),
                        },
                        "snapshot": snapshot,
                    }
                )
            if docs and self.store.documents.upsert(docs).invalid:
                raise FederationError(
                    "document_validation",
                    "GitHub evidence rejected by document contract",
                )
            # A full page means MAYBE more, not proof of a stable remote snapshot.
            cursor = (
                str(page + 1)
                if kind.endswith("comments") and len(records) == limit
                else None
            )
            result = _envelope(
                self.definition,
                request,
                items,
                started=started,
                cursor=cursor,
                warnings=[
                    "Remote pagination is not atomic; captured revisions are immutable."
                ],
            )
            self.store.finish(self.namespace, "github", observation, result)
            return result
        except Exception:
            self.store.fail(self.namespace, "github", observation)
            raise

    def captured(self, observation_id, *, principal_id, scopes):
        return self.store.replay(
            self.namespace,
            "github",
            observation_id,
            None,
            principal_id=principal_id,
            scopes=scopes,
        )


class InteractiveEvidenceSession:
    """Bounded interactive read capture; requires server-side guarded networking.

    The packaged Playwright init-page guard blocks undeclared origins, non-GET
    methods and redirects. The client also verifies the observed page URL after
    each action. This is not a browser sandbox for hostile downloaded programs.
    """

    def __init__(
        self,
        conn,
        client,
        *,
        domains,
        namespace,
        principal_id,
        network_guard_confirmed=False,
    ):
        if (
            not domains
            or len(domains) > 20
            or any(not re.fullmatch(r"[a-z0-9.-]+", value) for value in domains)
        ):
            raise ValueError("explicit bare allowed domains required")
        if not network_guard_confirmed:
            raise ValueError(
                "configure the supplied server network guard before interactive acquisition"
            )
        self.domains, self.namespace, self.principal_id = (
            set(domains),
            namespace,
            principal_id,
        )
        self.store = MCPCaptureStore(conn)
        self.remote = RemoteMCPAdapter(
            "playwright-research",
            client,
            tools=sorted(BROWSER_TOOLS),
            limits={"max_results": 20, "max_bytes": 8_000_000, "timeout_ms": 60000},
        )

    def _url(self, url):
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in self.domains
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
        ):
            raise FederationError(
                "domain_forbidden", "navigation outside declared public HTTPS origins"
            )
        return url

    def acquire(
        self,
        url,
        observation_id,
        *,
        scopes,
        actions=(),
        cancelled=None,
        screenshot=False,
    ):
        _authorize(self.namespace, self.principal_id, scopes, write=True)
        self._url(url)
        if not isinstance(actions, (list, tuple)) or len(actions) > 10:
            raise ValueError("interactive action budget exceeded")
        for action in actions:
            if not isinstance(action, dict) or action.get("kind") not in {
                "click",
                "wait",
            }:
                raise ValueError(
                    "only explicit read-navigation clicks or bounded waits are supported"
                )
            if action["kind"] == "click":
                self._url(action.get("expected_url", ""))
                if (
                    not isinstance(action.get("target"), str)
                    or not 1 <= len(action["target"]) <= 500
                ):
                    raise ValueError("bounded snapshot target required")
            elif (
                not isinstance(action.get("text"), str)
                or not 1 <= len(action["text"]) <= 1000
            ):
                raise ValueError("bounded wait-for text required")
        request = {"url": url, "actions": list(actions), "screenshot": screenshot}
        previous = self.store.replay(
            self.namespace,
            "browser",
            observation_id,
            request,
            principal_id=self.principal_id,
            scopes=scopes,
        )
        if previous:
            return previous
        self.store.reserve(
            self.namespace,
            "browser",
            observation_id,
            request,
            principal_id=self.principal_id,
            scopes=scopes,
        )
        captures = []

        def call(tool, arguments):
            if cancelled and cancelled():
                raise FederationError("cancelled", "browser acquisition cancelled")
            result = self.remote.query(
                {"kind": "tool", "name": tool, "arguments": arguments},
                scopes=scopes,
                cancelled=cancelled,
            )
            value = result["items"][0]["value"]
            if not isinstance(value, dict) or value.get("isError"):
                raise FederationError(
                    "remote_tool_error", "browser error is not captured source evidence"
                )
            return value

        def click_arguments(action):
            # Negotiate the connected server's schema; supported releases differ
            # between target and ref. Never fall back to arbitrary code/selectors.
            tools = self.remote.capabilities(scopes=scopes)["advertised"]["tools"]
            selected = [tool for tool in tools if tool.get("name") == "browser_click"]
            schema = selected[0].get("inputSchema", {}) if len(selected) == 1 else {}
            properties = schema.get("properties", {})
            key = (
                "target"
                if "target" in properties
                else "ref"
                if "ref" in properties
                else None
            )
            if key is None:
                raise FederationError(
                    "schema_drift",
                    "browser click needs a supported advertised snapshot target schema",
                )
            arguments = {key: action["target"]}
            if "element" in schema.get("required", []):
                arguments["element"] = "Snapshot target " + action["target"]
            from jsonschema import Draft202012Validator, ValidationError

            try:
                Draft202012Validator(schema).validate(arguments)
            except ValidationError as exc:
                raise FederationError(
                    "schema_drift", "unsupported browser click arguments"
                ) from exc
            return arguments

        def observe(expected):
            snapshot = call("browser_snapshot", {})
            text = "\n".join(
                part["text"]
                for part in snapshot.get("content", [])
                if part.get("type") == "text"
            )
            urls = re.findall(r"Page URL:\s*(https?://[^\s]+)", text)
            if not urls:
                raise FederationError(
                    "source_identity",
                    "browser snapshot did not expose its actual page URL",
                )
            observed = self._url(urls[-1])
            if observed != expected:
                raise FederationError(
                    "unexpected_navigation",
                    "browser navigated somewhere other than the approved destination",
                )
            captures.append(snapshot)
            return snapshot, text, observed

        try:
            click_payloads = {
                i: click_arguments(action)
                for i, action in enumerate(actions)
                if action["kind"] == "click"
            }
            captures.append(call("browser_navigate", {"url": url}))
            current_url = url
            snapshot_result, text, observed_url = observe(current_url)
            for index, action in enumerate(actions):
                if action["kind"] == "click":
                    captures.append(call("browser_click", click_payloads[index]))
                    current_url = action["expected_url"]
                else:
                    captures.append(call("browser_wait_for", {"text": action["text"]}))
                # Verify every action boundary before allowing the next one, not
                # just the final URL after an unapproved intermediate navigation.
                snapshot_result, text, observed_url = observe(current_url)
            stamp = int(time.time() * 1000)
            raw = _json(snapshot_result).encode()
            if len(raw) > 4_000_000:
                raise FederationError(
                    "result_too_large", "browser evidence exceeded capture bound"
                )
            receipt = self.store.snapshots.snapshot_bytes(
                observed_url,
                raw,
                stamp,
                content_type="application/json",
                final_url=observed_url,
            )
            images = []
            if screenshot:
                result = call("browser_take_screenshot", {"type": "png"})
                for part in result.get("content", []):
                    if part.get("type") == "image" and part.get("mimeType") in {
                        "image/png",
                        "image/jpeg",
                    }:
                        data = base64.b64decode(part["data"], validate=True)
                        if len(data) > 4_000_000:
                            raise FederationError(
                                "result_too_large",
                                "screenshot exceeded its byte budget",
                            )
                        images.append(
                            self.store.snapshots.snapshot_bytes(
                                observed_url,
                                data,
                                stamp,
                                content_type=part["mimeType"],
                                final_url=observed_url,
                            )
                        )
            identity = "browser-mcp:" + _hash(observed_url)[:32]
            document = Document(
                document_id=identity,
                source_type="web",
                source_id=observed_url,
                language="und",
                ingested_at=stamp,
                url=observed_url,
                title="Interactive source observation",
                content=text,
                metadata={
                    "namespace": self.namespace,
                    "representation": "accessibility-snapshot-not-original-html",
                    "capture_sha256": receipt["digest"],
                    "precise_source_offsets": False,
                    "screenshots_json": _json(images),
                    "action_trace_sha256": _hash(captures),
                },
            )
            if self.store.documents.upsert([document]).invalid:
                raise FederationError(
                    "document_validation", "browser evidence failed document validation"
                )
            output = {
                "status": "captured",
                "document_id": identity,
                "url": observed_url,
                "snapshot": receipt,
                "screenshots": images,
                "observed_at_ms": stamp,
                "precise_source_offsets": False,
            }
            self.store.finish(self.namespace, "browser", observation_id, output)
            return output
        except Exception:
            self.store.fail(self.namespace, "browser", observation_id)
            raise
        finally:
            # Cleanup is unconditional, including cancellation and invalid URLs
            # discovered after server-side navigation.
            try:
                self.remote.client.call_tool("browser_close", {})
            except Exception:  # noqa: BLE001 - cleanup failure must not hide original capture outcome
                self.cleanup_status = "browser_close_failed"
