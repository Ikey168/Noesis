"""Infrastructure assets, status revisions, capacity revisions and owner assertions (CI02, #2363).

``noesis-infrastructure-asset-record-v1`` is the provider-neutral shape every
infrastructure adapter emits (:mod:`src.ingestion.infrastructure_sources`).
One record is **one published asset as one release or extract states it**:

* **Asset identity** is ``provider + dataset + native_id`` (a GPPD
  ``gppd_idnr``, a GEM unit/project id, an OSM ``type/id``, an EIA layer
  feature id, an ENTSOG ``pointKey``); the release is never part of it.
* **Asset classes** cover power plants, generating units, pipelines, LNG
  terminals, transmission lines, substations, gas interconnection points and
  mines.
* **Status** keeps the publisher's own vocabulary (``status.published``)
  beside the normalized one (proposed, construction, operating, mothballed,
  retired, cancelled, unknown), with the publisher's effective date and its
  basis. **Capacities** keep value text, unit, direction and effective date as
  published; publisher estimates are flagged ``estimate``.
* **Owner and operator assertions** keep the name and share exactly as
  published; they are never resolved here (see
  :mod:`src.kb.infrastructure_identity`).
* **Geometry** is kept with a receipt (published CRS, stored CRS, precision
  and its basis) and projected onto the existing spatial storage through
  :meth:`src.kb.geospatial.GeospatialStore.store_geometry`; there is no
  second spatial store.

:class:`InfrastructureStore` keeps every distinct published state of an asset
as a revision (sequence, release, ``revision_of``) and derives append-only
**status revisions** and **capacity revisions** whenever a publication changes
them; earlier states are never overwritten. Every value carries its source,
revision and as-of time (release date when stated, else retrieval time,
labelled), and every acquisition step leaves a receipt.

Only attributes the publisher releases are stored: no vulnerability,
criticality or valuation field is accepted (:data:`FORBIDDEN_KEYS`).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

CONTRACT = "noesis-infrastructure-asset-record-v1"
SCHEMA_VERSION = "1.0.0"
READ_SCOPE = "knowledge:infrastructure:read"
WRITE_SCOPE = "knowledge:infrastructure:write"
REVIEW_SCOPE = "knowledge:infrastructure:review"
PROVIDERS = ("gppd", "gem", "osm", "eia", "entsog")
ASSET_CLASSES = ("power_plant", "generating_unit", "pipeline", "lng_terminal", "transmission_line", "substation",
                 "gas_point", "mine")
STATUSES = ("proposed", "construction", "operating", "mothballed", "retired", "cancelled", "unknown")
RELEASE_BASES = ("declared_release", "extract_timestamp", "provider_last_update", "retrieval_time")
OWNER_ROLES = ("owner", "operator", "parent")
GEOMETRY_TYPES = ("Point", "LineString", "MultiLineString", "Polygon")
# Keys that would carry a derived judgement the publisher never released.
FORBIDDEN_KEYS = frozenset({"vulnerability", "criticality", "risk_score", "valuation", "asset_value",
                            "attack_surface", "protection_level", "dependency_score"})
_INSTANT = re.compile(r"^\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2}))?)?)?$")
GEO_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write", "knowledge:geospatial:calculate"}
PRODUCER = {"name": "noesis-geospatial-infrastructure", "version": "1.0.0"}
STALE_AFTER_MS = 14 * 86_400_000


class InfrastructureError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _fail(message, code="invalid_infrastructure_record"):
    raise InfrastructureError(code, message)


def _text(value, field, *, optional=False, limit=2000):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(f"{field} must be nonempty text")
    return value


def _instant(value, field, *, optional=True):
    if value is None and optional:
        return None
    if not isinstance(value, str) or not _INSTANT.fullmatch(value):
        _fail(f"{field} must be an ISO-8601 year, month, date or instant with offset")
    return value


def decimal_text(value, field="value"):
    """Exact decimal text or ``None``; floats are rejected."""

    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{field} must be a decimal string or null, not {type(value).__name__}")
    try:
        number = Decimal(value)
    except InvalidOperation:
        _fail(f"{field} is not a decimal number")
    if not number.is_finite():
        _fail(f"{field} must be finite")
    return value


def forbidden_keys(value, path="$"):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def precision_from_decimals(*values):
    """Metres implied by the fewest decimal places any published coordinate carries (0.5 ulp at the equator)."""

    places = []
    for value in values:
        text = str(value)
        places.append(len(text.split(".", 1)[1]) if "." in text else 0)
    fewest = min(places) if places else 0
    return round(0.5 * 10 ** (-fewest) * 111_320, 3), f"coordinates published to {fewest} decimal places"


def _coordinates(kind, coordinates, field):
    def point(item):
        if not isinstance(item, (list, tuple)) or len(item) != 2 or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool) for v in item):
            _fail(f"{field} coordinates are [lon, lat] numbers")
        lon, lat = float(item[0]), float(item[1])
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            _fail(f"{field} coordinates are outside WGS84 bounds")
        return [lon, lat]

    if kind == "Point":
        return point(coordinates)
    if kind == "LineString":
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            _fail(f"{field} LineString needs at least two positions")
        return [point(p) for p in coordinates]
    if kind == "MultiLineString":
        if not isinstance(coordinates, list) or not coordinates:
            _fail(f"{field} MultiLineString needs lines")
        return [_coordinates("LineString", line, field) for line in coordinates]
    if not isinstance(coordinates, list) or not coordinates:
        _fail(f"{field} Polygon needs rings")
    rings = [[point(p) for p in ring] for ring in coordinates]
    if any(len(ring) < 4 or ring[0] != ring[-1] for ring in rings):
        _fail(f"{field} Polygon rings are closed with at least four positions")
    return rings


def _geometry(geometry, receipt):
    if geometry is None:
        return None, None
    if not isinstance(geometry, dict) or geometry.get("type") not in GEOMETRY_TYPES:
        _fail(f"geometry.type must be one of {', '.join(GEOMETRY_TYPES)}")
    out = {"type": geometry["type"], "coordinates": _coordinates(geometry["type"], geometry.get("coordinates"),
                                                                  "geometry")}
    if not isinstance(receipt, dict) or not receipt.get("crs_published") or receipt.get("precision_m") is None:
        _fail("a geometry carries its receipt: published CRS, stored CRS and precision with its basis",
              "geometry_receipt_required")
    precision = float(receipt["precision_m"])
    if precision < 0:
        _fail("precision_m is a non-negative distance")
    return out, {"crs_published": str(receipt["crs_published"]), "crs_stored": "EPSG:4326",
                 "precision_m": precision, "precision_basis": _text(receipt.get("precision_basis"),
                                                                    "geometry_receipt.precision_basis"),
                 "transformed": bool(receipt.get("transformed", False))}


def _status(status):
    if status is None:
        return None
    if not isinstance(status, dict) or status.get("normalized") not in STATUSES:
        _fail(f"status.normalized must be one of {', '.join(STATUSES)}")
    published = status.get("published")
    if published is not None and not isinstance(published, str):
        _fail("status.published is the publisher's own text")
    effective = _instant(status.get("effective_date"), "status.effective_date")
    basis = status.get("effective_basis") or ("unknown" if effective is None else None)
    _text(basis, "status.effective_basis", limit=300)
    return {"published": published, "normalized": status["normalized"], "effective_date": effective,
            "effective_basis": basis}


def _capacities(values):
    if not isinstance(values or [], list) or len(values or []) > 200:
        _fail("capacities is a bounded list")
    out, seen = [], set()
    for index, item in enumerate(values or []):
        if not isinstance(item, dict):
            _fail(f"capacities[{index}] is an object")
        metric = _text(item.get("metric"), f"capacities[{index}].metric", limit=100)
        direction = item.get("direction")
        if direction is not None and direction not in {"entry", "exit", "bidirectional"}:
            _fail(f"capacities[{index}].direction is entry, exit or bidirectional as published")
        key = (metric, direction, item.get("period"))
        if key in seen:
            _fail(f"duplicate capacity {key}")
        seen.add(key)
        out.append({"metric": metric, "value": decimal_text(item.get("value"), f"capacities[{index}].value"),
                    "unit": _text(item.get("unit"), f"capacities[{index}].unit", limit=60), "direction": direction,
                    "period": item.get("period"),
                    "effective_date": _instant(item.get("effective_date"), f"capacities[{index}].effective_date"),
                    "effective_basis": item.get("effective_basis"), "estimate": bool(item.get("estimate", False)),
                    "published_text": item.get("published_text")})
    return sorted(out, key=lambda c: (c["metric"], c["direction"] or "", c["period"] or ""))


def _owners(values):
    out = []
    for index, item in enumerate(values or []):
        if not isinstance(item, dict) or item.get("role") not in OWNER_ROLES:
            _fail(f"owners[{index}].role must be one of {', '.join(OWNER_ROLES)}")
        identifiers = item.get("identifiers") or []
        if not isinstance(identifiers, list) or any(not isinstance(i, dict) or not i.get("scheme") or not i.get("value")
                                                    for i in identifiers):
            _fail(f"owners[{index}].identifiers use scheme/value as published")
        out.append({"role": item["role"], "name": _text(item.get("name"), f"owners[{index}].name", limit=500),
                    "share": decimal_text(item.get("share"), f"owners[{index}].share"),
                    "share_text": item.get("share_text"),
                    "identifiers": [{"scheme": str(i["scheme"]), "value": str(i["value"])} for i in identifiers]})
    return out


def _identifiers(values):
    out = []
    for index, item in enumerate(values or []):
        if not isinstance(item, dict) or not item.get("scheme") or not item.get("value"):
            _fail(f"identifiers[{index}] use scheme/value as published")
        out.append({"scheme": str(item["scheme"]), "value": str(item["value"])})
    return sorted({(i["scheme"], i["value"]): i for i in out}.values(), key=lambda i: (i["scheme"], i["value"]))


def _release(release):
    if not isinstance(release, dict) or release.get("basis") not in RELEASE_BASES:
        _fail(f"release states key, released_at and basis ({', '.join(RELEASE_BASES)})", "release_required")
    _text(release.get("key"), "release.key", limit=500)
    released = _instant(release.get("released_at"), "release.released_at")
    if released is None and release["basis"] != "retrieval_time":
        _fail("a release dated by the publisher or a declaration states released_at", "release_required")
    return {"key": release["key"], "released_at": released, "basis": release["basis"], "label": release.get("label")}


def record(provider, dataset, native_id, asset_class, *, name, source_url, attribution, licence, release,
           retrieved_at, country=None, identifiers=None, geometry=None, geometry_receipt=None, status=None,
           capacities=None, owners=None, attributes=None, estimates=None, cited_notes=None,
           cited_references=None, element=None, locator=None, unknowns=None):
    """Build and validate one infrastructure asset record (fail closed)."""

    if provider not in PROVIDERS:
        _fail("unknown infrastructure provider")
    if asset_class not in ASSET_CLASSES:
        _fail(f"asset_class must be one of {', '.join(ASSET_CLASSES)}")
    _text(dataset, "dataset", limit=300)
    _text(native_id, "native_id", limit=300)
    if name is not None:
        _text(name, "name", limit=500)
    if not isinstance(source_url, str) or not source_url.startswith("https://"):
        _fail("source_url must be the publisher's https URL")
    _text(attribution, "attribution", limit=500)
    if not isinstance(licence, dict) or not licence.get("id") or not str(licence.get("terms_url") or "").startswith(
            "https://"):
        _fail("licence needs an id and https terms_url")
    geom, geom_receipt = _geometry(geometry, geometry_receipt)
    out = {
        "contract": CONTRACT, "schema_version": SCHEMA_VERSION, "provider": provider, "dataset": dataset,
        "native_id": native_id, "asset_class": asset_class, "name": name, "country": country,
        "identifiers": _identifiers(identifiers), "geometry": geom, "geometry_receipt": geom_receipt,
        "status": _status(status), "capacities": _capacities(capacities), "owners": _owners(owners),
        "attributes": dict(attributes or {}), "estimates": [dict(e) for e in estimates or []],
        "cited_notes": [dict(n) for n in cited_notes or []],
        "cited_references": [dict(r) for r in cited_references or []],
        "element": None if element is None else dict(element),
        "release": _release(release), "retrieved_at": _instant(retrieved_at, "retrieved_at", optional=False),
        "source_url": source_url, "attribution": attribution,
        "licence": {"id": licence["id"], "terms_url": licence["terms_url"],
                    "constraints": sorted(set(licence.get("constraints") or []))},
        "locator": dict(locator or {}), "unknowns": sorted(set(unknowns or [])),
    }
    for note in out["cited_notes"]:
        if not str(note.get("url") or "").startswith("https://") or set(note) - {"url", "label"}:
            _fail("cited notes are cited by https URL and label only; note text is never copied")
    for ref in out["cited_references"]:
        if not ref.get("scheme") or not ref.get("identifier"):
            _fail("cited references name the scheme and identifier the publisher states")
    for estimate in out["estimates"]:
        decimal_text(estimate.get("value"), "estimates.value")
        if estimate.get("basis") != "publisher estimate":
            _fail("estimates are the publisher's own estimates (basis 'publisher estimate')")
    if geom is None:
        out["unknowns"] = sorted(set(out["unknowns"]) | {"geometry"})
    if out["status"] is None:
        out["unknowns"] = sorted(set(out["unknowns"]) | {"status"})
    if out["release"]["released_at"] is None:
        out["unknowns"] = sorted(set(out["unknowns"]) | {"release.released_at"})
    bad = forbidden_keys(out)
    if bad:
        _fail(f"only attributes the publisher releases are stored; forbidden keys {bad}", "sensitive_enrichment_refused")
    return out


def validate(value):
    """Re-validate a stored or received record by rebuilding it."""

    if not isinstance(value, dict) or value.get("contract") != CONTRACT:
        _fail("not a noesis-infrastructure-asset-record-v1 record")
    return record(
        value.get("provider"), value.get("dataset"), value.get("native_id"), value.get("asset_class"),
        name=value.get("name"), source_url=value.get("source_url"), attribution=value.get("attribution"),
        licence=value.get("licence"), release=value.get("release"), retrieved_at=value.get("retrieved_at"),
        country=value.get("country"), identifiers=value.get("identifiers"), geometry=value.get("geometry"),
        geometry_receipt=value.get("geometry_receipt"), status=value.get("status"),
        capacities=value.get("capacities"), owners=value.get("owners"), attributes=value.get("attributes"),
        estimates=value.get("estimates"), cited_notes=value.get("cited_notes"),
        cited_references=value.get("cited_references"), element=value.get("element"), locator=value.get("locator"),
        unknowns=value.get("unknowns"))


# ------------------------------------------------------------ schema registry

SCHEMA_FILES = {"noesis-infrastructure-asset-record": "contracts/schemas/jsonschema/noesis-infrastructure-asset-record-v1.json"}


def schema_definitions(root=None):
    from pathlib import Path

    root = Path(root) if root else Path(__file__).resolve().parents[2]
    return {name: json.loads((root / path).read_text()) for name, path in SCHEMA_FILES.items()}


def validate_against_schema(value, root=None, definition=None):
    """JSON-schema errors for a record (``definition=None``) or one of its sub-records (a ``definitions`` name)."""

    from jsonschema import Draft7Validator

    schema = schema_definitions(root)["noesis-infrastructure-asset-record"]
    if definition is not None:
        schema = {"definitions": schema["definitions"], "$ref": f"#/definitions/{definition}"}
    return sorted(error.message for error in Draft7Validator(schema).iter_errors(value))


def register_schemas(conn, *, principal_id, scopes, root=None):
    """Register the record schema in the existing schema registry (idempotent per version)."""

    from src.kb.schema_registry import SchemaRegistry

    registry = SchemaRegistry(conn)
    results = []
    for name, content in sorted(schema_definitions(root).items()):
        definition = {
            "contract": "noesis-schema-module-v1", "name": name, "kind": "schema", "semantic_version": SCHEMA_VERSION,
            "content": content, "owner": "geospatial.infrastructure", "dependencies": [],
            "compatibility_policy": "backward", "provenance": {"kind": "imported", "source": "packs/geospatial"},
            "actor": {"principal_id": principal_id, "kind": "service"},
        }
        results.append(registry.register(definition, f"infrastructure-schema:{name}:{SCHEMA_VERSION}:"
                                                     f"{digest(content)[:16]}", principal_id=principal_id,
                                         scopes=scopes))
    return results


# ------------------------------------------------------------------- store

def authorize(namespace, scopes, required, *, write=False):
    """Infrastructure scope plus namespace access (operator bypasses)."""

    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise InfrastructureError("unauthorized", f"{required} and namespace access are required")


def ms(value):
    """ISO year/month/date/instant -> epoch ms (UTC; partial dates are their first instant)."""

    if value is None:
        return None
    text = str(value).replace("Z", "+00:00")
    if len(text) == 4:
        text += "-01-01"
    if len(text) == 7:
        text += "-01"
    if len(text) == 10:
        text += "T00:00:00+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def iso(value_ms):
    return None if value_ms is None else datetime.fromtimestamp(value_ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def table_exists(conn, name):
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def asset_id(namespace, provider, dataset, native_id):
    return "infra-asset:" + digest([namespace, provider, dataset, native_id])[:24]


TABLES = ("infra_assets", "infra_asset_revisions", "infra_status_revisions", "infra_capacity_revisions",
          "infra_owner_assertions", "infra_receipts", "infra_provider_state")
_DDL = """
CREATE TABLE IF NOT EXISTS infra_assets(
 asset_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, dataset TEXT NOT NULL,
 native_id TEXT NOT NULL, asset_class TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 UNIQUE(namespace, provider, dataset, native_id));
CREATE TABLE IF NOT EXISTS infra_asset_revisions(
 revision_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, namespace TEXT NOT NULL, sequence BIGINT NOT NULL,
 release_key TEXT NOT NULL, release_basis TEXT NOT NULL, released_at_ms BIGINT, retrieved_at_ms BIGINT NOT NULL,
 published_at_ms BIGINT NOT NULL, content_hash TEXT NOT NULL, record_json TEXT NOT NULL, geometry_id TEXT,
 receipt_id TEXT, revision_of TEXT, created_at_ms BIGINT NOT NULL, UNIQUE(asset_id, sequence));
CREATE TABLE IF NOT EXISTS infra_status_revisions(
 status_revision_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, namespace TEXT NOT NULL, sequence BIGINT NOT NULL,
 revision_id TEXT NOT NULL, published_status TEXT, normalized_status TEXT NOT NULL, effective_date TEXT,
 effective_basis TEXT, published_at_ms BIGINT NOT NULL, retrieved_at_ms BIGINT NOT NULL, supersedes TEXT,
 UNIQUE(asset_id, sequence));
CREATE TABLE IF NOT EXISTS infra_capacity_revisions(
 capacity_revision_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, namespace TEXT NOT NULL, metric TEXT NOT NULL,
 direction TEXT, period TEXT, sequence BIGINT NOT NULL, revision_id TEXT NOT NULL, value TEXT, unit TEXT NOT NULL,
 effective_date TEXT, estimate BOOLEAN NOT NULL, published_at_ms BIGINT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
 supersedes TEXT);
CREATE TABLE IF NOT EXISTS infra_owner_assertions(
 assertion_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, namespace TEXT NOT NULL, revision_id TEXT NOT NULL,
 position BIGINT NOT NULL, role TEXT NOT NULL, name TEXT NOT NULL, share TEXT, share_text TEXT,
 identifiers_json TEXT NOT NULL, published_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS infra_receipts(
 receipt_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, source TEXT NOT NULL,
 request_json TEXT NOT NULL, response_sha256 TEXT, bytes BIGINT, status TEXT NOT NULL, failure_code TEXT,
 execution TEXT NOT NULL, run_id TEXT NOT NULL, records BIGINT NOT NULL, coverage_json TEXT NOT NULL,
 retrieved_at_ms BIGINT NOT NULL, principal_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS infra_provider_state(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_execution TEXT, last_run_id TEXT, PRIMARY KEY(namespace, provider));
"""


class InfrastructureStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self):
        return table_exists(self.conn, "infra_asset_revisions")

    # ----------------------------------------------------------------- writes

    def receipt(self, namespace, *, provider, source, request, response_sha256, size, status, execution, run_id,
                records, principal_id, coverage=None, failure_code=None, retrieved_at_ms=None):
        """Record one bounded acquisition step (the request is kept without credentials)."""

        retrieved = int(retrieved_at_ms or self.now())
        body = {"namespace": namespace, "provider": provider, "source": source, "request": request,
                "response_sha256": response_sha256, "status": status, "run_id": run_id,
                "retrieved": retrieved if response_sha256 is None else None}
        receipt_id = "infra-receipt:" + digest(body)[:24]
        self.conn.execute("INSERT INTO infra_receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [receipt_id, namespace, provider, source, canonical(request), response_sha256, size, status,
                           failure_code, execution, run_id, int(records), canonical(coverage or {}), retrieved,
                           principal_id])
        return receipt_id

    def apply(self, namespace, records, *, receipt_id=None, run_id, principal_id, scopes, execution="injected"):
        """Store validated records: unchanged content is a no-op, anything else a new revision."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if namespace == "global":
            raise InfrastructureError("namespace_forbidden", "infrastructure records are written to a caller namespace")
        counts = {"assets": 0, "revisions": 0, "unchanged": 0, "status_revisions": 0, "capacity_revisions": 0}
        providers = set()
        for item in records:
            value = validate(dict(item))
            providers.add(value["provider"])
            for key, amount in self._apply_one(namespace, value, receipt_id=receipt_id,
                                               principal_id=principal_id).items():
                counts[key] += amount
        stamp = self.now()
        for provider in sorted(providers):
            self.conn.execute(
                "INSERT INTO infra_provider_state VALUES (?,?,?,NULL,NULL,?,?) ON CONFLICT (namespace, provider) DO UPDATE "
                "SET last_success_ms=excluded.last_success_ms, last_execution=excluded.last_execution, "
                "last_run_id=excluded.last_run_id", [namespace, provider, stamp, execution, run_id])
        return counts

    def _project_geometry(self, namespace, value, aid, principal_id):
        from src.kb.geospatial import GeospatialStore

        geo = GeospatialStore(self.conn, now=self.now)
        receipt = value["geometry_receipt"]
        stored = geo.store_geometry(
            namespace, value["geometry"], place_id=None, crs="EPSG:4326", precision_m=receipt["precision_m"],
            simplified_from=None, disputed=False, admin_hierarchy=[],
            source={"owner": "geospatial.infrastructure", "asset_id": aid, "provider": value["provider"],
                    "dataset": value["dataset"], "native_id": value["native_id"],
                    "crs_published": receipt["crs_published"], "attribution": value["attribution"]},
            evidence=[{"kind": "infrastructure-asset-geometry", "precision_basis": receipt["precision_basis"]}],
            principal_id=principal_id, scopes=GEO_SCOPES, producer=PRODUCER,
            policy={"crs": "explicit-v1", "enrichment": "publisher attributes only"})
        return stored["geometry_id"]

    def _apply_one(self, namespace, value, *, receipt_id, principal_id):
        aid = asset_id(namespace, value["provider"], value["dataset"], value["native_id"])
        counts = {"assets": 0, "revisions": 0, "unchanged": 0, "status_revisions": 0, "capacity_revisions": 0}
        row = self.conn.execute("SELECT asset_class FROM infra_assets WHERE asset_id=?", [aid]).fetchone()
        if row is None:
            self.conn.execute("INSERT INTO infra_assets VALUES (?,?,?,?,?,?,?)",
                              [aid, namespace, value["provider"], value["dataset"], value["native_id"],
                               value["asset_class"], self.now()])
            counts["assets"] += 1
        elif row[0] != value["asset_class"]:
            raise InfrastructureError("asset_conflict", "an asset never changes its class; a new class is a new asset")
        content = {k: v for k, v in value.items() if k != "retrieved_at"}
        if value["release"]["basis"] in {"retrieval_time", "extract_timestamp"}:
            # An undated re-publication or a later extract of an unchanged element is not a new revision: the
            # revision is dated by the first release or extract that published this content.
            content["release"] = {"basis": value["release"]["basis"]}
        content_hash = digest(content)
        if self.conn.execute("SELECT 1 FROM infra_asset_revisions WHERE asset_id=? AND content_hash=?",
                             [aid, content_hash]).fetchone():
            counts["unchanged"] += 1
            return counts
        previous = self.conn.execute("SELECT revision_id, sequence FROM infra_asset_revisions WHERE asset_id=? "
                                     "ORDER BY sequence DESC LIMIT 1", [aid]).fetchone()
        sequence = int(previous[1]) + 1 if previous else 1
        released = ms(value["release"]["released_at"])
        retrieved = ms(value["retrieved_at"])
        published = released if released is not None else retrieved
        revision_id = "infra-rev:" + digest([aid, sequence, content_hash])[:24]
        geometry_id = self._project_geometry(namespace, value, aid, principal_id) if value["geometry"] else None
        self.conn.execute(
            "INSERT INTO infra_asset_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [revision_id, aid, namespace, sequence, value["release"]["key"], value["release"]["basis"], released,
             retrieved, published, content_hash, canonical(value), geometry_id, receipt_id,
             previous[0] if previous else None, self.now()])
        counts["revisions"] += 1
        status = value["status"]
        if status is not None:
            last = self.conn.execute(
                "SELECT status_revision_id, sequence, published_status, normalized_status, effective_date "
                "FROM infra_status_revisions WHERE asset_id=? ORDER BY sequence DESC LIMIT 1", [aid]).fetchone()
            if last is None or (last[2], last[3], last[4]) != (status["published"], status["normalized"],
                                                               status["effective_date"]):
                seq = int(last[1]) + 1 if last else 1
                sid = "infra-status:" + digest([aid, seq, revision_id])[:24]
                self.conn.execute("INSERT INTO infra_status_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                  [sid, aid, namespace, seq, revision_id, status["published"], status["normalized"],
                                   status["effective_date"], status["effective_basis"], published, retrieved,
                                   last[0] if last else None])
                counts["status_revisions"] += 1
        for capacity in value["capacities"]:
            last = self.conn.execute(
                "SELECT capacity_revision_id, sequence, value, unit, effective_date, estimate FROM infra_capacity_revisions "
                "WHERE asset_id=? AND metric=? AND direction IS NOT DISTINCT FROM ? AND period IS NOT DISTINCT FROM ? "
                "ORDER BY sequence DESC LIMIT 1",
                [aid, capacity["metric"], capacity["direction"], capacity["period"]]).fetchone()
            if last is not None and (last[2], last[3], last[4], bool(last[5])) == (
                    capacity["value"], capacity["unit"], capacity["effective_date"], capacity["estimate"]):
                continue
            seq = int(last[1]) + 1 if last else 1
            cid = "infra-capacity:" + digest([aid, capacity["metric"], capacity["direction"], capacity["period"], seq,
                                              revision_id])[:24]
            self.conn.execute("INSERT INTO infra_capacity_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                              [cid, aid, namespace, capacity["metric"], capacity["direction"], capacity["period"], seq,
                               revision_id, capacity["value"], capacity["unit"], capacity["effective_date"],
                               capacity["estimate"], published, retrieved, last[0] if last else None])
            counts["capacity_revisions"] += 1
        for position, owner in enumerate(value["owners"]):
            oid = "infra-owner:" + digest([revision_id, position])[:24]
            self.conn.execute("INSERT INTO infra_owner_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                              [oid, aid, namespace, revision_id, position, owner["role"], owner["name"], owner["share"],
                               owner["share_text"], canonical(owner["identifiers"]), published])
        return counts

    def fail(self, namespace, provider, error, *, source, request, run_id, principal_id, execution="injected"):
        """Record a failed step; stored revisions stay as they are (the provider reads as stale)."""

        code = getattr(error, "code", None) or type(error).__name__
        receipt_id = self.receipt(namespace, provider=provider, source=source, request=request, response_sha256=None,
                                  size=None, status="failed", execution=execution, run_id=run_id, records=0,
                                  principal_id=principal_id, failure_code=str(code),
                                  coverage={"message": str(error)[:300]})
        stamp = self.now()
        self.conn.execute(
            "INSERT INTO infra_provider_state VALUES (?,?,NULL,?,?,?,?) ON CONFLICT (namespace, provider) DO UPDATE "
            "SET last_failure_ms=excluded.last_failure_ms, last_failure_code=excluded.last_failure_code, "
            "last_run_id=excluded.last_run_id", [namespace, provider, stamp, str(code), execution, run_id])
        return {"receipt_id": receipt_id, "provider": provider, "failure_code": str(code),
                "effect": "no stored value changed; dependent answers report the provider as stale"}

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace, provider):
        row = self.conn.execute(
            "SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution, last_run_id FROM "
            "infra_provider_state WHERE namespace=? AND provider=?", [namespace, provider]).fetchone() \
            if table_exists(self.conn, "infra_provider_state") else None
        if row is None:
            return {"provider": provider, "last_success_ms": None, "stale": True, "reason": "never acquired"}
        success, failure = row[0], row[1]
        stale = success is None or (failure is not None and failure > success) or self.now() - success > STALE_AFTER_MS
        return {"provider": provider, "last_success_ms": success, "last_failure_ms": failure,
                "last_failure_code": row[2], "last_execution": row[3], "last_run_id": row[4], "stale": bool(stale)}

    def assets(self, namespace, *, scopes, provider=None, asset_class=None, asset_ids=None):
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT asset_id, provider, dataset, native_id, asset_class FROM infra_assets WHERE namespace=? "
            "AND (? IS NULL OR provider=?) AND (? IS NULL OR asset_class=?) ORDER BY provider, dataset, native_id",
            [namespace, provider, provider, asset_class, asset_class]).fetchall()
        wanted = None if asset_ids is None else set(asset_ids)
        return [{"asset_id": r[0], "provider": r[1], "dataset": r[2], "native_id": r[3], "asset_class": r[4]}
                for r in rows if wanted is None or r[0] in wanted]

    _REVISION_COLUMNS = ("revision_id, asset_id, sequence, release_key, release_basis, released_at_ms, retrieved_at_ms, "
                         "published_at_ms, content_hash, record_json, geometry_id, receipt_id, revision_of")

    @staticmethod
    def _revision_row(row):
        return {"revision_id": row[0], "asset_id": row[1], "sequence": int(row[2]), "release_key": row[3],
                "release_basis": row[4], "released_at_ms": row[5], "retrieved_at_ms": row[6],
                "published_at_ms": row[7], "content_hash": row[8], "record": json.loads(row[9]),
                "geometry_id": row[10], "receipt_id": row[11], "revision_of": row[12]}

    def revisions(self, namespace, aid, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        return [self._revision_row(r) for r in self.conn.execute(
            f"SELECT {self._REVISION_COLUMNS} FROM infra_asset_revisions WHERE namespace=? AND asset_id=? "
            "ORDER BY sequence", [namespace, aid]).fetchall()]

    def revision(self, namespace, revision_id, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        row = self.conn.execute(f"SELECT {self._REVISION_COLUMNS} FROM infra_asset_revisions WHERE namespace=? AND "
                                "revision_id=?", [namespace, revision_id]).fetchone()
        if row is None:
            raise InfrastructureError("not_found", "revision is unavailable")
        return self._revision_row(row)

    def revision_as_of(self, namespace, aid, *, scopes, as_of_ms=None):
        """The revision published at or before ``as_of_ms`` (latest when omitted), or ``None``."""

        candidates = [r for r in self.revisions(namespace, aid, scopes=scopes)
                      if as_of_ms is None or r["published_at_ms"] <= as_of_ms]
        return max(candidates, key=lambda r: (r["published_at_ms"], r["sequence"])) if candidates else None

    def status_history(self, namespace, aid, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        keys = ("status_revision_id", "sequence", "revision_id", "published", "normalized", "effective_date",
                "effective_basis", "published_at_ms", "retrieved_at_ms", "supersedes")
        return [dict(zip(keys, r)) for r in self.conn.execute(
            "SELECT status_revision_id, sequence, revision_id, published_status, normalized_status, effective_date, "
            "effective_basis, published_at_ms, retrieved_at_ms, supersedes FROM infra_status_revisions "
            "WHERE namespace=? AND asset_id=? ORDER BY sequence", [namespace, aid]).fetchall()]

    def capacity_history(self, namespace, aid, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        keys = ("capacity_revision_id", "metric", "direction", "period", "sequence", "revision_id", "value", "unit",
                "effective_date", "estimate", "published_at_ms", "retrieved_at_ms", "supersedes")
        return [dict(zip(keys, r)) for r in self.conn.execute(
            "SELECT capacity_revision_id, metric, direction, period, sequence, revision_id, value, unit, effective_date, "
            "estimate, published_at_ms, retrieved_at_ms, supersedes FROM infra_capacity_revisions "
            "WHERE namespace=? AND asset_id=? ORDER BY metric, direction, period, sequence", [namespace, aid]).fetchall()]

    def owner_assertions(self, namespace, revision_id, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        return [{"assertion_id": r[0], "role": r[1], "name": r[2], "share": r[3], "share_text": r[4],
                 "identifiers": json.loads(r[5]), "revision_id": revision_id}
                for r in self.conn.execute(
                    "SELECT assertion_id, role, name, share, share_text, identifiers_json FROM infra_owner_assertions "
                    "WHERE namespace=? AND revision_id=? ORDER BY position", [namespace, revision_id]).fetchall()]

    def receipt_row(self, receipt_id):
        row = self.conn.execute("SELECT receipt_id, provider, source, request_json, response_sha256, status, execution, "
                                "run_id, retrieved_at_ms, coverage_json FROM infra_receipts WHERE receipt_id=?",
                                [receipt_id]).fetchone()
        if row is None:
            return None
        return {"receipt_id": row[0], "provider": row[1], "source": row[2], "request": json.loads(row[3]),
                "response_sha256": row[4], "status": row[5], "execution": row[6], "run_id": row[7],
                "retrieved_at_ms": row[8], "coverage": json.loads(row[9])}

    def receipts(self, namespace, *, scopes, provider=None):
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "infra_receipts"):
            return []
        return [{"receipt_id": r[0], "provider": r[1], "source": r[2], "status": r[3], "failure_code": r[4],
                 "execution": r[5], "records": r[6], "retrieved_at_ms": r[7]}
                for r in self.conn.execute(
                    "SELECT receipt_id, provider, source, status, failure_code, execution, records, retrieved_at_ms "
                    "FROM infra_receipts WHERE namespace=? AND (? IS NULL OR provider=?) ORDER BY retrieved_at_ms, "
                    "receipt_id", [namespace, provider, provider]).fetchall()]

    def citation(self, revision):
        """What a reader needs to check a value: source, release, as-of, licence constraints and receipt."""

        record_ = revision["record"]
        receipt = self.receipt_row(revision["receipt_id"]) if revision.get("receipt_id") else None
        return {"provider": record_["provider"], "dataset": record_["dataset"], "native_id": record_["native_id"],
                "source_url": record_["source_url"], "attribution": record_["attribution"],
                "licence": record_["licence"], "asset_id": revision["asset_id"], "revision_id": revision["revision_id"],
                "sequence": revision["sequence"], "release": record_["release"],
                "as_of_basis": "release date" if revision["released_at_ms"] is not None else "retrieval time",
                "published_at": iso(revision["published_at_ms"]), "retrieved_at": record_["retrieved_at"],
                "locator": record_.get("locator") or {}, "element": record_.get("element"),
                "cited_notes": record_.get("cited_notes") or [],
                "receipt": None if receipt is None else {k: receipt[k] for k in ("receipt_id", "response_sha256",
                                                                                 "execution", "retrieved_at_ms")}}


class InfrastructureProjector:
    """Source-pack runtime projector for ``noesis-infrastructure-asset-record-v1`` pages (``infrastructure`` connector)."""

    @staticmethod
    def scopes_for(namespace):
        return {WRITE_SCOPE, READ_SCOPE, f"namespace:{namespace}:write", f"namespace:{namespace}:read"}

    def __init__(self, conn):
        self.store = InfrastructureStore(conn)

    @staticmethod
    def _spec(source):
        return dict(source.get("infrastructure") or {})

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del documents
        spec = self._spec(source)
        namespace = str(spec.get("namespace") or "infrastructure")
        payload = [dict(item["infrastructure_record"]) for item in records if item.get("infrastructure_record")]
        receipt_id = self.store.receipt(
            namespace, provider=str(spec.get("provider")),
            source=f"{manifest['pack_id']}@{manifest['version']}:{source['source_id']}",
            request=dict(page_receipt.get("request") or {"step": page_receipt.get("step")}),
            response_sha256=page_receipt.get("response_sha256"), size=None, status="ok", execution="source-pack",
            run_id=run_id, records=len(payload), principal_id=principal_id, coverage=page_receipt.get("coverage"))
        return self.store.apply(namespace, payload, receipt_id=receipt_id, run_id=run_id, principal_id=principal_id,
                                scopes=self.scopes_for(namespace), execution="source-pack")

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest
        spec = self._spec(source)
        namespace = str(spec.get("namespace") or "infrastructure")
        provider = str(spec.get("provider"))
        if status != "complete":
            self.store.fail(namespace, provider, InfrastructureError("source_run_" + status, "source run did not complete"),
                            source=source["source_id"], request={}, run_id=run_id, principal_id=principal_id,
                            execution="source-pack")
        return {"status": status, "provider": provider, "namespace": namespace,
                "provider_state": self.store.provider_state(namespace, provider)}


def feature_enabled(conn, namespace=None):
    """Whether the Geospatial bundle's optional ``infrastructure`` feature is selected in the active plan."""

    del namespace  # composition selection is deployment-wide
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_name IN "
            "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')").fetchall()}
        if len(tables) < 4:
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='geospatial'").fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g ON g.generation_id=a.generation_id "
            "JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1").fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return "infrastructure" in ((plan.get("features") or {}).get("geospatial") or [])
