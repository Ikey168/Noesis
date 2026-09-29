"""EN11 (#2262): new releases, revisions and capacity changes through subscriptions."""

from __future__ import annotations

import duckdb
import pytest

from src.kb.energy_identity import EnergyIdentity
from src.kb.energy_monitoring import EnergyMonitor
from src.kb.energy_store import EnergyStoreError
from src.kb.subscriptions import SubscriptionStore
from tests.unit.energy.harness import NS, SCOPES, acquire

ZONE = "10Y1001A1001A82H"


def _fixture_now():
    return 1_790_488_800_000  # 2026-09-27T06:00:00Z, the fixtures' clock (staleness is judged against it)


def _commit(conn, watermark):
    # Stands in for the source run / maintenance orchestrator committing an ingestion watermark.
    SubscriptionStore(conn).commit_watermark(NS, watermark, kind="ingestion", detail={"generation": watermark})


def _kinds(result):
    return sorted(n["kind"] for n in result["notifications"])


def test_new_releases_then_revisions_with_old_and_new_citations_and_dedup():
    conn = duckdb.connect()
    acquire(conn, "energy-entsoe")
    monitor = EnergyMonitor(conn, now=_fixture_now)
    created = monitor.create(NS, "zone", subject=ZONE, principal_id="analyst", scopes=SCOPES,
                             record_types=["generation"])
    with pytest.raises(EnergyStoreError):
        monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    _commit(conn, 1)
    first = monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert _kinds(first) == ["new_release", "new_release"]
    replay = monitor.run(created["subscription_id"], 1, principal_id="analyst", scopes=SCOPES)
    assert replay["status"] == "replayed" and replay["notifications"] == []
    _commit(conn, 2)
    unchanged = monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert unchanged["notifications"] == []
    acquire(conn, "entsoe_revision_2")
    _commit(conn, 3)
    revised = monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    kinds = _kinds(revised)
    assert kinds == ["new_release", "revision"]
    revision = next(n for n in revised["notifications"] if n["kind"] == "revision")
    assert revision["changes"] == [{"period_start": "2026-09-24T01:00:00Z", "old": "9204", "new": "9240"}]
    assert revision["cites"]["old_vintage"]["status"] == "provisional"
    assert revision["cites"]["new_vintage"]["status"] == "revised"
    assert revision["cites"]["old_vintage"]["vintage_id"] != revision["cites"]["new_vintage"]["vintage_id"]
    polled = monitor.poll(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert len(polled["events"]) == 4


def test_capacity_changes_are_reported_with_both_values():
    conn = duckdb.connect()
    acquire(conn, "energy-eia-capacity")
    identity = EnergyIdentity(conn)
    for match in identity.propose(NS, principal_id="analyst", scopes=SCOPES)["proposed"]:
        identity.review(NS, match["match_id"], "accept", "same plant id", principal_id="reviewer", scopes=SCOPES)
    monitor = EnergyMonitor(conn, now=_fixture_now)
    created = monitor.create(NS, "plant", subject="ent-eia-plant-99901", principal_id="analyst", scopes=SCOPES,
                             record_types=["capacity"], watch=["capacity"])
    _commit(conn, 1)
    assert monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)["notifications"] == []
    # EIA republishes G1 with a lower net summer capacity in the next EIA-860M release.
    from src.ingestion import energy_sources as es
    from tests.unit.energy import fixture_builder as fb

    fixture = fb.load("energy-eia-capacity")
    body = fixture["native_pages"][0]["body"]
    for row in body["response"]["data"]:
        if row["generatorid"] == "G1" and row["period"] == "2026-07":
            row["net-summer-capacity-mw"] = "240"
    es.acquire(conn, "eia", {**fb.EIA_SELECTIONS["operating-generator-capacity"],
                             "release": {"label": "EIA-860M August 2026", "published_on": "2026-09-24"}},
               namespace=NS, scopes=SCOPES, principal_id="analyst", fetch=es.fixture_transport(fixture["native_pages"]),
               secret=es.FIXTURE_SECRET, now=lambda: 1_790_400_000_000)
    _commit(conn, 2)
    changed = monitor.run(created["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert _kinds(changed) == ["capacity_change"]
    note = changed["notifications"][0]
    assert note["old"]["value"] == "248" and note["new"]["value"] == "240"
    assert note["cites"]["new_vintage"]["release"]["label"] == "EIA-860M August 2026"


def test_thresholds_are_the_users_and_the_pack_derives_none():
    conn = duckdb.connect()
    acquire(conn, "energy-entsoe")
    monitor = EnergyMonitor(conn, now=_fixture_now)
    with pytest.raises(EnergyStoreError):
        monitor.create(NS, "bad", subject=ZONE, principal_id="analyst", scopes=SCOPES,
                       thresholds=[{"record_type": "price", "op": "lt", "value": "0"}])
    silent = monitor.create(NS, "none", subject=ZONE, principal_id="analyst", scopes=SCOPES, record_types=["price"],
                            watch=["thresholds"])
    user = monitor.create(NS, "negative-prices", subject=ZONE, principal_id="analyst", scopes=SCOPES,
                          record_types=["price"], watch=["thresholds"],
                          thresholds=[{"record_type": "price", "op": "lt", "value": "0", "unit": "EUR/MWh"}])
    _commit(conn, 1)
    assert monitor.run(silent["subscription_id"], principal_id="analyst", scopes=SCOPES)["notifications"] == []
    crossed = monitor.run(user["subscription_id"], principal_id="analyst", scopes=SCOPES)
    assert _kinds(crossed) == ["threshold_crossed"]
    assert crossed["notifications"][0]["cites"]["threshold_basis"] == "user-configured"
    assert "-3.50" in crossed["notifications"][0]["message"]
