"""Offline harness for the Geospatial housing feature (#1912): authored fixtures replayed through the runtime.

``geospatial-berlin`` 1.1.0 (``config/source_packs/geospatial.json``) is
installed, the ALKIS districts are replayed, and the pack is upgraded to 1.3.0
(``packs/geospatial/source_packs/geospatial-berlin-1.3.0.json``). Every housing
source then runs through :class:`SourcePackRuntime` with its pinned fixture, the
real WFS or housing adapter and :class:`src.kb.housing.HousingProjector`.
Later snapshots (a new Stichtag, a changed plan stage, a new rent-index edition,
revised statistics) go through the same adapters with authored transports.
Nothing here is live coverage; all values are fictional.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.housing_sources import FIXTURE_SECRET, fixture_request
from src.ingestion.housing_sources import fixture_transport as housing_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.wfs_api import fixture_transport as wfs_transport
from src.kb.geospatial import GeospatialStore
from src.kb.housing import HousingStore
from tests.unit import housing_fixture_builder as fb

ROOT = Path(__file__).resolve().parents[2]
BASE = ROOT / "config/source_packs/geospatial.json"
UPGRADE = fb.MANIFEST
PACK_ID = "geospatial-berlin"
NS = "global"
READ = "knowledge:housing:read"
WRITE = "knowledge:housing:write"
REVIEW = "knowledge:housing:review"
GEO = {
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:calculate",
}
SCOPES = {
    READ,
    WRITE,
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    *GEO,
    "knowledge:legal:read",
    "knowledge:political:dossier:read",
    "knowledge:read",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:transit:read",
    "namespace:legal:read",
    "namespace:research:read",
}
REVIEW_SCOPES = SCOPES | {REVIEW}
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
WFS_SOURCES = (
    "berlin-boris-bodenrichtwerte",
    "berlin-bebauungsplaene",
    "berlin-wohnlagen",
)
TABULAR_SOURCES = (
    "berlin-mietspiegel",
    "statistik-bb-bautaetigkeit",
    "destatis-genesis-bautaetigkeit",
)
HOUSING_SOURCES = WFS_SOURCES + TABULAR_SOURCES
BORIS_COLLECTION = "brw:bodenrichtwertzonen"
BPLAN_COLLECTION = "bplan:bplan_geltungsbereiche"
WOHNLAGEN_COLLECTION = "wohnlagen:wohnlagen_2099"
DISTRICTS = "alkis_bezirke:bezirksgrenzen"
# UTM 33N positions (authored): inside zone A and plan 1-99a; the corner shared by zones A and B; outside all zones.
ADDRESS_UTM = (391500.0, 5820500.0)
EDGE_UTM = (392000.0, 5821000.0)
OUTSIDE_UTM = (390200.0, 5820500.0)


def wgs84(point_utm: tuple[float, float]) -> list[float]:
    from src.integrations.spatial import transform_geometry

    result = transform_geometry(
        {"type": "Point", "coordinates": list(point_utm)}, "urn:ogc:def:crs:EPSG::25833"
    )
    return list(result["result"]["geometry"]["coordinates"])


def upgrade_manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(UPGRADE.read_text()))


def source(source_id: str) -> dict[str, Any]:
    return next(s for s in upgrade_manifest()["sources"] if s["source_id"] == source_id)


class Env:
    def __init__(self, conn=None, start_ms: int = 4_102_444_800_000) -> None:
        self.conn = conn or duckdb.connect(":memory:")
        self.clock = start_ms  # 2100-01-01T00:00:00Z

    def now(self) -> int:
        self.clock += 1
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def install(self) -> dict[str, Any]:
        manifest = validate_source_pack(json.loads(BASE.read_text()))
        SourcePackStore(self.conn).install(
            manifest, principal_id="operator", enable=True, now_ms=1
        )
        runtime = self.runtime()
        for item in manifest["sources"]:
            runtime.accept_license(PACK_ID, item["source_id"], principal_id="operator")
        return manifest

    def upgrade(self) -> tuple[dict[str, Any], dict[str, Any]]:
        from src.ingestion.source_pack_upgrades import SourcePackUpgradeStore

        candidate = json.loads(UPGRADE.read_text())
        upgrades = SourcePackUpgradeStore(
            self.conn, initialize=True, now=self.now, root=ROOT
        )
        impact = upgrades.preview_impact(
            candidate, principal_id="operator", scopes={"operator"}
        )
        added = [
            s["source_id"] for s in impact["preview"]["changes"]["sources"]["added"]
        ]
        receipt = upgrades.apply(
            candidate,
            preview_hash=impact["preview"]["preview_hash"],
            impact_hash=impact["impact_hash"],
            apply_key="housing-1.3.0",
            principal_id="operator",
            scopes={"operator"},
            accepted_license_sources=added,
            dns_resolver=PUBLIC_DNS,
            secret_available=lambda ref: ref == "NOESIS_DESTATIS_GENESIS_TOKEN",
        )
        return impact, receipt

    def run(
        self,
        source_ids,
        key: str,
        *,
        adapters: dict[str, Any] | None = None,
        operation: str | None = None,
    ) -> dict[str, Any]:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, ROOT)
        selected = {s: (adapters or {}).get(s) or fixtures[s] for s in source_ids}
        if operation is None:
            (operation,) = {
                source(s)["operations"][0] for s in source_ids if s in HOUSING_SOURCES
            } or {"features"}
        return runtime.run(
            {
                "pack_id": PACK_ID,
                "run_key": key,
                "operation": operation,
                "source_ids": list(source_ids),
                "max_results": 5000,
                "max_bytes": 50_000_000,
                "timeout_ms": 120_000,
            },
            principal_id="operator",
            adapters=selected,
            dns_resolver=PUBLIC_DNS,
            secret_resolver=lambda _ref: FIXTURE_SECRET,
        )

    def world(self) -> Env:
        """Districts, the 1.3.0 upgrade and every housing source replayed from its pinned fixture."""
        self.install()
        self.run(["berlin-bezirksgrenzen"], "districts")
        self.upgrade()
        self.run(list(WFS_SOURCES), "housing-wfs")
        self.run(list(TABULAR_SOURCES), "housing-tables")
        return self

    # ------------------------------------------------------------ later snapshots

    def wfs_adapter(
        self, source_id: str, features: list[dict[str, Any]], timestamp: str
    ):
        layer = {
            "berlin-boris-bodenrichtwerte": fb.BORIS,
            "berlin-bebauungsplaene": fb.BPLAN,
            "berlin-wohnlagen": fb.WOHNLAGEN,
        }[source_id]
        fixture = fb.wfs_fixture(layer, features, ["later-snapshot"], timestamp)
        return self.runtime().factory.compile(
            source(source_id), transport=wfs_transport(fixture["native_pages"])
        )

    def boris_2100(self, key: str = "boris-2100") -> dict[str, Any]:
        adapter = self.wfs_adapter(
            "berlin-boris-bodenrichtwerte",
            fb.boris_features("2100-01-01"),
            "2100-03-01T08:00:00Z",
        )
        return self.run(
            ["berlin-boris-bodenrichtwerte"],
            key,
            adapters={"berlin-boris-bodenrichtwerte": adapter},
        )

    def bplan_stage_change(self, key: str = "bplan-2") -> dict[str, Any]:
        adapter = self.wfs_adapter(
            "berlin-bebauungsplaene", fb.bplan_features(2), "2099-09-01T08:00:00Z"
        )
        return self.run(
            ["berlin-bebauungsplaene"],
            key,
            adapters={"berlin-bebauungsplaene": adapter},
        )

    def mietspiegel_2101(self) -> list[dict[str, Any]]:
        """A later edition, as a later manifest version would declare it: its adapter output is projected."""
        from src.kb.housing import HousingProjector

        document = fb.mietspiegel_document("2101")
        pages = [
            fb.page(fixture_request(document), fb.mietspiegel_csv("2101"), "text/csv")
        ]
        item = json.loads(json.dumps(source("berlin-mietspiegel")))
        item["housing"]["documents"] = [document]
        item.pop("source_hash")
        from src.ingestion.source_packs import _digest

        item["source_hash"] = _digest(item)
        adapter = self.runtime().factory.compile(
            item, transport=housing_transport(pages)
        )
        page = adapter.fetch_page(
            {"operation": "publications", "parameters": {}}, cursor=None
        )
        return HousingProjector(self.conn).project_page(
            run_id="mietspiegel-2101",
            manifest={},
            source=item,
            records=page.records,
            documents=[],
            page_receipt=page.receipt,
            principal_id="operator",
        )

    def statbb_revised(self, key: str = "statbb-revised") -> dict[str, Any]:
        """The same declared permit file republished with revised Mitte figures and a later Last-Modified."""
        documents = source("statistik-bb-bautaetigkeit")["housing"]["documents"]
        pages = [
            fb.page(
                fixture_request(d),
                fb.statbb_csv(d["statistic"], revised=d["statistic"] == "permits"),
                "text/csv",
                modified="2099-09-30" if d["statistic"] == "permits" else "2099-05-10",
            )
            for d in documents
        ]
        adapter = self.runtime().factory.compile(
            source("statistik-bb-bautaetigkeit"), transport=housing_transport(pages)
        )
        return self.run(
            ["statistik-bb-bautaetigkeit"],
            key,
            adapters={"statistik-bb-bautaetigkeit": adapter},
        )

    # ------------------------------------------------------------ places and context

    def address(
        self, name: str = "Musterstraße 1 (fiktiv)", point_utm=ADDRESS_UTM
    ) -> dict[str, Any]:
        return GeospatialStore(self.conn).register_place(
            NS,
            name,
            "address",
            names=[{"value": name, "language": "de", "kind": "canonical"}],
            source_ids={"fixture-address": name.casefold()},
            parent_ids=[],
            principal_id="alice",
            scopes={"knowledge:geospatial:write", "knowledge:geospatial:read"},
            geometry={"type": "Point", "coordinates": wgs84(point_utm)},
        )

    def store(self) -> HousingStore:
        return HousingStore(self.conn, initialize=False)


LEGAL_NS = "legal"
LEGAL_SCOPES = {"knowledge:legal:read", f"namespace:{LEGAL_NS}:read"}


def load_legal_works(conn) -> dict:
    """Fictional Berlin works: a Mietspiegel ordinance (GVBl. 2098 S. 42), the plan 1-99a ordinance
    (GVBl. 2099 S. 321) and an ordinance naming plan 1-98 whose gazette reference differs from the layer's."""
    from src.kb.legal import REGIONAL_CONTRACT, LegalStore

    def work(provider_id, title, gazette, enacted):
        return {
            "contract": REGIONAL_CONTRACT,
            "provider": "berlin-law",
            "provider_id": provider_id,
            "kind": "normative",
            "language": "de",
            "title": title,
            "fields": {"gazette_reference": gazette, "enactment_date": enacted},
        }

    records = [
        work(
            "jlr-MietSpVBE2098pP1",
            "Verordnung über den Berliner Mietspiegel (fiktiv)",
            "GVBl. 2098 S. 42",
            "2098-02-01",
        ),
        work(
            "jlr-BPlan1-99aVBE2099",
            "Verordnung über die Festsetzung des Bebauungsplans 1-99a im Bezirk Mitte "
            "(fiktiv)",
            "GVBl. 2099 S. 321",
            "2099-06-15",
        ),
        work(
            "jlr-BPlan1-98VBE2097",
            "Verordnung über die Festsetzung des Bebauungsplans 1-98 im Bezirk Mitte "
            "(fiktiv)",
            "GVBl. 2097 S. 56",
            "2097-02-01",
        ),
    ]
    return LegalStore(conn).project(
        LEGAL_NS, records, run_id="legal-fixture", source_id="berlin-law-fixture"
    )
