import hashlib
import json
from pathlib import Path

import duckdb
import pytest

from src.ingestion.document_store import DocumentStore
from src.ingestion.zenodo import ZenodoClient


def test_native_manifest_retains_identity_and_files():
    native = json.loads(
        Path("tests/fixtures/integrations/zenodo-native.json").read_text()
    )
    client = ZenodoClient(transport=lambda **_: {"content": json.dumps(native)})
    result = client.record(native["id"])
    assert result["files"] == native["files"]
    assert result["doi"] == native["doi"]
    assert result["links"] == native["links"]


def test_selected_artifacts_replay_and_checksum_failure_are_atomic():
    # Deliberately synthetic, Unicode document integration fixture.
    payloads = {
        "berlin.txt": "Behörden in Berlin".encode(),
        "english.txt": b"Research evidence",
    }
    native = {
        "id": 123,
        "doi": "10.5281/zenodo.123",
        "conceptrecid": "120",
        "metadata": {"license": {"id": "cc-by-4.0"}, "access_right": "open"},
        "files": [
            {
                "key": k,
                "size": len(v),
                "checksum": "md5:" + hashlib.md5(v).hexdigest(),
                "links": {"self": "https://zenodo.org/files/" + k},
            }
            for k, v in payloads.items()
        ],
    }
    corrupt = False

    def transport(**kwargs):
        if "/api/records/" in kwargs["url"]:
            return {"content": json.dumps(native)}
        key = kwargs["url"].rsplit("/", 1)[-1]
        return {
            "content": b"bad" if corrupt and key == "english.txt" else payloads[key]
        }

    client = ZenodoClient(transport=transport)
    conn = duckdb.connect()
    store = DocumentStore(conn)
    client.acquire(
        123, list(payloads), store, languages={"berlin.txt": "de", "english.txt": "en"}
    )
    client.acquire(
        123, list(payloads), store, languages={"berlin.txt": "de", "english.txt": "en"}
    )
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 2
    corrupt = True
    with pytest.raises(ValueError, match="manifest"):
        client.acquire(
            123,
            list(payloads),
            store,
            languages={"berlin.txt": "de", "english.txt": "en"},
        )
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 2
    native["metadata"]["access_right"] = "restricted"
    with pytest.raises(ValueError, match="restricted"):
        client.acquire(
            123,
            list(payloads),
            store,
            languages={"berlin.txt": "de", "english.txt": "en"},
        )
    with pytest.raises(ValueError, match="origin"):
        client._get("https://evil.example/artifact")


def test_binary_artifacts_versions_links_and_durable_replay(tmp_path):
    payload = b"PK\x03\x04\x00\xffsoftware archive - stored without execution"
    native = {
        "id": 123,
        "doi": "10.5281/zenodo.123",
        "conceptrecid": "120",
        "metadata": {
            "version": "1",
            "access_right": "open",
            "license": {"id": "mit"},
            "related_identifiers": [
                {
                    "identifier": "10.1234/paper",
                    "scheme": "doi",
                    "relation": "isSupplementTo",
                }
            ],
        },
        "files": [
            {
                "key": "software.zip",
                "size": len(payload),
                "checksum": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "links": {"self": "https://zenodo.org/files/software.zip"},
            }
        ],
    }
    inaccessible = False

    def transport(**kwargs):
        if "/api/records/" in kwargs["url"]:
            return {"content": json.dumps(native)}
        return {"status": 403 if inaccessible else 200, "content": payload}

    client = ZenodoClient(transport=transport)
    conn = duckdb.connect(str(tmp_path / "artifacts.duckdb"))
    store = DocumentStore(conn)

    def acquire():
        return client.acquire(
            native["id"],
            ["software.zip"],
            store,
            languages={"software.zip": "en"},
            artifact_only=True,
        )

    assert acquire().inserted == 1
    assert acquire().duplicate == 1
    old_id, old_ref = conn.execute(
        "SELECT document_id, content_ref FROM documents"
    ).fetchone()
    assert store.read_artifact(old_ref) == payload
    assert (
        store.related_resources(old_id)["links"][0]["target_identifier"]
        == "10.1234/paper"
    )
    native["id"], native["doi"], native["metadata"]["version"] = (
        124,
        "10.5281/zenodo.124",
        "2",
    )
    assert acquire().inserted == 1
    assert (
        conn.execute("SELECT count(*) FROM document_artifact_blobs").fetchone()[0] == 1
    )
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 2
    inaccessible = True
    with pytest.raises(ValueError, match="publicly accessible"):
        acquire()
    conn.close()
    conn = duckdb.connect(str(tmp_path / "artifacts.duckdb"))
    assert DocumentStore(conn).read_artifact(old_ref) == payload
    conn.close()


def test_invalid_document_rolls_back_artifact_bytes():
    payload = b"unparsed data"
    native = {
        "id": 123,
        "metadata": {"access_right": "open"},
        "files": [
            {
                "key": "data.bin",
                "size": len(payload),
                "checksum": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "links": {"self": "https://zenodo.org/files/data.bin"},
            }
        ],
    }
    client = ZenodoClient(
        transport=lambda **kwargs: {
            "content": json.dumps(native)
            if "/api/records/" in kwargs["url"]
            else payload
        }
    )
    conn = duckdb.connect()

    def reject(_):
        raise ValueError("fixture validation failure")

    store = DocumentStore(conn, validator=reject)
    # Initialize the content table outside the rolled-back acquisition.
    original = store.put_artifact(b"existing")
    with pytest.raises(ValueError, match="validation failed"):
        client.acquire(
            123, ["data.bin"], store, languages={"data.bin": "en"}, artifact_only=True
        )
    assert (
        conn.execute("SELECT count(*) FROM document_artifact_blobs").fetchone()[0] == 1
    )
    assert store.read_artifact(original) == b"existing"
    assert conn.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
