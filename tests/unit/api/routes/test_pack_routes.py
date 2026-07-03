"""Route tests for src/api/routes/pack_routes.py (M9 ecosystem, live entry point).

Loads the route module BY PATH (src.api.routes.__init__ eagerly imports heavy ML
modules), points the registry at a temp dir and the warehouse at an in-memory
DuckDB, and drives discover / install / uninstall / deploy through HTTP.
"""

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

pytest.importorskip("fastapi")
duckdb = pytest.importorskip("duckdb")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from src.domains import pack_install, pack_registry  # noqa: E402
from src.domains.pack_format import PackManifest  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "pack_routes_under_test", REPO / "src/api/routes/pack_routes.py"
)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def _manifest(version="1.0.0"):
    return PackManifest(
        name="energy",
        version=version,
        description="Energy pack",
        source_types=["news"],
        ui_flags={"energy": True},
        planner_keywords={"trend": ["grid", "outage"]},
        provisioning_templates=[
            {"name": "energy_kg", "description": "Energy KG", "sources": ["Energy Wire"],
             "backend": "table-prefix"}
        ],
    )


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("NOESIS_PACK_REGISTRY", str(tmp_path / "registry"))
    monkeypatch.setenv("NOESIS_PACKS_ADMIN", "on")
    pack_registry.publish(_manifest(), root=None)  # honours the env root
    conn = duckdb.connect(":memory:")
    monkeypatch.setattr(mod, "_conn", lambda: (conn, threading.Lock()))
    app = FastAPI()
    app.include_router(mod.router)
    yield TestClient(app)
    # Undo any global install state a test left behind.
    pack_install.uninstall("energy")
    from src.domains import registry as domain_registry
    domain_registry._REGISTRY.pop("energy", None)
    domain_registry._ENABLED.discard("energy")
    conn.close()


def test_discover_lists_published_packs(client):
    body = client.get("/api/v1/packs").json()
    assert body["admin_enabled"] is True
    assert any(p["name"] == "energy" for p in body["packs"])
    energy = next(p for p in body["packs"] if p["name"] == "energy")
    assert energy["latest_version"] == "1.0.0"
    assert energy["installed_version"] is None


def test_install_then_reflects_in_discovery_and_templates(client):
    resp = client.post("/api/v1/packs/install", json={"name": "energy"})
    assert resp.status_code == 200
    assert resp.json()["installed"]["name"] == "energy"

    body = client.get("/api/v1/packs").json()
    assert body["installed"] == {"energy": "1.0.0"}
    templates = client.get("/api/v1/packs/templates").json()
    assert any(t["name"] == "energy_kg" for t in templates["templates"])


def test_deploy_template_stands_up_a_kg(client):
    client.post("/api/v1/packs/install", json={"name": "energy"})
    resp = client.post("/api/v1/packs/templates/energy_kg/deploy")
    assert resp.status_code == 200
    assert not resp.json()["deployed"].get("error")


def test_uninstall_removes_the_pack(client):
    client.post("/api/v1/packs/install", json={"name": "energy"})
    assert client.delete("/api/v1/packs/energy").status_code == 200
    assert client.get("/api/v1/packs/installed").json()["installed"] == {}


def test_publish_a_new_version(client):
    resp = client.post(
        "/api/v1/packs/publish",
        json={"manifest": _manifest("2.0.0").to_dict()},
    )
    assert resp.status_code == 200
    assert resp.json()["published"]["version"] == "2.0.0"
    assert "2.0.0" in pack_registry.versions("energy", root=None)


def test_install_unknown_pack_is_404(client):
    assert client.post("/api/v1/packs/install", json={"name": "ghost"}).status_code == 404


def test_mutations_are_gated_when_admin_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("NOESIS_PACK_REGISTRY", str(tmp_path / "reg2"))
    monkeypatch.setenv("NOESIS_PACKS_ADMIN", "off")
    pack_registry.publish(_manifest(), root=None)
    app = FastAPI()
    app.include_router(mod.router)
    c = TestClient(app)
    # Reads still work; mutations are refused with 404 (disabled).
    assert c.get("/api/v1/packs").json()["admin_enabled"] is False
    assert c.post("/api/v1/packs/install", json={"name": "energy"}).status_code == 404
    assert c.delete("/api/v1/packs/energy").status_code == 404
