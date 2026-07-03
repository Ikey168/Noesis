"""End-to-end lifecycle tests for the Provisioner (R8 #607).

The exit criterion: deploy, then attach by criteria, then ingest, and the
scoped view has data; teardown archives; every step is visible in lineage.
"""

from datetime import datetime, timezone

import pytest

from src.provisioning import store
from src.provisioning.guardrails import Quotas
from src.provisioning.provisioner import Provisioner


class _Clock:
    def __init__(self):
        self.t = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t


def _prov(conn, **kw):
    return Provisioner(conn, quotas=kw.pop("quotas", Quotas()), clock=_Clock(), **kw)


def _seed(seed):
    seed.articles(
        [
            ("a1", "Solar power record", "u1", "c", _Clock()(), "Alpha", "energy"),
            ("a2", "Storage limits growth", "u2", "c", _Clock()(), "Alpha", "energy"),
            ("a3", "Chip fabs expand", "u3", "c", _Clock()(), "Beta", "tech"),
        ]
    )
    seed.claims(
        [
            ("c1", "Solar is cheapest.", "a1", "news", 0.9, "supported"),
            ("c2", "Storage is the constraint.", "a2", "news", 0.8, "disputed"),
        ]
    )
    seed.outlet_scores(
        [
            ("Alpha", "news", "2026-06-01", 0.8, 0.9, 0.7, 0.85, 40, 30, "2026-06-01"),
            ("Beta", "news", "2026-06-01", 0.5, 0.4, 0.5, 0.45, 20, 10, "2026-06-01"),
        ]
    )


def test_deploy_is_approval_gated(seed):
    prov = _prov(seed.conn)
    preview = prov.deploy("energy", "Energy KG")
    assert preview.get("preview") is True
    assert store.get_kg(seed.conn, "energy") is None  # nothing written

    done = prov.deploy("energy", "Energy KG", approve=True)
    assert done["deployed"] is True and done["created"] is True
    assert store.get_kg(seed.conn, "energy")["status"] == "deployed"


def test_full_lifecycle_deploy_attach_criteria_ingest_status(seed):
    _seed(seed)
    prov = _prov(seed.conn)

    prov.deploy("energy", "Energy KG", approve=True)

    # Attach by a quality criterion resolved via outlet_scores: only Alpha
    # clears transparency >= 0.7.
    attach = prov.attach_sources("energy", criteria={"min_transparency": 0.7, "type": "news"})
    assert attach["attached"] == 1
    bound = [s["source"] for s in attach["sources"]]
    assert bound == ["Alpha"]
    assert "selected because" in attach["sources"][0]["reason"]

    ingest = prov.ingest("energy")
    assert ingest["ingested"] is True
    assert ingest["routed"]["documents"] == 2
    assert ingest["totals"]["claims"] == 2

    status = prov.status("energy")
    assert status["counts"]["documents"] == 2
    assert status["source_count"] == 1
    assert status["source_health"][0]["documents"] == 2

    # Every step is in the lineage log.
    events = [e["event"] for e in status["lineage"]]
    assert {"deploy", "attach", "ingest"} <= set(events)


def test_teardown_archives_and_is_confirm_gated(seed):
    _seed(seed)
    prov = _prov(seed.conn)
    prov.deploy("energy", approve=True)
    prov.attach_sources("energy", sources=["Alpha"])
    prov.ingest("energy")

    # Without confirm: preview only, still deployed.
    preview = prov.teardown("energy")
    assert preview.get("code") == "confirm_required"
    assert store.get_kg(seed.conn, "energy")["status"] == "deployed"

    torn = prov.teardown("energy", confirm=True)
    assert torn["archived"] is True
    assert torn["archived_counts"]["documents"] == 2
    assert store.get_kg(seed.conn, "energy")["status"] == "archived"
    # Shared corpus untouched.
    assert seed.conn.execute("SELECT COUNT(*) FROM news_articles").fetchone()[0] == 3
    # Teardown is in lineage.
    assert prov.lineage("energy")["events"][0]["event"] == "teardown"


def test_criteria_with_no_match_binds_nothing(seed):
    _seed(seed)
    prov = _prov(seed.conn)
    prov.deploy("energy", approve=True)
    res = prov.attach_sources("energy", criteria={"min_transparency": 0.99})
    assert res["attached"] == 0
    assert "no sources matched" in res["note"]


def test_list_kgs_excludes_archived_by_default(seed):
    _seed(seed)
    prov = _prov(seed.conn)
    prov.deploy("energy", approve=True)
    prov.deploy("tech", approve=True)
    prov.teardown("tech", confirm=True)
    names = [k["name"] for k in prov.list_kgs()["kgs"]]
    assert names == ["energy"]
    all_names = {k["name"] for k in prov.list_kgs(include_archived=True)["kgs"]}
    assert all_names == {"energy", "tech"}
