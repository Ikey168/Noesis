"""Offline harness for the space-object registration feature (#2224): fictional fixtures through the real adapter.

Everything under ``tests/fixtures/astronomy_registration`` is authored in the
documented shapes (SO01) and names fictional objects only, aligned with the
Astronomy pack's fictional SATCAT/GCAT objects (``tests/unit/astronomy/harness.py``):
``FICTSAT 1`` (2099-001A, NORAD 99901) registered by Fictland in
``ST/SG/SER.E/9901``, transferred to the Republic of Examplia
(``ST/SG/SER.E/9950``) and re-entered on 2099-06-21 (``ST/SG/SER.E/9970``,
Aerospace predictions and a confirmed report, a DISCOS re-entry epoch); the
rocket body 2099-001B with no UN registration on record; ``FICTSAT 3``
(2099-003B, 99904) registered by the intergovernmental European Fictional
Space Organisation and later declared non-functional; and ``FICTCUBE``,
registered by name only. Nothing here is live coverage.
``python -m tests.unit.astronomy.registration_harness`` rewrites the pinned
production fixtures and their hashes in ``config/source_packs/astronomy.json``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from urllib.parse import urlsplit

from src.ingestion.astronomy_registration_sources import (
    FIXTURE_SECRET,
    RegistrationAdapter,
    fixture_transport,
    replay_native_fixture,
)
from src.ingestion.source_packs import _digest, validate_source_pack
from src.kb.astronomy_registration import RegistrationProjector
from tests.unit.astronomy.harness import PACK, PRINCIPAL, ROOT, ms

FIXTURES = ROOT / "tests/fixtures/astronomy_registration"
SOURCE_IDS = {
    "index": "unoosa-online-index",
    "documents": "unoosa-registration-documents",
    "discos_objects": "esa-discos-objects",
    "discos_reentries": "esa-discos-reentries",
    "aerospace": "aerospace-reentries",
}
DOCUMENTS = {
    "ser_e_9901.txt": {"symbol": "ST/SG/SER.E/9901", "published_on": "2099-04-01", "language": "en",
                       "registrant": "Fictland", "registrant_kind": "state"},
    "ser_e_9903.txt": {"symbol": "ST/SG/SER.E/9903", "published_on": "2099-05-10", "language": "en",
                       "registrant": "European Fictional Space Organisation",
                       "registrant_kind": "intergovernmental_organisation"},
    "ser_e_9950.txt": {"symbol": "ST/SG/SER.E/9950", "published_on": "2099-05-15", "language": "en",
                       "registrant": "Republic of Examplia", "registrant_kind": "state"},
    "ser_e_9960.txt": {"symbol": "ST/SG/SER.E/9960", "published_on": "2099-06-01", "language": "en",
                       "registrant": "European Fictional Space Organisation",
                       "registrant_kind": "intergovernmental_organisation"},
    "ser_e_9970.txt": {"symbol": "ST/SG/SER.E/9970", "published_on": "2099-06-25", "language": "en",
                       "registrant": "Republic of Examplia", "registrant_kind": "state"},
}
STAGES = {
    "index": {"2099-03-20": ["unoosa_index_2099-03-20.json"], "2099-07-05": ["unoosa_index_2099-07-05.json"]},
    "documents": {
        "2099-04-02": ["ser_e_9901.txt"],
        "2099-05-11": ["ser_e_9903.txt"],
        "2099-05-16": ["ser_e_9950.txt"],
        "2099-06-02": ["ser_e_9960.txt"],
        "2099-06-26": ["ser_e_9970.txt"],
    },
    "discos_objects": {"2099-06-01": ["discos_objects_2099-06-01.json"]},
    "discos_reentries": {"2099-06-25": ["discos_reentries_2099-06-25.json"]},
    "aerospace": {"2099-06-19": ["aerospace_99901_2099-06-19.html"], "2099-06-22": ["aerospace_99901_2099-06-22.html"]},
}
PRODUCTION_BODIES = {
    "index": "unoosa_index_2099-03-20.json",
    "documents": "ser_e_9901.txt",
    "discos_objects": "discos_objects_2099-06-01.json",
    "discos_reentries": "discos_reentries_2099-06-25.json",
    "aerospace": "aerospace_99901_2099-06-22.html",
}


def production(key: str) -> dict:
    manifest = validate_source_pack(json.loads(PACK.read_text()))
    return copy.deepcopy(next(s for s in manifest["sources"] if s["source_id"] == SOURCE_IDS[key]))


def fictional(key: str, files: list[str], *, mode: str | None = None) -> dict:
    """The production source with the fictional bounds and the fixture documents."""
    source = production(key)
    declared = source["astronomy_registration"]
    host = urlsplit(source["endpoint"]).hostname
    if "years" in declared:
        declared["years"] = ["2099"]
    if key.startswith("discos"):
        declared["objects"] = ["70001", "70004"]
        if mode:
            declared["mode"] = mode
    if key == "aerospace":
        declared["objects"] = ["99901"]
    documents = []
    for name in files:
        document = {"label": name, "url": f"https://{host}/fixture/{name}"}
        if key == "documents":
            document.update(DOCUMENTS[name])
        elif key == "index":
            document["source_as_of"] = name.rsplit("_", 1)[1].split(".")[0]
        elif key.startswith("discos"):
            document["resource"] = "objects" if key == "discos_objects" else "reentries"
            document["source_as_of"] = name.rsplit("_", 1)[1].split(".")[0]
        elif key == "aerospace":
            document["object"] = {"norad": "99901", "cospar": "2099-001A", "name": "FICTSAT 1"}
        documents.append(document)
    declared["documents"] = documents
    source.pop("source_hash", None)
    source["source_hash"] = _digest(source)
    return source


def pages(files: list[str]) -> list[dict]:
    return [{"request": f"/fixture/{name}", "status": 200, "body": (FIXTURES / name).read_text()} for name in files]


def acquire(conn, key: str, stage: str, *, secret: str | None = FIXTURE_SECRET, mode: str | None = None) -> list:
    """Run one stage's documents through the adapter and the projector at a simulated observation time."""
    files = STAGES[key][stage]
    source = fictional(key, files, mode=mode)
    adapter = RegistrationAdapter(source, transport=fixture_transport(pages(files)), secret=secret)
    projector = RegistrationProjector(conn)
    receipts, cursor, page_no = [], None, 0
    while True:
        page = adapter.fetch_page({"operation": "documents", "parameters": {}, "limit": 500}, cursor=cursor)
        page_no += 1
        projector.project_page(
            run_id=f"run:{key}:{stage}", manifest={}, source=source, records=page.records,
            documents=[{"ingested_at": ms(stage) + page_no}], page_receipt=dict(page.receipt),
            principal_id=PRINCIPAL,
        )
        receipts.append({**dict(page.receipt), "records": list(page.records)})
        cursor = page.next_cursor
        if cursor is None:
            return receipts


def steps(until: str | None = None, *, discos: bool = True) -> list[tuple[str, str]]:
    ordered = sorted((stage, key) for key, stages in STAGES.items() for stage in stages)
    return [(s, k) for s, k in ordered if (until is None or s <= until) and (discos or not k.startswith("discos"))]


def acquire_all(conn, *, until: str | None = None, discos: bool = True) -> None:
    for stage, key in steps(until, discos=discos):
        acquire(conn, key, stage)


def build_production_fixtures() -> dict:
    """Serve the authored (fictional) documents for the production URLs and pin their hashes."""
    pack = json.loads(PACK.read_text())
    by_id = {s["source_id"]: s for s in pack["sources"]}
    for key, source_id in SOURCE_IDS.items():
        declared = by_id[source_id]
        native = []
        for document in declared["astronomy_registration"]["documents"]:
            parts = urlsplit(document["url"])
            native.append({"request": parts.path + ("?" + parts.query if parts.query else ""), "status": 200,
                           "body": (FIXTURES / PRODUCTION_BODIES[key]).read_text(), "authored": True})
        fixture = {
            "captured": False,
            "provider": declared["astronomy_registration"]["provider"],
            "note": "Authored documents naming fictional objects (2099 designators, ST/SG/SER.E/99xx symbols, "
            "Fictland, Republic of Examplia, European Fictional Space Organisation), served for the production "
            "document URLs. The production bounds leave the fictional rows out of scope or rejected; parsing is "
            "exercised with the fictional bounds in tests/unit/astronomy/registration_harness.py.",
            "native_pages": native,
            "scenarios": ["bounded-selection", "out-of-scope-rows-counted", "authored-fictional"],
        }
        path = ROOT / declared["fixture"]["path"]
        raw = (json.dumps(fixture, indent=2, ensure_ascii=False) + "\n").encode()
        path.write_bytes(raw)
        declared["fixture"]["sha256"] = hashlib.sha256(raw).hexdigest()
        source = validate_source_pack({**pack, "sources": [declared]})["sources"][0]
        declared["fixture"]["expected_output_hash"] = _digest(replay_native_fixture(source, fixture))
    PACK.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n")
    return pack


if __name__ == "__main__":
    build_production_fixtures()
