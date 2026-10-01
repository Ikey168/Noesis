"""Medical devices MCP entry points: catalog registration, exact scopes, boundary and minimisation (MD12)."""

from __future__ import annotations

import asyncio
import json

import duckdb
import pytest

from src.kb.medical_devices_records import (
    NARRATIVE_SCOPE,
    MedicalDeviceError,
    personal_fields,
)
from src.mcp_host.catalog import _mutability, _required_scopes
from tests.unit import medical_devices_harness as h
from tools.knowledge_engine_mcp import server
from tools.knowledge_engine_mcp.clinical import CLINICAL_TOOLS
from tools.knowledge_engine_mcp.medical_devices import (
    DEVICE_SCOPES,
    DEVICE_TOOLS,
    DEVICE_WRITES,
    checked,
)

READ = {"knowledge:clinical:read", f"namespace:{h.NS}:read"}
WRITE = READ | {"knowledge:clinical:write", f"namespace:{h.NS}:write"}


@pytest.fixture(scope="module")
def mcp_env(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("devices") / "medical-devices-mcp.duckdb")
    conn = duckdb.connect(path)
    h.load_all(conn)
    conn.close()
    patch = pytest.MonkeyPatch()
    state = {"principal": "alice", "scopes": set()}
    patch.setattr(server, "_context", lambda: (state["principal"], state["scopes"]))
    patch.setattr(server, "_connection", lambda *, read_only: duckdb.connect(path, read_only=read_only))
    yield asyncio.run(server.mcp.get_tools()), state
    patch.undo()


def call(tools, state, name, scopes, **kwargs):
    state["scopes"] = set(scopes)
    return tools[name].fn(**kwargs)


def test_tools_are_registered_in_the_catalog_with_every_scope_they_always_use(mcp_env):
    tools, _ = mcp_env
    assert DEVICE_TOOLS <= set(tools) and set(DEVICE_SCOPES) == DEVICE_TOOLS and DEVICE_TOOLS <= CLINICAL_TOOLS
    catalog = json.loads((h.ROOT / "contracts/generated/noesis-mcp-catalog-v1.json").read_text())
    by_name = {t["name"]: t for t in catalog["tools"]}
    for name in DEVICE_TOOLS:
        mutability = _mutability(name)
        assert mutability == ("write" if name in DEVICE_WRITES else "read"), name
        assert _required_scopes("knowledge_engine_mcp", mutability, name) == DEVICE_SCOPES[name]
        assert by_name[name]["required_scopes"] == DEVICE_SCOPES[name]
    # The answering tools restate the exclusions in their descriptions.
    for name in ("medical_device_regulatory_history", "medical_device_adverse_event_counts"):
        text = " ".join(tools[name].description.split())
        assert "No safety verdict" in text or "no signal detection" in text, name


def test_the_journey_runs_through_the_tools_with_their_declared_scopes(mcp_env):
    tools, state = mcp_env
    contracts = call(tools, state, "medical_device_source_contracts", set())
    assert contracts["minimisation"]["id"] == "medical-devices-minimisation-v1" and "boundary" in contracts
    ready = call(tools, state, "medical_devices_readiness", READ, namespace=h.NS)
    assert ready["sources"]["openfda-device"]["records"] > 0 and ready["sources"]["eudamed"]["live"] == \
        "unverified-live"
    history = call(tools, state, "medical_device_regulatory_history", READ, namespace=h.NS, subject="K999001",
                   as_of="2025-08-01")
    assert history["jurisdictions"]["US"]["clearances"] and history["exclusions"]
    counts = call(tools, state, "medical_device_adverse_event_counts", READ, namespace=h.NS, subject="ZXA",
                  received_from="2025-01-01", received_to="2025-06-30")
    assert counts["caveats"] and counts["published_counts"]
    bundle = call(tools, state, "export_medical_device_evidence_bundle", READ, namespace=h.NS, subject="ZXA",
                  as_of="2025-08-01", received_from="2025-01-01", received_to="2025-06-30")
    assert all(i["record_revision"]["revision_id"] for i in bundle["items"])
    proposed = call(tools, state, "propose_medical_device_identities", WRITE, namespace=h.NS)
    assert proposed["candidates"]
    links = call(tools, state, "link_medical_device_records", WRITE, namespace=h.NS)
    assert links["summary"]["provider-missing"] >= 1
    report = call(tools, state, "medical_device_record_history", READ, namespace=h.NS,
                  record_key="medical-devices:fda:mdr:9999001-2025-00001")
    assert report["revisions"][0]["record"]["fields"]["narratives"][0]["text"] is None
    shown = call(tools, state, "medical_device_record_history", READ | {NARRATIVE_SCOPE}, namespace=h.NS,
                 record_key="medical-devices:fda:mdr:9999001-2025-00001")
    assert shown["revisions"][0]["record"]["fields"]["narratives"][0]["text"]
    for answer in (contracts, ready, history, counts, bundle, proposed, links, report):
        assert personal_fields(answer) == []


def test_outputs_are_refused_when_they_would_carry_an_assessment_or_a_personal_field():
    with pytest.raises(MedicalDeviceError):
        checked({"items": [{"signal": True}]})
    with pytest.raises(MedicalDeviceError):
        checked({"report": {"patient_age": "63"}})
    assert checked({"ok": 1})["exclusions"]
