"""M4.1: per-tenant namespace isolation. Two tenants on the same warehouse see
and act on only their own namespaces; a name owned by one tenant cannot be
co-opted or torn down by another."""

from datetime import datetime, timezone

from src.provisioning import store
from src.provisioning.provisioner import Provisioner


def _clock():
    return datetime(2026, 6, 1, tzinfo=timezone.utc)


def _provs(conn):
    store.ensure_schema(conn)
    acme = Provisioner(conn, clock=_clock, tenant="acme")
    globex = Provisioner(conn, clock=_clock, tenant="globex")
    acme.deploy("energy", "acme energy", approve=True)
    globex.deploy("markets", "globex markets", approve=True)
    return acme, globex


def test_each_tenant_lists_only_its_own_namespaces(conn):
    acme, globex = _provs(conn)
    assert [k["name"] for k in acme.list_kgs()["kgs"]] == ["energy"]
    assert [k["name"] for k in globex.list_kgs()["kgs"]] == ["markets"]


def test_a_tenant_cannot_see_anothers_namespace(conn):
    acme, globex = _provs(conn)
    # acme cannot status or view globex's KG.
    assert acme.status("markets")["code"] == "not_found"
    # globex cannot status acme's KG.
    assert globex.status("energy")["code"] == "not_found"


def test_a_tenant_cannot_tear_down_anothers_namespace(conn):
    acme, globex = _provs(conn)
    out = acme.teardown("markets", confirm=True)
    assert out.get("code") == "not_found"
    # globex's KG is untouched.
    assert globex.status("markets").get("kg", {}).get("name") == "markets"


def test_a_name_owned_by_another_tenant_is_refused(conn):
    acme, globex = _provs(conn)
    out = globex.deploy("energy", "globex tries energy", approve=True)
    assert out.get("code") == "name_taken"
    # Still owned by acme.
    assert acme.status("energy")["kg"]["tenant"] == "acme"


def test_default_tenant_is_isolated_from_named_tenants(conn):
    acme, _ = _provs(conn)
    default = Provisioner(conn, clock=_clock)  # tenant defaults to "default"
    default.deploy("policy", "default policy", approve=True)
    assert [k["name"] for k in default.list_kgs()["kgs"]] == ["policy"]
    assert default.status("energy")["code"] == "not_found"
