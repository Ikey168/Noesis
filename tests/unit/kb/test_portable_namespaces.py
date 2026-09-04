from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from jsonschema import Draft7Validator

from src.kb.portable_namespaces import (
    PortableNamespaceError,
    PortableNamespaceStore,
    canonical_bytes,
    decrypt_package,
    encrypt_package,
    sign_package,
    verify_signature,
)

ROOT=Path(__file__).resolve().parents[3]
SCOPES={"knowledge:namespace:export","knowledge:namespace:import","namespace:research:read","namespace:copy:write"}


@pytest.fixture()
def store(tmp_path: Path):
    conn=duckdb.connect(str(tmp_path/"portable.duckdb")); value=PortableNamespaceStore(conn)
    yield value
    conn.close()


def seeded(store: PortableNamespaceStore) -> None:
    store.put_component("research","document","doc-1",{"title":"Study","private_note":"secret"},source_id="crossref",observed_at_ms=100)
    store.put_component("research","chunk","chunk-1",{"text":"result"},dependencies=["doc-1"],source_id="crossref",observed_at_ms=101)
    store.put_component("research","embedding","embedding-1",{"vector":[0.1,0.2]},dependencies=["chunk-1"],sensitivity="restricted",observed_at_ms=102)
    store.put_component("research","schema","schema-1",{"version":"1.0.0"})


def test_manifest_schema_and_empty_fixture(store: PortableNamespaceStore) -> None:
    schema=json.loads((ROOT/"contracts/schemas/jsonschema/noesis-knowledge-package-manifest-v1.json").read_text()); fixture=json.loads((ROOT/"contracts/examples/portable-namespaces/research-manifest.json").read_text())
    Draft7Validator.check_schema(schema); assert not list(Draft7Validator(schema).iter_errors(fixture))


def test_export_is_byte_stable_filtered_and_metadata_only(store: PortableNamespaceStore) -> None:
    seeded(store); one=store.export("research",scopes=SCOPES); two=store.export("research",scopes=SCOPES)
    assert canonical_bytes(one)==canonical_bytes(two)
    assert store.verify(one)["valid"]
    filtered=store.export("research",mode="filtered",filters={"sources":["crossref"],"since_ms":101},scopes=SCOPES)
    assert {item["component_id"] for item in filtered["payload"]}=={"chunk-1","doc-1"}
    metadata=store.export("research",mode="metadata-only",scopes=SCOPES)
    assert metadata["payload"]==[] and metadata["disclosure_receipt"]["unverifiable"]


def test_redaction_has_no_private_or_dangling_references(store: PortableNamespaceStore) -> None:
    seeded(store); package=store.export("research",redaction={"sensitivities":["restricted"],"fields":["private_note"]},scopes=SCOPES)
    assert "secret" not in canonical_bytes(package).decode()
    ids={item["component_id"] for item in package["payload"]}
    assert all(set(item["dependencies"])<=ids for item in package["payload"])
    assert package["disclosure_receipt"]["omitted"]


def test_preview_atomic_import_roundtrip_idempotency_and_conflicts(store: PortableNamespaceStore) -> None:
    seeded(store); package=store.export("research",scopes=SCOPES)
    preview=store.preview_import(package,"copy",conflict_policy="new-namespace",scopes=SCOPES)
    result=store.import_package(package,"copy","import-1",conflict_policy="new-namespace",scopes=SCOPES,principal_id="alice",expected_preview_hash=preview["preview_hash"])
    assert result["imported"]==4 and store.import_package(package,"copy","import-1",conflict_policy="new-namespace",scopes=SCOPES,principal_id="alice")==result
    copied=store.export("copy",scopes={"operator"}); assert copied["manifest"]["component_counts"]==package["manifest"]["component_counts"]
    changed=json.loads(json.dumps(package)); changed["payload"][0]["content"]={"different":True}; changed["payload"][0]["content_hash"]="0"*64
    with pytest.raises(PortableNamespaceError,match="validation"): store.preview_import(changed,"copy",scopes=SCOPES)


def test_keep_both_remap_cancellation_and_limits(store: PortableNamespaceStore) -> None:
    seeded(store); package=store.export("research",scopes=SCOPES)
    store.import_package(package,"copy","initial",scopes=SCOPES,principal_id="alice")
    altered=json.loads(json.dumps(package)); altered["payload"][0]["content"]["edition"]=2
    from src.kb.portable_namespaces import _digest
    altered["payload"][0]["content_hash"]=_digest(altered["payload"][0]["content"]); altered["manifest"]["components"][0]["content_hash"]=altered["payload"][0]["content_hash"]
    manifest=dict(altered["manifest"]); manifest.pop("content_hash"); altered["manifest"]["content_hash"]=_digest({"manifest":manifest,"payload":altered["payload"]})
    result=store.import_package(altered,"copy","keep",conflict_policy="keep-both",scopes=SCOPES,principal_id="alice")
    assert result["renamed"]
    with pytest.raises(PortableNamespaceError,match="cancelled"): store.import_package(package,"cancelled","cancel",scopes={"operator"},principal_id="alice",cancelled=lambda:True)
    assert store.conn.execute("SELECT COUNT(*) FROM portable_namespace_components WHERE namespace='cancelled'").fetchone()==(0,)
    store.max_package_bytes=10
    with pytest.raises(PortableNamespaceError,match="byte limit"): store.verify(package)


def test_detached_signature_encryption_tamper_and_wrong_key(store: PortableNamespaceStore) -> None:
    seeded(store); package=store.export("research",scopes=SCOPES); private=Ed25519PrivateKey.generate()
    private_bytes=private.private_bytes(serialization.Encoding.Raw,serialization.PrivateFormat.Raw,serialization.NoEncryption()); public_bytes=private.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw)
    signature=sign_package(package,private_bytes,key_id="research-key")
    assert verify_signature(package,signature,public_bytes,required_key_id="research-key")["valid"]
    tampered=json.loads(json.dumps(package)); tampered["payload"][0]["content"]={}
    assert not verify_signature(tampered,signature,public_bytes)["valid"]
    key=bytes(range(32)); envelope=encrypt_package(package,key,recipient_id="partner")
    assert decrypt_package(envelope,key,recipient_id="partner")==package
    with pytest.raises(PortableNamespaceError,match="recipient"): decrypt_package(envelope,key,recipient_id="other")
    with pytest.raises(PortableNamespaceError,match="tampered"): decrypt_package(envelope,bytes(reversed(range(32))),recipient_id="partner")


def test_namespace_scope_and_metadata_import_guards(store: PortableNamespaceStore) -> None:
    with pytest.raises(PortableNamespaceError,match="authorized"): store.export("research",scopes={"knowledge:namespace:export"})
    metadata=store.export("research",mode="metadata-only",scopes=SCOPES)
    with pytest.raises(PortableNamespaceError,match="metadata-only"): store.import_package(metadata,"copy","meta",scopes=SCOPES,principal_id="alice")
