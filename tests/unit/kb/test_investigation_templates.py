import copy
import json
from pathlib import Path

import duckdb
import pytest

from src.ingestion.source_packs import SourcePackStore
from src.kb.investigation_templates import InvestigationTemplateStore
from src.kb.research_projects import ResearchProjectError

AUTH = {
    "principal_id": "alice",
    "scopes": {
        "knowledge:projects:read",
        "knowledge:projects:write",
        "namespace:r:write",
        "domain:political:read",
    },
}
DEFINITION = {
    "name": "Regulatory review",
    "description": "Recurring Berlin review",
    "parameters": {"topic": "Research topic"},
    "questions": ["What changed for ${topic}?"],
    "success_criteria": ["Cite retained revisions"],
    "scope": {"domains": ["political"], "namespaces": ["r"]},
    "source_packs": [{"pack_id": "official-political-records", "version": "1.0.0"}],
    "report_outline": ["Changes to ${topic}", "Unresolved questions"],
}


def install(conn):
    SourcePackStore(conn).install(
        json.loads(Path("config/source_packs/political.json").read_text()),
        principal_id="alice",
    )


def test_preview_instantiation_revision_isolation_and_restart(tmp_path):
    path = str(tmp_path / "templates.duckdb")
    conn = duckdb.connect(path)
    install(conn)
    store = InvestigationTemplateStore(conn)
    template = store.create("r", "template", DEFINITION, **AUTH)
    args = ("r", template["template_id"], 1, {"topic": "Solarförderung"})
    preview = store.preview(*args, **AUTH)
    assert preview["questions"] == ["What changed for Solarförderung?"]
    assert preview["can_instantiate"] and not preview["acquisition_started"]
    assert any(
        s["credential_status"] == "missing"
        for s in preview["source_packs"][0]["sources"]
    )
    first = store.instantiate(*args, "project-1", {"requests": 10}, **AUTH)
    assert store.instantiate(*args, "project-1", {"requests": 10}, **AUTH)["idempotent"]
    second = store.instantiate(*args, "project-2", {}, **AUTH)
    assert first["project_id"] != second["project_id"] and first["links"] == []
    changed = copy.deepcopy(DEFINITION)
    changed["questions"] = ["Updated question about ${topic}"]
    store.revise("r", template["template_id"], 1, definition=changed, **AUTH)
    store.projects.revise(
        "r", first["project_id"], 1, questions=["Independent project edit"], **AUTH
    )
    conn.close()
    conn = duckdb.connect(path)
    store = InvestigationTemplateStore(conn, initialize=False)
    original = store.projects.inspect("r", first["project_id"], **AUTH)
    assert original["template_origin"]["template_revision"] == 1
    assert original["questions"] == ["Independent project edit"]
    assert (
        store.projects.inspect("r", second["project_id"], **AUTH)["questions"]
        == preview["questions"]
    )
    assert (
        store.inspect("r", template["template_id"], revision=1, **AUTH)["definition"]
        == DEFINITION
    )
    assert len(store.list("r", **AUTH)["templates"]) == 1
    with pytest.raises(ResearchProjectError, match="changed"):
        store.revise("r", template["template_id"], 1, archive=True, **AUTH)
    store.revise("r", template["template_id"], 2, archive=True, **AUTH)
    assert store.instantiate(*args, "project-1", {"requests": 10}, **AUTH)["idempotent"]
    assert not store.preview(*args, **AUTH)["can_instantiate"]
    with pytest.raises(ResearchProjectError, match="archived"):
        store.instantiate(*args, "new", {}, **AUTH)
    conn.close()


@pytest.mark.parametrize(
    "parameters", [{}, {"topic": ""}, {"topic": "x", "unknown": "y"}, {"topic": 1}]
)
def test_parameters_do_not_create_partial_projects(parameters):
    conn = duckdb.connect()
    store = InvestigationTemplateStore(conn)
    t = store.create("r", "template", DEFINITION, **AUTH)
    with pytest.raises(ResearchProjectError):
        store.instantiate("r", t["template_id"], 1, parameters, "project", {}, **AUTH)
    assert conn.execute("SELECT count(*) FROM research_projects").fetchone()[0] == 0


def test_missing_dependency_and_revoked_access():
    conn = duckdb.connect()
    store = InvestigationTemplateStore(conn)
    t = store.create("r", "template", DEFINITION, **AUTH)
    args = ("r", t["template_id"], 1, {"topic": "x"})
    assert not store.preview(*args, **AUTH)["can_instantiate"]
    with pytest.raises(ResearchProjectError, match="install"):
        store.instantiate(*args, "project", {}, **AUTH)
    with pytest.raises(ResearchProjectError) as exc:
        store.inspect(
            "r",
            t["template_id"],
            principal_id="alice",
            scopes=AUTH["scopes"] - {"domain:political:read"},
        )
    assert exc.value.code == "unauthorized"
    with pytest.raises(ResearchProjectError, match="reused"):
        store.create("r", "template", {**DEFINITION, "name": "Different"}, **AUTH)
