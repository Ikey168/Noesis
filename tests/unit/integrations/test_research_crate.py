import base64
import copy
import io
import json
import zipfile

import pytest

pytest.importorskip("rocrate")

from scripts.benchmark_integration_rocrate import PUBLICATION, example
from src.integrations.common import IntegrationError
from src.integrations.export import export_rocrate
from src.kb.research_packages import WRITE_SCOPE


def unpack(result):
    archive = zipfile.ZipFile(io.BytesIO(base64.b64decode(result["bytes_b64"])))
    files = {name: archive.read(name) for name in archive.namelist()}
    graph = json.loads(files["ro-crate-metadata.json"])["@graph"]
    return files, {entity["@id"]: entity for entity in graph}


def test_native_member_mapping_lineage_and_byte_replay(tmp_path):
    store, package = example()
    exported = export_rocrate(package, metadata=PUBLICATION)
    files, graph = unpack(exported)
    assert export_rocrate(package, metadata=PUBLICATION) == exported
    assert json.loads(files["native-package.json"]) == package
    assert store.verify(json.loads(files["native-package.json"]))["valid"]
    mapping = json.loads(files["noesis-mapping.json"])
    paths = {m["component_id"]: m["path"] for m in mapping["members"]}
    assert len(paths) == 4
    assert "Dataset" in graph[paths["data:berlin"]]["@type"]
    assert "Report" in graph[paths["report:berlin"]]["@type"]
    assert graph[paths["report:berlin"]]["identifier"] == ["report:berlin", "report:r2"]
    assert graph[paths["model:pinned"]]["version"] == "test-revision"
    assert graph[paths["analysis:1"]]["isBasedOn"] == [
        {"@id": paths["data:berlin"]},
        {"@id": paths["model:pinned"]},
    ]
    software = [e for e in graph.values() if e["@type"] == "SoftwareApplication"]
    assert software[0]["softwareVersion"] == "1.0"
    assert all(not name.startswith("/") and ".." not in name for name in files)
    for name, data in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    from rocrate.rocrate import ROCrate

    reopened = ROCrate(str(tmp_path))
    assert reopened.dereference(paths["report:berlin"])["identifier"] == [
        "report:berlin",
        "report:r2",
    ]
    store.conn.close()


def test_missing_and_restricted_members_are_not_materialized():
    store, package = example(partial=True)
    files, _ = unpack(export_rocrate(package, metadata=PUBLICATION))
    all_bytes = b"".join(files.values())
    assert b"SECRET-DO-NOT-EXPORT" not in all_bytes
    assert b"ORIGINAL-DO-NOT-EXPORT" not in all_bytes
    assert b"Public summary" in all_bytes
    mapping = json.loads(files["noesis-mapping.json"])
    assert {x["reason"] for x in mapping["omissions"]} == {"missing", "inaccessible"}
    assert {x["component_id"] for x in mapping["members"]}.isdisjoint(
        {"missing:1", "restricted:1"}
    )
    verified = store.verify(json.loads(files["native-package.json"]))
    assert not verified["valid"]
    assert verified["missing_members"] == [
        {"component_id": "missing:1", "reason": "missing"}
    ]
    store.conn.close()


def test_explicit_publication_metadata_and_native_integrity_required():
    store, package = example()
    with pytest.raises(IntegrationError, match="publication date"):
        export_rocrate(package)
    broken = copy.deepcopy(package)
    broken["members"][0]["content"] = {"tampered": True}
    with pytest.raises(IntegrationError, match="verification"):
        export_rocrate(broken, metadata=PUBLICATION)
    with pytest.raises(IntegrationError, match="license"):
        export_rocrate(package, metadata={"datePublished": "2026-09-05"})
    store.conn.close()


def test_store_export_author_identifier_and_path_injection():
    store, package = example()
    store.register_component(
        "research",
        "document",
        "../../unsafe",
        {"text": "Authored identity test"},
        metadata={
            "authors": [
                {
                    "name": "Synthetic identifier fixture",
                    "orcid": "https://orcid.org/0000-0000-0000-0001",
                }
            ]
        },
        principal_id="fixture",
        scopes={WRITE_SCOPE},
    )
    exported = store.export_rocrate(
        "research",
        package["package_id"],
        ["../../unsafe"],
        metadata=PUBLICATION,
        principal_id="fixture",
        scopes={WRITE_SCOPE},
    )
    files, graph = unpack(exported)
    assert all(".." not in name for name in files)
    assert graph["https://orcid.org/0000-0000-0000-0001"]["@type"] == "Person"
    store.conn.close()


def test_export_preserves_native_signature_for_separate_verification():
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    store, package = example()
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
        key_id="fixture",
        key_version=1,
    )
    files, _ = unpack(export_rocrate(signed, metadata=PUBLICATION))
    original = json.loads(files["native-package.json"])
    verified = store.verify(
        original,
        public_keys={"fixture": base64.b64encode(public_bytes).decode()},
        require_signature=True,
    )
    assert verified["valid"] and verified["signature_status"] == "valid"
    store.conn.close()
