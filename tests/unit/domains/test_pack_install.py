"""M9.3: install a pack into a running instance without code changes. After
install(name), the pack's panels validate and surface, its enrichers run, its
planner keywords steer routing, its ui_flags are active, and its provisioning
templates are deployable. Uninstall reverses all of it."""

import pytest

from src.domains import pack_install, pack_registry
from src.domains import registry as domain_registry
from src.domains.pack_format import PackManifest

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


def test_installed_pack_enricher_runs(installed):
    pack = domain_registry.get_pack("energy")
    assert pack is not None and len(pack.enrichers) == 1
    enricher = pack.enrichers[0]
    assert enricher.applies_to("news")
    assert enricher.run({"content": "The grid suffered a major outage overnight."}) == {"tag": "energy"}
    assert enricher.run({"content": "A story about the local sports team."}) is None


def test_installed_pack_ui_flags_are_active(installed):
    # ui_flags are advisory metadata now (the UI retired), but the installed
    # pack still carries them on its DomainPack.
    pack = domain_registry.get_pack("energy")
    assert pack is not None and pack.ui_flags.get("energy") is True


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
    assert "energy" in pack_install.installed_packs()

    assert pack_install.uninstall("energy") is True
    domain_registry._REGISTRY.pop("energy", None)
    domain_registry._ENABLED.discard("energy")

    # Everything the pack contributed is gone.
    assert "energy" not in pack_install.installed_packs()
    assert "energy_kg" not in pack_install.list_templates()
