import asyncio
import copy
import json
from pathlib import Path

import duckdb
from jsonschema import Draft202012Validator

from src.ingestion.revisions import DocumentRevisionStore
from src.kb.authored_reports import AuthoredReportStore
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit.kb.test_authored_reports import CONTENT
from tests.unit.kb.test_investigation_comparisons import setup
from tests.unit.kb.test_investigation_templates import DEFINITION, install
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.investigations import (
    ALERT_READS,
    ALERT_WRITES,
    COMPARISON_READS,
    COMPARISON_WRITES,
    TEMPLATE_READS,
    TEMPLATE_WRITES,
)


def test_public_workflows_reopen_with_generated_schemas(tmp_path, monkeypatch):
    path = str(tmp_path / "public.duckdb")
    conn, _store, left, right = setup(duckdb.connect(path))
    install(conn)
    source = DocumentRevisionStore(conn).observe(
        {"document_id": "doc", "content": "The reported value increased."}
    )
    content = copy.deepcopy(CONTENT)
    dep = content["sections"][0]["assertions"][0]["dependencies"][0]
    dep["revision"] = dep["locator"]["revision_id"] = source["revision_id"]
    report = AuthoredReportStore(conn).create(
        "r", "report", content, principal_id="alice", scopes={"operator"}
    )
    conn.close()
    scopes = {"operator"}
    monkeypatch.setattr(server, "_context", lambda: ("alice", scopes))
    monkeypatch.setattr(
        server,
        "_connection",
        lambda *, read_only: duckdb.connect(path, read_only=read_only),
    )
    tools = asyncio.run(server.mcp.get_tools())

    def call(name, **kwargs):
        result = tools[name].fn(**kwargs)
        assert "error" not in result, result
        return result

    template = call(
        "create_investigation_template",
        namespace="r",
        request_key="template",
        definition=DEFINITION,
    )
    identity = {
        "namespace": "r",
        "template_id": template["template_id"],
        "revision": 1,
        "parameters": {"topic": "Berlin"},
    }
    preview = call("preview_investigation_template", **identity)
    assert preview["can_instantiate"]
    project = call(
        "instantiate_investigation_template",
        **identity,
        request_key="templated",
        budget={},
    )
    Draft202012Validator(
        json.loads(
            Path(
                "contracts/schemas/jsonschema/noesis-research-project-v1.json"
            ).read_text()
        )
    ).validate(project)
    assert (
        call(
            "inspect_research_project", namespace="r", project_id=project["project_id"]
        )["template_origin"]["template_revision"]
        == 1
    )
    sub = call(
        "subscribe_cited_evidence",
        target={
            "kind": "report",
            "namespace": "r",
            "id": report["report_id"],
            "revision": 1,
        },
        request_key="watch",
        categories=["revised"],
    )
    call("evaluate_cited_evidence_subscription", subscription_id=sub["subscription_id"])
    cursor = call("poll_cited_evidence_alerts", subscription_id=sub["subscription_id"])[
        "cursor"
    ]
    conn = duckdb.connect(path)
    DocumentRevisionStore(conn).observe(
        {
            "document_id": "doc",
            "content": "The reported value decreased after correction.",
        }
    )
    conn.close()
    call("evaluate_cited_evidence_subscription", subscription_id=sub["subscription_id"])
    alerts = call(
        "poll_cited_evidence_alerts",
        subscription_id=sub["subscription_id"],
        cursor=cursor,
    )
    assert alerts["events"]
    schema_root = Path("contracts/schemas/jsonschema")
    for schema_name, value in [
        ("noesis-investigation-template-v1", template),
        ("noesis-citation-alert-v1", alerts["events"][0]),
    ]:
        Draft202012Validator(
            json.loads((schema_root / (schema_name + ".json")).read_text())
        ).validate(value)
    call(
        "acknowledge_cited_evidence_alert",
        subscription_id=sub["subscription_id"],
        event_id=alerts["events"][0]["event_id"],
    )
    comparison = call(
        "create_investigation_comparison",
        namespace="r",
        request_key="comparison",
        left=left,
        right=right,
    )
    Draft202012Validator(
        json.loads(
            (schema_root / "noesis-investigation-comparison-v1.json").read_text()
        )
    ).validate(comparison)
    exported = call(
        "export_investigation_comparison",
        namespace="r",
        comparison_id=comparison["comparison_id"],
    )
    assert exported["comparison"]["finding_changes"] == comparison["finding_changes"]
    for name in TEMPLATE_WRITES | ALERT_WRITES | COMPARISON_WRITES:
        assert _mutability(name) == "write"
        family = "subscriptions" if name in ALERT_WRITES else "projects"
        expected = [f"knowledge:{family}:read", f"knowledge:{family}:write"]
        if name in COMPARISON_WRITES:
            expected.append("knowledge:recipes:read")
        assert _required_scopes("knowledge_engine_mcp", "write", name) == expected
    for name in TEMPLATE_READS | ALERT_READS | COMPARISON_READS:
        assert _mutability(name) == "read"
    scopes.clear()
    assert (
        tools["inspect_investigation_template"].fn(
            namespace="r", template_id=template["template_id"]
        )["error"]["code"]
        == "unauthorized"
    )
    assert (
        tools["poll_cited_evidence_alerts"].fn(subscription_id=sub["subscription_id"])[
            "error"
        ]["code"]
        == "unauthorized"
    )
    assert (
        tools["export_investigation_comparison"].fn(
            namespace="r", comparison_id=comparison["comparison_id"]
        )["error"]["code"]
        == "unauthorized"
    )
