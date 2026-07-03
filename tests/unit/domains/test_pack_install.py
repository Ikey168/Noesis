"""M9.3: install a pack into a running instance without code changes. After
install(name), the pack's panels validate and surface, its enrichers run, its
planner keywords steer routing, its ui_flags are active, and its provisioning
templates are deployable. Uninstall reverses all of it."""

import pytest

from src.domains import pack_install, pack_registry
from src.domains import registry as domain_registry
from src.domains.pack_format import PackManifest
from src.genui import catalog, planner
from src.genui.adaptivity import merged_ui_flags
from src.genui.discovery import merged_catalog_dict
from src.genui.spec import validate_spec

duckdb = pytest.importorskip("duckdb")


def _manifest():
    return PackManifest(
        name="energy",
        version="1.0.0",
        description="Energy-sector pack",
        source_types=["news"],
        ui_flags={"energy": True},
        panels=[
            {
                "type": "energy_outages",
                "title": "Grid outages",
                "description": "Outages over time.",
                "endpoint": None,
                "facets": ["events", "trend"],
                "tables": ["news_articles"],
                "ui_flag": "energy",
                "default_span": 6,
                "topic_param": "topic",
            }
        ],
        planner_keywords={"trend": ["grid", "megawatt", "blackout"]},
        enrichers=[
            {
                "name": "energy_tag",
                "kind": "keyword_tag",
                "description": "Tag energy docs.",
                "source_types": ["news"],
                "params": {"field": "content", "label": "energy",
                           "keywords": ["grid", "outage", "blackout"]},
            }
        ],
        provisioning_templates=[
            {
                "name": "energy_kg",
                "description": "Energy KG",
                "ontology": {"entities": ["utility"]},
                "sources": ["Energy Wire"],
                "backend": "table-prefix",
            }
        ],
    )


@pytest.fixture
def installed(tmp_path):
    """A fresh registry with the energy pack published; install it, then tear
    down every runtime registration so no global state leaks."""
    root = str(tmp_path / "registry")
    pack_registry.publish(_manifest(), root=root)
    result = pack_install.install("energy", root=root)
    try:
        yield result
    finally:
        pack_install.uninstall("energy")
        domain_registry._REGISTRY.pop("energy", None)
        domain_registry._ENABLED.discard("energy")


def test_install_pulls_from_registry_without_code_changes(installed):
    assert installed["name"] == "energy" and installed["version"] == "1.0.0"
    assert pack_install.installed_packs() == {"energy": "1.0.0"}


def test_installed_pack_panel_surfaces_and_validates(installed):
    # The pack panel resolves in the catalog ...
    assert catalog.get_panel_def("energy_outages") is not None
    assert "energy_outages" in catalog.all_panel_types()
    # ... surfaces on the merged catalog (GET /api/v1/ui/panels) ...
    types = {p["type"] for p in merged_catalog_dict()}
    assert "energy_outages" in types
    # ... and a spec that uses it validates.
    spec = {
        "spec_version": "ui-spec-v1",
        "intent": "grid outages",
        "title": "Energy",
        "subtitle": "",
        "generated_by": "heuristic",
        "facets": ["trend", "events"],
        "topic": "grid",
        "source_type": None,
        "panels": [
            {"id": "p1", "type": "energy_outages", "title": "Grid outages", "span": 6,
             "priority": 0.7, "rationale": "", "endpoint": None,
             "params": {"topic": "grid"}, "body": ""}
        ],
    }
    assert validate_spec(spec) == []


def test_installed_pack_enricher_runs(installed):
    pack = domain_registry.get_pack("energy")
    assert pack is not None and len(pack.enrichers) == 1
    enricher = pack.enrichers[0]
    assert enricher.applies_to("news")
    assert enricher.run({"content": "The grid suffered a major outage overnight."}) == {"tag": "energy"}
    assert enricher.run({"content": "A story about the local sports team."}) is None


def test_installed_pack_ui_flags_are_active(installed):
    assert merged_ui_flags().get("energy") is True


def test_installed_pack_keywords_steer_the_planner(installed):
    scores = planner.score_facets("grid capacity and megawatt output")
    assert scores.get("trend", 0) >= 2  # grid + megawatt both hit the trend facet


def test_installed_pack_template_is_deployable(installed):
    assert "energy_kg" in pack_install.list_templates()
    conn = duckdb.connect(":memory:")
    try:
        result = pack_install.deploy_template(conn, "energy_kg")
        assert not result.get("error"), result
        # The template stood up a deployed KG through the Provisioner.
        from src.provisioning import store
        kg = store.get_kg(conn, "energy_kg")
        assert kg is not None and kg["status"] == "deployed"
    finally:
        conn.close()


def test_deploy_unknown_template_is_reported():
    conn = duckdb.connect(":memory:")
    try:
        out = pack_install.deploy_template(conn, "no_such_template")
        assert out.get("code") == "template_not_found"
    finally:
        conn.close()


def test_uninstall_reverses_everything(tmp_path):
    root = str(tmp_path / "registry")
    pack_registry.publish(_manifest(), root=root)
    pack_install.install("energy", root=root)
    assert catalog.get_panel_def("energy_outages") is not None

    assert pack_install.uninstall("energy") is True
    domain_registry._REGISTRY.pop("energy", None)
    domain_registry._ENABLED.discard("energy")

    # Everything the pack contributed is gone.
    assert catalog.get_panel_def("energy_outages") is None
    assert "energy" not in pack_install.installed_packs()
    assert "energy_kg" not in pack_install.list_templates()
    assert merged_ui_flags().get("energy") is None
    # The planner keywords are withdrawn (blackout was a pack-only keyword).
    assert planner.score_facets("blackout").get("trend", 0) == 0
