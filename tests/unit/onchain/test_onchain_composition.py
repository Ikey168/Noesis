"""The On-chain Observations bundle: manifest, provider, exclusions and the OSINT feature (#2058)."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest

from src.composition.adapter import adapt_all
from src.composition.contracts import (
    validate_composition_manifest,
    validate_provider_descriptor,
)
from src.composition.resolver import resolve
from src.composition.shadow import provider_descriptors
from src.kb import onchain
from src.kb.onchain import OnchainStore
from src.osint.dossier import entity_dossier
from tests.unit.onchain import fixture_builder as fb

ROOT = Path(__file__).resolve().parents[3]
MANIFEST = json.loads((ROOT / "packs/onchain/manifest.json").read_text())
COMPOSITION = json.loads((ROOT / "packs/onchain/composition.json").read_text())
REQUIRED_EXCLUSIONS = (
    "person attribution",
    "wallet or key access",
    "transaction signing or submission",
    "exchange or KYC data",
)


def plan_for(pack, features=None):
    bundles = adapt_all()
    root = {"pack": pack, "version": bundles[pack]["version"]}
    if features is not None:
        root["features"] = features
    result = resolve([root], list(bundles.values()), provider_descriptors())
    assert result.ok, result.failure
    return result.plan


def test_manifest_is_valid_and_the_composition_view_matches_it():
    assert validate_composition_manifest(MANIFEST) == []
    assert "onchain" in adapt_all()
    assert COMPOSITION["requires"] == MANIFEST["requires"]
    assert COMPOSITION["optional_features"] == MANIFEST["optional_features"]
    assert COMPOSITION["exclusions"] == MANIFEST["advisory"]["exclusions"]
    assert {c["id"] for c in MANIFEST["contributes"]["capabilities"]} == {
        "onchain.observations",
        "onchain.clustering",
    }
    assert [f["id"] for f in MANIFEST["optional_features"]] == ["acquisition"]
    for exclusion in REQUIRED_EXCLUSIONS:
        assert any(exclusion in e for e in MANIFEST["advisory"]["exclusions"]), (
            exclusion
        )


def test_descriptor_declares_operations_scopes_stores_probes_and_source_pack():
    descriptor = next(d for d in provider_descriptors() if d["id"] == "onchain.core")
    assert validate_provider_descriptor(descriptor) == []
    assert descriptor["implementation"]["server"] == "noesis-onchain"
    assert {o["side_effect"] for o in descriptor["operations"]} == {"read-only"}
    owned = {s["record_type"] for s in descriptor["stores"]}
    others = {
        s["record_type"]
        for d in provider_descriptors()
        if d["id"] != "onchain.core"
        for s in d["stores"]
    }
    assert not owned & others
    assert all(s["store"] == "src.kb.onchain" for s in descriptor["stores"])
    assert {t for s in descriptor["stores"] for t in s["tables"]} == set(onchain.TABLES)
    assert descriptor["source_packs"] == [
        {"pack_id": "onchain-observations", "version": "1.0.0", "range": "^1.0.0"}
    ]


def test_plan_binds_the_bundle_provider_and_shared_identity_and_acquisition_only_when_selected():
    plan = plan_for("onchain")
    bound = {b["provider"] for b in plan["bindings"] if "onchain" in b["consumers"]}
    assert bound == {"onchain.core", "platform.entity-identity"}
    with_acquisition = plan_for("onchain", ["acquisition"])
    bound = {
        b["provider"]
        for b in with_acquisition["bindings"]
        if "onchain" in b["consumers"]
    }
    assert bound == {
        "onchain.core",
        "platform.entity-identity",
        "platform.source-runtime",
    }
    assert ("onchain-observations", "^1.0.0") in {
        (p["pack_id"], p.get("range")) for p in plan["source_packs"]
    }


def test_osint_onchain_feature_is_off_by_default_and_binds_onchain_when_selected():
    osint = json.loads((ROOT / "packs/osint/composition.json").read_text())
    feature = next(f for f in osint["optional_features"] if f["id"] == "onchain")
    assert feature["default"] is False and "never for persons" in feature["description"]
    assert "onchain" not in {p["id"] for p in plan_for("osint")["packs"]}
    plan = plan_for("osint", ["onchain"])
    assert any(
        b["provider"] == "onchain.core" and b["capability"] == "onchain.observations"
        for b in plan["bindings"]
    )


@pytest.fixture()
def dossier_conn():
    conn = duckdb.connect(":memory:")
    for source in fb.RAW_FILES:
        fb.run_journey(conn, source, namespace="onchain")
    conn.execute(
        "CREATE TABLE document_actors (document_id VARCHAR, source_type VARCHAR, actor_name VARCHAR, "
        "entity_id VARCHAR, role VARCHAR, confidence DOUBLE, extracted_at VARCHAR)"
    )
    conn.execute(
        "CREATE TABLE canonical_entities (canonical_id TEXT PRIMARY KEY, preferred_name TEXT NOT NULL, "
        "entity_type TEXT, created_at BIGINT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO canonical_entities VALUES ('org:exampla','Exampla Foundation','organization',1),"
        "('person:jane','Jane Example','person',1)"
    )
    yield conn
    conn.close()


def _accept(conn):
    store = OnchainStore(conn)
    key = f"eip155:1|{fb.lower('exampla-token-contract')}|ethereum-lists-tokens|token"
    ref = store.propose_label_reference(
        "onchain",
        key,
        "org:exampla",
        reason="the token label names the foundation",
        principal_id="analyst",
        scopes=fb.SCOPES,
    )
    return store, store.review_label_reference(
        "onchain",
        ref["reference_id"],
        "accept",
        "checked",
        principal_id="reviewer",
        scopes=fb.SCOPES,
    )


def test_entity_dossier_cites_contract_origin_for_an_organization_through_accepted_references(
    dossier_conn,
):
    request = {"namespace": "onchain", "scopes": {onchain.READ_SCOPE}}
    before = entity_dossier(
        dossier_conn, "org:exampla", entity_type="organization", onchain=request
    )["onchain"]
    assert before["status"] == "no_accepted_references"
    store, accepted = _accept(dossier_conn)
    section = entity_dossier(
        dossier_conn, "org:exampla", entity_type="organization", onchain=request
    )["onchain"]
    assert (
        section["status"] == "assembled"
        and "Not a statement that the entity controls" in section["notice"]
    )
    contract = section["contracts"][0]
    assert contract["deployment"]["deployer"] == fb.lower("exampla-deployer")
    assert contract["deployment"]["citation"]["cited"] is True
    assert contract["labels"][0]["quote"] == "Exampla Token"
    # Names are never matched; persons are never assembled; the read scope is required.
    by_name = entity_dossier(
        dossier_conn, "Exampla Foundation", entity_type="organization", onchain=request
    )
    assert by_name["onchain"]["status"] == "not_resolved"
    person = entity_dossier(
        dossier_conn, "person:jane", entity_type="person", onchain=request
    )
    assert (
        person.get("code") == "person_requires_documents"
        or person["onchain"]["status"] == "not_assembled_for_person"
    )
    denied = entity_dossier(
        dossier_conn,
        "org:exampla",
        entity_type="organization",
        onchain={"namespace": "onchain", "scopes": {"knowledge:read"}},
    )
    assert denied["onchain"]["status"] == "unauthorized"
    store.revert_label_reference(
        "onchain",
        accepted["reference_id"],
        "wrong",
        principal_id="reviewer",
        scopes=fb.SCOPES,
    )
    after = entity_dossier(
        dossier_conn, "org:exampla", entity_type="organization", onchain=request
    )["onchain"]
    assert after["status"] == "no_accepted_references"


def test_dossier_without_the_feature_has_no_onchain_section(dossier_conn):
    assert "onchain" not in entity_dossier(
        dossier_conn, "org:exampla", entity_type="organization"
    )
