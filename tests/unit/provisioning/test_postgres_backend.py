"""M4.3: the external-Postgres provisioning backend. Brings Postgres to parity
with the attached-DuckDB backend by attaching an external Postgres database under
the KG alias (DuckDB postgres extension). Covers the DSN resolution, the ATTACH
plumbing, alias-qualified table naming, teardown-by-detach, and deploy wiring; a
live end-to-end path runs only when NOESIS_PROV_PG_DSN is configured."""

import os

import pytest

from src.provisioning import namespaces as ns
from src.provisioning import store
from src.provisioning.provisioner import Provisioner


def test_postgres_is_attached_like_with_alias_qualified_tables():
    assert ns._attached_like("postgres") is True
    tables = ns.namespace_tables("climate", "postgres")
    assert tables == {
        "documents": "kg_climate.documents",
        "entities": "kg_climate.entities",
        "claims": "kg_climate.claims",
    }


def test_postgres_dsn_resolution(monkeypatch):
    monkeypatch.delenv("NOESIS_PROV_PG_DSN", raising=False)
    # Explicit DSN wins.
    assert ns.postgres_dsn("climate", dsn="host=a dbname=b") == "host=a dbname=b"
    # Per-tenant override, then shared.
    monkeypatch.setenv("NOESIS_PROV_PG_DSN", "host=shared")
    monkeypatch.setenv("NOESIS_PROV_PG_DSN_ACME", "host=acme")
    assert ns.postgres_dsn("climate", tenant="acme") == "host=acme"
    assert ns.postgres_dsn("climate", tenant="globex") == "host=shared"
    # None configured -> a clear error, no silent fallback.
    monkeypatch.delenv("NOESIS_PROV_PG_DSN", raising=False)
    monkeypatch.delenv("NOESIS_PROV_PG_DSN_ACME", raising=False)
    with pytest.raises(ValueError):
        ns.postgres_dsn("climate", tenant="globex")


def test_postgres_attach_sql_is_typed():
    assert ns.postgres_attach_sql("climate", "host=a dbname=b") == (
        "ATTACH 'host=a dbname=b' AS kg_climate (TYPE POSTGRES)"
    )


class _FakeConn:
    """Records SQL; reports whether the KG alias is already attached (drives the
    ATTACH-if-absent and DETACH-if-present branches)."""

    def __init__(self, attached=False):
        self.sql = []
        self._attached = attached

    def execute(self, sql, params=None):
        self.sql.append(sql)
        attached = self._attached and "duckdb_databases" in sql

        class _R:
            def fetchall(self_inner):
                return [[1]] if attached else []

            def fetchone(self_inner):
                return [0]

        return _R()


def test_ensure_attached_postgres_issues_typed_attach():
    fake = _FakeConn(attached=False)  # not yet attached -> ATTACH is issued
    alias = ns.ensure_attached_postgres(fake, "climate", "host=a dbname=b")
    assert alias == "kg_climate"
    assert "ATTACH 'host=a dbname=b' AS kg_climate (TYPE POSTGRES)" in fake.sql


def test_archive_postgres_detaches_without_dropping():
    fake = _FakeConn(attached=True)  # attached -> DETACH is issued
    out = ns.archive_namespace(fake, "climate", "postgres", db_path="host=a")
    # Detach is issued (schema left in place), never a DROP.
    assert any(s.startswith("DETACH") for s in fake.sql)
    assert not any("DROP" in s for s in fake.sql)
    assert out["detached_db"] == "host=a"


def test_deploy_postgres_without_dsn_is_a_clean_error(conn, monkeypatch):
    monkeypatch.delenv("NOESIS_PROV_PG_DSN", raising=False)
    store.ensure_schema(conn)
    prov = Provisioner(conn, tenant="acme")
    out = prov.deploy("climate", backend="postgres", approve=True)
    assert out.get("code") == "no_pg_dsn"


@pytest.mark.skipif(
    not os.getenv("NOESIS_PROV_PG_DSN"),
    reason="live Postgres backend requires NOESIS_PROV_PG_DSN and the duckdb postgres extension",
)
def test_deploy_and_ingest_on_live_postgres(conn):
    store.ensure_schema(conn)
    prov = Provisioner(conn)
    dep = prov.deploy("climate", backend="postgres", approve=True)
    assert dep.get("deployed") and dep["backend"] == "postgres"
    prov.attach_sources("climate", sources=["X"])
    ing = prov.ingest("climate")
    assert ing.get("ingested")
