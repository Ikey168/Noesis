"""Source integration for compositions (C06): upgrade impact, schedule owners, shared runs, limits."""

from __future__ import annotations

import duckdb
import pytest

from src.composition.contracts import validate_readiness
from src.composition.lifecycle import CompositionCoordinator
from src.composition.readiness import assess
from src.ingestion.source_pack_runtime import (
    AGGREGATE_BLOCKER,
    FixturePageAdapter,
    SourcePackRuntime,
    claim_schedule_owner,
    release_schedule_owner,
    schedule_owners,
)
from src.ingestion.source_pack_upgrades import SourcePackUpgradeStore
from src.ingestion.source_packs import SourcePackError, SourcePackStore
from tests.unit import composition_corpus as corpus
from tests.unit.composition.test_catalog_readiness import _world
from tests.unit.ingestion.test_source_pack_upgrades import candidate, pack

OPERATOR = {"operator"}
INTERVAL = {"kind": "interval", "interval_s": 600}


@pytest.fixture
def seeded():
    conn = duckdb.connect(":memory:")
    old = pack()
    SourcePackStore(conn).install(old, principal_id="operator", enable=True)
    clock = iter(range(10_000, 10_000_000))
    runtime = SourcePackRuntime(conn, now=lambda: next(clock), sleep=lambda _delay: None)
    for source in old["sources"]:
        runtime.accept_license(old["pack_id"], source["source_id"], principal_id="operator")
    yield conn, old, runtime
    conn.close()


def _compose(conn, old, spec):
    """Activate osint + science where science pins the research source pack with ``spec``."""

    coordinator = CompositionCoordinator(conn)
    ref = {"pack_id": old["pack_id"], "version": old["version"], "range": spec}
    world = corpus.resolver_world()
    science = corpus.pack("science", requires=[corpus.SPATIAL], aliases={"research": "science"},
                          contributes_extra={"source_packs": [ref]})
    osint = corpus.pack("osint", requires=[corpus.SPATIAL], contributes_extra={"source_packs": [ref]})
    for manifest in [osint, science] + world["packs"][2:]:
        coordinator.install(manifest)
    for descriptor in world["providers"]:
        coordinator.install(descriptor)
    coordinator.select("osint", "^1.0.0")
    coordinator.select("science", "^1.0.0")
    coordinator.activate("gen-1")
    return coordinator


def _apply_args(upgrade, newer, scopes=OPERATOR):
    preview = upgrade.preview_impact(newer, principal_id="operator", scopes=scopes)
    return preview, {"preview_hash": preview["preview"]["preview_hash"], "impact_hash": preview["impact_hash"],
                     "apply_key": "review-1", "principal_id": "operator", "scopes": scopes,
                     "dns_resolver": lambda _: ["8.8.8.8"], "secret_available": lambda _: True,
                     "accepted_license_sources": [v["source_id"] for v in newer["sources"]]}


# ------------------------------------------------------------ C06.1


def test_preview_lists_visible_composition_dependents_and_withholds_inaccessible(seeded):
    conn, old, _ = seeded
    coordinator = _compose(conn, old, "^1.0.0")
    upgrade = SourcePackUpgradeStore(conn)
    preview, _ = _apply_args(upgrade, candidate(old))
    (effect,) = [e for e in preview["effects"] if e["kind"] == "composition"]
    assert effect["id"] == coordinator.active()["plan_digest"] and effect["role"] == "active"
    assert effect["candidate_compatible"] is True and effect["future_execution"] == "requires_new_plan"
    reader = upgrade.preview_impact(candidate(old), principal_id="analyst", scopes={"knowledge:read"})
    assert not [e for e in reader["effects"] if e["kind"] == "composition"]
    assert reader["inaccessible"] == {"withheld": True}
    assert preview["inaccessible"]["compositions"] == 0


def test_incompatible_upgrade_is_blocked_naming_the_plan_digest(seeded):
    conn, old, _ = seeded
    coordinator = _compose(conn, old, f"~{old['version']}")
    upgrade = SourcePackUpgradeStore(conn)
    _, args = _apply_args(upgrade, candidate(old))
    with pytest.raises(SourcePackError) as blocked:
        upgrade.apply(candidate(old), **args)
    digest = coordinator.active()["plan_digest"]
    assert blocked.value.code == "composition_incompatible" and digest in str(blocked.value)
    assert SourcePackStore(conn).status(old["pack_id"])["version"] == old["version"]


def test_compatible_upgrade_applies_and_historical_runs_keep_their_pins(seeded):
    conn, old, _ = seeded
    coordinator = _compose(conn, old, "^1.0.0")
    coordinator.pin_run("run-historical")
    upgrade = SourcePackUpgradeStore(conn)
    newer = candidate(old)
    _, args = _apply_args(upgrade, newer)
    receipt = upgrade.apply(newer, **args)
    assert receipt["candidate_version"] == newer["version"]
    pinned_plan = coordinator.generation(coordinator.active()["id"])["plan"]
    assert pinned_plan["source_packs"][0]["version"] == old["version"]
    effects = upgrade._impacts(old["pack_id"], "operator", OPERATOR)[0]
    composition = [e for e in effects if e["kind"] == "composition"]
    assert {e["retained_status"] for e in composition} == {"retained"}


def test_non_composed_upgrade_impact_is_unchanged(seeded):
    conn, old, _ = seeded
    upgrade = SourcePackUpgradeStore(conn)
    preview, _ = _apply_args(upgrade, candidate(old))
    assert set(preview["inaccessible"]) == {"templates", "projects", "reports", "schedules"}
    assert not [e for e in preview["effects"] if e["kind"] == "composition"]


# ------------------------------------------------------------ C06.2


def test_shared_schedule_survives_until_the_last_owner_releases_it(seeded):
    conn, old, runtime = seeded
    runtime.set_schedule_owned(old["pack_id"], INTERVAL, principal_id="operator", owner="composition:osint")
    claim_schedule_owner(conn, old["pack_id"], "composition:science", now_ms=1)
    first = release_schedule_owner(conn, old["pack_id"], "composition:osint")
    assert first["released"] and not first["schedule_removed"]
    assert runtime.schedules()["schedules"][0]["pack_id"] == old["pack_id"]
    last = release_schedule_owner(conn, old["pack_id"], "composition:science")
    assert last["schedule_removed"] and runtime.schedules()["schedules"] == []


def test_pack_owned_and_legacy_schedules_are_not_removed_by_other_owners(seeded):
    conn, old, runtime = seeded
    runtime.set_schedule_owned(old["pack_id"], INTERVAL, principal_id="operator", owner="pack:research")
    assert not release_schedule_owner(conn, old["pack_id"], "composition:osint")["released"]
    assert schedule_owners(conn, old["pack_id"]) == ["pack:research"]
    conn.execute("DELETE FROM source_pack_schedule_owners")
    runtime.set_schedule(old["pack_id"], INTERVAL, principal_id="operator")  # legacy: no owners
    claim_schedule_owner(conn, old["pack_id"], "composition:osint", now_ms=1)
    assert schedule_owners(conn, old["pack_id"]) == ["composition:osint", "legacy:operator"]
    assert not release_schedule_owner(conn, old["pack_id"], "composition:osint")["schedule_removed"]
    assert len(runtime.schedules()["schedules"]) == 1


def test_disabling_composition_roots_releases_their_schedule_ownership(seeded):
    conn, old, runtime = seeded
    coordinator = _compose(conn, old, "^1.0.0")
    runtime.set_schedule_owned(old["pack_id"], INTERVAL, principal_id="operator", owner="composition:osint")
    coordinator.claim_source_schedules("science")
    assert schedule_owners(conn, old["pack_id"]) == ["composition:osint", "composition:science"]
    coordinator.disable("osint", "disable-osint")
    assert len(runtime.schedules()["schedules"]) == 1
    coordinator.disable("science", "disable-science")
    assert runtime.schedules()["schedules"] == []


# ------------------------------------------------------------ C06.3 / C06.4


def _crossref(old):
    return next(s for s in old["sources"] if s["source_id"] == "crossref-works")


def _request(old, **updates):
    selected = _crossref(old)
    return {"pack_id": old["pack_id"], "operation": selected["operations"][0],
            "source_ids": [selected["source_id"]], "required_sources": [selected["source_id"]],
            "max_pages": 5, "max_results": 100, "parameters": {"query": "spatial"}, **updates}


def _shared(runtime, old, consumer, records, *, namespace="global", **options):
    installed = runtime._manifest(old["pack_id"])[0]
    adapter = FixturePageAdapter(_crossref(installed), [records])
    result = runtime.run_shared(_request(old), consumer=consumer, principal_id="operator",
                                context={"namespace": namespace, "access": "operator"},
                                adapters={"crossref-works": adapter}, dns_resolver=lambda _: ["8.8.8.8"], **options)
    return result, adapter


def test_equivalent_acquisitions_share_one_run_and_one_receipt(seeded):
    conn, old, runtime = seeded
    first, adapter_a = _shared(runtime, old, "osint", [{"id": "doi:1", "title": "One"}])
    second, adapter_b = _shared(runtime, old, "science", [{"id": "doi:1", "title": "One"}])
    assert first["run_id"] == second["run_id"] and second["joined"] and not first["joined"]
    assert first["receipt_ref"] == second["receipt_ref"] and second["consumers"] == ["osint", "science"]
    assert adapter_b.calls == [] and len(adapter_a.calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM source_pack_runs").fetchone() == (1,)


def test_different_context_or_mapping_never_share_a_run(seeded):
    conn, old, runtime = seeded
    base, _ = _shared(runtime, old, "osint", [{"id": "doi:1", "title": "One"}])
    other_ns, _ = _shared(runtime, old, "osint", [{"id": "doi:2", "title": "Two"}], namespace="team-a")
    other_map, _ = _shared(runtime, old, "osint", [{"id": "doi:3", "title": "Three"}], mapping_version="2.0.0")
    assert len({base["run_id"], other_ns["run_id"], other_map["run_id"]}) == 3
    assert conn.execute("SELECT COUNT(*) FROM source_pack_runs").fetchone() == (3,)


def test_aggregate_account_limit_holds_across_consumers_and_is_a_distinct_blocker(seeded):
    conn, old, runtime = seeded
    runtime.set_account_limit("crossref-account", max_results=3, max_pages=10, window_ms=86_400_000)
    first, _ = _shared(runtime, old, "osint", [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
                       account="crossref-account")
    assert first["aggregate"]["used"]["results"] == 2
    second, _ = _shared(runtime, old, "science", [{"id": "c", "title": "C"}], namespace="team-a",
                        account="crossref-account")
    assert second["aggregate"]["clamped_budgets"]["max_results"] == 1
    assert second["aggregate"]["exhausted"]
    third, adapter = _shared(runtime, old, "legal", [{"id": "d", "title": "D"}], namespace="team-b",
                             account="crossref-account")
    assert third["status"] == "blocked" and third["blocker"]["kind"] == AGGREGATE_BLOCKER
    assert third["run_id"] is None and adapter.calls == []
    assert conn.execute("SELECT COUNT(*) FROM source_pack_runs").fetchone() == (2,)
    assert AGGREGATE_BLOCKER != "budget_exhausted"


def test_readiness_reports_aggregate_exhaustion_distinctly():
    import tests.unit.composition.test_catalog_readiness as catalog_tests

    view = _world()
    document = assess(view, conn=catalog_tests._conn(), scopes=catalog_tests.SCOPES,
                      credentials={"geospatial.core": "configured"}, live_verified={"geospatial.core.refresh-places"},
                      exhausted_providers={"geospatial.core"}, now_ms=lambda: 1)
    assert validate_readiness(document) == []
    ops = {o["operation"]: o for o in document["operations"]}
    assert [b["kind"] for b in ops["refresh-places"]["blockers"]] == ["aggregate_limit_exhausted"]
    assert ops["refresh-places"]["state"] == "blocked"
    assert ops["calculate-spatial-relation"]["state"] == "ready"  # local reads do not spend the account
