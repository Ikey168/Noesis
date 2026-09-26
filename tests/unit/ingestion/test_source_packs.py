from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import duckdb
import pytest
from jsonschema import Draft7Validator

from src.ingestion.source_packs import (
    SourcePackConformance,
    SourcePackError,
    SourcePackStore,
    load_source_packs,
    replay_native_fixture,
    validate_source_pack,
)

ROOT = Path(__file__).resolve().parents[3]
PACK_DIR = ROOT / "config/source_packs"


def raw(name: str) -> dict:
    return json.loads((PACK_DIR / f"{name}.json").read_text())


@pytest.fixture()
def conn():
    value = duckdb.connect(":memory:")
    yield value
    value.close()


def test_all_production_packs_validate_against_contract() -> None:
    packs = load_source_packs(PACK_DIR)
    assert len(packs) == 11
    assert {domain for pack in packs for domain in pack["domains"]} == {
        "economic",
        "geospatial",
        "legal",
        "osint",
        "political",
        "products",
        "research",
        "scientific",
        "technical",
    }
    assert sum(len(pack["sources"]) for pack in packs) == 40
    schema = json.loads(
        (ROOT / "contracts/schemas/jsonschema/noesis-source-pack-v1.json").read_text()
    )
    Draft7Validator.check_schema(schema)
    for pack in packs:
        assert not list(Draft7Validator(schema).iter_errors(pack))
        assert all(
            source["endpoint"].startswith("https://") for source in pack["sources"]
        )
        assert all(source["license"]["terms_url"] for source in pack["sources"])
        assert validate_source_pack(pack) == pack


def test_offline_fixtures_are_pinned_and_replay_deterministically() -> None:
    gate = SourcePackConformance(ROOT)
    for manifest in (raw(path.stem) for path in sorted(PACK_DIR.glob("*.json"))):
        seen = []

        def runner(source, fixture, seen=seen):
            seen.append(source["source_id"])
            if fixture.get("native_pages"):
                # Captured native envelopes replay through the real adapter.
                return replay_native_fixture(source, fixture)
            return fixture["normalized"]

        connectors = {
            source["connector"]: runner
            for source in validate_source_pack(manifest)["sources"]
        }
        result = gate.offline(manifest, runners=connectors)
        assert result["offline"] and result["valid"]
        assert result["coverage"]["configured"] == result["coverage"]["verified"]
        assert len(seen) == len(result["sources"])
        assert all(item["scenarios"] for item in result["sources"])


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (
            lambda pack: pack["sources"][0].update(connector="shell"),
            "unknown_connector",
        ),
        (
            lambda pack: pack["sources"][0].update(endpoint="http://localhost/private"),
            "unsafe_endpoint",
        ),
        (
            lambda pack: pack["defaults"]["budgets"].update(max_pages=1000),
            "unbounded_source",
        ),
        (
            lambda pack: pack["defaults"].update(
                auth={"kind": "required-secret", "token": "leak"}
            ),
            "invalid_auth",
        ),
        (
            lambda pack: pack["defaults"].update(
                fixture={"path": "x", "sha256": "bad"}
            ),
            "unpinned_fixture",
        ),
    ],
)
def test_validation_rejects_unsafe_unbounded_or_unpinned_sources(
    mutation, code: str
) -> None:
    pack = raw("research")
    mutation(pack)
    with pytest.raises(SourcePackError) as caught:
        validate_source_pack(pack)
    assert caught.value.code == code


def test_fixture_path_escape_and_drift_are_rejected(tmp_path: Path) -> None:
    pack = raw("research")

    def point_fixtures(path: str) -> None:
        # Sources are checked in id order and some declare their own fixture.
        for holder in [pack["defaults"], *pack["sources"]]:
            if "fixture" in holder:
                holder["fixture"]["path"] = path

    point_fixtures("../outside.json")
    with pytest.raises(SourcePackError) as escaped:
        SourcePackConformance(tmp_path).offline(pack)
    assert escaped.value.code == "unsafe_fixture"

    fixture = tmp_path / "fixture.json"
    fixture.write_text('{"normalized": []}')
    point_fixtures("fixture.json")
    with pytest.raises(SourcePackError) as drift:
        SourcePackConformance(tmp_path).offline(pack)
    assert drift.value.code == "fixture_drift"


def _next_minor(version: str) -> str:
    major, minor, _patch = (int(part) for part in version.split("."))
    return f"{major}.{minor + 1}.0"


def test_install_enable_upgrade_and_idempotency_are_pack_scoped(conn) -> None:
    store = SourcePackStore(conn)
    research = raw("research")
    political = raw("political")
    installed = store.install(research, principal_id="operator", now_ms=10)
    assert not installed["enabled"]
    store.install(political, principal_id="operator", enable=True, now_ms=11)
    enabled = store.set_enabled(
        "research-discovery", True, principal_id="operator", now_ms=12
    )
    assert enabled["enabled"] and store.status("official-political-records")["enabled"]
    assert store.install(research, principal_id="operator")["idempotent"]

    upgrade = copy.deepcopy(research)
    upgrade["version"] = _next_minor(research["version"])
    upgrade["description"] += " Upgraded."
    upgraded = store.install(upgrade, principal_id="operator", now_ms=20)
    assert upgraded["version"] == upgrade["version"] and upgraded["enabled"]
    assert conn.execute(
        "SELECT COUNT(*) FROM source_pack_versions WHERE pack_id='research-discovery'"
    ).fetchone() == (2,)
    changed = copy.deepcopy(upgrade)
    changed["description"] += " conflict"
    with pytest.raises(SourcePackError) as conflict:
        store.install(changed, principal_id="operator")
    assert conflict.value.code == "immutable_version"
    downgrade = copy.deepcopy(research)
    downgrade["version"] = "0.9.0"
    with pytest.raises(SourcePackError) as old:
        store.install(downgrade, principal_id="operator")
    assert old.value.code == "version_downgrade"


def test_upgrade_preview_is_semantic_read_only_and_version_checked(conn) -> None:
    store = SourcePackStore(conn)
    installed = raw("research")
    store.install(installed, principal_id="operator", now_ms=10)
    candidate = copy.deepcopy(installed)
    candidate["version"] = _next_minor(installed["version"])
    candidate["domains"].reverse()
    candidate["sources"].reverse()
    candidate["sources"] = [
        source
        for source in candidate["sources"]
        if source["source_id"] != "datacite-dois"
    ]
    crossref = next(
        source
        for source in candidate["sources"]
        if source["source_id"] == "crossref-works"
    )
    crossref["endpoint"] = "https://api.crossref.org/works-v2"
    crossref["mapping"] = {"target_schema": "scholarly-work-v2", "version": "2.0.0"}
    crossref["license"]["redistribution"] = "restricted"
    crossref["auth"] = {"kind": "required-secret", "secret_ref": "NOESIS_CROSSREF_KEY"}
    added = copy.deepcopy(crossref)
    added["source_id"] = "crossref-books"
    candidate["sources"].append(added)
    preview = store.preview_upgrade(candidate)
    schema = json.loads(
        (
            ROOT
            / "contracts/schemas/jsonschema/noesis-source-pack-upgrade-preview-v1.json"
        ).read_text()
    )
    assert not list(Draft7Validator(schema).iter_errors(preview))
    assert preview["contract"] == "noesis-source-pack-upgrade-preview-v1"
    assert preview["installed_hash"] != preview["candidate_hash"]
    assert preview["changes"]["sources"]["removed"][0]["source_id"] == "datacite-dois"
    assert preview["changes"]["sources"]["added"][0]["source_id"] == "crossref-books"
    changed = preview["changes"]["sources"]["changed"]
    assert {"endpoint", "mapping", "license", "auth"} <= set(changed[0]["fields"])
    assert "NOESIS_CROSSREF_KEY" in json.dumps(preview)
    assert installed["version"] == store.status("research-discovery")["version"]
    assert conn.execute("SELECT COUNT(*) FROM source_pack_audit").fetchone() == (1,)

    reordered = copy.deepcopy(installed)
    reordered["sources"].reverse()
    reordered["sources"][0]["operations"].reverse()
    assert store.preview_upgrade(reordered)["idempotent"]
    assert store.preview_upgrade(candidate)["preview_hash"] == preview["preview_hash"]

    conflicting = copy.deepcopy(installed)
    conflicting["description"] = "Changed without a new version"
    with pytest.raises(SourcePackError, match="different content") as error:
        store.preview_upgrade(conflicting)
    assert error.value.code == "immutable_version"
    candidate["version"] = "0.1.0"
    with pytest.raises(SourcePackError) as error:
        store.preview_upgrade(candidate)
    assert error.value.code == "version_downgrade"
    candidate["version"] = "invalid"
    with pytest.raises(SourcePackError) as error:
        store.preview_upgrade(candidate)
    assert error.value.code == "invalid_version"


def test_upgrade_preview_through_mcp_is_read_only_and_scoped(
    tmp_path, monkeypatch
) -> None:
    from tools.knowledge_engine_mcp import server

    path = str(tmp_path / "packs.duckdb")
    with duckdb.connect(path) as database:
        SourcePackStore(database).install(raw("research"), principal_id="operator")
    opened = []

    def connection(*, read_only):
        opened.append(read_only)
        return duckdb.connect(path, read_only=read_only)

    scopes = {"knowledge:read"}
    monkeypatch.setattr(server, "_connection", connection)
    monkeypatch.setattr(server, "_context", lambda: ("reader", scopes))
    candidate = raw("research")
    candidate["version"] = _next_minor(candidate["version"])
    tools = asyncio.run(server.mcp.get_tools())
    preview = tools["preview_source_pack_upgrade"].fn(candidate=candidate)
    assert preview["installed_version"] == raw("research")["version"]
    assert opened == [True]
    scopes.clear()
    denied = tools["preview_source_pack_upgrade"].fn(candidate=candidate)
    assert denied["error"]["code"] == "unauthorized"
    assert opened == [True]


def test_secret_readiness_health_redaction_and_domain_coverage(conn) -> None:
    store = SourcePackStore(conn)
    for manifest in (raw(path.stem) for path in sorted(PACK_DIR.glob("*.json"))):
        store.install(manifest, principal_id="operator", enable=True, now_ms=10)
    political = store.status(
        "official-political-records",
        secret_available=lambda name: name == "NOESIS_BUNDESTAG_API_KEY",
    )
    readiness = {
        item["source_id"]: item["authentication"]["ready"]
        for item in political["sources"]
    }
    assert readiness["de-bundestag-dip"]
    assert not readiness["eu-eurlex-regulatory"]
    health = store.record_health(
        "official-political-records",
        "us-federal-register-executive",
        status="healthy",
        classification="ok",
        detail={
            "cursor": "next-1",
            "quota_remaining": 99,
            "response_body": "must-not-leak",
            "authorization": "must-not-leak",
        },
        checked_at_ms=20,
    )
    assert health["detail"] == {"cursor": "next-1", "quota_remaining": 99}
    encoded = json.dumps(store.status("official-political-records"))
    assert "must-not-leak" not in encoded
    coverage = store.coverage()
    assert set(coverage["domains"]) == {
        "economic",
        "geospatial",
        "legal",
        "osint",
        "political",
        "products",
        "research",
        "scientific",
        "technical",
    }


def test_live_gate_is_explicit_bounded_and_classifies_failures() -> None:
    gate = SourcePackConformance(ROOT)
    manifest = raw("political")
    disabled = gate.live(manifest, lambda source: {}, enabled=False)
    assert disabled["status"] == "disabled" and disabled["requests"] == 0

    class RateLimited(RuntimeError):
        code = "rate_limited"

    called = []

    def probe(source):
        called.append(source["source_id"])
        if len(called) == 2:
            raise RateLimited("HTTP 429")
        return {
            "schema_hash": "schema-1",
            "quota_remaining": 8,
            "response_body": "not-reported",
        }

    result = gate.live(manifest, probe, enabled=True, max_requests=2)
    assert result["requests"] == 2 and len(called) == 2
    assert result["sources"][1]["classification"] == "rate-limiting"
    assert "not-reported" not in json.dumps(result)
