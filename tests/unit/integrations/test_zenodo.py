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
