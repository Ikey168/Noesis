"""M3.3: the acceptance harness stands up a domain KG from a real connector run
(no simulation) and the run is reconstructable from its audit trail."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[3]


def _load_main():
    path = REPO / "scripts/provisioning/m3_acceptance.py"
    spec = importlib.util.spec_from_file_location("m3_acceptance_mod", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.main


def test_m3_acceptance_stands_up_a_domain_from_a_connector_run():
    # main() sets NEURONEWS_DB_PATH to a temp warehouse; restore it after so the
    # env does not leak into other tests.
    saved = {k: os.environ.get(k) for k in ("NEURONEWS_DB_PATH", "NOESIS_DB_PATH")}
    try:
        out = _load_main()()
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    assert out["ok"] is True
    assert out["written"] == 4 and out["routed"] == 4 and out["corpus"] == 4
    assert "pipeline_run" in out["events"]
    assert out["events"][:3] == ["deploy", "attach_pipeline", "attach"]
