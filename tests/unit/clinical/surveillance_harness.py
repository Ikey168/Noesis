"""Offline surveillance harness: the real source-pack runtime, surveillance connector and stores over authored fixtures.

Provider responses come from the ``clinical-evidence`` source pack's authored
surveillance fixtures (``tests/fixtures/source_packs/surveillance-*.json``,
built by :mod:`tests.unit.surveillance_fixture_builder`) through an injected
transport, so the runtime, the connector (with the SDMX and GENESIS dataset
connectors), the projector and the surveillance store all run for real while
nothing leaves the process. Page receipts say ``execution: fixture``; this is
never live coverage.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import duckdb

from tests.unit import surveillance_fixture_builder as fb

ROOT = Path(__file__).resolve().parents[3]
PACK = ROOT / "config" / "source_packs" / "clinical-evidence.json"
NS = "clinical"
SCOPES = {
    "knowledge:clinical:read",
    "knowledge:clinical:write",
    "knowledge:clinical:review",
    "knowledge:ingestion:execute",
    f"namespace:{NS}:read",
    f"namespace:{NS}:write",
    "knowledge:schema:read",
    "knowledge:schema:register",
    "knowledge:subscriptions:read",
    "knowledge:subscriptions:write",
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:calculate",
}
READ_ONLY = {"knowledge:clinical:read", f"namespace:{NS}:read"}
SOURCES = {
    "rki": "rki-tuberkulose-meldedaten",
    "gho": "who-gho-tuberculosis",
    "eurostat": "eurostat-causes-of-death-tuberculosis",
    "destatis": "destatis-todesursachen-tuberkulose",
}
START_MS = 4_082_000_000_000  # 2099-05-11, after every fixture release


def ms(day: str) -> int:
    from src.ingestion.surveillance_sources import day_ms

    return day_ms(day)


class Web:
    """Request path+query -> native page; swap bodies to simulate new releases or outages."""

    def __init__(self) -> None:
        self.pages: dict[str, dict[str, Any]] = {}
        for path in sorted(
            (ROOT / "tests/fixtures/source_packs").glob("surveillance-*.json")
        ):
            for page in json.loads(path.read_text())["native_pages"]:
                self.pages[page["request"]] = dict(page)

    def set(
        self, request: str, body: str, *, status: int = 200, headers: dict | None = None
    ) -> None:
        self.pages[request] = {
            "request": request,
            "status": status,
            "headers": headers or {},
            "body": body,
        }

    def transport(self, **kwargs):
        from src.ingestion.surveillance_sources import fixture_transport

        return fixture_transport(list(self.pages.values()))(**kwargs)


class Clock:
    def __init__(self, start: int = START_MS) -> None:
        self.value = start

    def __call__(self) -> int:
        self.value += 1000
        return self.value


class Env:
    def __init__(self, path: str | None = None) -> None:
        from src.ingestion.source_pack_runtime import SourcePackRuntime
        from src.ingestion.source_packs import SourcePackStore, validate_source_pack

        self.conn = duckdb.connect(path) if path else duckdb.connect()
        self.clock = Clock()
        self.web = Web()
        self.manifest = validate_source_pack(json.loads(PACK.read_text()))
        SourcePackStore(self.conn).install(
            self.manifest, principal_id="operator", enable=True, now_ms=1
        )
        self.runtime = SourcePackRuntime(self.conn, now=self.clock)
        for source in self.manifest["sources"]:
            self.runtime.accept_license(
                self.manifest["pack_id"], source["source_id"], principal_id="operator"
            )

    def acquire(self, run_key: str, keys=None) -> dict[str, Any]:
        from src.ingestion.surveillance_sources import FIXTURE_SECRET

        manifest, _ = self.runtime._manifest("clinical-evidence")
        wanted = {SOURCES[k] for k in (keys or SOURCES)}
        selected = [s for s in manifest["sources"] if s["source_id"] in wanted]
        adapters = {
            s["source_id"]: self.runtime.factory.compile(
                s, transport=self.web.transport, secret=FIXTURE_SECRET
            )
            for s in selected
        }
        receipt = self.runtime.run(
            {
                "pack_id": "clinical-evidence",
                "run_key": run_key,
                "operation": "records",
                "source_ids": sorted(wanted),
                "max_pages": 50,
                "max_results": 1000,
                "mode": "backfill",
                "backfill": {"from_ms": 0},
            },
            principal_id="operator",
            adapters=adapters,
            secret_resolver=lambda _ref: FIXTURE_SECRET,
            dns_resolver=lambda _host: ["8.8.8.8"],
        )
        self._align_store_clock()
        return receipt

    def _align_store_clock(self) -> None:
        projector = self.runtime.projectors.get("noesis-surveillance-record-v1")
        if projector is not None:
            projector.store.now = self.clock

    def upgrade_rki(self, tag: str, version: str = "0.1.5") -> None:  # 0.1.3 capacity (#2215), 0.1.4 devices (#2654)
        """The operator pins the next RKI release tag (a source-pack upgrade); the new CSV is served."""
        from src.ingestion.source_packs import SourcePackStore, validate_source_pack

        manifest = json.loads(PACK.read_text())
        manifest["version"] = version
        source = next(
            s for s in manifest["sources"] if s["source_id"] == SOURCES["rki"]
        )
        source["surveillance"]["documents"] = [fb.rki_document(tag)]
        validated = validate_source_pack(manifest)
        SourcePackStore(self.conn).install(
            validated, principal_id="operator", enable=True, now_ms=2
        )
        self.runtime.accept_license(
            "clinical-evidence", SOURCES["rki"], principal_id="operator"
        )
        self.web.set(
            fb.rki_request(tag), fb.rki_csv(tag), headers={"Content-Type": "text/plain"}
        )

    def eurostat_update(self, stamp: str = "20/09/99 11:00:00") -> None:
        self.web.set(
            fb.eurostat_request(),
            fb.eurostat_csv(stamp),
            headers={"Content-Type": "text/csv"},
        )

    def import_ecdc(
        self, principal_id: str = "operator-anna", export: dict | None = None
    ) -> dict[str, Any]:
        from src.kb.surveillance import SurveillanceStore

        return SurveillanceStore(self.conn, now=self.clock).import_export(
            NS, export or fb.ecdc_export(), principal_id=principal_id, scopes=SCOPES
        )

    def load_all(self) -> dict[str, Any]:
        receipt = self.acquire("r1")
        self.import_ecdc()
        return receipt

    def store(self):
        from src.kb.surveillance import SurveillanceStore

        return SurveillanceStore(self.conn, now=self.clock)

    def series(self, **filters) -> list[dict[str, Any]]:
        return self.store().find_series(NS, **filters)

    def one(self, **filters) -> dict[str, Any]:
        found = self.series(**filters)
        assert len(found) == 1, [
            (s["provider"], s["geography"], s["kind"], s["dimensions"]) for s in found
        ]
        return found[0]


def rki_source(tag: str = "2099-01-20") -> dict[str, Any]:
    from src.ingestion.source_packs import validate_source_pack

    manifest = json.loads(PACK.read_text())
    source = copy.deepcopy(
        next(s for s in manifest["sources"] if s["source_id"] == SOURCES["rki"])
    )
    source["surveillance"]["documents"] = [fb.rki_document(tag)]
    manifest["sources"] = [source]
    return validate_source_pack(manifest)["sources"][0]


def source(key: str) -> dict[str, Any]:
    from src.ingestion.source_packs import validate_source_pack

    return next(
        s
        for s in validate_source_pack(json.loads(PACK.read_text()))["sources"]
        if s["source_id"] == SOURCES[key]
    )


def import_boundaries(conn) -> None:
    """Fictional boundaries: Germany, Bayern (AGS 09 / NUTS DE2), Berlin, and one Bavarian district (09184 missing)."""
    from src.kb.geospatial_features import GeospatialFeatureStore

    store = GeospatialFeatureStore(conn)
    square = [[[11.5, 48.1], [11.6, 48.1], [11.6, 48.2], [11.5, 48.2], [11.5, 48.1]]]
    geo = {"knowledge:geospatial:read", "knowledge:geospatial:write"}

    def collection(name, features, provider, title):
        store.import_feature_collection(
            "geo",
            json.dumps(
                {
                    "type": "FeatureCollection",
                    "features": [
                        {
                            "type": "Feature",
                            "id": fid,
                            "geometry": {"type": "Polygon", "coordinates": square},
                            "properties": props,
                        }
                        for fid, props in features
                    ],
                }
            ),
            provider=provider,
            collection=name,
            source_crs="EPSG:4326",
            title_property=title,
            principal_id="p",
            scopes=geo,
        )

    collection(
        "gisco:countries",
        [
            (
                "cntr.DE",
                {
                    "CNTR_ID": "DE",
                    "ISO3_CODE": "DEU",
                    "NAME": "Germany (fictional geometry)",
                },
            )
        ],
        "gisco",
        "NAME",
    )
    collection(
        "bkg:vg250:lan",
        [
            ("lan.09", {"AGS": "09", "NUTS": "DE2", "GEN": "Bayern"}),
            ("lan.11", {"AGS": "11", "NUTS": "DE3", "GEN": "Berlin"}),
        ],
        "bkg",
        "GEN",
    )
    collection(
        "bkg:vg250:krs",
        [("krs.09162", {"AGS": "09162", "GEN": "München (Stadt)"})],
        "bkg",
        "GEN",
    )


def raw(name: str) -> Any:
    return json.loads(
        (ROOT / "tests/fixtures/clinical/surveillance" / name).read_text()
    )


def align_terms(env: Env, principal_id: str = "alice") -> dict[str, Any]:
    """Publish the MeSH and ICD subsets, the ICD-MeSH crosswalk and the surveillance term crosswalks."""
    from src.kb.clinical_terms import ClinicalTerms

    terms = ClinicalTerms(env.conn, now=env.clock)
    mesh, icd = raw("mesh_tuberculosis.json"), raw("icd10_who_subset.json")
    terms.publish_mesh(
        mesh["descriptors"], mesh["version"], principal_id=principal_id, scopes=SCOPES
    )
    published = terms.publish_icd(
        icd["codes"],
        icd["version"],
        system=icd["system"],
        principal_id=principal_id,
        scopes=SCOPES,
        mesh_version=mesh["version"],
        curations=icd["mesh_curations"],
    )
    aligned = terms.align_surveillance(
        NS,
        principal_id=principal_id,
        scopes=SCOPES,
        mesh_version=mesh["version"],
        icd={"system": icd["system"], "version": icd["version"]},
        curations=raw("term_curations.json")["curations"],
    )
    return {"icd": published, "surveillance": aligned}


LAND_NUTS = {"nuts": {"collection": "bkg:vg250:lan", "property": "NUTS"}}


def resolve(env: Env, principal_id: str = "alice") -> dict[str, Any]:
    from src.kb.surveillance_places import SurveillancePlaces

    return SurveillancePlaces(env.conn, now=env.clock).resolve_geographies(
        NS,
        principal_id=principal_id,
        scopes=SCOPES,
        geo_namespace="geo",
        collections=LAND_NUTS,
    )


def places(env: Env):
    from src.kb.surveillance_places import SurveillancePlaces

    return SurveillancePlaces(env.conn, now=env.clock)


def seed_documents(env: Env) -> dict[str, str]:
    """Paper documents (fictional) that cite, mention or merely share a condition with the surveillance series."""
    from services.ingest.common.document_model import Document
    from src.ingestion.document_store import DocumentStore

    specs = {
        "rki-citing": {
            "title": "Tuberculosis notifications in Bavarian districts, 2098-2099 (fictional)",
            "content": "We describe notified tuberculosis cases.",
            "data_availability_statement": "Notification data are the RKI release "
            "robert-koch-institut/Fiktive_Tuberkulose-Meldedaten@2099-01-20 (doi:10.5281/zenodo.9900001).",
        },
        "eurostat-citing": {
            "title": "Tuberculosis mortality in Germany (fictional)",
            "content": "Deaths by cause.",
            "references": [
                {
                    "text": "Eurostat. Causes of death by NUTS 2 regions (hlth_cd_aro). "
                    "Luxembourg, 2099."
                },
                {"text": "Eurostat. Standardised death rates (hlth_cd_asdr2), 2099."},
                {"text": "Fictional archive, doi:10.5281/zenodo.9900077."},
            ],
        },
        "gho-mention": {
            "title": "Estimating tuberculosis burden (fictional)",
            "content": "Rates were compared with the WHO indicator NOE_TB_NOTIF_RATE.",
        },
        "condition-only": {
            "title": "Tuberculosis in Germany (fictional)",
            "content": "Tuberculosis notifications in Germany and Bayern declined.",
        },
    }
    store = DocumentStore(env.conn)
    payloads, ids = [], {}
    for key, spec in specs.items():
        metadata = {
            "content_representation": "plain-text-abstract",
            "doi": f"10.5555/sv-{key}",
        }
        if spec.get("data_availability_statement"):
            metadata["data_availability_statement"] = spec[
                "data_availability_statement"
            ]
        if spec.get("references"):
            metadata["references_json"] = json.dumps(spec["references"])
        document = Document(
            document_id=f"spdoc:sv:{key}",
            source_type="paper",
            source_id="europe-pmc",
            language="en",
            ingested_at=env.clock(),
            url=f"https://example.org/{key}",
            title=spec["title"],
            content=spec["content"],
            authors=["A. Fictional"],
            metadata=metadata,
        )
        payloads.append(document.to_dict())
        ids[key] = document.document_id
    outcome = store.upsert(payloads)
    assert not outcome.invalid, outcome.dead_letter
    return ids


def seed_trial(env: Env) -> str:
    """A registered trial (fictional) that declares the Eurostat dataset among its references."""
    from src.kb.clinical_records import ClinicalRecordStore, record

    trial = record(
        "registered-trial",
        registry="ctgov",
        identifier="NCT09900017",
        title="Fictional tuberculosis screening trial",
        source_url="https://clinicaltrials.gov/study/NCT09900017",
        native_version={
            "version": "1",
            "date": "2099-01-10",
            "basis": "registry-history",
        },
        status={"native": "RECRUITING", "normalized": "recruiting"},
        phase={"normalized": ["not-applicable"]},
        sponsor={"lead": "Fictional Sponsor"},
        secondary_identifiers=[],
        conditions=[{"term": "Tuberculosis"}],
        interventions=[],
        design={},
        registration={},
        arm_keys=[],
        outcome_keys=[],
        declared_references=[
            {
                "pmid": None,
                "doi": None,
                "type": "BACKGROUND",
                "citation": "Eurostat hlth_cd_aro, tuberculosis deaths (fictional citation).",
            }
        ],
        brief_summary="Background rates from NOE_TB_INC_EST are discussed.",
    )
    ClinicalRecordStore(env.conn).ingest(
        NS,
        "ctgov",
        [trial],
        observation_id="trial-seed",
        observed_at_ms=env.clock(),
        scopes=SCOPES,
    )
    return "NCT09900017"
