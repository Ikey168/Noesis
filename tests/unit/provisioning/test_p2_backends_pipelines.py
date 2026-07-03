"""Track P2 tests: attached-database backend (#640), kg_attach_pipeline (#641),
orchestrated ingest (#642), and the new guardrails (#643)."""

from datetime import datetime, timezone

import pytest

from src.provisioning import namespaces, store
from src.provisioning.guardrails import Quotas
from src.provisioning.provisioner import Provisioner


def _now():
    return datetime(2026, 7, 1, tzinfo=timezone.utc)


def _seed(seed):
    seed.articles(
        [
            ("a1", "Solar record", "u1", "c", _now(), "Alpha Wire", "energy"),
            ("a2", "Storage limits", "u2", "c", _now(), "Alpha Wire", "energy"),
            ("b1", "Chip fabs", "u3", "c", _now(), "Beta Wire", "tech"),
        ]
    )
    seed.claims([("c1", "Solar is cheap.", "a1", "news", 0.9, "supported")])


@pytest.fixture(autouse=True)
def _attached_db_dir(monkeypatch, tmp_path):
    # Keep attached DBs inside the test's tmp dir; monkeypatch so the env var
    # never leaks into other tests (NOESIS_DB_PATH overrides NEURONEWS_DB_PATH).
    monkeypatch.setenv("NOESIS_DB_PATH", str(tmp_path / "wh.duckdb"))


def _prov(seed, tmp_path, **kw):
    return Provisioner(seed.conn, quotas=kw.pop("quotas", Quotas()), clock=_now, **kw)


# ------------------------------------------------------------- attached backend


def test_attached_backend_gives_a_kg_its_own_database(seed, tmp_path):
    _seed(seed)
    prov = _prov(seed, tmp_path)
    dep = prov.deploy("energy", "Energy", approve=True, backend="attached")
    assert dep["backend"] == "attached"
    prov.attach_sources("energy", sources=["Alpha Wire"])
    ing = prov.ingest("energy")
    assert ing["backend"] == "attached"
    assert ing["routed"]["documents"] == 2

    # The routed rows live in the KG's own attached database, not the shared one.
    dbs = {r[0] for r in seed.conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    assert "kg_energy" in dbs
    n = seed.conn.execute("SELECT COUNT(*) FROM kg_energy.documents").fetchone()[0]
    assert n == 2
    # The shared warehouse has no table-prefix table for this KG.
    assert not namespaces._table_exists(seed.conn, "kg_energy_documents")


def test_attached_teardown_detaches_and_keeps_the_file(seed, tmp_path):
    _seed(seed)
    prov = _prov(seed, tmp_path)
    prov.deploy("energy", approve=True, backend="attached")
    prov.attach_sources("energy", sources=["Alpha Wire"])
    prov.ingest("energy")
    db_file = namespaces.attached_db_path("energy")

    torn = prov.teardown("energy", confirm=True)
    assert torn["backend"] == "attached"
    dbs = {r[0] for r in seed.conn.execute("SELECT database_name FROM duckdb_databases()").fetchall()}
    assert "kg_energy" not in dbs  # detached
    import os

    assert os.path.exists(db_file)  # file kept, never deleted
    # Shared corpus untouched.
    assert seed.conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 3


def test_database_quota_blocks_a_new_attached_deploy(seed, tmp_path):
    prov = _prov(seed, tmp_path, quotas=Quotas(max_databases=1))
    assert prov.deploy("one", approve=True, backend="attached")["deployed"] is True
    blocked = prov.deploy("two", approve=True, backend="attached")
    assert blocked.get("code") == "quota_max_databases"
    # A table-prefix KG is not counted against the database quota.
    assert prov.deploy("three", approve=True)["deployed"] is True


# --------------------------------------------------------------- pipelines


def test_attach_pipeline_is_approval_gated_and_contract_validated(seed, tmp_path):
    _seed(seed)
    prov = _prov(seed, tmp_path)
    prov.deploy("energy", approve=True)

    preview = prov.attach_pipeline("energy", "energy-rss", "rss", {"url": "http://x/f.xml"})
    assert preview.get("preview") is True
    assert store.count_pipelines(seed.conn, "energy") == 0  # nothing bound yet

    done = prov.attach_pipeline("energy", "energy-rss", "rss", {"url": "http://x/f.xml"}, approve=True)
    assert done["attached"] is True
    pipes = store.list_pipelines(seed.conn, "energy")
    assert pipes[0]["connector"] == "energy-rss" and pipes[0]["contract"] == "article-ingest-v1"


def test_attach_pipeline_refuses_a_config_that_fails_the_contract(seed, tmp_path):
    _seed(seed)
    prov = _prov(seed, tmp_path)
    prov.deploy("energy", approve=True)
    # A feed connector with no url fails the local contract check.
    bad = prov.attach_pipeline("energy", "energy-rss", "rss", {}, approve=True)
    assert bad.get("code") == "contract_invalid"


def test_pipeline_quota_enforced(seed, tmp_path):
    _seed(seed)
    prov = _prov(seed, tmp_path, quotas=Quotas(max_pipelines_per_kg=1))
    prov.deploy("energy", approve=True)
    assert prov.attach_pipeline("energy", "p1", "rss", {"url": "http://x/1"}, approve=True)["attached"]
    blocked = prov.attach_pipeline("energy", "p2", "rss", {"url": "http://x/2"}, approve=True)
    assert blocked.get("code") == "quota_max_pipelines"


def test_orchestrated_ingest_runs_bound_pipelines_then_routes(seed, tmp_path):
    _seed(seed)
    ran = []

    def runner(spec):
        ran.append(spec["connector"])
        return {"connector": spec["connector"], "documents_ingested": 1}

    prov = _prov(seed, tmp_path, pipeline_runner=runner)
    prov.deploy("energy", approve=True)
    prov.attach_sources("energy", sources=["Alpha Wire"])
    prov.attach_pipeline("energy", "energy-rss", "rss", {"url": "http://x/f"}, approve=True)

    out = prov.ingest("energy")
    assert ran == ["energy-rss"]  # the connector ran before routing
    assert out["pipeline_runs"][0]["ok"] is True
    assert out["routed"]["documents"] == 2  # then routing copied the docs
    # Progress is visible in status/lineage.
    st = prov.status("energy")
    assert st["pipeline_count"] == 1
    assert any(e["event"] == "attach_pipeline" for e in st["lineage"])


def test_ingest_degrades_without_a_runner(seed, tmp_path):
    _seed(seed)
    prov = _prov(seed, tmp_path)  # no runner
    prov.deploy("energy", approve=True)
    prov.attach_pipeline("energy", "energy-rss", "rss", {"url": "http://x/f"}, approve=True)
    prov.attach_sources("energy", sources=["Alpha Wire"])
    out = prov.ingest("energy")
    assert out["pipeline_runs"] == []  # no runner -> pure routing
    assert out["routed"]["documents"] == 2


def test_teardown_detaches_pipelines(seed, tmp_path):
    _seed(seed)
    prov = _prov(seed, tmp_path)
    prov.deploy("energy", approve=True)
    prov.attach_pipeline("energy", "p1", "rss", {"url": "http://x/1"}, approve=True)
    torn = prov.teardown("energy", confirm=True)
    assert torn["pipelines_detached"] == 1
    assert store.count_pipelines(seed.conn, "energy") == 0
