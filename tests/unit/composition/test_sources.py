"""Source upgrade impact, schedule ownership, shared acquisitions and limits (C06, #1824-#1827)."""

from __future__ import annotations

import copy

import duckdb
import pytest

from src.composition import contracts as c
from src.composition import lifecycle as lc
from src.composition.resolver import check_resume
from src.composition.sources import claim_schedule, source_pins
from src.ingestion.source_pack_upgrades import SourcePackUpgradeStore
from src.ingestion.source_packs import SourcePackError, validate_source_pack
from src.kb.geospatial import GeospatialStore
from tests.unit.geospatial_pack_helpers import PUBLIC_DNS, WfsServer, install, point, source

GEO = next(p for p in c.load_providers() if p["provider_id"] == "noesis.geospatial")
CONTRACTS = {"noesis-spatial-result-v1": ["1.0.0"], "noesis-geospatial-geometry-v2": ["2.0.0"],
             "noesis-geocode-resolution-v1": ["1.0.0"]}
PACK_ID = "geospatial-berlin"
WEEKLY = {"kind": "interval", "interval_s": 604800}


def consumer(name, requires=("spatial.relation",)):
    return c.validate_manifest({"pack_format": c.PACK_FORMAT_V2, "name": name, "version": "1.0.0",
                                "requires": [{"capability": r, "range": "^1.0.0"} for r in requires],
                                "contributes": {}})


@pytest.fixture(autouse=True)
def fresh_runtime():
    from src.domains import registry

    saved = registry._AUTHORITY
    lc.reset_runtime()
    yield
    registry.set_authority(saved)
    lc.reset_runtime()


@pytest.fixture
def world():
    conn = duckdb.connect(":memory:")
    GeospatialStore(conn)
    value, runtime = install(conn)
    coordinator = lc.Coordinator(conn, contracts=CONTRACTS, now=runtime.now)
    coordinator.store.install_provider(GEO, principal_id="operator")
    for name in ("osint", "research"):
        coordinator.store.install_manifest(consumer(name), principal_id="operator")
        coordinator.store.select(name, "^1.0.0", principal_id="operator")
    coordinator.activate("gen-1", principal_id="operator")
    return conn, value, runtime, coordinator


def bumped(value, version):
    candidate = copy.deepcopy(value)
    candidate.pop("manifest_hash", None)
    candidate["version"] = version
    return validate_source_pack(candidate)


def _apply_args(preview):
    return {"preview_hash": preview["preview"]["preview_hash"], "impact_hash": preview["impact_hash"],
            "principal_id": "operator", "scopes": {"operator"}, "dns_resolver": PUBLIC_DNS,
            "secret_available": lambda _: True}


# --------------------------------------------------------------------------- #
# C06.1 upgrade impact over composition dependents
# --------------------------------------------------------------------------- #

def test_plan_pins_the_source_version_with_its_range(world):
    conn, value, _, coordinator = world
    (pin,) = coordinator.store.active_plan()["source_packs"]
    assert pin["pack_id"] == PACK_ID and pin["version"] == value["version"]
    assert pin["ranges"] == ["^1.0.0"] and pin["manifest_hash"] == value["manifest_hash"]


def test_incompatible_upgrade_is_blocked_naming_the_plan(world):
    conn, value, _, coordinator = world
    candidate = bumped(value, "2.0.0")
    upgrade = SourcePackUpgradeStore(conn)
    preview = upgrade.preview_impact(candidate, principal_id="operator", scopes={"operator"})
    effect = next(e for e in preview["effects"] if e["kind"] == "composition-plan")
    digest = coordinator.store.active_plan()["digest"]
    assert effect["id"] == digest and effect["consumers"] == ["osint", "research"]
    assert effect["candidate_compatible"] is False
    with pytest.raises(SourcePackError) as blocked:
        upgrade.apply(candidate, apply_key="major", **_apply_args(preview))
    assert blocked.value.code == "composition_incompatible"
    assert digest in blocked.value.details["plan_digests"]
    assert source_pins(conn)[PACK_ID]["version"] == value["version"]


def test_compatible_upgrade_applies_and_historical_plans_keep_their_pins(world):
    conn, value, _, coordinator = world
    plan = coordinator.store.active_plan()
    candidate = bumped(value, "1.2.0")
    upgrade = SourcePackUpgradeStore(conn)
    preview = upgrade.preview_impact(candidate, principal_id="operator", scopes={"operator"})
    receipt = upgrade.apply(candidate, apply_key="minor",
                            accepted_license_sources=[s["source_id"] for s in candidate["sources"]],
                            **_apply_args(preview))
    assert receipt["candidate_version"] == "1.2.0"
    assert coordinator.store.plan(plan["digest"]) == plan  # historical pin unchanged
    resume = check_resume(plan, manifests=coordinator.store.manifests(),
                          providers=coordinator.store.providers(), contracts=CONTRACTS,
                          source_packs=source_pins(conn))
    assert resume["status"] == "new-plan-required"
    assert resume["changes"] == [{"kind": "source_pack", "id": f"{PACK_ID}@{value['version']}",
                                  "current_version": "1.2.0"}]
    coordinator.activate("gen-2", principal_id="operator")
    assert coordinator.store.active_plan()["source_packs"][0]["version"] == "1.2.0"


def test_preview_omits_composition_dependents_the_caller_cannot_see(world):
    conn, value, _, coordinator = world
    coordinator.store.pin_run("run-private", coordinator.store.active_plan()["digest"], owner="alice")
    coordinator.store.deselect("research", principal_id="operator")
    coordinator.activate("gen-2", principal_id="operator")
    upgrade = SourcePackUpgradeStore(conn)
    bob = upgrade.preview_impact(bumped(value, "1.2.0"), principal_id="bob", scopes={"knowledge:read"})
    roles = [e["role"] for e in bob["effects"] if e["kind"] == "composition-plan"]
    assert roles == ["active-generation"] and bob["inaccessible"] == {"withheld": True}
    operator = upgrade.preview_impact(bumped(value, "1.2.0"), principal_id="operator", scopes={"operator"})
    assert sorted(e["role"] for e in operator["effects"] if e["kind"] == "composition-plan") == [
        "active-generation", "pinned-run"]


# --------------------------------------------------------------------------- #
# C06.2 schedule ownership
# --------------------------------------------------------------------------- #

def test_shared_schedule_survives_until_every_owner_releases(world):
    conn, _, runtime, _ = world
    claim_schedule(conn, "osint", PACK_ID, WEEKLY, principal_id="operator")
    claim_schedule(conn, "research", PACK_ID, WEEKLY, principal_id="operator")
    assert runtime.schedule_owners(PACK_ID) == ["composition:osint", "composition:research"]
    first = runtime.release_schedule(PACK_ID, "composition:osint", principal_id="operator")
    assert first["removed"] is False and runtime.schedules()["schedules"]
    second = runtime.release_schedule(PACK_ID, "composition:research", principal_id="operator")
    assert second["removed"] is True and runtime.schedules()["schedules"] == []


def test_another_packs_release_never_removes_an_owned_schedule(world):
    conn, _, runtime, _ = world
    claim_schedule(conn, "osint", PACK_ID, WEEKLY, principal_id="operator")
    result = runtime.release_schedule(PACK_ID, "composition:research", principal_id="operator")
    assert result == {"pack_id": PACK_ID, "removed": False, "owners": ["composition:osint"]}
    assert runtime.schedules()["schedules"][0]["pack_id"] == PACK_ID


def test_disabling_a_root_releases_only_its_own_schedule_ownership(world):
    conn, _, runtime, coordinator = world
    runtime.set_schedule(PACK_ID, WEEKLY, principal_id="operator")  # legacy owner
    claim_schedule(conn, "osint", PACK_ID, WEEKLY, principal_id="operator")
    coordinator.disable("osint", "disable-osint", principal_id="operator")
    assert runtime.schedule_owners(PACK_ID) == ["legacy"]
    assert runtime.schedules()["schedules"][0]["pack_id"] == PACK_ID


# --------------------------------------------------------------------------- #
# C06.3 shared acquisitions
# --------------------------------------------------------------------------- #

def _request(**extra):
    return {"pack_id": PACK_ID, "operation": "features", "source_ids": ["berlin-schulen"],
            "required_sources": ["berlin-schulen"], "max_results": 5000, "max_bytes": 5_000_000,
            "timeout_ms": 120_000, **extra}


def _shared(runtime, value, server, consumer, namespace="global", **extra):
    adapter = runtime.factory.compile(source(value, "berlin-schulen"), transport=server)
    return runtime.run_shared(_request(**extra), consumer=consumer, namespace=namespace,
                              access_context="public", principal_id="operator",
                              adapters={"berlin-schulen": adapter}, dns_resolver=PUBLIC_DNS)


def test_same_key_from_two_consumers_is_one_run_with_two_references(world):
    conn, value, runtime, _ = world
    server = WfsServer([point(f"school-{i}", i, i) for i in range(3)])
    first = _shared(runtime, value, server, "osint")
    calls = len(server.calls)
    second = _shared(runtime, value, server, "research")
    assert len(server.calls) == calls  # one acquisition
    assert first["run_id"] == second["run_id"]
    assert first["shared"]["receipt_ref"] == second["shared"]["receipt_ref"]
    assert second["shared"]["joined"] is True and first["shared"]["joined"] is False
    assert [c["consumer"] for c in runtime.shared_consumers(first["run_id"])] == ["osint", "research"]
    assert conn.execute("SELECT count(*) FROM source_pack_runs").fetchone()[0] == 1


def test_different_namespace_or_mapping_never_share(world):
    conn, value, runtime, _ = world
    server = WfsServer([point("school-1", 1, 1)])
    first = _shared(runtime, value, server, "osint", namespace="global")
    other = _shared(runtime, value, server, "research", namespace="team-a")
    assert first["run_id"] != other["run_id"]
    base = runtime.acquisition_key(_request(), namespace="global", access_context="public")
    remapped = copy.deepcopy(value)
    remapped.pop("manifest_hash", None)
    remapped["version"] = "1.1.1"
    next(s for s in remapped["sources"] if s["source_id"] == "berlin-schulen")["mapping"]["version"] = "1.0.1"
    from src.ingestion.source_packs import SourcePackStore

    SourcePackStore(conn).install(validate_source_pack(remapped), principal_id="operator", enable=True)
    assert runtime.acquisition_key(_request(), namespace="global", access_context="public") != base
    assert runtime.acquisition_key(_request(), namespace="global", access_context="entitled") != base


# --------------------------------------------------------------------------- #
# C06.4 aggregate account limits
# --------------------------------------------------------------------------- #

def test_consumers_cannot_multiply_the_account_quota(world):
    conn, value, runtime, coordinator = world
    account = runtime.default_account(PACK_ID)
    runtime.set_account_limit(account, window_ms=3_600_000, max_pages=2, max_bytes=50_000_000)
    server = WfsServer([point("school-1", 1, 1)])
    _shared(runtime, value, server, "osint", namespace="a", max_pages=1)
    _shared(runtime, value, server, "research", namespace="a", max_pages=1)  # join: no new usage
    _shared(runtime, value, server, "research", namespace="b", max_pages=1)
    with pytest.raises(SourcePackError) as exhausted:
        _shared(runtime, value, server, "osint", namespace="c", max_pages=1)
    assert exhausted.value.code == "aggregate_limit_exhausted"
    assert runtime.account_state(account)["used_pages"] == 2
    readiness = coordinator.readiness(principal_id="operator", scopes={"operator"})
    kinds = {b["kind"] for op in readiness["operations"] for b in op["blockers"]}
    assert "aggregate-limit-exhausted" in kinds


def test_per_run_budgets_still_hold_for_each_consumer(world):
    conn, value, runtime, _ = world
    server = WfsServer([point(f"school-{i}", i, i) for i in range(5)])
    _shared(runtime, value, server, "osint")
    with pytest.raises(SourcePackError) as over:
        _shared(runtime, value, server, "research", max_results=2)
    assert over.value.code == "budget_exceeded"
    assert over.value.details["budgets"] == ["max_results"]
