"""M10.3: the investigator agent runs an investigation end to end over the MCP
surface (dossier, relationship path, timeline, trace) and never invokes a gated
tool while the review gate is off."""

import pytest

from src.agent.analyst import kg_name_for
from src.agent.investigator import GATED_TOOLS, InvestigatorAgent
from src.agent.local_backend import build_local_caller
from src.agent.runtime import AgentRuntime, NotAllowed, PLANE_OSINT, PLANE_PROVISIONING

duckdb = pytest.importorskip("duckdb")


def _seed(conn):
    conn.execute(
        "CREATE TABLE news_articles (id VARCHAR, title VARCHAR, url VARCHAR, content VARCHAR, "
        "publish_date TIMESTAMP, source VARCHAR, category VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO news_articles (id, title, url, source, publish_date) VALUES (?,?,?,?,?)",
        [
            ("d1", "Delta summit", "http://a/1", "Alpha Wire", "2026-06-01"),
            ("d2", "Delta follow-up", "http://b/1", "Beta Journal", "2026-06-05"),
        ],
    )
    conn.execute(
        "CREATE TABLE argument_claims (claim_id VARCHAR, claim_text VARCHAR, document_id VARCHAR, "
        "source_type VARCHAR, confidence DOUBLE, factcheck_verdict VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO argument_claims VALUES (?,?,?,?,?,?)",
        [
            ("k1", "Severe flooding struck the delta in June.", "d1", "news", 0.9, None),
            ("k2", "Recovery in the delta continued.", "d2", "news", 0.8, None),
        ],
    )
    conn.execute(
        "CREATE TABLE document_actors (document_id VARCHAR, source_type VARCHAR, actor_name VARCHAR, "
        "entity_id VARCHAR, role VARCHAR, confidence DOUBLE, extracted_at VARCHAR)"
    )
    conn.executemany(
        "INSERT INTO document_actors (document_id, actor_name, entity_id, role) VALUES (?,?,?,?)",
        [
            ("d1", "Jordan Rivera", "person:jr", "speaker"),
            ("d1", "Casey Morgan", "person:cm", "subject"),
            ("d2", "Jordan Rivera", "person:jr", "speaker"),
        ],
    )


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("NOESIS_OSINT_GATED_TOOLS", "off")  # gate closed
    conn = duckdb.connect(str(tmp_path / "wh.duckdb"))
    _seed(conn)
    rt = AgentRuntime(build_local_caller(conn))
    yield rt
    conn.close()


TITLE = "delta flooding response"


def test_investigator_runs_end_to_end_over_the_r11_surface(runtime):
    result = InvestigatorAgent(runtime).run(
        TITLE,
        entities=["Jordan Rivera"],
        related_pair=("Jordan Rivera", "Casey Morgan"),
        topic="delta",
        claim_id="k1",
        sources=["Alpha Wire", "Beta Journal"],
    )

    # An investigation namespace was opened (provisioned) for the case.
    assert result.kg["name"] == kg_name_for(TITLE)
    assert result.kg["provisioned"] is True

    # The R11 surface was driven and returned usable findings.
    tools_run = {f["tool"] for f in result.surface}
    assert {"entity_dossier", "relationship_path", "timeline_reconstruct", "trace_artifact"} <= tools_run
    assert result.findings >= 3

    # It is auditable.
    assert result.audit is not None


def test_investigator_never_invokes_a_gated_tool_while_the_gate_is_off(runtime):
    result = InvestigatorAgent(runtime).run(
        TITLE, entities=["Jordan Rivera"], topic="delta", claim_id="k1",
    )
    # No gated tool appears anywhere in the run transcript.
    assert result.gated_calls == 0
    assert not any(c.tool in GATED_TOOLS for c in runtime.transcript())


def test_runtime_refuses_a_gated_tool_while_the_gate_is_off(runtime):
    # Even an explicit attempt is refused by the runtime (defence in depth).
    with pytest.raises(NotAllowed):
        runtime.call(PLANE_OSINT, "geolocate_claims", {"entity": "Jordan Rivera"})
    with pytest.raises(NotAllowed):
        runtime.call(PLANE_OSINT, "narrative_coordination", {"topic": "delta"})


def test_investigator_crosses_provisioning_and_osint(runtime):
    InvestigatorAgent(runtime).run(TITLE, entities=["Jordan Rivera"], topic="delta")
    planes = {c.plane for c in runtime.transcript()}
    assert planes == {PLANE_PROVISIONING, PLANE_OSINT}
