"""Offline harness for the Astronomy pack (#2149): fictional fixtures through the real adapter and projector.

Everything under ``tests/fixtures/astronomy`` is authored in the documented
shapes (AS01) and names fictional objects only: the small body ``2099 AB12``
later identified with the numbered ``(999901) Fictaria`` (primary ``2098 QX7``),
the hosts ``Fict-101``, ``Fict-202`` and ``Fict-303``, the ``Fictron`` launches of
2099 from the ``FKSC`` site, and SWPC serials 9001-9004. Nothing here is live
coverage. Pages go through :class:`AstronomyAdapter` with the fixture transport
and then through :class:`AstronomyProjector` exactly as the source-pack runtime
calls it. ``python -m tests.unit.astronomy.harness`` rewrites the pinned
production fixtures and their hashes in ``config/source_packs/astronomy.json``.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import duckdb

from src.ingestion.astronomy_sources import AstronomyAdapter, fixture_transport
from src.ingestion.source_packs import _digest, validate_source_pack
from src.kb.astronomy_store import AstronomyProjector

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/astronomy"
PACK = ROOT / "config/source_packs/astronomy.json"
NS = "astronomy"
OWN_NS = "ownership"
GEO_NS = "geo"
PRINCIPAL = "analyst"
SCOPES = {
    "knowledge:astronomy:read",
    "knowledge:astronomy:write",
    "knowledge:astronomy:review",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:ownership:read",
    "knowledge:ownership:write",
    "knowledge:ownership:review",
    f"namespace:{OWN_NS}:read",
    f"namespace:{OWN_NS}:write",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:review",
    "knowledge:schema:register",
}
READ_ONLY = {"knowledge:astronomy:read", f"namespace:{NS}:read"}
SOURCE_IDS = {
    "identifier": "mpc-designation-identifier",
    "mpcorb": "mpc-mpcorb-nea",
    "sbdb": "jpl-sbdb-solutions",
    "sentry": "jpl-sentry-listings",
    "ps": "exoplanet-archive-ps",
    "pscomppars": "exoplanet-archive-pscomppars",
    "toi": "exoplanet-archive-toi",
    "koi": "exoplanet-archive-koi",
    "removed": "exoplanet-archive-removed",
    "gcat_launch": "gcat-launch-log",
    "gcat_satcat": "gcat-satellite-catalog",
    "gcat_orgs": "gcat-organizations",
    "satcat": "celestrak-satcat",
    "swpc": "noaa-swpc-alerts",
}
# The observation stages of the authored files (the key is the simulated observation day, optionally with an hour).
STAGES = {
    "identifier": {
        "2099-01-20": ["mpc_identifier_2099-01-20.json"],
        "2099-04-10": ["mpc_identifier_2099-04-10.json"],
    },
    "mpcorb": {
        "2099-02-01": ["mpcorb_2099-02-01.dat"],
        "2099-05-01": ["mpcorb_2099-05-01.dat"],
    },
    "sbdb": {
        "2099-02-05": ["sbdb_2099-02-05.json"],
        "2099-05-05": ["sbdb_2099-05-05.json"],
    },
    "sentry": {
        "2099-02-06": ["sentry_2099-02-06.json"],
        "2099-04-12": ["sentry_2099-04-12.json"],
    },
    "ps": {"2099-01-15": ["ps_2099-01-15.csv"], "2099-06-01": ["ps_2099-06-01.csv"]},
    "pscomppars": {"2099-01-15": ["pscomppars_2099-01-15.csv"]},
    "toi": {"2099-01-15": ["toi_2099-01-15.csv"], "2099-05-02": ["toi_2099-05-02.csv"]},
    "koi": {"2099-01-15": ["cumulative_2099-01-15.csv"]},
    "removed": {"2099-06-01": ["removed_2099-06-01.csv"]},
    "gcat_launch": {"2099-05-01": ["gcat_launch_2099-05-01.tsv"]},
    "gcat_satcat": {
        "2099-05-01": ["gcat_satcat_2099-05-01.tsv"],
        "2099-07-01": ["gcat_satcat_2099-07-01.tsv"],
    },
    "gcat_orgs": {"2099-05-01": ["gcat_orgs_2099-05-01.tsv"]},
    "satcat": {
        "2099-06-01": ["satcat_2099-06-01.csv"],
        "2099-07-01": ["satcat_2099-07-01.csv"],
    },
    "swpc": {
        "2099-09-01T13": ["swpc_alerts_2099-09-01T13.json"],
        "2099-09-02T07": ["swpc_alerts_2099-09-02T07.json"],
    },
}
OBJECT = "2099 AB12"
NUMBERED = "(999901)"
HOSTS = [
    {"name": "Fict-101", "tic": "999000101"},
    {"name": "Fict-202", "tic": "999000202"},
    {"name": "Fict-303", "kepid": "99900303"},
]
CODES = ["FICTSPACE", "FICTSYS", "FICTOPS", "FKSC"]
# Documents whose content carries a file date (the source's own date for the document).
DATED = {"mpcorb", "gcat_launch", "gcat_satcat", "gcat_orgs", "satcat"}


def ms(stage: str, hour: int = 12) -> int:
    if "T" in stage:
        day, stage_hour = stage.split("T")
        return int(
            datetime.fromisoformat(day)
            .replace(hour=int(stage_hour), tzinfo=UTC)
            .timestamp()
            * 1000
        )
    return int(
        datetime.fromisoformat(stage).replace(hour=hour, tzinfo=UTC).timestamp() * 1000
    )


def production(key: str) -> dict:
    manifest = validate_source_pack(json.loads(PACK.read_text()))
    return copy.deepcopy(
        next(s for s in manifest["sources"] if s["source_id"] == SOURCE_IDS[key])
    )


def _rehash(source: dict) -> dict:
    source.pop("source_hash", None)
    source["source_hash"] = _digest(source)
    return source


def fictional(key: str, files: list[str]) -> dict:
    """The production source with the fictional bounds and the fixture documents."""
    source = production(key)
    declared = source["astronomy"]
    host = urlsplit(source["endpoint"]).hostname
    if "objects" in declared:
        declared["objects"] = [OBJECT, NUMBERED]
    if "hosts" in declared:
        declared["hosts"] = copy.deepcopy(HOSTS)
    if "years" in declared:
        declared["years"] = ["2099"]
    if "codes" in declared:
        declared["codes"] = list(CODES)
    documents = []
    for name in files:
        document = {
            "label": name,
            "url": f"https://{host}/fixture/{name}",
            "listing": "complete" if key == "ps" else "partial",
        }
        if key in DATED:
            document["source_as_of"] = name.rsplit("_", 1)[1].split(".")[0][:10]
        if key in {"sbdb", "sentry"}:
            document["object"] = OBJECT
        documents.append(document)
    declared["documents"] = documents
    return _rehash(source)


def pages(files: list[str]) -> list[dict]:
    return [
        {
            "request": f"/fixture/{name}",
            "status": 200,
            "body": (FIXTURES / name).read_text(),
        }
        for name in files
    ]


def acquire(
    conn,
    key: str,
    stage: str,
    *,
    run_id: str | None = None,
    observed_at_ms: int | None = None,
) -> list:
    """Run one stage's documents through the adapter and the projector at a simulated observation time."""
    files = STAGES[key][stage]
    source = fictional(key, files)
    adapter = AstronomyAdapter(source, transport=fixture_transport(pages(files)))
    projector = AstronomyProjector(conn)
    receipts, cursor, page_no = [], None, 0
    while True:
        page = adapter.fetch_page(
            {"operation": "documents", "parameters": {}, "limit": 5000}, cursor=cursor
        )
        page_no += 1
        observed = (observed_at_ms or ms(stage)) + page_no
        projector.project_page(
            run_id=run_id or f"run:{key}:{stage}",
            manifest={},
            source=source,
            records=page.records,
            documents=[{"ingested_at": observed}],
            page_receipt=dict(page.receipt),
            principal_id=PRINCIPAL,
        )
        receipts.append(dict(page.receipt))
        cursor = page.next_cursor
        if cursor is None:
            return receipts


def steps(until: str | None = None) -> list[tuple[str, str]]:
    ordered = sorted((stage, key) for key, stages in STAGES.items() for stage in stages)
    return [(stage, key) for stage, key in ordered if until is None or stage <= until]


def acquire_all(conn, *, until: str | None = None) -> None:
    """Every stage in observation order (optionally only those on or before ``until``)."""
    for stage, key in steps(until):
        acquire(conn, key, stage)


def connection():
    return duckdb.connect(":memory:")


def seed_papers(conn, clock) -> dict[str, str]:
    """Fictional Science literature records (paper documents) the fixtures cite by bibcode or DOI."""
    from services.ingest.common.document_model import Document
    from src.ingestion.document_store import DocumentStore

    specs = {
        "fict-101-b": {
            "title": "A transiting sub-Neptune around Fict-101 (fictional)",
            "metadata": {
                "doi": "10.5555/fict.2099.101a",
                "bibcode": "2099AJ....999..101F",
            },
        },
        "fict-101-discovery": {
            "title": "Discovery of Fict-101 b (fictional)",
            "metadata": {"doi": "10.5555/fict.2099.101"},
        },
        "unrelated": {
            "title": "Fict-303 c revisited (fictional; cites nothing the archive states)",
            "metadata": {"doi": "10.5555/fict.2099.777"},
        },
    }
    payloads, ids = [], {}
    for key, spec in specs.items():
        document = Document(
            document_id=f"astro-doc:{key}",
            source_type="paper",
            source_id="crossref",
            language="en",
            ingested_at=clock(),
            url=f"https://example.org/{key}",
            title=spec["title"],
            content=spec["title"],
            authors=["A. Fictional"],
            metadata={
                "content_representation": "plain-text-abstract",
                **spec["metadata"],
            },
        )
        payloads.append(document.to_dict())
        ids[key] = document.document_id
    outcome = DocumentStore(conn).upsert(payloads)
    assert not outcome.invalid, outcome.dead_letter
    return ids


# ------------------------------------------------------------------ production fixtures

PRODUCTION_BODIES = {
    "identifier": "mpc_identifier_2099-01-20.json",
    "mpcorb": "mpcorb_2099-02-01.dat",
    "sbdb": "sbdb_2099-02-05.json",
    "sentry": "sentry_2099-02-06.json",
    "ps": "ps_2099-01-15.csv",
    "pscomppars": "pscomppars_2099-01-15.csv",
    "toi": "toi_2099-01-15.csv",
    "koi": "cumulative_2099-01-15.csv",
    "removed": "removed_2099-06-01.csv",
    "gcat_launch": "gcat_launch_2099-05-01.tsv",
    "gcat_satcat": "gcat_satcat_2099-05-01.tsv",
    "gcat_orgs": "gcat_orgs_2099-05-01.tsv",
    "satcat": "satcat_2099-06-01.csv",
    "swpc": "swpc_alerts_2099-09-01T13.json",
}


def build_production_fixtures() -> dict:
    """Serve the authored (fictional) documents for the production URLs and pin their hashes."""
    from src.ingestion.astronomy_sources import replay_native_fixture

    pack = json.loads(PACK.read_text())
    by_id = {s["source_id"]: s for s in pack["sources"]}
    for key, source_id in SOURCE_IDS.items():
        declared = by_id[source_id]
        native = []
        for document in declared["astronomy"]["documents"]:
            parts = urlsplit(document["url"])
            native.append(
                {
                    "request": parts.path + ("?" + parts.query if parts.query else ""),
                    "status": 200,
                    "body": (FIXTURES / PRODUCTION_BODIES[key]).read_text(),
                    "authored": True,
                }
            )
        fixture = {
            "captured": False,
            "provider": declared["astronomy"]["provider"],
            "note": "Authored documents naming fictional objects (2099 designations, Fict-* hosts, 2099 launches, "
            "SWPC serials 9001-9004), served for the production document URLs. The production bounds are "
            "therefore out of scope for every bounded row; parsing is exercised with the fictional bounds in "
            "tests/unit/astronomy/harness.py.",
            "native_pages": native,
            "scenarios": [
                "bounded-selection",
                "out-of-scope-rows-counted",
                "authored-fictional",
            ],
        }
        path = ROOT / declared["fixture"]["path"]
        raw = (json.dumps(fixture, indent=2, ensure_ascii=False) + "\n").encode()
        path.write_bytes(raw)
        declared["fixture"]["sha256"] = hashlib.sha256(raw).hexdigest()
        source = validate_source_pack({**pack, "sources": [declared]})["sources"][0]
        declared["fixture"]["expected_output_hash"] = _digest(
            replay_native_fixture(source, fixture)
        )
    PACK.write_text(json.dumps(pack, indent=2, ensure_ascii=False) + "\n")
    return pack


if __name__ == "__main__":
    build_production_fixtures()
