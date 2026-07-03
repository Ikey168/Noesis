"""M4.2: per-tenant quotas and read-write authority. Quotas are counted and
enforced per tenant (one tenant's budget never blocks another, and a tenant can
carry its own override), and a cross-tenant write is refused."""

from datetime import datetime, timezone

from src.provisioning import store
from src.provisioning.guardrails import Quotas
from src.provisioning.provisioner import Provisioner


def _clock():
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def test_for_tenant_reads_per_tenant_override(monkeypatch):
    monkeypatch.setenv("NOESIS_PROV_MAX_KGS", "1")
    monkeypatch.setenv("NOESIS_PROV_MAX_KGS_GLOBEX", "3")
    assert Quotas.for_tenant("acme").max_kgs == 1  # falls back to global
    assert Quotas.for_tenant("globex").max_kgs == 3  # tenant override
    assert Quotas.for_tenant("default").max_kgs == 1


def test_deploy_quota_is_counted_per_tenant(conn, monkeypatch):
    monkeypatch.setenv("NOESIS_PROV_MAX_KGS", "1")
    store.ensure_schema(conn)
    acme = Provisioner(conn, clock=_clock, tenant="acme")
    globex = Provisioner(conn, clock=_clock, tenant="globex")

    assert acme.deploy("acme_one", approve=True).get("deployed")
    # acme is at its 1-KG budget; a second acme deploy is refused...
    blocked = acme.deploy("acme_two", approve=True)
    assert blocked.get("code", "").startswith("quota")
    # ...but globex's budget is independent, so it can still deploy its own KG.
    assert globex.deploy("globex_one", approve=True).get("deployed")


def test_per_tenant_override_raises_one_tenants_budget(conn, monkeypatch):
    monkeypatch.setenv("NOESIS_PROV_MAX_KGS", "1")
    monkeypatch.setenv("NOESIS_PROV_MAX_KGS_GLOBEX", "3")
    store.ensure_schema(conn)
    globex = Provisioner(conn, clock=_clock, tenant="globex")
    assert globex.deploy("g1", approve=True).get("deployed")
    assert globex.deploy("g2", approve=True).get("deployed")
    assert globex.deploy("g3", approve=True).get("deployed")


def test_cross_tenant_writes_are_refused(conn):
    store.ensure_schema(conn)
    acme = Provisioner(conn, clock=_clock, tenant="acme")
    globex = Provisioner(conn, clock=_clock, tenant="globex")
    acme.deploy("energy", approve=True)

    # globex cannot attach, ingest, pipeline-bind or tear down acme's KG: the KG
    # is invisible to it, so every write is refused (never a success).
    refused = {"not_found", "not_deployed"}
    assert globex.attach_sources("energy", sources=["X"]).get("code") in refused
    assert globex.ingest("energy").get("code") in refused
    assert globex.attach_pipeline(
        "energy", connector="c", connector_type="rss",
        config={"url": "http://x"}, approve=True,
    ).get("code") in refused
    assert globex.teardown("energy", confirm=True).get("code") in refused

    # acme's KG is intact and still owned by acme.
    assert acme.status("energy")["kg"]["tenant"] == "acme"
