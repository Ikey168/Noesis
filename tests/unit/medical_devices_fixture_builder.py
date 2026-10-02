"""Rebuild the pinned medical-devices source-pack fixtures and their pins (#2654).

``python -m tests.unit.medical_devices_fixture_builder`` recomposes
``tests/fixtures/source_packs/clinical-devices-*.json`` from the authored responses in
``tests/fixtures/medical_devices`` and rewrites each source's ``fixture`` pin (file hash and expected replay
output hash) in ``config/source_packs/clinical-evidence.json``. Other sources are left byte-identical. The offline
tests check that the pinned files equal what this builder produces.
"""

from __future__ import annotations

import hashlib
import json

from src.ingestion.source_packs import (
    _digest,
    replay_native_fixture,
    validate_source_pack,
)
from tests.unit.medical_devices_harness import (
    PACK,
    ROOT,
    SOURCES,
    build_pack_fixture,
    pack_source,
)

FIXTURE_DIR = ROOT / "tests/fixtures/source_packs"


def fixture_path(source_id: str) -> str:
    return f"tests/fixtures/source_packs/{source_id}.json"


def fixture_bytes(source_id: str) -> bytes:
    return (json.dumps(build_pack_fixture(source_id), indent=1, sort_keys=True, ensure_ascii=False) + "\n").encode()


def rebuild() -> dict[str, str]:
    manifest = json.loads(PACK.read_text())
    by_id = {s["source_id"]: s for s in manifest["sources"]}
    for source_id in SOURCES:
        raw = fixture_bytes(source_id)
        (ROOT / fixture_path(source_id)).write_bytes(raw)
        entry = {**pack_source(source_id), "fixture": {"path": fixture_path(source_id),
                                                       "sha256": hashlib.sha256(raw).hexdigest(),
                                                       "expected_output_hash": "0" * 64}}
        if source_id in by_id:
            by_id[source_id].clear()
            by_id[source_id].update(entry)
        else:
            manifest["sources"].append(entry)
            by_id[source_id] = entry
    validated = {s["source_id"]: s for s in validate_source_pack(manifest)["sources"]}
    hashes = {}
    for source_id in SOURCES:
        output = replay_native_fixture(validated[source_id], json.loads((ROOT / fixture_path(source_id)).read_text()))
        hashes[source_id] = by_id[source_id]["fixture"]["expected_output_hash"] = _digest(output)
    PACK.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return hashes


if __name__ == "__main__":
    print(json.dumps(rebuild(), indent=1))
