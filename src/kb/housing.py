"""Housing, land-value and urban-planning records: the ``geospatial.housing`` owner (#1912, U02).

Records (contract ``noesis-housing-record-v1``) live in namespace-scoped,
revision-addressable ``housing_*`` tables. Geometries stay in the Geospatial
stores: a zone, plan or residential-area record references the
``geospatial_features`` feature and feature revision it was read from and the
``geospatial_geometries`` geometry of that revision - it never copies them.

* **land-value-zone revision** - one published Bodenrichtwert: zone
  identifier, value exactly as delivered (text and decimal), currency, unit,
  valuation date (Stichtag), type of use and the other published qualifiers.
* **rent-index edition and cell** - a Mietspiegel edition (qualifying date,
  date it applies from, publication URL and page) and its cells (published
  key, dimensions and lower/middle/upper range fields, unit, currency).
* **development-plan stage** - a plan's published procedural stage with its
  stage date and the explicit references the layer publishes.
* **residential-area category** - a Wohnlage category per Mietspiegel edition,
  kept as the source's label, never translated into a value or score.
* **permit and completion statistic** - one figure per reporting area,
  period and measure, with the publication date as its vintage.
* **housing-indicator vintage** - a reference to a Destatis series vintage in
  the dataset ``ObservationStore`` (values are not copied here).

Revision semantics, the same for every record type: an *entity* is everything
that distinguishes two published facts - source, identifier and the source's
own date (valuation date, edition, stage, vintage). Re-reading an unchanged
entity adds nothing, whatever the fetch time. A changed reading of the same
entity is a *correction*: a new revision that supersedes the current one, and a
return to earlier content is recorded as a further correction (deduplication is
only against the current revision). Entities with different dates, editions,
vintages or sources are stored side by side - never merged or averaged - and
"current" always follows the source's own date, whatever the arrival order.
A missing or unreadable date stays unknown; it never defaults to the
acquisition time. Units are labelled (through pint where installed) and money
is never converted.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from src.ingestion.housing_sources import (
    LAYERS,
    NO_SURFACE,
    PLAN_STAGES,
    PROVIDER_CONTRACTS,
    REVIEW_BOUNDARY,
    UNITS,
    day,
    layer_declaration,
    parse_decimal,
    plan_key,
    reporting_area,
    tabular_declaration,
)

CONTRACT = "noesis-housing-record-v1"
READ_SCOPE = "knowledge:housing:read"
WRITE_SCOPE = "knowledge:housing:write"
REVIEW_SCOPE = "knowledge:housing:review"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = (
    "source_revision",
    "land_value_revision",
    "rent_index_edition",
    "rent_index_cell",
    "plan_stage",
    "area_category",
    "building_statistic",
    "indicator_vintage",
)
# Keys that would carry a valuation, advice, an interpolated or merged number.
FORBIDDEN_KEYS = frozenset(
    {
        "estimated_value",
        "market_value",
        "fair_rent",
        "recommended_rent",
        "interpolated_value",
        "average_value",
        "merged_value",
        "investment_score",
        "tenancy_advice",
        "legal_advice",
    }
)
_TABLES = {
    "land_value_revision": "housing_land_value_revisions",
    "rent_index_edition": "housing_rent_index_editions",
    "rent_index_cell": "housing_rent_index_cells",
    "plan_stage": "housing_plan_stages",
    "area_category": "housing_area_categories",
    "building_statistic": "housing_building_statistics",
    "indicator_vintage": "housing_indicator_vintages",
}
_COMMON = (
    "namespace TEXT NOT NULL, record_id TEXT NOT NULL, entity_key TEXT NOT NULL, revision_no INTEGER NOT NULL, "
    "supersedes TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL, content_json TEXT NOT NULL, "
    "source_json TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT NOT NULL, run_id TEXT, "
    "sequence BIGINT NOT NULL, recorded_at_ms BIGINT NOT NULL"
)
_DDL = f"""
CREATE SEQUENCE IF NOT EXISTS housing_record_sequence START 1;
CREATE TABLE IF NOT EXISTS housing_source_revisions (
  namespace TEXT NOT NULL, source_revision_id TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT NOT NULL,
  kind TEXT NOT NULL, format TEXT NOT NULL, document_json TEXT NOT NULL, published_on TEXT NOT NULL,
  published_at TEXT, publication_basis TEXT, edition_json TEXT, content_sha256 TEXT NOT NULL,
  file_sha256 TEXT NOT NULL, evidence_origin TEXT NOT NULL, url TEXT, run_id TEXT, sequence BIGINT NOT NULL,
  recorded_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, source_revision_id)
);
CREATE TABLE IF NOT EXISTS housing_land_value_revisions (
  {_COMMON}, collection TEXT NOT NULL, zone_id TEXT NOT NULL, valuation_date TEXT, feature_id TEXT NOT NULL,
  feature_revision_id TEXT NOT NULL, geometry_id TEXT, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS housing_rent_index_editions (
  {_COMMON}, edition_id TEXT NOT NULL, valid_from TEXT, published_on TEXT NOT NULL,
  source_revision_id TEXT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS housing_rent_index_cells (
  {_COMMON}, edition_id TEXT NOT NULL, cell_key TEXT NOT NULL, source_revision_id TEXT NOT NULL,
  PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS housing_plan_stages (
  {_COMMON}, collection TEXT NOT NULL, plan_key TEXT NOT NULL, stage TEXT NOT NULL, stage_date TEXT,
  feature_id TEXT NOT NULL, feature_revision_id TEXT NOT NULL, geometry_id TEXT, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS housing_area_categories (
  {_COMMON}, collection TEXT NOT NULL, area_id TEXT NOT NULL, edition_id TEXT NOT NULL, category TEXT NOT NULL,
  feature_id TEXT NOT NULL, feature_revision_id TEXT NOT NULL, geometry_id TEXT, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS housing_building_statistics (
  {_COMMON}, statistic TEXT NOT NULL, area_scheme TEXT NOT NULL, area_code TEXT NOT NULL, period TEXT NOT NULL,
  measure TEXT NOT NULL, vintage TEXT NOT NULL, source_revision_id TEXT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS housing_indicator_vintages (
  {_COMMON}, series_id TEXT NOT NULL, geography TEXT NOT NULL, measure TEXT NOT NULL, as_of_ms BIGINT NOT NULL,
  published_on TEXT NOT NULL, source_revision_id TEXT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS housing_projection_outcomes (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, item_key TEXT NOT NULL,
  outcome TEXT NOT NULL, reason TEXT NOT NULL, detail_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, run_id, source_id, item_key, outcome, reason)
);
"""


class HousingError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return (
        default
        if value in (None, "")
        else json.loads(value)
        if isinstance(value, str)
        else value
    )


def authorize(
    namespace: str, scopes: Iterable[str], required: str, *, write: bool = False
) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise HousingError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes: Iterable[str], required: str) -> None:
    """A scope needed only for an optional part of an answer, checked when that part is requested."""
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise HousingError(
            "unauthorized", f"{required} is required for this part of the answer"
        )


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
    )


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry a valuation, advice or an interpolated or merged number."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found


def iso_day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise HousingError("invalid_date", f"{value!r} is not an ISO date") from exc


def published_value(value: Any, number_format: str = "plain") -> dict[str, Any]:
    """A published figure as text and decimal; nothing is rounded or converted."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return {"value_text": None, "value": None}
    if isinstance(value, bool):
        raise HousingError("value_unparseable", "a boolean is not a published figure")
    if isinstance(value, (int, float)):
        text = json.dumps(value)
        number = parse_decimal(text)
    else:
        text = " ".join(str(value).split())
        try:
            number = parse_decimal(text, number_format)
        except ValueError as exc:
            raise HousingError(
                "value_unparseable", f"value {value!r} is not a number"
            ) from exc
    return {"value_text": text, "value": None if number is None else str(number)}


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Geospatial bundle's optional ``housing`` feature is selected in the active plan."""
    del namespace  # composition selection is deployment-wide
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return False
        managed = conn.execute(
            "SELECT authority FROM composition_authority WHERE bundle='geospatial'"
        ).fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return "housing" in ((plan.get("features") or {}).get("geospatial") or [])


class HousingStore:
    def __init__(
        self,
        conn: Any,
        *,
        initialize: bool = True,
        now: Callable[[], int] | None = None,
    ) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "housing_land_value_revisions")

    # ------------------------------------------------------------------ writes

    def _sequence(self) -> int:
        return int(
            self.conn.execute("SELECT nextval('housing_record_sequence')").fetchone()[0]
        )

    def _revise(
        self,
        record_type: str,
        namespace: str,
        entity: Sequence[Any],
        content: Mapping[str, Any],
        *,
        columns: Mapping[str, Any],
        source: Mapping[str, Any],
        source_id: str,
        provider: str,
        run_id: str | None,
    ) -> dict[str, Any]:
        """Record one reading of an entity: unchanged, the first revision, or a correction of the current one."""
        table = _TABLES[record_type]
        entity_key = digest([record_type, *entity])
        content_hash = digest(content)
        current = self.conn.execute(
            f"SELECT record_id, revision_no, content_hash FROM {table} WHERE namespace=? AND entity_key=? "
            "ORDER BY revision_no DESC LIMIT 1",
            [namespace, entity_key],
        ).fetchone()
        if current and current[2] == content_hash:
            return {
                "state": "unchanged",
                "record_id": current[0],
                "entity_key": entity_key,
            }
        revision_no = int(current[1]) + 1 if current else 1
        record_id = (
            f"housing-{record_type.replace('_', '-')}:"
            + digest(
                [
                    namespace,
                    entity_key,
                    revision_no,
                    content_hash,
                    current[0] if current else None,
                ]
            )[:24]
        )
        values = {
            "namespace": namespace,
            "record_id": record_id,
            "entity_key": entity_key,
            "revision_no": revision_no,
            "supersedes": current[0] if current else None,
            "change": "correction" if current else "initial",
            "content_hash": content_hash,
            "content_json": canonical(content),
            "source_json": canonical(source),
            "source_id": source_id,
            "provider": provider,
            "run_id": run_id,
            "sequence": self._sequence(),
            "recorded_at_ms": self.now(),
            **columns,
        }
        self.conn.execute(
            f"INSERT INTO {table} ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
            list(values.values()),
        )
        return {
            "state": "corrected" if current else "recorded",
            "record_id": record_id,
            "entity_key": entity_key,
            "supersedes": values["supersedes"],
        }

    def record_outcome(
        self,
        namespace: str,
        run_id: str,
        source_id: str,
        item_key: str,
        outcome: str,
        reason: str,
        detail: Mapping[str, Any],
    ) -> None:
        """A feature or row that was not projected, with the reason - reported, never silently dropped."""
        self.conn.execute(
            "INSERT OR IGNORE INTO housing_projection_outcomes VALUES (?,?,?,?,?,?,?,?)",
            [
                namespace,
                run_id,
                source_id,
                item_key,
                outcome,
                reason,
                canonical(dict(detail)),
                self.now(),
            ],
        )

    # -------------------------------------------------------------- WFS layers

    def apply_feature(
        self,
        namespace: str,
        declaration: Mapping[str, Any],
        feature: Mapping[str, Any],
        *,
        source_id: str,
        run_id: str | None,
    ) -> dict[str, Any]:
        """Project the housing attributes of one acquired feature revision (see ``layer_declaration``)."""
        layer = declaration["layer"]
        attributes = dict(declaration["attributes"])
        properties = dict(feature.get("properties") or {})
        provider = str(feature["provider"])
        collection = str(feature["collection"])
        source = {
            "kind": "feature-revision",
            "source_id": source_id,
            "provider": provider,
            "collection": collection,
            "native_id": feature["native_id"],
            "feature_id": feature["feature_id"],
            "feature_revision_id": feature["revision_id"],
            "geometry_id": feature.get("geometry_id"),
            "response_sha256": feature.get("response_sha256"),
            "run_id": run_id,
        }
        # The published outline, not the store's geometry id: an id that changes with unrelated attributes would
        # pose as a correction of the housing record.
        geometry_sha256 = digest(feature.get("geometry"))
        feature_columns = {
            "collection": collection,
            "feature_id": feature["feature_id"],
            "feature_revision_id": feature["revision_id"],
            "geometry_id": feature.get("geometry_id"),
        }

        def attribute(name: str, required: bool = True) -> Any:
            key = attributes.get(name)
            if not key:
                return None
            if key not in properties:
                if required:
                    raise HousingError(
                        "attribute_missing",
                        f"published attribute {key!r} ({name}) is missing",
                    )
                return None
            return properties[key]

        if layer == "land-value":
            zone_id = " ".join(str(attribute("zone_id") or "").split())
            if not zone_id:
                raise HousingError("attribute_missing", "the zone identifier is empty")
            value = published_value(
                attribute("land_value"),
                str(declaration.get("number_format") or "plain"),
            )
            if value["value"] is None:
                raise HousingError("value_unparseable", "the zone states no value")
            unit_declared = dict(declaration.get("unit") or {})
            unit = " ".join(
                str(
                    attribute("unit", required=False)
                    or unit_declared.get("published")
                    or ""
                ).split()
            )
            if unit not in UNITS:
                raise HousingError(
                    "unit_undeclared", f"unit {unit!r} is not a declared unit"
                )
            currency = (
                attribute("currency", required=False)
                or unit_declared.get("currency")
                or UNITS[unit]["currency"]
            )
            raw_date = attribute("valuation_date", required=False)
            valuation_date = day(raw_date)
            qualifiers = {
                name: properties.get(key)
                for name, key in sorted(
                    dict(declaration.get("qualifiers") or {}).items()
                )
                if key in properties
            }
            content = {
                "zone_id": zone_id,
                "valuation_date": valuation_date,
                "valuation_date_text": None if raw_date is None else str(raw_date),
                "valuation_date_basis": "published Stichtag"
                if valuation_date
                else "unknown (not published)",
                **value,
                "currency": currency,
                "unit": unit,
                "use_type": attribute("use", required=False),
                "qualifiers": qualifiers,
                "zone_name": attribute("zone_name", required=False),
                "geometry_sha256": geometry_sha256,
            }
            return self._revise(
                "land_value_revision",
                namespace,
                [source_id, provider, collection, zone_id, valuation_date],
                content,
                columns={
                    **feature_columns,
                    "zone_id": zone_id,
                    "valuation_date": valuation_date,
                },
                source=source,
                source_id=source_id,
                provider=provider,
                run_id=run_id,
            )
        if layer == "development-plan":
            plan_id = " ".join(str(attribute("plan_id") or "").split())
            key = plan_key(plan_id)
            if not key:
                raise HousingError("attribute_missing", "the plan identifier is empty")
            label = " ".join(str(attribute("stage") or "").split())
            stages = {
                " ".join(k.split()).casefold(): v
                for k, v in dict(declaration.get("stages") or {}).items()
            }
            stage = stages.get(label.casefold(), "unrecognized")
            date_attribute = dict(declaration.get("stage_dates") or {}).get(stage)
            raw_date = properties.get(date_attribute) if date_attribute else None
            stage_date = day(raw_date)
            references = []
            for attr, spec in sorted(
                dict(declaration.get("reference_attributes") or {}).items()
            ):
                text = " ".join(str(properties.get(attr) or "").split())
                if text:
                    references.append(
                        {
                            "scheme": spec["scheme"],
                            "identifier": text,
                            "relation": spec.get("relation") or "stage_decided_by",
                            "attribute": attr,
                            "stage": spec.get("stage"),
                        }
                    )
            content = {
                "plan_id": plan_id,
                "plan_label": attribute("plan_label", required=False),
                "district": attribute("district", required=False),
                "stage": stage,
                "stage_label": label or None,
                "stage_recognized": stage != "unrecognized",
                "stage_date": stage_date,
                "stage_date_text": None if raw_date is None else str(raw_date),
                "stage_date_basis": f"published attribute {date_attribute}"
                if stage_date
                else "unknown (not published)",
                "references": references,
                "geometry_sha256": geometry_sha256,
            }
            return self._revise(
                "plan_stage",
                namespace,
                [source_id, provider, collection, key, stage],
                content,
                columns={
                    **feature_columns,
                    "plan_key": key,
                    "stage": stage,
                    "stage_date": stage_date,
                },
                source=source,
                source_id=source_id,
                provider=provider,
                run_id=run_id,
            )
        if layer == "residential-area":
            category = " ".join(str(attribute("category") or "").split())
            if not category:
                raise HousingError("attribute_missing", "the category is empty")
            edition = dict(declaration["edition"])
            area_id = str(feature["native_id"])
            content = {
                "area_id": area_id,
                "category": category,
                "category_basis": "source-labelled category; not a value or score",
                "edition": {
                    "edition_id": edition["edition_id"],
                    "edition": edition.get("edition"),
                    "valid_from": day(edition.get("valid_from")),
                },
                "geometry_sha256": geometry_sha256,
            }
            return self._revise(
                "area_category",
                namespace,
                [source_id, provider, collection, edition["edition_id"], area_id],
                content,
                columns={
                    **feature_columns,
                    "area_id": area_id,
                    "edition_id": edition["edition_id"],
                    "category": category,
                },
                source=source,
                source_id=source_id,
                provider=provider,
                run_id=run_id,
            )
        raise HousingError("invalid_layer", f"unknown housing layer {layer!r}")

    # ------------------------------------------------------ tabular publications

    def _source_revision(
        self,
        namespace: str,
        header: Mapping[str, Any],
        *,
        source_id: str,
        run_id: str | None,
    ) -> dict[str, Any]:
        document = dict(header.get("document") or {})
        identity = [
            namespace,
            source_id,
            header["provider"],
            header["format"],
            document.get("url") or document.get("table") or document.get("label"),
            header.get("edition"),
            header["published_on"],
            header.get("published_at"),
            header["content_sha256"],
        ]
        revision_id = "housing-source:" + digest(identity)[:24]
        existing = self.conn.execute(
            "SELECT 1 FROM housing_source_revisions WHERE namespace=? AND source_revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if not existing:
            self.conn.execute(
                "INSERT INTO housing_source_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    revision_id,
                    source_id,
                    header["provider"],
                    header["kind"],
                    header["format"],
                    canonical(document),
                    header["published_on"],
                    header.get("published_at"),
                    header.get("publication_basis"),
                    canonical(header.get("edition")),
                    header["content_sha256"],
                    header["file_sha256"],
                    header.get("evidence_origin") or "live",
                    header.get("url"),
                    run_id,
                    self._sequence(),
                    self.now(),
                ],
            )
        return self.source_revision(namespace, revision_id)

    def apply_publication(
        self,
        namespace: str,
        header: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
        *,
        source_id: str,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one tabular publication (all-or-nothing) and its items; unchanged readings add nothing."""
        counts: dict[str, int] = {}
        self.conn.execute("BEGIN")
        try:
            revision = self._source_revision(
                namespace, header, source_id=source_id, run_id=run_id
            )
            source = {
                "kind": "publication",
                **{
                    k: revision[k]
                    for k in (
                        "source_revision_id",
                        "source_id",
                        "provider",
                        "published_on",
                        "publication_basis",
                        "evidence_origin",
                        "url",
                    )
                },
                "document_label": revision["document"].get("label"),
                "page": revision["document"].get("page"),
                "publication_url": revision["document"].get("publication_url"),
            }
            for state in self._apply_items(
                namespace, header, items, source=source, run_id=run_id
            ):
                counts[state] = counts.get(state, 0) + 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "status": "applied",
            "source_revision_id": revision["source_revision_id"],
            "states": counts,
        }

    def _apply_items(self, namespace, header, items, *, source, run_id) -> list[str]:
        kind, provider, source_id = (
            header["kind"],
            header["provider"],
            source["source_id"],
        )
        states = []
        if kind == "rent-index":
            edition = dict(header.get("edition") or {})
            if not edition.get("edition_id"):
                raise HousingError(
                    "invalid_publication", "a rent-index publication names its edition"
                )
            content = {
                **edition,
                "published_on": header["published_on"],
                "references": list(
                    dict(header.get("document") or {}).get("references") or []
                ),
            }
            states.append(
                self._revise(
                    "rent_index_edition",
                    namespace,
                    [source_id, provider, edition["edition_id"]],
                    content,
                    columns={
                        "edition_id": edition["edition_id"],
                        "valid_from": edition.get("valid_from"),
                        "published_on": header["published_on"],
                        "source_revision_id": source["source_revision_id"],
                    },
                    source=source,
                    source_id=source_id,
                    provider=provider,
                    run_id=run_id,
                )["state"]
            )
            for cell in items:
                states.append(
                    self._revise(
                        "rent_index_cell",
                        namespace,
                        [source_id, provider, edition["edition_id"], cell["cell_key"]],
                        dict(cell),
                        columns={
                            "edition_id": edition["edition_id"],
                            "cell_key": cell["cell_key"],
                            "source_revision_id": source["source_revision_id"],
                        },
                        source=source,
                        source_id=source_id,
                        provider=provider,
                        run_id=run_id,
                    )["state"]
                )
            return states
        if kind == "building-statistics":
            for item in items:
                area = dict(item["reporting_area"])
                entity = [
                    source_id,
                    provider,
                    item["statistic"],
                    area["scheme"],
                    area["code"],
                    item["period"],
                    item["measure"],
                    header["published_on"],
                ]
                states.append(
                    self._revise(
                        "building_statistic",
                        namespace,
                        entity,
                        {
                            **dict(item),
                            "vintage": header["published_on"],
                            "vintage_basis": header.get("publication_basis"),
                        },
                        columns={
                            "statistic": item["statistic"],
                            "area_scheme": area["scheme"],
                            "area_code": area["code"],
                            "period": item["period"],
                            "measure": item["measure"],
                            "vintage": header["published_on"],
                            "source_revision_id": source["source_revision_id"],
                        },
                        source=source,
                        source_id=source_id,
                        provider=provider,
                        run_id=run_id,
                    )["state"]
                )
            return states
        if kind == "housing-indicator":
            from services.ingest.common.series_model import SeriesRecord
            from src.ingestion.connectors.dataset.store import ObservationStore

            observations = ObservationStore(self.conn)
            for item in items:
                record = SeriesRecord.from_dict(dict(item))
                stored = self.conn.execute(
                    "SELECT period, value FROM dataset_observations WHERE series_id=? AND as_of=? ORDER BY period",
                    [record.series_id, record.as_of],
                ).fetchall()
                incoming = sorted((o.period, o.value) for o in record.observations)
                if stored and [(p, v) for p, v in stored] != incoming:
                    # The same table stamp with other values: a silent change the publisher did not date.
                    raise HousingError(
                        "vintage_conflict",
                        "a GENESIS table republished other values under an unchanged Updated stamp",
                        series_id=record.series_id,
                    )
                header_row = self.conn.execute(
                    "SELECT as_of FROM dataset_series WHERE series_id=?",
                    [record.series_id],
                ).fetchone()
                if header_row is None or int(header_row[0]) <= record.as_of:
                    # The header follows the newest table stamp, whatever the arrival order.
                    observations.upsert(record)
                elif not stored:
                    for observation in record.observations:
                        self.conn.execute(
                            "INSERT INTO dataset_observations (series_id, period, as_of, value) VALUES (?,?,?,?)",
                            [
                                record.series_id,
                                observation.period,
                                record.as_of,
                                observation.value,
                            ],
                        )
                metadata = dict(record.metadata)
                content = {
                    "series_id": record.series_id,
                    "title": record.title,
                    "unit": record.unit,
                    "unit_published": metadata.get("published_unit"),
                    "measure": metadata.get("measure"),
                    "table": metadata.get("table"),
                    "value_code": metadata.get("value_code"),
                    "geography": record.geography,
                    "geography_label": metadata.get("geography_label"),
                    "dimensions": metadata.get("dimensions") or {},
                    "periods": [o.period for o in record.observations],
                    "value_texts": metadata.get("value_texts") or {},
                    "signs": metadata.get("signs") or {},
                    "as_of_ms": record.as_of,
                    "vintage_basis": metadata.get("vintage_basis"),
                    "published_on": metadata.get("published_on"),
                    "published_at": metadata.get("published_at"),
                    "license": record.license,
                    "values_in": "dataset_observations (ObservationStore) at this series_id and as_of",
                }
                states.append(
                    self._revise(
                        "indicator_vintage",
                        namespace,
                        [source_id, provider, record.series_id, record.as_of],
                        content,
                        columns={
                            "series_id": record.series_id,
                            "geography": record.geography or "",
                            "measure": metadata.get("measure") or "",
                            "as_of_ms": record.as_of,
                            "published_on": metadata.get("published_on")
                            or header["published_on"],
                            "source_revision_id": source["source_revision_id"],
                        },
                        source=source,
                        source_id=source_id,
                        provider=provider,
                        run_id=run_id,
                    )["state"]
                )
            return states
        raise HousingError(
            "invalid_publication", f"unknown housing publication kind {kind!r}"
        )

    # ------------------------------------------------------------------- reads

    def source_revision(
        self, namespace: str, source_revision_id: str
    ) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT source_revision_id, source_id, provider, kind, format, document_json, published_on, published_at, "
            "publication_basis, edition_json, content_sha256, file_sha256, evidence_origin, url, run_id, "
            "recorded_at_ms FROM housing_source_revisions WHERE namespace=? AND source_revision_id=?",
            [namespace, source_revision_id],
        ).fetchone()
        if row is None:
            raise HousingError(
                "not_found", "source revision is not visible in this namespace"
            )
        return {
            "source_revision_id": row[0],
            "source_id": row[1],
            "provider": row[2],
            "kind": row[3],
            "format": row[4],
            "document": _load(row[5], {}),
            "published_on": row[6],
            "published_at": row[7],
            "publication_basis": row[8],
            "edition": _load(row[9], None),
            "content_sha256": row[10],
            "file_sha256": row[11],
            "evidence_origin": row[12],
            "url": row[13],
            "run_id": row[14],
            "recorded_at_ms": row[15],
        }

    def _rows(
        self,
        record_type: str,
        namespace: str,
        where: str = "",
        params: Sequence[Any] = (),
        *,
        current_only: bool = True,
    ) -> list[dict[str, Any]]:
        table = _TABLES[record_type]
        if not table_exists(self.conn, table):
            return []
        rows = self.conn.execute(
            f"SELECT record_id, entity_key, revision_no, supersedes, change, content_json, source_json, source_id, "
            f"provider, run_id, sequence, recorded_at_ms FROM {table} t WHERE namespace=? {where} "
            "ORDER BY entity_key, revision_no",
            [namespace, *params],
        ).fetchall()
        out, latest = [], {}
        for row in rows:
            try:
                item = {
                    "contract": CONTRACT,
                    "record_type": record_type,
                    "record_id": row[0],
                    "entity_key": row[1],
                    "revision_no": int(row[2]),
                    "supersedes": row[3],
                    "change": row[4],
                    **json.loads(row[5]),
                    "source_revision": json.loads(row[6]),
                    "source_id": row[7],
                    "provider": row[8],
                    "run_id": row[9],
                    "sequence": int(row[10]),
                    "recorded_at_ms": int(row[11]),
                }
                if item["source_revision"].get("kind") == "feature-revision":
                    # The geometry of the feature revision this reading was first recorded from (never copied).
                    item["geometry_id"] = item["source_revision"].get("geometry_id")
            except (TypeError, ValueError) as exc:
                # One unreadable row is reported, never allowed to hide the others.
                item = {
                    "record_id": row[0],
                    "entity_key": row[1],
                    "revision_no": row[2],
                    "invalid": True,
                    "reason": f"unreadable record: {exc}",
                }
            out.append(item)
            latest[row[1]] = row[0]
        if current_only:
            return [
                item
                for item in out
                if latest.get(item["entity_key"]) == item["record_id"]
            ]
        return out

    def records(
        self,
        record_type: str,
        namespace: str,
        *,
        current_only: bool = True,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        """Records of one type, filtered on their indexed columns; corrections kept unless ``current_only``."""
        if record_type not in _TABLES:
            raise HousingError(
                "invalid_record_type", f"record type is one of {sorted(_TABLES)}"
            )
        clauses, params = [], []
        for column, value in sorted(filters.items()):
            if value is None:
                continue
            if not column.isidentifier():
                raise HousingError("invalid_filter", "unknown filter")
            clauses.append(f"AND t.{column}=?")
            params.append(value)
        try:
            return self._rows(
                record_type,
                namespace,
                " ".join(clauses),
                params,
                current_only=current_only,
            )
        except Exception as exc:  # noqa: BLE001 - an unknown column is a caller error, reported as such
            if "not found" in str(exc).lower() or "binder" in str(exc).lower():
                raise HousingError(
                    "invalid_filter", f"unknown filter for {record_type}"
                ) from exc
            raise

    def record(self, namespace: str, record_id: str) -> dict[str, Any]:
        for record_type, table in _TABLES.items():
            if not table_exists(self.conn, table):
                continue
            row = self.conn.execute(
                f"SELECT entity_key FROM {table} WHERE namespace=? AND record_id=?",
                [namespace, record_id],
            ).fetchone()
            if row:
                history = self._rows(
                    record_type,
                    namespace,
                    "AND t.entity_key=?",
                    [row[0]],
                    current_only=False,
                )
                item = next(h for h in history if h["record_id"] == record_id)
                return {
                    **item,
                    "current": history[-1]["record_id"] == record_id,
                    "history": [h["record_id"] for h in history],
                }
        raise HousingError(
            "not_found", "housing record is not visible in this namespace"
        )

    def land_value_history(
        self,
        namespace: str,
        *,
        zone_id: str | None = None,
        feature_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Every valuation date and source of one zone, side by side, oldest valuation date first."""
        rows = self.records(
            "land_value_revision", namespace, zone_id=zone_id, feature_id=feature_id
        )
        return sorted(
            rows,
            key=lambda r: (r.get("valuation_date") or "", r.get("source_id") or ""),
        )

    def plan_history(self, namespace: str, plan_id: str) -> list[dict[str, Any]]:
        rows = self.records("plan_stage", namespace, plan_key=plan_key(plan_id))
        order = {stage: i for i, stage in enumerate(PLAN_STAGES)}
        return sorted(
            rows,
            key=lambda r: (
                r.get("stage_date") or "9999",
                order.get(r.get("stage"), 99),
                r.get("sequence", 0),
            ),
        )

    def editions(self, namespace: str) -> list[dict[str, Any]]:
        return sorted(
            self.records("rent_index_edition", namespace),
            key=lambda r: (r.get("valid_from") or "", r.get("edition_id") or ""),
        )

    def edition(self, namespace: str, edition_id: str) -> dict[str, Any]:
        found = self.records("rent_index_edition", namespace, edition_id=edition_id)
        if not found:
            raise HousingError("not_found", "rent-index edition is not recorded")
        cells = sorted(
            self.records("rent_index_cell", namespace, edition_id=edition_id),
            key=lambda c: c.get("cell_key") or "",
        )
        return {"editions": found, "cells": cells, "note": NO_SURFACE}

    def statistics(
        self,
        namespace: str,
        *,
        scheme: str | None = None,
        code: str | None = None,
        statistic: str | None = None,
        measure: str | None = None,
    ) -> list[dict[str, Any]]:
        code = reporting_area(scheme, code) if scheme and code else code
        return sorted(
            self.records(
                "building_statistic",
                namespace,
                area_scheme=scheme,
                area_code=code,
                statistic=statistic,
                measure=measure,
            ),
            key=lambda r: (
                r.get("period") or "",
                r.get("measure") or "",
                r.get("vintage") or "",
                r.get("source_id") or "",
            ),
        )

    def indicator_vintages(
        self,
        namespace: str,
        *,
        geography: str | None = None,
        series_id: str | None = None,
    ) -> list[dict[str, Any]]:
        return sorted(
            self.records(
                "indicator_vintage", namespace, geography=geography, series_id=series_id
            ),
            key=lambda r: (r.get("series_id") or "", r.get("as_of_ms") or 0),
        )

    def indicator_values(
        self, namespace: str, series_id: str, *, as_of_ms: int
    ) -> dict[str, Any]:
        """A series' values from the ObservationStore at the vintage released on or before ``as_of_ms``."""
        from src.ingestion.connectors.dataset.store import ObservationStore

        vintages = [
            v
            for v in self.indicator_vintages(namespace, series_id=series_id)
            if int(v["as_of_ms"]) <= int(as_of_ms)
        ]
        if not vintages:
            return {
                "series_id": series_id,
                "status": "historical_vintage_unavailable",
                "observations": [],
            }
        chosen = max(vintages, key=lambda v: int(v["as_of_ms"]))
        observations = ObservationStore(self.conn).get_observations(
            series_id, as_of=int(chosen["as_of_ms"])
        )
        return {
            "series_id": series_id,
            "status": "selected",
            "vintage": chosen,
            "selection_basis": "the table vintage (Updated stamp) released on or before the requested time",
            "observations": [
                {
                    "period": o.period,
                    "value": o.value,
                    "value_text": chosen["value_texts"].get(o.period),
                    "sign": chosen["signs"].get(o.period),
                }
                for o in observations
            ],
        }

    def outcomes(
        self, namespace: str, *, run_id: str | None = None, source_id: str | None = None
    ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "housing_projection_outcomes"):
            return []
        rows = self.conn.execute(
            "SELECT run_id, source_id, item_key, outcome, reason, detail_json FROM housing_projection_outcomes "
            "WHERE namespace=? AND (? IS NULL OR run_id=?) AND (? IS NULL OR source_id=?) "
            "ORDER BY run_id, source_id, item_key",
            [namespace, run_id, run_id, source_id, source_id],
        ).fetchall()
        return [
            dict(
                zip(("run_id", "source_id", "item_key", "outcome", "reason"), r[:5]),
                detail=_load(r[5], {}),
            )
            for r in rows
        ]


class HousingProjector:
    """Source-pack runtime projector for ``noesis-housing-record-v1``.

    WFS layers are first projected into the Geospatial feature store by
    :class:`src.kb.geospatial_features.GeospatialFeatureProjector` (the existing
    WFS source path); their housing attributes are then read from the feature
    revision each record produced. Tabular publications are recorded whole.
    """

    def __init__(self, conn: Any) -> None:
        from src.kb.geospatial_features import GeospatialFeatureProjector

        self.conn = conn
        self.store = HousingStore(conn)
        self.features = GeospatialFeatureProjector(conn)

    def project_page(
        self,
        *,
        run_id,
        manifest,
        source,
        records,
        documents,
        page_receipt,
        principal_id,
    ):
        if source.get("connector") in {"wfs", "geojson"}:
            return self._project_features(
                run_id=run_id,
                manifest=manifest,
                source=source,
                records=records,
                documents=documents,
                page_receipt=page_receipt,
                principal_id=principal_id,
            )
        declared = tabular_declaration(source)
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = (
                dict(item.get("housing_publication") or {}),
                item.get("housing_item"),
            )
            if not header or not isinstance(body, Mapping):
                raise HousingError(
                    "invalid_record", "page record is not a housing publication item"
                )
            groups.setdefault(
                header["file_sha256"] + canonical(header.get("document")), (header, [])
            )[1].append(dict(body))
        return [
            self.store.apply_publication(
                declared["namespace"],
                header,
                items,
                source_id=source["source_id"],
                run_id=run_id,
            )
            for header, items in groups.values()
        ]

    def _project_features(
        self,
        *,
        run_id,
        manifest,
        source,
        records,
        documents,
        page_receipt,
        principal_id,
    ):
        from src.ingestion.geojson_features import feature_key

        declaration = layer_declaration(source)
        counts = self.features.project_page(
            run_id=run_id,
            manifest=manifest,
            source=source,
            records=records,
            documents=documents,
            page_receipt=page_receipt,
            principal_id=principal_id,
        )
        namespace = declaration["namespace"]
        geo_namespace = str(
            dict(source.get("geospatial") or {}).get("namespace") or DEFAULT_NAMESPACE
        )
        states: dict[str, int] = {}
        self.conn.execute("BEGIN")
        try:
            for record in records:
                rejection = record.get("rejection")
                if rejection:
                    rejection = dict(rejection)
                    self.store.record_outcome(
                        namespace,
                        run_id,
                        source["source_id"],
                        f"{rejection.get('collection')}:{rejection.get('feature_index')}",
                        "not_projected",
                        f"feature_rejected:{rejection.get('code')}",
                        {"native_id": rejection.get("native_id")},
                    )
                    states["feature_rejected"] = states.get("feature_rejected", 0) + 1
                    continue
                feature = dict(record["feature"])
                feature_id = feature_key(
                    str(feature["provider"]),
                    str(feature["collection"]),
                    str(feature["native_id"]),
                )
                row = self.conn.execute(
                    "SELECT o.state, r.revision_id, r.geometry_id, r.lifecycle, r.properties_json FROM "
                    "geospatial_feature_observations o LEFT JOIN geospatial_feature_current c ON "
                    "c.feature_id=o.feature_id LEFT JOIN geospatial_feature_revisions r ON "
                    "r.revision_id=c.revision_id WHERE o.run_id=? AND o.source_id=? AND o.feature_id=? "
                    "AND o.namespace=?",
                    [run_id, source["source_id"], feature_id, geo_namespace],
                ).fetchone()
                if (
                    row is None
                    or row[0] not in {"projected", "unchanged"}
                    or row[3] != "active"
                ):
                    reason = (
                        "geometry_not_projected"
                        if row is None or row[0] == "failed"
                        else f"feature_{row[3]}"
                    )
                    self.store.record_outcome(
                        namespace,
                        run_id,
                        source["source_id"],
                        feature_id,
                        "not_projected",
                        reason,
                        {
                            "native_id": feature["native_id"],
                            "observation": None if row is None else row[0],
                        },
                    )
                    states[reason] = states.get(reason, 0) + 1
                    continue
                try:
                    outcome = self.store.apply_feature(
                        namespace,
                        declaration,
                        {
                            **feature,
                            "feature_id": feature_id,
                            "revision_id": row[1],
                            "geometry_id": row[2],
                            "properties": _load(row[4], {}),
                            "response_sha256": dict(
                                record.get("feature_page") or {}
                            ).get("response_sha256"),
                        },
                        source_id=source["source_id"],
                        run_id=run_id,
                    )
                    state = outcome["state"]
                except HousingError as exc:
                    self.store.record_outcome(
                        namespace,
                        run_id,
                        source["source_id"],
                        feature_id,
                        "not_projected",
                        exc.code,
                        {"native_id": feature["native_id"], "message": str(exc)},
                    )
                    state = exc.code
                states[state] = states.get(state, 0) + 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"features": counts, "housing": states}

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        if source.get("connector") in {"wfs", "geojson"}:
            snapshot = self.features.finish_source(
                run_id=run_id,
                manifest=manifest,
                source=source,
                status=status,
                principal_id=principal_id,
            )
            declaration = layer_declaration(source)
            return {
                **snapshot,
                "housing_outcomes": self.store.outcomes(
                    declaration["namespace"],
                    run_id=run_id,
                    source_id=source["source_id"],
                ),
            }
        declared = tabular_declaration(source)
        row = self.conn.execute(
            "SELECT source_revision_id, published_on FROM housing_source_revisions WHERE namespace=? AND "
            "source_id=? ORDER BY published_on DESC, sequence DESC LIMIT 1",
            [declared["namespace"], source["source_id"]],
        ).fetchone()
        return {
            "status": status,
            "latest_source_revision_id": row[0] if row else None,
            "latest_published_on": row[1] if row else None,
        }


def register_schemas(
    conn: Any, *, principal_id: str, scopes: Any
) -> list[dict[str, Any]]:
    """Register the housing record contract as a schema module in the shared registry."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = (
        Path(__file__).resolve().parents[2]
        / "contracts/schemas/jsonschema"
        / f"{CONTRACT}.json"
    )
    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "housing-record",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": json.loads(path.read_text()),
        "owner": "geospatial.housing",
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {
            "kind": "imported",
            "source": f"contracts/schemas/jsonschema/{CONTRACT}.json",
        },
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [
        SchemaRegistry(conn).register(
            definition,
            "housing-schema:housing-record:1.0.0",
            principal_id=principal_id,
            scopes=scopes,
        )
    ]


def readiness(conn: Any) -> dict[str, Any]:
    store = HousingStore(conn, initialize=False)
    ready = store.ready()
    counts = {}
    for record_type, table in _TABLES.items():
        counts[record_type] = (
            int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            if table_exists(conn, table)
            else 0
        )
    return {
        "feature": "housing",
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "layers": list(LAYERS),
        "records": counts,
        "providers": {
            provider: {
                k: contract[k]
                for k in ("delivers", "access_decision", "reason", "primary_publisher")
            }
            for provider, contract in PROVIDER_CONTRACTS.items()
        },
        "review_boundary": REVIEW_BOUNDARY,
        "value_surface": NO_SURFACE,
        "note": "offline fixture evidence and live evidence are reported per publication (evidence_origin); no "
        "provider is live until a dated run verifies it",
    }
