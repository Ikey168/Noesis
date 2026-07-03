"""M9.4: the end-to-end pack-lifecycle acceptance. The harness runs
package -> publish -> install -> live on the shipped example pack and reports
every stage green."""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[3]


def _load_harness():
    path = REPO / "scripts/domains/m9_acceptance.py"
    spec = importlib.util.spec_from_file_location("m9_acceptance_mod", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_m9_lifecycle_acceptance_reports_green():
    result = _load_harness().main()
    assert result["ok"] is True
    # Every stage of the lifecycle passed.
    for stage in ("package_ok", "publish_ok", "install_ok", "panel_ok",
                  "enricher_ok", "flag_ok", "planner_ok", "template_ok"):
        assert result[stage] is True, stage


def test_harness_leaves_no_global_state():
    from src.domains import pack_install
    from src.genui import catalog

    _load_harness().main()
    # The harness cleaned up after itself: nothing installed, no pack panel left.
    assert "energy" not in pack_install.installed_packs()
    assert catalog.get_panel_def("energy_outages") is None
