"""Offline Climate and Environment harness: real adapters and stores, authored fixtures.

Two acquisition paths run for real with no network:

* **source-pack runtime** — the ``climate-environment`` pack and the
  ``geospatial-berlin`` 1.1.0/1.2.0 packs are installed and run with their
  pinned native-page fixtures (``tests/fixtures/source_packs``); the
  ``environment`` connector and projector populate the stores;
* **DurableHTTP** — :class:`EnvironmentClient` requests are served from the
  raw authored files in ``tests/fixtures/environment`` through an injected
  transport (receipts say ``execution: injected``).

Fixture provenance is authored (see the fixtures README) and is never live
coverage.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import duckdb

from src.ingestion.environment_providers import FIXTURE_SECRET, PROVIDER_HOSTS, SECRETS, EnvironmentClient, acquire
from src.ingestion.provider_execution import DurableHTTP
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.kb.environment_store import EnvironmentEvidenceStore
from src.kb.geospatial import GeospatialStore
from tests.unit.environment import fixture_builder

ROOT = Path(__file__).resolve().parents[3]
NS = "environment"
NOTICE = "Authored offline fixture; not a live capture"
PACK = ROOT / "packs/climate-environment/source_packs/climate-environment.json"
UPGRADE = ROOT / "packs/climate-environment/source_packs/geospatial-berlin-1.2.0.json"
GEOSPATIAL = ROOT / "config/source_packs/geospatial.json"
SCOPES = {
    "knowledge:environment:read", "knowledge:environment:write", "knowledge:ingestion:execute",
    f"namespace:{NS}:read", f"namespace:{NS}:write",
    "knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate",
    "knowledge:subscriptions:read", "knowledge:subscriptions:write",
}
REVIEWER_SCOPES = SCOPES | {"knowledge:environment:review"}
ALEXANDERPLATZ = [13.4132, 52.5219]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub


def ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def pack_manifest():
    return validate_source_pack(json.loads(PACK.read_text()))


def selections():
    return {s["environment"]["provider"]: s["environment"]["selection"] for s in json.loads(PACK.read_text())["sources"]}


class FixtureWeb:
    """Route DurableHTTP requests to authored raw files; ``overrides`` swaps a file (re-publication, outage)."""

    def __init__(self):
        self.overrides: dict[str, str] = {}
        self.failures: dict[str, int] = {}
        self.calls: list[dict] = []

    def transport(self, *, method, url, params, body, headers, timeout_s, max_bytes):
        del method, body, headers, timeout_s, max_bytes
        from urllib.parse import urlsplit

        host = urlsplit(url).hostname
        provider = next(p for p, hosts in PROVIDER_HOSTS.items() if host in hosts)
        self.calls.append({"provider": provider, "url": url, "params": {k: v for k, v in params.items() if k != "securityToken"}})
        if provider in self.failures:
            return {"status": self.failures[provider], "headers": {}, "content": b"unavailable"}
        public = {k: v for k, v in params.items() if k != "securityToken"}
        content, kind = fixture_builder.route(provider, {"url": url, "params": public}, self.overrides)
        return {"status": 200, "headers": {"Content-Type": kind}, "content": content}


class Env:
    def __init__(self, now_iso="2026-09-26T09:00:00+00:00", conn=None):
        self.conn = conn or duckdb.connect()
        self.web = FixtureWeb()
        self.clock = ms(now_iso)
        self.store = EnvironmentEvidenceStore(self.conn, now=self.now)
        self.budgets = 0

    def now(self):
        return self.clock

    def tick(self, seconds=60):
        self.clock += seconds * 1000

    def client(self, provider):
        self.budgets += 1
        http = DurableHTTP(self.conn, budget_id=f"env-{provider}-{self.budgets}", provider=provider, principal_id="alice",
                           allowed_hosts=PROVIDER_HOSTS[provider], reuse_notice=NOTICE, transport=self.web.transport,
                           now=self.now, max_requests=40)
        return EnvironmentClient(http, principal_id="alice", secret=FIXTURE_SECRET if provider in SECRETS else None)

    def acquire(self, provider, selection=None, *, observation=None, namespace=NS, scopes=SCOPES):
        client = self.client(provider)
        self.tick()
        return acquire(client, selection or selections()[provider], namespace=namespace, scopes=scopes,
                       reuse_notice=NOTICE, observation=observation or f"obs-{provider}-{self.clock}",
                       principal_id="alice", store=self.store)

    def acquire_all(self, namespace=NS, scopes=SCOPES):
        return [self.acquire(p, namespace=namespace, scopes=scopes) for p in selections()]

    # --------------------------------------------------------- geospatial side

    def runtime(self):
        return SourcePackRuntime(self.conn, now=self._runtime_clock, sleep=lambda _d: None)

    def _runtime_clock(self):
        self.clock += 1
        return self.clock

    def install_berlin(self, *, run=True):
        manifest = validate_source_pack(json.loads(GEOSPATIAL.read_text()))
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for source in manifest["sources"]:
            runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id="operator")
        if run:
            self.run_sources("geospatial-berlin", ["berlin-bezirksgrenzen"], "berlin-districts", operation="features")
        return manifest

    def run_sources(self, pack_id, source_ids, key, *, operation):
        runtime = self.runtime()
        adapters = runtime.fixture_adapters(pack_id, ROOT)
        return runtime.run({"pack_id": pack_id, "run_key": key, "operation": operation, "source_ids": source_ids,
                            "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
                           principal_id="operator", adapters={s: adapters[s] for s in source_ids},
                           dns_resolver=PUBLIC_DNS, secret_resolver=lambda _ref: FIXTURE_SECRET)

    def upgrade_umweltatlas(self, *, run=True):
        from src.ingestion.source_pack_upgrades import SourcePackUpgradeStore

        candidate = json.loads(UPGRADE.read_text())
        upgrades = SourcePackUpgradeStore(self.conn, initialize=True, now=self.now, root=ROOT)
        impact = upgrades.preview_impact(candidate, principal_id="operator", scopes={"operator"})
        new_sources = [s["source_id"] for s in impact["preview"]["changes"]["sources"]["added"]]
        receipt = upgrades.apply(candidate, preview_hash=impact["preview"]["preview_hash"],
                                 impact_hash=impact["impact_hash"], apply_key="umweltatlas-1.2.0",
                                 principal_id="operator", scopes={"operator"},
                                 accepted_license_sources=new_sources, dns_resolver=PUBLIC_DNS)
        run_receipt = self.run_sources("geospatial-berlin", new_sources, "umweltatlas", operation="features") if run else None
        return impact, receipt, run_receipt

    def install_environment_pack(self):
        manifest = pack_manifest()
        SourcePackStore(self.conn).install(manifest, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for source in manifest["sources"]:
            runtime.accept_license(manifest["pack_id"], source["source_id"], principal_id="operator")
        return manifest

    def place(self, name="Alexanderplatz", coordinates=ALEXANDERPLATZ, namespace=NS):
        return GeospatialStore(self.conn).register_place(
            namespace, name, "square", names=[{"value": name, "language": "de", "kind": "canonical"}],
            source_ids={"fixture": name.casefold()}, parent_ids=[], principal_id="alice",
            scopes={"knowledge:geospatial:write", "knowledge:geospatial:read"},
            geometry={"type": "Point", "coordinates": list(coordinates)})


def world(conn=None, *, umweltatlas=True, acquire=True):
    """Berlin districts (+ Umweltatlas upgrade), every provider acquired, and the Alexanderplatz place."""

    env = Env(conn=conn)
    env.install_berlin()
    if umweltatlas:
        env.upgrade_umweltatlas()
    if acquire:
        results = env.acquire_all()
        assert all(r["ok"] for r in results), [r.get("failure") for r in results if not r["ok"]]
    env.alexanderplatz = env.place()
    return env
