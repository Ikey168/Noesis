"""Offline harness for the Geospatial real-estate feature (#2228): authored fixtures replayed through the runtime.

The ``geospatial-real-estate`` source pack
(``packs/geospatial/source_packs/geospatial-real-estate.json``) is installed and
every source runs through :class:`SourcePackRuntime` with its pinned fixture:
the native ``real-estate`` adapter for PPD, UK HPI, DVF and Eurostat, and the
existing WFS adapter for the INSPIRE parcels. Later releases (a PPD change file
with C and D rows, a UK HPI release, a DVF release, a Eurostat vintage and a
changed parcel geometry) go through the same adapters with authored transports.
Nothing here is live coverage; values are fictional.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from src.ingestion.real_estate_sources import fixture_transport
from src.ingestion.source_pack_runtime import SourcePackRuntime
from src.ingestion.source_packs import SourcePackStore, validate_source_pack
from src.ingestion.wfs_api import fixture_transport as wfs_transport
from src.kb.geospatial import GeospatialStore
from src.kb.real_estate import RealEstateStore
from tests.unit.real_estate import fixture_builder as fb

ROOT = Path(__file__).resolve().parents[3]
PACK_ID = "geospatial-real-estate"
NS = "global"
READ, WRITE, REVIEW = "knowledge:housing:read", "knowledge:housing:write", "knowledge:housing:review"
SCOPES = {READ, WRITE, f"namespace:{NS}:read", f"namespace:{NS}:write", "knowledge:geospatial:read",
          "knowledge:geospatial:write", "knowledge:geospatial:calculate", "knowledge:subscriptions:read",
          "knowledge:subscriptions:write", "knowledge:legal:read", "namespace:legal:read"}
REVIEW_SCOPES = SCOPES | {REVIEW}
NATIVE = ["hmlr-price-paid-data", "hmlr-uk-hpi", "dvf-geolocalisees", "eurostat-house-price-index"]
PARCELS = ["inspire-cp-france", "inspire-cp-nordrhein-westfalen"]
PUBLIC_DNS = lambda _host: ["8.8.8.8"]  # noqa: E731 - resolver stub
OWNER_MARKERS = ("proprietaire", "NOM FICTIF")


def manifest() -> dict[str, Any]:
    return validate_source_pack(json.loads(fb.PACK.read_text()))


def source(source_id: str) -> dict[str, Any]:
    return next(s for s in manifest()["sources"] if s["source_id"] == source_id)


def wgs84(point: tuple[float, float], srs: str = fb.FR_SRS) -> list[float]:
    from src.integrations.spatial import transform_geometry

    result = transform_geometry({"type": "Point", "coordinates": list(point)}, srs)
    return list(result["result"]["geometry"]["coordinates"])


class Env:
    def __init__(self, conn=None, start_ms: int = 4_080_000_000_000) -> None:
        self.conn = conn or duckdb.connect(":memory:")
        self.clock = start_ms  # 2099-04-14
        self.value = manifest()
        SourcePackStore(self.conn).install(self.value, principal_id="operator", enable=True, now_ms=1)
        runtime = self.runtime()
        for item in self.value["sources"]:
            runtime.accept_license(PACK_ID, item["source_id"], principal_id="operator")

    def now(self) -> int:
        self.clock += 1_000
        return self.clock

    def runtime(self) -> SourcePackRuntime:
        return SourcePackRuntime(self.conn, now=self.now, sleep=lambda _d: None)

    def run(self, source_ids: list[str], key: str, *, adapters: dict[str, Any] | None = None) -> dict[str, Any]:
        runtime = self.runtime()
        fixtures = runtime.fixture_adapters(PACK_ID, ROOT)
        selected = {s: (adapters or {}).get(s) or fixtures[s] for s in source_ids}
        operation = "features" if source_ids[0] in PARCELS else "publications"
        result = runtime.run(
            {"pack_id": PACK_ID, "run_key": key, "operation": operation, "source_ids": list(source_ids),
             "max_results": 5000, "max_bytes": 50_000_000, "timeout_ms": 120_000},
            principal_id="operator", adapters=selected, dns_resolver=PUBLIC_DNS, secret_resolver=lambda _r: None)
        return result

    def loaded(self) -> Env:
        for source_ids, key in ((NATIVE, "real-estate-tables"), (PARCELS, "real-estate-parcels")):
            result = self.run(source_ids, key)
            assert result["status"] == "complete", result
        return self

    # ------------------------------------------------------------ later releases

    def native(self, item: dict[str, Any], pages: list[dict[str, Any]]):
        return self.runtime().factory.compile(item, transport=fixture_transport(pages))

    def ppd_release_2(self, key: str = "ppd-2") -> dict[str, Any]:
        adapter = self.native(source("hmlr-price-paid-data"), fb.ppd_pages(2))
        return self.run(["hmlr-price-paid-data"], key, adapters={"hmlr-price-paid-data": adapter})

    def declare(self, source_id: str, documents: list[dict[str, Any]]) -> dict[str, Any]:
        """Install the next pack version declaring a later release document (as an operator upgrade would)."""
        installed = self.runtime()._manifest(PACK_ID)[0]
        major, minor, patch = (int(p) for p in installed["version"].split("."))
        raw = json.loads(fb.PACK.read_text())
        raw["version"] = f"{major}.{minor}.{patch + 1}"
        for item in raw["sources"]:
            previous = next(s for s in installed["sources"] if s["source_id"] == item["source_id"])
            item["real_estate"]["documents"] = (documents if item["source_id"] == source_id
                                                else previous["real_estate"].get("documents"))
            if item["real_estate"]["documents"] is None:
                item["real_estate"].pop("documents")
        from src.ingestion.source_pack_upgrades import SourcePackUpgradeStore
        from src.ingestion.source_packs import SourcePackConformance

        conformance = SourcePackConformance(ROOT).offline(validate_source_pack(raw))
        hashes = {item["source_id"]: item["output_hash"] for item in conformance["sources"]}
        for item in raw["sources"]:
            item["fixture"]["expected_output_hash"] = hashes[item["source_id"]]
        upgrades = SourcePackUpgradeStore(self.conn, initialize=True, now=self.now, root=ROOT)
        impact = upgrades.preview_impact(raw, principal_id="operator", scopes={"operator"})
        upgrades.apply(raw, preview_hash=impact["preview"]["preview_hash"], impact_hash=impact["impact_hash"],
                       apply_key=f"real-estate-{raw['version']}", principal_id="operator", scopes={"operator"},
                       accepted_license_sources=[], dns_resolver=PUBLIC_DNS, secret_available=lambda _ref: False)
        runtime = self.runtime()
        for item in raw["sources"]:
            # A new pack generation starts every cursor afresh (audited).
            runtime.release_stale_checkpoint(PACK_ID, item["source_id"], principal_id="operator")
        return next(s for s in validate_source_pack(raw)["sources"] if s["source_id"] == source_id)

    def ukhpi_release(self, release: str = "2099-04", key: str = "ukhpi-2") -> dict[str, Any]:
        self.declare("hmlr-uk-hpi", [fb.ukhpi_document(release)])
        return self.run(["hmlr-uk-hpi"], key)

    def dvf_release(self, release: str = "2099-10", key: str = "dvf-2") -> dict[str, Any]:
        self.declare("dvf-geolocalisees", [fb.dvf_document(release)])
        return self.run(["dvf-geolocalisees"], key)

    def eurostat_vintage(self, key: str = "eurostat-2") -> dict[str, Any]:
        adapter = self.native(source("eurostat-house-price-index"), fb.eurostat_pages(2))
        return self.run(["eurostat-house-price-index"], key, adapters={"eurostat-house-price-index": adapter})

    def parcel_revision(self, key: str = "parcels-2") -> dict[str, Any]:
        adapter = self.runtime().factory.compile(source("inspire-cp-france"), transport=wfs_transport(fb.fr_pages(2)))
        return self.run(["inspire-cp-france"], key, adapters={"inspire-cp-france": adapter})

    # ------------------------------------------------------------ places

    def place(self, name: str, kind: str, source_ids: dict[str, str], geometry: dict | None = None) -> dict:
        return GeospatialStore(self.conn).register_place(
            NS, name, kind, names=[{"value": name, "language": "und", "kind": "canonical"}], source_ids=source_ids,
            parent_ids=[], principal_id="alice", scopes={"knowledge:geospatial:write", "knowledge:geospatial:read"},
            geometry=geometry)

    def store(self) -> RealEstateStore:
        return RealEstateStore(self.conn, initialize=False)


def owner_markers(value: Any) -> list[str]:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return [m for m in OWNER_MARKERS if m in text]


PARIS_4E_BOX = {"type": "Polygon", "coordinates": [[[2.34, 48.84], [2.37, 48.84], [2.37, 48.86], [2.34, 48.86],
                                                    [2.34, 48.84]]]}


def seed_places(env: Env) -> dict[str, str]:
    """Fictional places: a postcode district (with its borough's GSS code), a PPD address, a synthetic point that
    lies inside parcel AB0013 (to exercise geometry-derived candidates), Paris 4e and Germany."""
    district = env.place("ZZ1 (fictional district)", "postcode-district",
                         {"uk-postcode-district": "ZZ1", "ons-gss": "E09000033"})
    address = env.place("Flat 3, 12 Example Street", "address", {"uk-address": "FLAT 3 12 EXAMPLE STREET ZZ1 1AA"})
    point = env.place("ZZ1 2BB (synthetic point)", "postcode", {"uk-postcode": "ZZ1 2BB"},
                      geometry={"type": "Point", "coordinates": wgs84((452655.0, 5410915.0))})
    paris = env.place("Paris 4e Arrondissement", "commune", {"insee-commune": "75104", "eurostat-geo": "FR"},
                      geometry=PARIS_4E_BOX)
    germany = env.place("Deutschland", "country", {"eurostat-geo": "DE"})
    return {"district": district["place_id"], "address": address["place_id"], "point": point["place_id"],
            "paris": paris["place_id"], "germany": germany["place_id"]}


def load_legal_work(conn) -> dict:
    """One fictional regional work with a gazette reference a parcel publication can cite."""
    from src.kb.legal import REGIONAL_CONTRACT, LegalStore

    record = {"contract": REGIONAL_CONTRACT, "provider": "berlin-law", "provider_id": "jlr-FlurstVBE2099",
              "kind": "normative", "language": "de", "title": "Verordnung über Flurstücksangaben (fiktiv)",
              "fields": {"gazette_reference": "GVBl. 2099 S. 777", "enactment_date": "2099-01-15"}}
    return LegalStore(conn).project("legal", [record], run_id="legal-fixture", source_id="legal-fixture")
