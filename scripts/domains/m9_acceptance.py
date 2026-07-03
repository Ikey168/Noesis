"""
M9 acceptance: the full domain-pack lifecycle, package -> publish -> install -> live.

Runs the whole ecosystem end to end on the shipped example pack
(``packs/energy/pack.json``) and confirms the pack's capabilities are live in a
fresh instance:

  1. **package** (M9.1): the manifest is packaged into noesis-pack-v1 and validates;
  2. **publish** (M9.2): it publishes to a temporary registry and is discoverable
     and versioned there;
  3. **install** (M9.3): a fresh instance installs it from the registry with no
     code changes;
  4. **live** capabilities: its panel resolves and validates in a spec, its
     enricher runs, its ui_flag is active, its keywords steer the planner, and
     its provisioning template deploys through the Provisioner.

Run:  python scripts/domains/m9_acceptance.py

The executable form of docs/domains-m9-acceptance.md. Cleans up all runtime
registrations at the end, so it leaves no global state behind.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _energy_spec():
    return {
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


def main() -> dict:
    import duckdb

    from src.domains import pack_install, pack_registry
    from src.domains import registry as domain_registry
    from src.domains.pack_format import load_manifest, validate_manifest
    from src.genui import catalog, planner
    from src.genui.adaptivity import merged_ui_flags
    from src.genui.spec import validate_spec
    from src.provisioning import store

    print("M9 acceptance: package -> publish -> install -> live\n")

    shipped = REPO_ROOT / "packs" / "energy" / "pack.json"
    tmp = tempfile.mkdtemp()
    registry_root = str(Path(tmp) / "registry")

    try:
        # 1) package (M9.1): the shipped manifest loads and validates.
        manifest = load_manifest(str(shipped))
        package_ok = validate_manifest(manifest.to_dict()) == []
        print(f"1. package: {manifest.name} {manifest.version} validates: {package_ok}")

        # 2) publish (M9.2): publish and confirm discoverable + versioned.
        pack_registry.publish(manifest, root=registry_root)
        discovered = pack_registry.discover(registry_root)
        publish_ok = (
            any(d["name"] == "energy" for d in discovered)
            and pack_registry.latest_version("energy", registry_root) == manifest.version
        )
        print(f"2. publish: discoverable={publish_ok}, versions="
              f"{pack_registry.versions('energy', registry_root)}")

        # 3) install (M9.3): a fresh instance installs from the registry.
        report = pack_install.install("energy", root=registry_root)
        install_ok = pack_install.installed_packs().get("energy") == manifest.version
        print(f"3. install: {report['name']} {report['version']} "
              f"panels={report['panels']} enrichers={report['enrichers']} "
              f"templates={report['templates']}")

        # 4) live capabilities.
        panel_ok = (
            catalog.get_panel_def("energy_outages") is not None
            and validate_spec(_energy_spec()) == []
        )
        pack = domain_registry.get_pack("energy")
        enricher_ok = (
            pack is not None
            and pack.enrichers
            and pack.enrichers[0].run({"content": "The grid suffered an outage."}) == {"tag": "energy"}
        )
        flag_ok = merged_ui_flags().get("energy") is True
        planner_ok = planner.score_facets("grid megawatt capacity").get("trend", 0) >= 2

        conn = duckdb.connect(":memory:")
        deploy = pack_install.deploy_template(conn, "energy_kg")
        kg = store.get_kg(conn, "energy_kg")
        template_ok = not deploy.get("error") and kg is not None and kg["status"] == "deployed"
        conn.close()

        print(f"4. live: panel={panel_ok}, enricher={enricher_ok}, ui_flag={flag_ok}, "
              f"planner={planner_ok}, template_deployed={template_ok}")

        ok = all([package_ok, publish_ok, install_ok, panel_ok, enricher_ok,
                  flag_ok, planner_ok, template_ok])
        print("\nRESULT: " + (
            "OK - packaged, published, installed, and every capability is live"
            if ok else "FAIL"
        ))
        return {
            "package_ok": package_ok,
            "publish_ok": publish_ok,
            "install_ok": install_ok,
            "panel_ok": panel_ok,
            "enricher_ok": enricher_ok,
            "flag_ok": flag_ok,
            "planner_ok": planner_ok,
            "template_ok": template_ok,
            "ok": bool(ok),
        }
    finally:
        # Leave no global state behind.
        pack_install.uninstall("energy")
        domain_registry._REGISTRY.pop("energy", None)
        domain_registry._ENABLED.discard("energy")


if __name__ == "__main__":
    result = main()
    sys.exit(0 if result["ok"] else 1)
