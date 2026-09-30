"""Offline Humanitarian harness: the real source-pack runtime, adapters and stores over authored fixtures.

The ``humanitarian-response`` source pack is installed and run with its
pinned native-page fixtures; the ``humanitarian`` connector and projector
populate :mod:`src.kb.humanitarian_store`. ``revised`` swaps in the authored
re-publication (a corrected report, a new report and a revised HDX resource).
Fixture provenance is authored and never live coverage.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb

from src.ingestion.humanitarian_sources import FIXTURE_SECRET, HumanitarianAdapter, fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from tests.unit.humanitarian import fixture_builder

ROOT = Path(__file__).resolve().parents[3]
NS = "humanitarian"
PACK_ID = "humanitarian-response"
PACK = ROOT / "config/source_packs/humanitarian.json"
BOUNDARIES = ROOT / "tests/fixtures/humanitarian/cod_ab_sdn_adm1.geojson"
SCOPES = {
    "knowledge:humanitarian:read", "knowledge:humanitarian:write", "knowledge:ingestion:execute",
    f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write",
}
REVIEWER_SCOPES = SCOPES | {"knowledge:humanitarian:review"}
OPERATIONS = {"reliefweb-reports-sdn": "reports", "reliefweb-disasters-sdn": "disasters", "hdx-sdn-datasets": "datasets",
              "ucdp-candidate-sdn": "events", "ucdp-ged-sdn": "events", "acled-events": "events"}
ACQUIRED = ("reliefweb-disasters-sdn", "reliefweb-reports-sdn", "hdx-sdn-datasets", "ucdp-candidate-sdn", "ucdp-ged-sdn")
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub


def ms(value):
    text = value if "T" in value else value + "T00:00:00+00:00"
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def pack_manifest():
    return validate_source_pack(json.loads(PACK.read_text()))


class World:
    def __init__(self, conn=None, *, now_iso="2099-07-01T00:00:00+00:00"):
        self.conn = conn or duckdb.connect()
        self.clock = ms(now_iso)
        self.runs = 0

    def now(self):
        return self.clock

    def _runtime_clock(self):
        self.clock += 1
        return self.clock

    def runtime(self):
        return SourcePackRuntime(self.conn, now=self._runtime_clock, sleep=lambda _d: None)

    def install(self):
        manifest = pack_manifest()
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for source in manifest["sources"]:
            runtime.accept_license(PACK_ID, source["source_id"], principal_id="operator")
        return manifest

    def run(self, source_id, *, revised=False, key=None):
        runtime = self.runtime()
        if revised:
            source = next(s for s in runtime._manifest(PACK_ID)[0]["sources"] if s["source_id"] == source_id)
            adapter = HumanitarianAdapter(source, transport=fixture_transport(
                fixture_builder.native_pages(source_id, revised=True)), secret=FIXTURE_SECRET)
        else:
            adapter = runtime.fixture_adapters(PACK_ID, ROOT)[source_id]
        self.runs += 1
        return runtime.run({"pack_id": PACK_ID, "run_key": key or f"{source_id}-{self.runs}",
                            "operation": OPERATIONS[source_id], "source_ids": [source_id], "max_results": 5000,
                            "max_bytes": 50_000_000, "timeout_ms": 120_000},
                           principal_id="operator", adapters={source_id: adapter}, dns_resolver=PUBLIC_DNS,
                           secret_resolver=lambda _ref: FIXTURE_SECRET)

    def acquire_all(self):
        return [self.run(source_id) for source_id in ACQUIRED]

    def boundaries(self):
        from src.kb.humanitarian_identity import HumanitarianIdentity

        return HumanitarianIdentity(self.conn).import_admin_boundaries(
            NS, json.loads(BOUNDARIES.read_text()), vintage="COD-AB SDN fixture 2098-03-01",
            source_url="https://data.humdata.org/dataset/fixture-cod-ab-sdn", principal_id="alice", scopes=SCOPES)


def world(*, boundaries=True):
    value = World()
    value.install()
    value.acquire_all()
    if boundaries:
        value.boundaries()
    return value
