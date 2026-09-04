from __future__ import annotations

import base64
import json
from pathlib import Path

import duckdb
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft202012Validator

from src.kb.research_packages import (
    IMPORT_SCOPE,
    READ_SCOPE,
    WRITE_SCOPE,
    ResearchPackageError,
    ResearchPackageStore,
)

SCHEMAS = Path(__file__).resolve().parents[3] / "contracts/schemas/jsonschema"


def validate(name, value):
    Draft202012Validator(json.loads((SCHEMAS / name).read_text())).validate(value)


def manifest(**updates):
    value = {
        "format_version": "1.0",
        "question": "What changed?",
        "plan": {"recipe_id": "r1"},
        "snapshot": {"generation": 7},
        "evidence": ["claim:1"],
        "transformations": [{"tool": "compare"}],
        "findings": [{"text": "changed"}],
        "limitations": ["partial coverage"],
        "policies": {"view": "public"},
        "compatibility": {"minimum_noesis": "1"},
        "extensions": {"x-example": {"value": 1}},
    }
    value.update(updates)
    return value


def setup_store():
    store = ResearchPackageStore(duckdb.connect(":memory:"), now=lambda: 100)
    created = store.create_manifest(
        "research", manifest(), principal_id="p", scopes={WRITE_SCOPE}
    )
    return store, created


def test_manifest_unknown_extensions_canonical_order_version_and_schema():
    store = ResearchPackageStore(duckdb.connect(":memory:"), now=lambda: 100)
    bad = store.validate_manifest({**manifest(), "unknown": True})
    assert not bad["valid"] and bad["errors"][0]["code"] == "unknown_field"
    invalid_extension = store.validate_manifest(
        {**manifest(), "extensions": {"vendor": {}}}
    )
    assert not invalid_extension["valid"]
    incompatible = store.validate_manifest(manifest(format_version="9.0"))
    assert not incompatible["compatible"] and incompatible["negotiated_version"] is None
    one = store.create_manifest(
        "research", manifest(), principal_id="p", scopes={WRITE_SCOPE}
    )
    reordered = dict(reversed(list(manifest().items())))
    two = store.create_manifest(
        "research", reordered, principal_id="p", scopes={WRITE_SCOPE}
    )
    assert one["package_id"] == two["package_id"] and two["idempotent"]
    validate("noesis-research-package-manifest-v1.json", one)


def test_dependency_closure_shared_inaccessible_redacted_omitted_deterministic():
    store, _ = setup_store()
    store.register_component(
        "research",
        "claim",
        "root:a",
        {"text": "A"},
        dependencies=["shared", "secret", "missing"],
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    store.register_component(
        "research",
        "claim",
        "root:b",
        {"text": "B"},
        dependencies=["shared"],
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    store.register_component(
        "research",
        "document",
        "shared",
        {"full": "source"},
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    store.register_component(
        "research",
        "document",
        "secret",
        {"full": "classified"},
        access_status="redacted",
        redacted_content={"summary": "withheld"},
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    first = store.closure("research", ["root:b", "root:a"], scopes={READ_SCOPE})
    second = store.closure("research", ["root:a", "root:b"], scopes={READ_SCOPE})
    assert first["closure_hash"] == second["closure_hash"]
    assert len([m for m in first["members"] if m["component_id"] == "shared"]) == 1
    assert next(m for m in first["members"] if m["component_id"] == "secret")[
        "redacted"
    ]
    assert first["omissions"] == [{"component_id": "missing", "reason": "missing"}]
    validate("noesis-research-package-closure-v1.json", first)


def test_deterministic_export_signature_tamper_encryption_rotation_and_large_asset():
    store, created = setup_store()
    store.register_component(
        "research",
        "asset",
        "large",
        {"content_address": "sha256:abc", "byte_length": 2_000_000_000},
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    package = store.build(
        "research",
        created["package_id"],
        ["large"],
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    replay = store.build(
        "research",
        created["package_id"],
        ["large"],
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    assert (
        package["canonical_bytes_b64"] == replay["canonical_bytes_b64"]
        and package["reproducible"]
    )
    private = Ed25519PrivateKey.generate()
    private_bytes = private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    public_bytes = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    signed = store.sign(
        package,
        base64.b64encode(private_bytes).decode(),
        key_id="research-key",
        key_version=2,
    )
    verified = store.verify(
        signed,
        public_keys={"research-key": base64.b64encode(public_bytes).decode()},
        require_signature=True,
    )
    assert verified["valid"] and verified["key_version"] == 2
    tampered = {
        **signed,
        "members": [{**signed["members"][0], "content": {"tampered": True}}],
    }
    assert not store.verify(
        tampered, public_keys={"research-key": base64.b64encode(public_bytes).decode()}
    )["valid"]
    key = base64.b64encode(bytes(range(32))).decode()
    envelope = store.encrypt(signed, key, recipient_id="peer", key_version=3)
    decrypted = store.decrypt(envelope, key, recipient_id="peer")
    assert (
        decrypted["content_hash"] == package["content_hash"]
        and envelope["envelope"]["key_version"] == 3
    )
    validate("noesis-research-package-v1.json", package)
    validate("noesis-research-package-verification-v1.json", verified)


def test_isolated_import_untrusted_recipe_collision_partial_and_rollback():
    store, created = setup_store()
    store.register_component(
        "research",
        "recipe",
        "recipe:unsafe",
        {"command": "do not run"},
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    package = store.build(
        "research",
        created["package_id"],
        ["recipe:unsafe"],
        principal_id="p",
        scopes={WRITE_SCOPE},
    )
    with pytest.raises(ResearchPackageError, match="must start"):
        store.import_package(
            package, "research", principal_id="p", scopes={IMPORT_SCOPE}
        )
    incompatible = {
        **package,
        "manifest": {**package["manifest"], "format_version": "9.0"},
    }
    with pytest.raises(ResearchPackageError, match="not supported"):
        store.import_package(
            incompatible,
            "import:incompatible",
            principal_id="p",
            scopes={IMPORT_SCOPE},
        )
    receipt = store.import_package(
        package, "import:peer", principal_id="p", scopes={IMPORT_SCOPE}
    )
    assert receipt["disabled_recipes"] == ["recipe:unsafe"]
    replay = store.replay(
        "import:peer", receipt["import_id"], scopes={READ_SCOPE}, allow_executable=True
    )
    assert replay["executed_recipes"] == []
    store.conn.execute(
        "UPDATE research_package_imported_components SET content_hash='different' WHERE target_namespace='import:peer'"
    )
    # A new package hash with the same member identity is rejected rather than overwriting.
    altered = {**package, "manifest": {**package["manifest"], "question": "another"}}
    core = {
        k: v
        for k, v in altered.items()
        if k
        not in {
            "content_hash",
            "status",
            "canonical_bytes_b64",
            "byte_length",
            "reproducible",
            "signature",
            "envelope",
        }
    }
    altered["content_hash"] = (
        __import__("hashlib")
        .sha256(
            json.dumps(
                core, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        )
        .hexdigest()
    )
    with pytest.raises(ResearchPackageError, match="overwrite"):
        store.import_package(
            altered, "import:peer", principal_id="p", scopes={IMPORT_SCOPE}
        )
    rolled = store.rollback(
        "import:peer", receipt["import_id"], principal_id="p", scopes={IMPORT_SCOPE}
    )
    assert rolled["status"] == "rolled_back"
    validate("noesis-research-package-import-v1.json", receipt)


def test_auth_cancellation_offline_six_domains():
    store = ResearchPackageStore(duckdb.connect(":memory:"), now=lambda: 100)
    for namespace in (
        "research",
        "political",
        "economic",
        "osint",
        "technical",
        "scientific",
    ):
        created = store.create_manifest(
            namespace,
            manifest(question=namespace),
            principal_id="p",
            scopes={WRITE_SCOPE},
        )
        cancelled = store.build(
            namespace,
            created["package_id"],
            [],
            cancel_requested=True,
            principal_id="p",
            scopes={WRITE_SCOPE},
        )
        assert cancelled["status"] == "cancelled"
    with pytest.raises(ResearchPackageError, match="scope"):
        store.closure("research", [], scopes=set())
