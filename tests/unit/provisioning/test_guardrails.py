"""Guardrail failing-path tests (R8 #608).

Each guardrail has a test that proves enforcement, plus the convergence test:
a re-run of a failed provision converges without duplicates.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.provisioning import store
from src.provisioning.guardrails import Quotas
from src.provisioning.provisioner import Provisioner


class _Clock:
    def __init__(self, start=None):
        self.t = start or datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def advance(self, seconds):
        self.t = self.t + timedelta(seconds=seconds)

    def __call__(self):
        return self.t


def _seed(seed):
    seed.articles(
        [
            ("a1", "Solar record", "u1", "c", datetime(2026, 6, 1, tzinfo=timezone.utc), "Alpha", "energy"),
            ("a2", "Storage limits", "u2", "c", datetime(2026, 6, 1, tzinfo=timezone.utc), "Alpha", "energy"),
        ]
    )


def test_max_kgs_quota_blocks_new_deploy(seed):
    prov = Provisioner(seed.conn, quotas=Quotas(max_kgs=1), clock=_Clock())
    assert prov.deploy("one", approve=True)["deployed"] is True
    blocked = prov.deploy("two", approve=True)
    assert blocked.get("code") == "quota_max_kgs"
    assert store.get_kg(seed.conn, "two") is None


def test_redeploy_of_existing_name_ignores_quota(seed):
    prov = Provisioner(seed.conn, quotas=Quotas(max_kgs=1), clock=_Clock())
    prov.deploy("one", "first", approve=True)
    # Re-deploying the same name converges even at the quota ceiling.
    again = prov.deploy("one", "updated", approve=True)
    assert again["deployed"] is True and again["created"] is False
    assert store.count_deployed(seed.conn) == 1


def test_max_sources_quota_blocks_attach(seed):
    prov = Provisioner(seed.conn, quotas=Quotas(max_sources_per_kg=1), clock=_Clock())
    prov.deploy("kg", approve=True)
    res = prov.attach_sources("kg", sources=["Alpha", "Beta"])
    assert res.get("code") == "quota_max_sources"
    assert store.count_sources(seed.conn, "kg") == 0


def test_ingest_rate_cap_blocks_second_ingest(seed):
    _seed(seed)
    clock = _Clock()
    prov = Provisioner(
        seed.conn, quotas=Quotas(ingest_min_interval_s=3600), clock=clock
    )
    prov.deploy("kg", approve=True)
    prov.attach_sources("kg", sources=["Alpha"])
    first = prov.ingest("kg")
    assert first["ingested"] is True
    clock.advance(60)  # 1 minute later, well inside the 1h cap
    second = prov.ingest("kg")
    assert second.get("code") == "rate_capped"


def test_deploy_requires_approval(seed):
    prov = Provisioner(seed.conn, clock=_Clock())
    preview = prov.deploy("kg")
    assert preview.get("preview") is True
    assert store.get_kg(seed.conn, "kg") is None


def test_teardown_requires_confirm(seed):
    prov = Provisioner(seed.conn, clock=_Clock())
    prov.deploy("kg", approve=True)
    res = prov.teardown("kg")
    assert res.get("code") == "confirm_required"
    assert store.get_kg(seed.conn, "kg")["status"] == "deployed"


def test_invalid_name_is_refused(seed):
    prov = Provisioner(seed.conn, clock=_Clock())
    res = prov.deploy("Bad Name", approve=True)
    assert res.get("code") == "invalid_name"


def test_rerun_of_failed_provision_converges_without_duplicates(seed):
    """Simulate a provision that fails partway (attach hits the source quota),
    then re-run the whole sequence with room: it converges to a single clean
    KG with no duplicated rows."""
    _seed(seed)
    clock = _Clock()
    # First attempt: quota too small, attach fails after deploy succeeds.
    tight = Provisioner(seed.conn, quotas=Quotas(max_sources_per_kg=0), clock=clock)
    tight.deploy("energy", "Energy", approve=True)
    failed = tight.attach_sources("energy", sources=["Alpha"])
    assert failed.get("code") == "quota_max_sources"

    # Re-run with a healthy quota: deploy converges (no duplicate KG), attach
    # and ingest complete.
    ok = Provisioner(seed.conn, quotas=Quotas(), clock=clock)
    ok.deploy("energy", "Energy", approve=True)
    ok.attach_sources("energy", sources=["Alpha"])
    ok.attach_sources("energy", sources=["Alpha"])  # duplicate attach
    ok.ingest("energy")
    ok.ingest("energy")  # duplicate ingest

    assert store.count_deployed(seed.conn) == 1
    assert store.count_sources(seed.conn, "energy") == 1  # no duplicate source
    from src.provisioning import namespaces

    counts = namespaces.namespace_counts(seed.conn, "energy")
    assert counts["documents"] == 2  # routed once, not four times
