"""Tests for investigations-as-provisioned-KGs, the audit trail, the OSINT
telemetry and the review gate (R11 #618)."""

from datetime import datetime, timezone

from src.osint import (
    GATED_TOOLS,
    investigation_audit,
    is_gated,
    list_investigations,
    osint_telemetry,
)
from src.provisioning.provisioner import Provisioner


def _now():
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def _open_investigation(seed, name):
    seed.articles(
        [
            ("d1", "doc one", "http://a/1", "Alpha Wire", "2026-05-01"),
            ("d2", "doc two", "http://a/2", "Alpha Wire", "2026-05-02"),
        ]
    )
    prov = Provisioner(seed.conn, clock=_now)
    prov.deploy(name, "An investigation", approve=True)
    prov.attach_sources(name, sources=["Alpha Wire"])
    prov.ingest(name)
    return prov


def test_investigation_reconstructs_from_audit_trail(seed):
    _open_investigation(seed, "op_daybreak")
    audit = investigation_audit(seed.conn, "op_daybreak")
    assert audit["reconstructable"] is True
    events = [e["event"] for e in audit["audit_trail"]]
    # Oldest-first, the full provisioning sequence is replayable.
    assert events == ["deploy", "attach", "ingest"]
    assert audit["kg"]["name"] == "op_daybreak"
    assert audit["sources"][0]["source"] == "Alpha Wire"


def test_unknown_investigation_errors(seed):
    from src.provisioning import store

    store.ensure_schema(seed.conn)
    assert investigation_audit(seed.conn, "nope")["code"] == "not_found"


def test_list_investigations_counts_actions(seed):
    _open_investigation(seed, "op_a")
    out = list_investigations(seed.conn)
    assert out["count"] == 1
    assert out["investigations"][0]["action_count"] == 3


def test_osint_telemetry_leads_with_threads_and_contradictions(seed):
    _open_investigation(seed, "op_a")
    seed.evidence([("e1", "k1", "d2", "news", "supports", 0.9)])
    seed.conflicts([("k1", "k2", "contradicts", 0.8, "energy")])
    tel = osint_telemetry(seed.conn)
    labels = {s["label"] for s in tel["signals"]}
    assert labels == {"OPEN THREADS", "CORROBORATED", "CONTRADICTED"}
    assert tel["signals"][0]["value"] == 1  # one open investigation
    assert tel["ticker"]["label"] == "NEWLY CONTRADICTED"


def test_osint_telemetry_empty_without_activity(seed):
    from src.provisioning import store

    store.ensure_schema(seed.conn)
    assert osint_telemetry(seed.conn) == {}


def test_gated_tools_are_absent_from_the_server(monkeypatch):
    """Enforcement of the review gate: the gated tools must not be exposed by the
    OSINT MCP server until the gate passes."""
    import importlib.util
    import sys
    from pathlib import Path

    # Hermetic: the gate reads either env prefix, so clear both to assert the
    # default-closed state regardless of the ambient environment.
    monkeypatch.delenv("NOESIS_OSINT_GATED_TOOLS", raising=False)
    monkeypatch.delenv("NEURONEWS_OSINT_GATED_TOOLS", raising=False)
    repo = Path(__file__).resolve().parents[3]
    path = repo / "tools/osint_mcp/server.py"
    spec = importlib.util.spec_from_file_location("osint_gate_check", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    import asyncio

    from fastmcp.client import Client

    async def _names():
        async with Client(module.mcp) as c:
            return {t.name for t in await c.list_tools()}

    served = asyncio.run(_names())
    for gated in GATED_TOOLS:
        assert is_gated(gated)
        assert gated not in served, f"gated tool {gated} must not be served"


def _served_tool_names(monkeypatch, flag):
    import asyncio
    import importlib.util
    import sys
    from pathlib import Path

    # Hermetic: the gate reads either env prefix. Always clear the legacy prefix,
    # and drive only the canonical one, so an ambient NEURONEWS_ value cannot
    # flip the gate under the test.
    monkeypatch.delenv("NEURONEWS_OSINT_GATED_TOOLS", raising=False)
    if flag is None:
        monkeypatch.delenv("NOESIS_OSINT_GATED_TOOLS", raising=False)
    else:
        monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", flag)
    repo = Path(__file__).resolve().parents[3]
    path = repo / "tools/osint_mcp/server.py"
    spec = importlib.util.spec_from_file_location(f"osint_gate_{flag}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    from fastmcp.client import Client

    async def _names():
        async with Client(module.mcp) as c:
            return {t.name for t in await c.list_tools()}

    return asyncio.run(_names())


def test_gated_tools_appear_only_when_the_flag_is_on(monkeypatch):
    """The flag is the gate's enforcement: absent by default, present when a
    human deliberately turns NOESIS_OSINT_GATED_TOOLS on."""
    off = _served_tool_names(monkeypatch, None)
    assert "geolocate_claims" not in off and "narrative_coordination" not in off

    on = _served_tool_names(monkeypatch, "on")
    assert "geolocate_claims" in on and "narrative_coordination" in on


def test_is_gated():
    assert is_gated("geolocate_claims")
    assert is_gated("narrative_coordination")
    assert not is_gated("corroborate")
