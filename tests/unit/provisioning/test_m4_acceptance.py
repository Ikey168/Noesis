"""M4.4: the two-tenant concurrent isolation acceptance harness. Two tenants run
their provisioning lifecycles interleaved and stay isolated, with independent
quotas and no cross-tenant read or write."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

pytest.importorskip("duckdb")

REPO = Path(__file__).resolve().parents[3]


def _load_main():
    path = REPO / "scripts/provisioning/m4_acceptance.py"
    spec = importlib.util.spec_from_file_location("m4_acceptance_mod", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.main


def test_two_tenants_isolated_and_concurrent():
    keys = ("NEURONEWS_DB_PATH", "NOESIS_DB_PATH", "NOESIS_PROV_MAX_KGS")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        out = _load_main()()
    finally:
        for key, val in saved.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    assert out["ok"] is True
    assert out["acme_kgs"] == ["acme_energy"]
    assert out["globex_kgs"] == ["globex_markets"]
    assert out["cross_read"] == "not_found"
    assert out["acme_docs"] == 3 and out["globex_docs"] == 3
