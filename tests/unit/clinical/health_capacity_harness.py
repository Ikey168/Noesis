"""Offline health-capacity harness: the real source-pack runtime, health-capacity connector and stores over fixtures.

Provider responses come from the ``clinical-evidence`` source pack's authored
health-capacity fixtures (``tests/fixtures/source_packs/health-capacity-*.json``,
built by :mod:`tests.unit.health_capacity_fixture_builder`) through an injected
transport, so the runtime, the connector (with the SDMX connector), the
surveillance projector and store and the capacity views all run for real while
nothing leaves the process. Page receipts say ``execution: fixture``; this is
never live coverage.
"""

from __future__ import annotations

import json
from typing import Any

from tests.unit import health_capacity_fixture_builder as fb
from tests.unit.clinical import surveillance_harness as sh

ROOT = sh.ROOT
PACK = sh.PACK
NS = sh.NS
SCOPES = set(sh.SCOPES)
READ_ONLY = set(sh.READ_ONLY)
SOURCES = dict(fb.SOURCE_IDS)
GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write"}


class Web(sh.Web):
    """Every pinned native page of the pack (surveillance and health capacity), swappable per test."""

    def __init__(self) -> None:
        super().__init__()
        for path in sorted((ROOT / "tests/fixtures/source_packs").glob("health-capacity-*.json")):
            for page in json.loads(path.read_text())["native_pages"]:
                self.pages[page["request"]] = dict(page)


class Env(sh.Env):
    def __init__(self, path: str | None = None) -> None:
        super().__init__(path)
        self.web = Web()
        self.version = [0, 1, 3]

    def acquire(self, run_key: str, keys=None) -> dict[str, Any]:
        from src.ingestion.surveillance_sources import FIXTURE_SECRET

        manifest, _ = self.runtime._manifest("clinical-evidence")
        wanted = {SOURCES[k] for k in (keys or SOURCES)}
        selected = [s for s in manifest["sources"] if s["source_id"] in wanted]
        adapters = {
            s["source_id"]: self.runtime.factory.compile(s, transport=self.web.transport, secret=FIXTURE_SECRET)
            for s in selected
        }
        receipt = self.runtime.run(
            {"pack_id": "clinical-evidence", "run_key": run_key, "operation": "records",
             "source_ids": sorted(wanted), "max_pages": 50, "max_results": 1000, "mode": "backfill",
             "backfill": {"from_ms": 0}},
            principal_id="operator",
            adapters=adapters,
            secret_resolver=lambda _ref: FIXTURE_SECRET,
            dns_resolver=lambda _host: ["8.8.8.8"],
        )
        self._align_store_clock()
        return receipt

    # ------------------------------------------------------------------ later releases (tests only, not pinned)

    def gho_revision(self, code: str = "WHS6_102", *, rows=None, last_modified="Mon, 15 Sep 2098 08:00:00 GMT"):
        """GHO republishes the indicator with changed values (a new vintage on re-acquisition)."""
        rows = rows or [dict(r, NumericValue=79.9) if (r["SpatialDim"], r["TimeDim"]) == ("DEU", 2097) else r
                        for r in fb.GHO_INDICATORS[code]["rows"]]
        for page in fb.gho_pages(code, rows=rows, last_modified=last_modified):
            self.web.pages[page["request"]] = page

    def eurostat_update(self, dataset: str = "hlth_rs_bds1", stamp: str = "20/09/98 11:00:00") -> None:
        page = fb.eurostat_page(dataset, stamp)
        self.web.pages[page["request"]] = page

    def upgrade(self, source_key: str, documents: list[dict[str, Any]], pages: list[dict[str, Any]]) -> None:
        """The operator pins changed documents (a source-pack upgrade: a new dataflow version or a new definition
        edition); the matching native pages are served."""
        from src.ingestion.source_packs import SourcePackStore, validate_source_pack

        self.version[2] += 1
        manifest = json.loads(PACK.read_text())
        manifest["version"] = ".".join(str(p) for p in self.version)
        source = next(s for s in manifest["sources"] if s["source_id"] == SOURCES[source_key])
        source["surveillance"]["documents"] = documents
        SourcePackStore(self.conn).install(validate_source_pack(manifest), principal_id="operator", enable=True,
                                           now_ms=2)
        # The installed manifest now lacks the other upgrades; carry every source's accepted licence forward.
        for src in manifest["sources"]:
            self.runtime.accept_license("clinical-evidence", src["source_id"], principal_id="operator")
        for page in pages:
            self.web.pages[page["request"]] = page

    def oecd_version(self, version: str = "1.1") -> None:
        documents = [fb.oecd_document("beds", version), fb.oecd_document("workforce")]
        self.upgrade("oecd", documents, [fb.oecd_page("beds", version)])

    def gho_definition_edition(self, code: str = "WHS6_102", *, valid_from: str = "2097-01-01") -> None:
        """GHO publishes a new metadata edition of an indicator's definition from ``valid_from``."""
        spec = fb.GHO_INDICATORS[code]
        definitions = [
            {"key": code, "condition": code, "version": "GHO IMR 2090", "valid_from": "2090",
             "valid_to": "2096-12-31", "text": spec["definition"],
             "locator": f"{fb.GHO_METADATA}/imr-details/{code}"},
            {"key": code, "condition": code, "version": "GHO IMR 2098", "valid_from": valid_from,
             "text": spec["definition"].replace("rehabilitation centres", "rehabilitation centres, excluding "
                                                                          "day-care places"),
             "locator": f"{fb.GHO_METADATA}/imr-details/{code}"},
        ]
        documents = [fb.gho_document(c, definitions=definitions if c == code else None) for c in fb.GHO_INDICATORS]
        self.upgrade("gho", documents, fb.gho_pages(code, last_modified="Mon, 20 Oct 2098 08:00:00 GMT"))

    # ------------------------------------------------------------------ views

    def capacity(self):
        from src.kb.health_capacity import HealthCapacityStore

        return HealthCapacityStore(self.conn, now=self.clock)

    def indicator(self, provider: str, source_code: str, place: str) -> dict[str, Any]:
        found = [i for i in self.capacity().indicators(NS, scopes=SCOPES, provider=provider)
                 if i["source_code"] == source_code and i["place"]["code"] == place]
        assert len(found) == 1, [(i["provider"], i["source_code"], i["place"]) for i in found]
        return found[0]


def register_places(conn) -> dict[str, str]:
    """Germany and France as Geospatial places carrying their ISO 3166-1 codes (and Germany's NUTS code)."""
    from src.kb.geospatial import GeospatialStore

    store = GeospatialStore(conn)
    ids = {}
    for name, iso3, iso2 in (("Germany", "DEU", "DE"), ("France", "FRA", "FR")):
        place = store.register_place(
            "global", f"{name} (capacity fixture)", "country",
            names=[{"value": name, "language": "en", "kind": "canonical"}],
            source_ids={"iso3166-1-alpha3": iso3, "iso3166-1-alpha2": iso2, "nuts": iso2},
            parent_ids=[], principal_id="operator", scopes=GEO_SCOPES,
            place_key=f"capacity-fixture:{iso3}",
        )
        ids[iso3] = place["place_id"]
    return ids
