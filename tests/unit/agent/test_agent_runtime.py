"""M10.1: the agent host runtime. An agent calls across the planes through
the runtime within enforced budgets and allowlists; gated OSINT tools are refused
while the review gate is closed; every call is recorded."""

import pytest

from src.agent import runtime
from src.agent.runtime import (
    AgentRuntime,
    Budget,
    BudgetExceeded,
    NotAllowed,
    PLANE_OSINT,
    PLANE_PROVISIONING,
    default_planes,
)


def _fake_caller(results=None):
    calls = []

    def caller(server, tool, arguments):
        calls.append((server, tool, arguments))
        if results and (server, tool) in results:
            payload = results[(server, tool)]
            if isinstance(payload, Exception):
                raise payload
            return payload
        return {"server": server, "tool": tool, "ok": True}

    caller.calls = calls
    return caller


def test_agent_calls_across_planes():
    rt = AgentRuntime(_fake_caller())
    prov = rt.call(PLANE_PROVISIONING, "kg_deploy", {"name": "energy_kg"})
    osint = rt.call(PLANE_OSINT, "corroborate", {"claim_id": "k1"})

    assert prov["tool"] == "kg_deploy"
    assert osint["tool"] == "corroborate"
    # Both planes were exercised and recorded, on the right servers.
    transcript = rt.transcript()
    assert [c.plane for c in transcript] == [PLANE_PROVISIONING, PLANE_OSINT]
    assert transcript[0].server == "neuronews-provisioning"
    assert transcript[1].server == "neuronews-osint"
    assert all(c.ok for c in transcript)


def test_step_budget_is_enforced():
    rt = AgentRuntime(_fake_caller(), budget=Budget(max_steps=2))
    rt.call(PLANE_PROVISIONING, "kg_deploy")
    rt.call(PLANE_OSINT, "corroborate")
    assert rt.steps_remaining() == 0
    with pytest.raises(BudgetExceeded):
        rt.call(PLANE_OSINT, "corroborate")


def test_per_plane_budget_is_enforced():
    rt = AgentRuntime(_fake_caller(), budget=Budget(max_steps=10, max_per_plane={"osint": 1}))
    rt.call(PLANE_OSINT, "corroborate")
    with pytest.raises(BudgetExceeded):
        rt.call(PLANE_OSINT, "entity_dossier")
    # Other planes are unaffected by the osint cap.
    assert rt.call(PLANE_PROVISIONING, "kg_status")["ok"] is True


def test_unknown_plane_and_unlisted_tool_are_refused():
    rt = AgentRuntime(_fake_caller())
    with pytest.raises(NotAllowed):
        rt.call("weather", "forecast")
    with pytest.raises(NotAllowed):
        rt.call(PLANE_PROVISIONING, "drop_everything")
    # Nothing was dispatched or recorded for refused calls.
    assert rt.steps_used == 0


def test_gated_osint_tools_are_refused_while_the_gate_is_closed(monkeypatch):
    monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", "off")
    rt = AgentRuntime(_fake_caller(), planes=default_planes())
    with pytest.raises(NotAllowed):
        rt.call(PLANE_OSINT, "geolocate_claims", {"entity": "someone"})
    with pytest.raises(NotAllowed):
        rt.call(PLANE_OSINT, "narrative_coordination")
    assert rt.steps_used == 0


def test_gated_osint_tools_are_admitted_when_the_gate_is_open(monkeypatch):
    monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", "on")
    rt = AgentRuntime(_fake_caller(), planes=default_planes())
    out = rt.call(PLANE_OSINT, "narrative_coordination", {"topic": "x"})
    assert out["ok"] is True


def test_tool_error_is_recorded_not_raised():
    caller = _fake_caller({("neuronews-osint", "corroborate"): RuntimeError("boom")})
    rt = AgentRuntime(caller)
    out = rt.call(PLANE_OSINT, "corroborate", {"claim_id": "k1"})
    assert out["error"] == "boom"
    rec = rt.transcript()[-1]
    assert rec.ok is False and rec.error == "boom"
    # The failed call still counts against the budget.
    assert rt.steps_used == 1


def test_every_call_reaches_the_audit_sink():
    seen = []
    rt = AgentRuntime(_fake_caller(), audit_sink=seen.append)
    rt.call(PLANE_PROVISIONING, "kg_deploy", {"name": "energy_kg"})
    rt.call(PLANE_OSINT, "corroborate", {"claim_id": "x"})
    assert [c.tool for c in seen] == ["kg_deploy", "corroborate"]
    # The audit record is JSON-ready and elides the bulky result payload.
    summary = seen[0].summary()
    assert summary["plane"] == "provisioning" and summary["ok"] is True
    assert "result" not in summary


def test_gated_tools_enabled_reads_the_env(monkeypatch):
    monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", "on")
    assert runtime.gated_tools_enabled() is True
    monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", "off")
    assert runtime.gated_tools_enabled() is False
