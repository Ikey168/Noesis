"""Offline harness: authored responses through the real OSS adapter, projector and source-pack runtime."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import duckdb

from src.ingestion.oss_ecosystem_sources import OssEcosystemAdapter, fixture_transport
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.oss_ecosystem_records import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE
from src.kb.oss_ecosystem_store import OssEcosystemProjector
from tests.unit.oss_ecosystems import fixture_builder as fb

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config/source_packs/oss-ecosystems.json"
NS = "global"
NAMESPACE = {f"namespace:{NS}:read", f"namespace:{NS}:write"}
READ = {READ_SCOPE} | NAMESPACE
WRITE = READ | {WRITE_SCOPE}
REVIEW = WRITE | {REVIEW_SCOPE}
REGISTRY_SOURCES = ("pypi-json", "npm-registry", "crates-io", "maven-central")
ALL_SOURCES = (
    "spdx-license-list",
    *REGISTRY_SOURCES,
    "deps-dev-npm",
    "software-heritage",
)
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
PERSONAL_STRINGS = (
    "Ada Example",
    "ada@example.invalid",
    "ada-example",
    "Grace Example",
)


def manifest() -> dict:
    return validate_source_pack(json.loads(PACK.read_text()))


def source(source_id: str) -> dict:
    return copy.deepcopy(
        next(s for s in manifest()["sources"] if s["source_id"] == source_id)
    )


def fetch(
    source_id: str, poll: int, *, parameters: dict | None = None
) -> tuple[list[dict], dict]:
    item = source(source_id)
    adapter = OssEcosystemAdapter(
        item, transport=fixture_transport(fb.pages(source_id, poll))
    )
    records, cursor = [], None
    for _ in range(50):
        page = adapter.fetch_page(
            {
                "operation": item["operations"][0],
                "parameters": parameters or {},
                "limit": 5000,
            },
            cursor=cursor,
        )
        records.extend(page.records)
        cursor = page.next_cursor
        if cursor is None:
            break
    return records, item


def ingest(
    conn, poll: int, sources=ALL_SOURCES, *, observed_at_ms: int | None = None
) -> list[dict]:
    """Project one poll of every named source, observed at the poll's time."""

    projector = OssEcosystemProjector(conn)
    observed = observed_at_ms or fb.POLL_MS[poll]
    results = []
    for source_id in sources:
        records, item = fetch(source_id, poll)
        documents = [
            {
                "document_id": f"doc:{r['id']}:{poll}",
                "ingested_at": observed,
                "metadata": {"source_pack_record_id": r["id"]},
            }
            for r in records
        ]
        results += projector.project_page(
            run_id=f"run:{source_id}:{poll}",
            manifest=None,
            source=item,
            records=records,
            documents=documents,
            page_receipt={},
            principal_id="operator",
        )
    return results


def world(polls=(1, 2)) -> duckdb.DuckDBPyConnection:
    conn = duckdb.connect()
    for poll in polls:
        ingest(conn, poll)
    return conn


def install_pack(conn):
    from src.ingestion.source_pack_runtime import SourcePackRuntime

    value = manifest()
    SourcePackStore(conn).install(value, principal_id="operator", enable=True, now_ms=1)
    runtime = SourcePackRuntime(conn, sleep=lambda _s: None)
    for item in value["sources"]:
        runtime.accept_license(
            value["pack_id"], item["source_id"], principal_id="operator"
        )
    return runtime


def run_fixture_pack(conn, key: str, source_ids=ALL_SOURCES) -> dict:
    runtime = install_pack(conn)
    adapters = runtime.fixture_adapters("oss-ecosystems", ROOT)
    return runtime.run(
        {
            "pack_id": "oss-ecosystems",
            "run_key": key,
            "operation": "acquire",
            "source_ids": list(source_ids),
            "max_results": 5000,
            "max_bytes": 50_000_000,
            "timeout_ms": 120_000,
        },
        principal_id="operator",
        adapters=adapters,
        dns_resolver=PUBLIC_DNS,
        secret_resolver=lambda _ref: None,
    )


def all_statements(conn) -> str:
    return "\n".join(
        r[0]
        for r in conn.execute("SELECT statement_json FROM oss_revisions").fetchall()
    )
