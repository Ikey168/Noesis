"""Capture a bounded public research observation and verify offline replay.

The MCP endpoint must already be configured. Browser deployments must run the
Noesis network guard; credentials are read into memory and never serialized.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.kb.mcp_research import (
    GitHubResearchSource,
    InteractiveEvidenceSession,
    MCPCaptureStore,
)
from src.kb.mcp_transport import MCPHTTPClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=["github", "browser"], required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--network-approved", action="store_true")
    parser.add_argument("--repository")
    parser.add_argument("--number", type=int)
    parser.add_argument(
        "--record-kind",
        default="issue",
        choices=["issue", "pull_request", "issue_comments", "pull_request_comments"],
    )
    parser.add_argument("--url")
    parser.add_argument("--wait-text")
    parser.add_argument("--network-guard-confirmed", action="store_true")
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--gh-auth", action="store_true")
    args = parser.parse_args()
    if not args.network_approved:
        parser.error("explicit --network-approved is required")
    if args.kind == "github" and (not args.repository or not args.number):
        parser.error("GitHub requires --repository and --number")
    if args.kind == "browser" and (not args.url or not args.network_guard_confirmed):
        parser.error("browser requires --url and --network-guard-confirmed")
    import duckdb

    token = None
    if args.kind == "github":
        token = os.environ.get(args.token_env)
        if args.gh_auth:
            token = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            ).stdout.strip()
    observation = "live-" + str(time.time_ns())
    namespace, principal = "mcp-live-evaluation", "evaluation-operator"
    args.database.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "contract": "noesis-mcp-live-evaluation-v1",
        "kind": args.kind,
        "endpoint": args.endpoint,
        "database": str(args.database.resolve()),
        "observation": observation,
        "write_operations_on_source": False,
    }
    started = time.perf_counter()
    with duckdb.connect(str(args.database)) as conn:
        with MCPHTTPClient(
            args.endpoint, kind=args.kind, token=token, timeout_s=30, max_bytes=8000000
        ) as client:
            report["server_version"] = client.version
            report["allowed_tools"] = [tool["name"] for tool in client.list_tools()]
            if args.kind == "github":
                source = GitHubResearchSource(
                    conn,
                    client,
                    repositories=[args.repository],
                    namespace=namespace,
                    principal_id=principal,
                    max_requests=10,
                )
                report["capture"] = source.query(
                    {
                        "repository": args.repository,
                        "number": args.number,
                        "record_kind": args.record_kind,
                        "observation_id": observation,
                        "limit": 5,
                    },
                    scopes={"operator"},
                )
            else:
                source = InteractiveEvidenceSession(
                    conn,
                    client,
                    domains=[urlsplit(args.url).hostname],
                    namespace=namespace,
                    principal_id=principal,
                    network_guard_confirmed=True,
                )
                report["capture"] = source.acquire(
                    args.url,
                    observation,
                    scopes={"operator"},
                    actions=[{"kind": "wait", "text": args.wait_text}]
                    if args.wait_text
                    else [],
                )
        report["transport_thread_closed"] = not client._thread.is_alive()
    # Reopen the durable store only after the real transport has shut down.
    with duckdb.connect(str(args.database)) as conn:
        replay = MCPCaptureStore(conn).replay(
            namespace,
            args.kind,
            observation,
            None,
            principal_id=principal,
            scopes={"operator"},
        )
        report["offline_replay"] = replay
        report["replay_verified"] = replay is not None and replay.get("replayed", False)
        report["captured_documents"] = [
            {"document_id": row[0], "url": row[1], "content": row[2]}
            for row in conn.execute(
                "SELECT document_id,url,content FROM documents"
            ).fetchall()
        ]
    report["elapsed_seconds"] = time.perf_counter() - started
    report["evidence_semantics"] = (
        "Live captured MCP representations; neither original HTTP response bytes nor independent human quality judgments."
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(
        json.dumps(
            {
                key: report[key]
                for key in [
                    "kind",
                    "server_version",
                    "transport_thread_closed",
                    "replay_verified",
                    "elapsed_seconds",
                ]
            }
        )
    )


if __name__ == "__main__":
    main()
