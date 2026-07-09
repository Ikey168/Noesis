"""M10.4: full audit-trail coverage and replay. Every agent call is written to
the provisioning audit trail, and an agent run is fully reconstructable from that
trail alone -- proven at the unit level and by the end-to-end acceptance
harness."""

import importlib.util
from pathlib import Path

import pytest

from src.agent.audit import AGENT_EVENT, provisioning_audit_sink, replay_run
from src.agent.runtime import AgentRuntime, PLANE_OSINT, PLANE_PROVISIONING

duckdb = pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[3]


def _fake_caller(server, tool, arguments):
    return {"server": server, "tool": tool, "echo": arguments}


@pytest.fixture
def conn():
    c = duckdb.connect(":memory:")
    yield c
    c.close()


def test_every_agent_call_is_written_to_the_provisioning_audit_trail(conn):
    rt = AgentRuntime(_fake_caller, audit_sink=provisioning_audit_sink(conn, "run-1"))
    rt.call(PLANE_PROVISIONING, "kg_deploy", {"name": "energy_kg"})
    rt.call(PLANE_OSINT, "corroborate", {"claim_id": "k1"})
    rt.call(PLANE_OSINT, "entity_dossier", {"entity": "X"})

    from src.provisioning import store
    events = store.list_events(conn, name="run-1", limit=100)
    assert len(events) == 3
    assert all(e["event"] == AGENT_EVENT for e in events)
    # The trail lives in the same provisioning_events log used for KG lineage.
    assert {e["detail"]["tool"] for e in events} == {"kg_deploy", "corroborate", "entity_dossier"}


def test_run_is_reconstructable_from_the_trail(conn):
    rt = AgentRuntime(_fake_caller, audit_sink=provisioning_audit_sink(conn, "run-2"))
    rt.call(PLANE_PROVISIONING, "kg_list", {})
    rt.call(PLANE_PROVISIONING, "kg_deploy", {"name": "kg2", "approve": True})
    rt.call(PLANE_OSINT, "entity_dossier", {"entity": "Rivera"})

    live = [c.summary() for c in rt.transcript()]
    replayed = replay_run(conn, "run-2")
    # Same length, same order, same (plane, tool, arguments) per call.
    assert len(replayed) == len(live)
    for r, l in zip(replayed, live):
        assert r["step"] == l["step"]
        assert (r["plane"], r["tool"]) == (l["plane"], l["tool"])
        assert r["arguments"] == l["arguments"]
        assert r["ok"] == l["ok"]


def test_replay_is_scoped_to_its_run(conn):
    sink_a = provisioning_audit_sink(conn, "run-a")
    sink_b = provisioning_audit_sink(conn, "run-b")
    AgentRuntime(_fake_caller, audit_sink=sink_a).call(PLANE_OSINT, "corroborate", {"claim_id": "k1"})
    rt_b = AgentRuntime(_fake_caller, audit_sink=sink_b)
    rt_b.call(PLANE_OSINT, "corroborate", {"claim_id": "x"})
    rt_b.call(PLANE_OSINT, "source_reliability", {"source": "Alpha"})

    assert len(replay_run(conn, "run-a")) == 1
    assert len(replay_run(conn, "run-b")) == 2  # runs do not bleed into each other


def test_failed_calls_are_also_recorded(conn):
    def boom(server, tool, arguments):
        raise RuntimeError("nope")

    rt = AgentRuntime(boom, audit_sink=provisioning_audit_sink(conn, "run-fail"))
    rt.call(PLANE_OSINT, "corroborate", {"claim_id": "k1"})
    replayed = replay_run(conn, "run-fail")
    assert len(replayed) == 1
    assert replayed[0]["ok"] is False  # the failure is in the record, not hidden


def _load_harness():
    path = REPO / "scripts/agent/m10_acceptance.py"
    spec = importlib.util.spec_from_file_location("m10_acceptance_mod", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_m10_acceptance_harness_reports_green():
    result = _load_harness().main()
    assert result["ok"] is True
    assert result["all_recorded"] and result["same_sequence"] and result["same_arguments"]
    assert result["events"] == result["calls"]
