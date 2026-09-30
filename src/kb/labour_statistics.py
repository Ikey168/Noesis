"""Labour indicators, definitions, observations and release vintages in the Economics series storage (#2219, LB02).

Records follow contract ``noesis-labour-statistics-record-v1``. Numeric observations and their vintages live in the
existing Economics series storage - :func:`src.domains.economic.model.register_series` writes the indicator
(``economic_indicators``), the provider series mapping (``economic_series_map``), the release/retrieval clocks and
``revision_of`` (``economic_vintages``), the values per vintage (``dataset_observations`` through the dataset
``ObservationStore``) and the bitemporal ledger entries. No new series store is introduced; this module adds only
the labour-specific metadata the generic store has no place for:

* **release** - one acquired publication (an SDMX-CSV response or a BLS series response) with its release clock
  (Eurostat ``LAST UPDATE``, a declared release date or the retrieval time) and basis label, digests, the dataflow
  version the response states and the evidence origin. Re-acquiring an unchanged file adds nothing.
* **indicator (series)** - source, native series/dataflow key, concept and measure, unit and unit multiplier,
  frequency, seasonal adjustment (``NSA``, ``SA``, ``trend``), definition basis (ILO harmonised, OECD harmonised,
  EU-LFS or national, with reference), estimate type (an ILO modelled estimate and a nationally reported series
  are different series), place code and sector/occupation codes with classification version.
* **definition** - the indicator's definition as the source states it (basis, age bounds, coverage, methodology
  notes, the survey and note attributes), revisioned: a changed definition is a new definition revision.
* **vintage** - one release of a series, pointing at its ``economic_vintages`` row, with content digest, the
  definition revision and dataflow version in force and the changes against the previous vintage (new periods,
  revised values, benchmark revision, definition or metadata change). An unchanged re-publication adds no vintage;
  changed values without a new release clock are refused (``vintage_conflict``).
* **observation** - place, period, value as published (exact text), status (``reported``, ``confidential``,
  ``not_published``), unit multiplier, flags and footnotes verbatim; the numeric value is read back from
  ``dataset_observations``.
* **comparability note** - the :mod:`src.kb.demographics_comparability` structure (typed relation, statement,
  cited definitions and source revisions, proposed/accepted/rejected/reverted), attached to series or definitions
  and optionally to a period range; source-stated breaks and survey notes are recorded as ``source-stated`` notes.

Nothing here nowcasts, forecasts, fills a missing period, blends sources or re-harmonises a definition.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any

from src.ingestion.labour_sources import (
    CONCEPTS,
    DEFINITION_BASES,
    EXCLUSIONS,
    FORMATS,
    LIVE_VERIFICATION,
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    SEASONAL,
    STATUSES,
)

CONTRACT = "noesis-labour-statistics-record-v1"
ANSWER_CONTRACT = "noesis-labour-statistics-answer-v1"
COMPARABILITY_CONTRACT = "noesis-labour-comparability-v1"
READ_SCOPE = "knowledge:labour:read"
WRITE_SCOPE = "knowledge:labour:write"
REVIEW_SCOPE = "knowledge:labour:review"
DEFAULT_NAMESPACE = "global"
DOMAIN = "economics"
FEATURE = "labour-statistics"
RECORD_TYPES = ("release", "indicator", "definition", "vintage", "observation", "comparability_note")
# The Economics model's seasonal-adjustment vocabulary for each labour value (trend-cycle series are adjusted).
ECONOMIC_SEASONAL = {"NSA": "not_adjusted", "SA": "adjusted", "trend": "adjusted"}
RELATIONS = (
    "different_definition_basis",
    "different_age_bounds",
    "different_survey_coverage",
    "different_seasonal_adjustment",
    "different_estimate_type",
    "break_in_series",
    "source_note",
    "not_comparable",
)
SINGLE_SIDED = ("break_in_series", "source_note")
ACTIVE_STATES = ("source-stated", "proposed", "accepted")
CHANGE_KINDS = ("new_period", "revised_value", "benchmark_revision", "definition_change")
# Keys that would carry a derived, estimated, blended or forecast number.
FORBIDDEN_KEYS = frozenset(
    {
        "nowcast",
        "nowcasted",
        "forecast",
        "forecast_value",
        "projection",
        "predicted",
        "estimated_value",
        "imputed",
        "imputed_value",
        "interpolated",
        "gap_filled",
        "filled_value",
        "blended",
        "blended_value",
        "combined_value",
        "average_value",
        "harmonised_value",
        "rebased_value",
        "derived_value",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS labour_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_json TEXT NOT NULL, published_on TEXT, published_at TEXT, release_basis TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, release_label TEXT, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  item_count INTEGER NOT NULL, structure_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, url TEXT,
  sequence INTEGER NOT NULL, run_id TEXT NOT NULL, recorded_by TEXT, retrieved_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS labour_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, native_key TEXT NOT NULL,
  dataflow_json TEXT NOT NULL, indicator_json TEXT NOT NULL, concept TEXT NOT NULL, measure TEXT NOT NULL,
  estimate_type TEXT NOT NULL, definition_basis TEXT NOT NULL, definition_key TEXT NOT NULL,
  seasonal_adjustment TEXT NOT NULL, frequency TEXT NOT NULL, unit_json TEXT NOT NULL, unit_multiplier TEXT,
  area_scheme TEXT NOT NULL, area_code TEXT NOT NULL, area_json TEXT NOT NULL, sector_json TEXT,
  occupation_json TEXT, dimensions_json TEXT NOT NULL, references_json TEXT NOT NULL, denominator_json TEXT,
  revision_window INTEGER, economic_indicator_id TEXT NOT NULL, first_release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS labour_definitions (
  namespace TEXT NOT NULL, definition_id TEXT NOT NULL, definition_key TEXT NOT NULL, revision INTEGER NOT NULL,
  provider TEXT NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL, release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, definition_id)
);
CREATE TABLE IF NOT EXISTS labour_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  economic_as_of BIGINT NOT NULL, release_at_ms BIGINT NOT NULL, release_at_basis TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, content_hash TEXT NOT NULL, definition_id TEXT NOT NULL,
  dataflow_version TEXT, source_notes_json TEXT NOT NULL, changes_json TEXT NOT NULL, sequence INTEGER NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS labour_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  status TEXT NOT NULL, unit_multiplier TEXT, flags_json TEXT NOT NULL, attributes_json TEXT NOT NULL,
  footnotes_json TEXT NOT NULL, PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS labour_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  status TEXT NOT NULL, PRIMARY KEY(namespace, release_id, series_id)
);
CREATE TABLE IF NOT EXISTS labour_comparability (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, pair_key TEXT NOT NULL, left_json TEXT NOT NULL,
  right_json TEXT, relation TEXT NOT NULL, statement TEXT NOT NULL, periods_json TEXT NOT NULL,
  cited_json TEXT NOT NULL, origin TEXT NOT NULL, state TEXT NOT NULL, history_json TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, note_id)
);
"""


class LabourError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise LabourError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise LabourError("unauthorized", f"{required} is required for this part of the answer")


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry a derived, filled, blended or forecast number."""
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


def release_ms(published_on: str | None, published_at: str | None, retrieved_ms: int) -> int:
    """The release clock (UTC epoch ms): the stated instant, a date's midnight, else the retrieval time."""
    if published_at:
        stamp = datetime.fromisoformat(str(published_at).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000)
    if published_on:
        return int(
            datetime.combine(date.fromisoformat(published_on), datetime.min.time(), tzinfo=timezone.utc).timestamp()
            * 1000
        )
    return int(retrieved_ms)


def iso_from_ms(value: int | None) -> str | None:
    return None if value is None else datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()


def _selected_features(conn: Any) -> list[str]:
    try:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_name IN "
                "('composition_authority', 'composition_active', 'composition_generations', 'composition_plans')"
            ).fetchall()
        }
        if len(tables) < 4:
            return []
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='economics'").fetchone()
        if not managed or managed[0] != "composition":
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get("economics") or [])


def feature_enabled(conn: Any) -> bool:
    """Whether the Economics bundle's optional ``labour-statistics`` feature is selected in the active plan."""
    return FEATURE in _selected_features(conn)


def _check_item(item: Mapping[str, Any]) -> None:
    if forbidden_keys(dict(item)):
        raise LabourError("invalid_release", "published records carry no derived, filled or forecast value")
    for key in ("provider", "native_key", "indicator", "definition", "unit", "area", "frequency"):
        if not item.get(key):
            raise LabourError("invalid_release", f"a labour series states its {key}")
    if dict(item["indicator"]).get("concept") not in CONCEPTS:
        raise LabourError("invalid_release", "unknown indicator concept")
    if item.get("seasonal_adjustment") not in SEASONAL:
        raise LabourError("invalid_release", "a labour series states its seasonal adjustment (NSA, SA or trend)")
    if dict(item["definition"]).get("basis") not in DEFINITION_BASES:
        raise LabourError("invalid_release", "a labour series states its definition basis")
    periods = set()
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise LabourError("invalid_release", "each observation states its status")
        if obs.get("status") != "reported" and obs.get("value") is not None:
            raise LabourError("invalid_release", "a confidential or unpublished observation carries no value")
        if obs["period"] in periods:
            raise LabourError("invalid_release", "a series states a period twice")
        periods.add(obs["period"])


def period_window(periods: Sequence[str], window: int | None) -> set[str]:
    """The most recent ``window`` periods (the provider's regular revision window); every period without one."""
    ordered = sorted(periods)
    if not window:
        return set(ordered)
    return set(ordered[-int(window):])


class LabourStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "labour_vintages")

    # ------------------------------------------------------------------ writes

    def _release(self, namespace, header, *, source_id, run_id, retrieved, recorded_by):
        provider, fmt = str(header.get("provider") or ""), header.get("format")
        if fmt not in FORMATS or FORMATS[fmt]["provider"] != provider:
            raise LabourError("invalid_release", "release names a known provider and format")
        document = dict(header.get("document") or {})
        release_id = "lb-release:" + digest([namespace, provider, source_id, header["file_sha256"], document])[:24]
        if self.conn.execute(
            "SELECT 1 FROM labour_releases WHERE namespace=? AND release_id=?", [namespace, release_id]
        ).fetchone():
            return release_id, False
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM labour_releases WHERE namespace=? AND provider=?",
            [namespace, provider],
        ).fetchone()[0]
        origin = header.get("evidence_origin")
        origin = origin if origin in {"fixture", "operator"} else "live"
        self.conn.execute(
            "INSERT INTO labour_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                release_id,
                provider,
                source_id,
                fmt,
                canonical(document),
                header.get("published_on"),
                header.get("published_at"),
                str(header.get("release_basis") or "retrieval_time"),
                release_ms(header.get("published_on"), header.get("published_at"), retrieved),
                header.get("release_label"),
                header["file_sha256"],
                header.get("content_sha256") or "",
                int(header.get("item_count") or 0),
                canonical(header.get("structure") or {}),
                origin,
                header.get("url"),
                int(sequence) + 1,
                run_id,
                recorded_by,
                retrieved,
            ],
        )
        return release_id, True

    def apply_release(
        self,
        namespace: str,
        header: Mapping[str, Any],
        items: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
        retrieved_at_ms: int | None = None,
        recorded_by: str | None = None,
    ) -> dict[str, Any]:
        """Record one publication: a vintage per series whose content or definition changed; idempotent by file."""
        if int(header.get("item_count", -1)) != len(items):
            raise LabourError("incomplete_release", "a release carries every item it states")
        for item in items:
            _check_item(item)
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        clock = release_ms(header.get("published_on"), header.get("published_at"), retrieved)
        if clock > retrieved:
            raise LabourError("invalid_release", "a release cannot be dated after its retrieval")
        counts = {"series": 0, "vintages": 0, "unchanged_vintages": 0, "definitions": 0, "source_notes": 0}
        self.conn.execute("BEGIN")
        try:
            release_id, created = self._release(
                namespace, header, source_id=source_id, run_id=run_id, retrieved=retrieved, recorded_by=recorded_by
            )
            if not created:
                self.conn.execute("COMMIT")
                return {"release_id": release_id, "status": "unchanged", **counts}
            basis = str(header.get("release_basis") or "retrieval_time")
            structure = dict(header.get("structure") or {})
            declared_kind = dict(dict(header.get("document") or {}).get("release") or {}).get("kind")
            vintage_ids, seen = [], set()
            for item in items:
                series_id, new_series = self._series(namespace, item, release_id)
                if series_id in seen:
                    raise LabourError("invalid_release", "a release states the same series twice")
                seen.add(series_id)
                counts["series"] += int(new_series)
                definition_id, new_definition = self._definition(namespace, item, release_id)
                counts["definitions"] += int(new_definition)
                vintage_id, status = self._vintage(
                    namespace, series_id, item, header, release_id, clock, basis, retrieved, definition_id,
                    structure.get("dataflow_version"), declared_kind,
                )
                self.conn.execute(
                    "INSERT INTO labour_release_members VALUES (?,?,?,?,?)",
                    [namespace, release_id, series_id, vintage_id, status],
                )
                if status == "new":
                    vintage_ids.append(vintage_id)
                    counts["vintages"] += 1
                    counts["source_notes"] += self._source_notes(namespace, series_id, item, release_id)
                else:
                    counts["unchanged_vintages"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "release_id": release_id,
            "status": "applied",
            "published_on": header.get("published_on"),
            "release_basis": basis,
            "vintage_ids": vintage_ids,
            **counts,
        }

    @staticmethod
    def series_key(item: Mapping[str, Any]) -> list[Any]:
        """Source, native key, dataflow (without version), concept, measure, estimate type, definition basis,
        seasonal adjustment, frequency and unit: an ILO modelled estimate and a national series never share a key."""
        dataflow = dict(item.get("dataflow") or {})
        return [
            item["provider"],
            str(item["native_key"]),
            str(dataflow.get("reference") or "").rsplit(",", 1)[0] if item["provider"] in {"ilostat", "oecd"}
            else str(dataflow.get("reference") or ""),
            item["indicator"]["concept"],
            item["indicator"]["measure"],
            item["estimate_type"],
            item["definition"]["basis"],
            item["seasonal_adjustment"],
            item["frequency"],
            dict(item["unit"]).get("code") or dict(item["unit"]).get("label"),
        ]

    @staticmethod
    def definition_key(item: Mapping[str, Any]) -> str:
        definition = dict(item["definition"])
        return "lb-definition:" + digest(
            [item["provider"], definition.get("indicator_code"), definition["basis"], item["estimate_type"],
             item["indicator"]["concept"], item["indicator"]["measure"]]
        )[:24]

    def _series(self, namespace, item, release_id):
        series_id = "lb-series:" + digest([namespace, *self.series_key(item)])[:24]
        if self.conn.execute(
            "SELECT 1 FROM labour_series WHERE namespace=? AND series_id=?", [namespace, series_id]
        ).fetchone():
            return series_id, False
        self.conn.execute(
            "INSERT INTO labour_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                series_id,
                item["provider"],
                str(item["native_key"]),
                canonical(dict(item.get("dataflow") or {})),
                canonical(dict(item["indicator"])),
                item["indicator"]["concept"],
                item["indicator"]["measure"],
                item["estimate_type"],
                item["definition"]["basis"],
                self.definition_key(item),
                item["seasonal_adjustment"],
                item["frequency"],
                canonical(dict(item["unit"])),
                item.get("unit_multiplier"),
                item["area"]["scheme"],
                str(item["area"]["code"]),
                canonical(dict(item["area"])),
                None if not item.get("sector") else canonical(dict(item["sector"])),
                None if not item.get("occupation") else canonical(dict(item["occupation"])),
                canonical(dict(item.get("dimensions") or {})),
                canonical(list(item.get("references") or [])),
                None if not item.get("denominator") else canonical(dict(item["denominator"])),
                None if item.get("revision_window_periods") is None else int(item["revision_window_periods"]),
                "labour-indicator:" + series_id.split(":", 1)[1],
                release_id,
                self.now(),
            ],
        )
        return series_id, True

    def _definition(self, namespace, item, release_id):
        key = self.definition_key(item)
        content = {
            **dict(item["definition"]),
            "source_notes": [
                {k: n.get(k) for k in ("kind", "attribute", "value")}
                for n in item.get("source_notes") or []
                if n.get("kind") != "break"
            ],
            "references": list(item.get("references") or []),
        }
        content_hash = digest(content)
        latest = self.conn.execute(
            "SELECT definition_id, content_hash, revision FROM labour_definitions WHERE namespace=? AND "
            "definition_key=? ORDER BY revision DESC LIMIT 1",
            [namespace, key],
        ).fetchone()
        if latest and latest[1] == content_hash:
            return latest[0], False
        existing = self.conn.execute(
            "SELECT definition_id FROM labour_definitions WHERE namespace=? AND definition_key=? AND content_hash=?",
            [namespace, key, content_hash],
        ).fetchone()
        if existing:
            return existing[0], False
        revision = 1 if latest is None else int(latest[2]) + 1
        definition_id = f"{key}@{revision}"
        self.conn.execute(
            "INSERT INTO labour_definitions VALUES (?,?,?,?,?,?,?,?,?)",
            [namespace, definition_id, key, revision, item["provider"], canonical(content), content_hash, release_id,
             self.now()],
        )
        return definition_id, True

    @staticmethod
    def _content(observations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {k: o.get(k) for k in ("period", "value_text", "value", "status", "flags", "footnotes", "unit_multiplier")}
            for o in sorted(observations, key=lambda o: o["period"])
        ]

    def _vintage(self, namespace, series_id, item, header, release_id, clock, basis, retrieved, definition_id,
                 dataflow_version, declared_kind):
        from services.ingest.common.series_model import SeriesRecord
        from src.domains.economic.model import EconomicModelError, register_series

        observations = list(item.get("observations") or [])
        content = self._content(observations)
        content_hash = digest(content)
        previous = self.vintage_rows(namespace, series_id)
        same_clock = [v for v in previous if v["release_at_ms"] == clock]
        if same_clock:
            if same_clock[0]["content_hash"] != content_hash:
                raise LabourError(
                    "vintage_conflict",
                    "the publication changed values without a new release time; the stored vintage is kept",
                    series_id=series_id,
                )
            return same_clock[0]["vintage_id"], "unchanged"
        prior = previous[-1] if previous else None
        changes = self._changes(namespace, prior, observations, definition_id, dataflow_version, item, declared_kind)
        if prior is not None and prior["content_hash"] == content_hash and not changes["definition_change"]:
            # An unchanged re-publication adds no vintage: the earlier vintage stays current.
            return prior["vintage_id"], "unchanged"
        if prior is not None and clock < prior["release_at_ms"]:
            raise LabourError("stale_release", "a release dated before the series' latest vintage is not appended",
                              series_id=series_id)
        record = SeriesRecord(
            series_id=series_id,
            provider=item["provider"],
            title=str(dict(item["indicator"]).get("label") or dict(header.get("document") or {}).get("label")
                      or item["native_key"]),
            frequency=item["frequency"],
            as_of=int(clock),
            observations=[
                {"period": o["period"], "value": None if o.get("value") is None else float(Decimal(o["value"]))}
                for o in sorted(observations, key=lambda o: o["period"])
            ],
            unit=dict(item["unit"]).get("label"),
            geography=str(item["area"]["code"]),
            source_url=header.get("url"),
            metadata={
                "provider_release_at_ms": int(clock),
                "provider_release_time_status": f"labour release clock ({basis})",
                "acquired_at_ms": int(retrieved),
                "vintage_basis": basis,
                "source_document_id": release_id,
            },
        )
        semantics = {
            "indicator_id": "labour-indicator:" + series_id.split(":", 1)[1],
            "canonical_name": str(dict(item["indicator"]).get("label") or item["indicator"]["concept"]),
            "concept": f"labour {item['indicator']['concept']} {item['indicator']['measure']}",
            "definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "seasonal_adjustment": ECONOMIC_SEASONAL[item["seasonal_adjustment"]],
            "price_basis": "not_applicable",
            "provider_code": str(item["native_key"]),
            "provider_definition": dict(item["definition"]).get("source_text") or item["indicator"]["concept"],
            "attributes": {
                "labour_series_id": series_id,
                "labour_seasonal_adjustment": item["seasonal_adjustment"],
                "definition_basis": item["definition"]["basis"],
                "estimate_type": item["estimate_type"],
                "contract": CONTRACT,
            },
        }
        try:
            economic = register_series(self.conn, record, semantics=semantics, domain=DOMAIN)
        except EconomicModelError as exc:
            raise LabourError(exc.code, str(exc)) from exc
        sequence = 1 + len(previous)
        vintage_id = "lb-vintage:" + digest([namespace, series_id, clock, content_hash, definition_id])[:24]
        notes = [dict(n) for n in item.get("source_notes") or []]
        self.conn.execute(
            "INSERT INTO labour_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, economic["vintage"]["as_of"], clock, basis, retrieved,
             content_hash, definition_id, dataflow_version, canonical(notes), canonical(changes), sequence,
             self.now()],
        )
        for obs in observations:
            self.conn.execute(
                "INSERT INTO labour_observations VALUES (?,?,?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("value_text"), obs.get("value"), obs["status"],
                 obs.get("unit_multiplier"), canonical(dict(obs.get("flags") or {})),
                 canonical(dict(obs.get("attributes") or {})), canonical(list(obs.get("footnotes") or []))],
            )
        return vintage_id, "new"

    def _changes(self, namespace, prior, observations, definition_id, dataflow_version, item, declared_kind):
        """What a new vintage changes against the previous one (facts of the two records, nothing estimated)."""
        if prior is None:
            return {"first": True, "new_periods": sorted(o["period"] for o in observations), "revised": [],
                    "benchmark_revision": False, "definition_change": False, "dataflow_version": None}
        before = {o["period"]: o for o in self.observations(namespace, prior["vintage_id"])}
        after = {o["period"]: o for o in observations}
        new_periods = sorted(set(after) - set(before))
        revised = []
        for period in sorted(set(after) & set(before)):
            b, a = before[period], after[period]
            if (b["value_text"], b["status"], b["flags"], b["footnotes"]) != (
                a.get("value_text"), a["status"], dict(a.get("flags") or {}), list(a.get("footnotes") or [])
            ):
                revised.append({
                    "period": period,
                    "before": {"value": b["value"], "status": b["status"], "flags": b["flags"],
                               "footnotes": b["footnotes"]},
                    "after": {"value": a.get("value"), "status": a["status"], "flags": dict(a.get("flags") or {}),
                              "footnotes": list(a.get("footnotes") or [])},
                })
        window = item.get("revision_window_periods")
        recent = period_window(list(before), window)
        value_changes = [r for r in revised if r["before"]["value"] != r["after"]["value"]]
        beyond = sorted(r["period"] for r in value_changes if r["period"] not in recent)
        benchmark = bool(value_changes) and (declared_kind == "benchmark" or bool(beyond))
        prior_version = prior.get("dataflow_version")
        return {
            "first": False,
            "new_periods": new_periods,
            "revised": revised,
            "benchmark_revision": benchmark,
            "benchmark_basis": None if not benchmark else (
                "declared benchmark release" if declared_kind == "benchmark"
                else f"values changed for periods {beyond} beyond the declared regular revision window "
                f"({window} periods)"),
            "definition_change": prior["definition_id"] != definition_id or (
                prior_version is not None and dataflow_version is not None and prior_version != dataflow_version),
            "definition": {"before": prior["definition_id"], "after": definition_id},
            "dataflow_version": {"before": prior_version, "after": dataflow_version},
        }

    def _source_notes(self, namespace, series_id, item, release_id) -> int:
        """Source-stated breaks and survey/coverage notes as comparability notes attached to the series and periods."""
        comparability = LabourComparability(self.conn, now=self.now, initialize=False)
        created = 0
        for note in item.get("source_notes") or []:
            if note.get("kind") == "catalog":
                continue
            relation = "break_in_series" if note.get("kind") == "break" else "source_note"
            statement = (
                "the source flags a break in series" if relation == "break_in_series"
                else f"{note.get('attribute')}: {note.get('value')}"
            )
            created += int(comparability._insert(
                namespace, {"kind": "series", "id": series_id}, None, relation, statement,
                list(note.get("periods") or []), origin="source", state="source-stated",
                principal_id=f"source:{item['provider']}", release_id=release_id,
            ))
        return created

    # ------------------------------------------------------------------ reads

    _RELEASE_KEYS = (
        "release_id", "provider", "source_id", "format", "document", "published_on", "published_at",
        "release_basis", "release_at_ms", "release_label", "file_sha256", "content_sha256", "item_count",
        "structure", "evidence_origin", "url", "sequence", "run_id", "recorded_by", "retrieved_at_ms",
    )

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, document_json, published_on, published_at, "
            "release_basis, release_at_ms, release_label, file_sha256, content_sha256, item_count, structure_json, "
            "evidence_origin, url, sequence, run_id, recorded_by, retrieved_at_ms FROM labour_releases WHERE "
            "namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone()
        if row is None:
            raise LabourError("not_found", "release is not visible in this namespace")
        view = dict(zip(self._RELEASE_KEYS, row))
        view["document"] = _load(view["document"], {})
        view["structure"] = _load(view["structure"], {})
        return {"contract": CONTRACT, "record_type": "release", "namespace": namespace, **view}

    def source_revision(self, namespace: str, release_id: str) -> dict[str, Any]:
        release = self.release(namespace, release_id)
        return {
            k: release[k]
            for k in ("release_id", "provider", "source_id", "file_sha256", "published_on", "published_at",
                      "release_basis", "release_at_ms", "release_label", "url", "evidence_origin", "retrieved_at_ms")
        } | {
            "document": release["document"].get("label"),
            "retrieved_at": iso_from_ms(release["retrieved_at_ms"]),
            "dataflow_version": release["structure"].get("dataflow_version"),
            "live_verification": LIVE_VERIFICATION.get(release["provider"], {}).get("status"),
        }

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "labour_releases"):
            return []
        rows = self.conn.execute(
            "SELECT release_id FROM labour_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY release_at_ms, sequence",
            [namespace, provider, provider],
        ).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def release_series(self, namespace: str, release_id: str) -> list[dict[str, str]]:
        return [
            {"series_id": r[0], "vintage_id": r[1], "status": r[2]}
            for r in self.conn.execute(
                "SELECT series_id, vintage_id, status FROM labour_release_members WHERE namespace=? AND release_id=? "
                "ORDER BY series_id",
                [namespace, release_id],
            ).fetchall()
        ]

    _SERIES_COLUMNS = (
        "series_id, provider, native_key, dataflow_json, indicator_json, estimate_type, definition_basis, "
        "definition_key, seasonal_adjustment, frequency, unit_json, unit_multiplier, area_json, sector_json, "
        "occupation_json, dimensions_json, references_json, denominator_json, revision_window, "
        "economic_indicator_id, first_release_id"
    )

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, native_key, dataflow, indicator, estimate_type, basis, definition_key, seasonal,
         frequency, unit, unit_multiplier, area, sector, occupation, dimensions, references, denominator, window,
         economic_indicator_id, first_release) = row
        vintages = self.vintage_rows(namespace, series_id)
        current = vintages[-1] if vintages else None
        return {
            "contract": CONTRACT,
            "record_type": "indicator",
            "namespace": namespace,
            "series_id": series_id,
            "provider": provider,
            "native_key": native_key,
            "dataflow": _load(dataflow, {}),
            "indicator": _load(indicator, {}),
            "estimate_type": estimate_type,
            "definition_basis": basis,
            "definition_key": definition_key,
            "seasonal_adjustment": seasonal,
            "frequency": frequency,
            "unit": _load(unit, {}),
            "unit_multiplier": unit_multiplier,
            "area": _load(area, {}),
            "sector": _load(sector, None),
            "occupation": _load(occupation, None),
            "dimensions": _load(dimensions, {}),
            "references": _load(references, []),
            "denominator": _load(denominator, None),
            "revision_window_periods": window,
            "economic_series": {"domain": DOMAIN, "series_id": series_id, "indicator_id": economic_indicator_id},
            "first_release_id": first_release,
            "vintage_count": len(vintages),
            "current_vintage_id": None if current is None else current["vintage_id"],
            "current_definition_id": None if current is None else current["definition_id"],
        }

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            f"SELECT {self._SERIES_COLUMNS} FROM labour_series WHERE namespace=? AND series_id=?",
            [namespace, series_id],
        ).fetchone()
        if row is None:
            raise LabourError("not_found", "series is not visible in this namespace")
        return self._series_view(namespace, row)

    def find_series(
        self,
        namespace: str,
        *,
        provider: str | None = None,
        concept: str | None = None,
        area_codes: Iterable[str] | None = None,
        seasonal_adjustment: str | None = None,
        definition_basis: str | None = None,
        estimate_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {self._SERIES_COLUMNS}, area_code FROM labour_series WHERE namespace=? AND "
            "(? IS NULL OR provider=?) AND (? IS NULL OR concept=?) AND (? IS NULL OR seasonal_adjustment=?) AND "
            "(? IS NULL OR definition_basis=?) AND (? IS NULL OR estimate_type=?) "
            "ORDER BY provider, concept, area_code, native_key, series_id",
            [namespace, provider, provider, concept, concept, seasonal_adjustment, seasonal_adjustment,
             definition_basis, definition_basis, estimate_type, estimate_type],
        ).fetchall()
        wanted = None if area_codes is None else {str(c) for c in area_codes}
        out = []
        for row in rows:
            if wanted is not None and row[-1] not in wanted:
                continue
            out.append(self._series_view(namespace, row[:-1]))
            if len(out) >= limit:
                break
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        """Every vintage in release-clock order, each with ``revision_of`` its predecessor and its changes."""
        rows = self.conn.execute(
            "SELECT vintage_id, release_id, economic_as_of, release_at_ms, release_at_basis, retrieved_at_ms, "
            "content_hash, definition_id, dataflow_version, changes_json, sequence FROM labour_vintages "
            "WHERE namespace=? AND series_id=? ORDER BY release_at_ms, sequence",
            [namespace, series_id],
        ).fetchall()
        out, previous = [], None
        for row in rows:
            view = dict(zip(("vintage_id", "release_id", "economic_as_of", "release_at_ms", "release_at_basis",
                             "retrieved_at_ms", "content_hash", "definition_id", "dataflow_version", "changes",
                             "sequence"), row))
            view["changes"] = _load(view["changes"], {})
            view["series_id"] = series_id
            view["release_at"] = iso_from_ms(view["release_at_ms"])
            view["retrieved_at"] = iso_from_ms(view["retrieved_at_ms"])
            view["revision_of"] = None if previous is None else previous["vintage_id"]
            view["economic_vintage_id"] = f"{series_id}@{view['economic_as_of']}"
            out.append(view)
            previous = view
        return out

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT series_id FROM labour_vintages WHERE namespace=? AND vintage_id=?", [namespace, vintage_id]
        ).fetchone()
        if row is None:
            raise LabourError("not_found", "vintage is not visible in this namespace")
        view = next(v for v in self.vintage_rows(namespace, row[0]) if v["vintage_id"] == vintage_id)
        return {"contract": CONTRACT, "record_type": "vintage", "namespace": namespace, **view,
                "source_revision": self.source_revision(namespace, view["release_id"])}

    def observations(self, namespace: str, vintage_id: str, *, period_from: str | None = None,
                     period_to: str | None = None) -> list[dict[str, Any]]:
        vintage = self.conn.execute(
            "SELECT series_id, economic_as_of FROM labour_vintages WHERE namespace=? AND vintage_id=?",
            [namespace, vintage_id],
        ).fetchone()
        if vintage is None:
            return []
        numeric = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT period, value FROM dataset_observations WHERE series_id=? AND as_of=?", [vintage[0], vintage[1]]
            ).fetchall()
        } if table_exists(self.conn, "dataset_observations") else {}
        rows = self.conn.execute(
            "SELECT period, value_text, value, status, unit_multiplier, flags_json, attributes_json, footnotes_json "
            "FROM labour_observations WHERE namespace=? AND vintage_id=? ORDER BY period",
            [namespace, vintage_id],
        ).fetchall()
        out = []
        for period, value_text, value, status, multiplier, flags, attributes, footnotes in rows:
            if period_from and period < period_from:
                continue
            if period_to and period[: len(period_to)] > period_to:
                continue
            out.append({
                "record_type": "observation",
                "period": period,
                "value_text": value_text,
                "value": value,
                "numeric_value": numeric.get(period),
                "status": status,
                "unit_multiplier": multiplier,
                "flags": _load(flags, {}),
                "attributes": _load(attributes, {}),
                "footnotes": _load(footnotes, []),
            })
        return out

    def select_vintage(self, namespace: str, series_id: str, *, as_of_ms: int | None = None
                       ) -> tuple[dict[str, Any] | None, str | None]:
        """The vintage released on or before the cutoff (release-cutoff semantics of the economic release store)."""
        vintages = self.vintage_rows(namespace, series_id)
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        eligible = [v for v in vintages if v["release_at_ms"] <= cutoff]
        if eligible:
            return eligible[-1], None
        return None, "no_release_by_as_of" if vintages else "no_vintage"

    def values(self, namespace: str, series_id: str, *, vintage_id: str | None = None, as_of_ms: int | None = None,
               period_from: str | None = None, period_to: str | None = None) -> dict[str, Any]:
        series = self.series(namespace, series_id)
        reason = None
        if vintage_id:
            vintage = next((v for v in self.vintage_rows(namespace, series_id) if v["vintage_id"] == vintage_id), None)
            if vintage is None:
                raise LabourError("not_found", "vintage does not belong to this series")
        else:
            vintage, reason = self.select_vintage(namespace, series_id, as_of_ms=as_of_ms)
        if vintage is None:
            return {"contract": ANSWER_CONTRACT, "series": series, "status": "unavailable", "reason": reason,
                    "observations": []}
        return {
            "contract": ANSWER_CONTRACT,
            "series": series,
            "status": "available",
            "vintage": {**vintage, "source_revision": self.source_revision(namespace, vintage["release_id"])},
            "definition": self.definition(namespace, vintage["definition_id"]),
            "observations": self.observations(namespace, vintage["vintage_id"], period_from=period_from,
                                              period_to=period_to),
            "note": "values as published in this vintage; confidential and unpublished periods carry no value",
        }

    def definition(self, namespace: str, definition_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT definition_id, definition_key, revision, provider, content_json, release_id FROM "
            "labour_definitions WHERE namespace=? AND definition_id=?",
            [namespace, definition_id],
        ).fetchone()
        if row is None:
            raise LabourError("not_found", "definition is not visible in this namespace")
        return {"contract": CONTRACT, "record_type": "definition", "namespace": namespace, "definition_id": row[0],
                "definition_key": row[1], "revision": row[2], "provider": row[3], "content": json.loads(row[4]),
                "source_revision": self.source_revision(namespace, row[5])}

    def definitions(self, namespace: str, definition_key: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "labour_definitions"):
            return []
        rows = self.conn.execute(
            "SELECT definition_id FROM labour_definitions WHERE namespace=? AND (? IS NULL OR definition_key=?) "
            "ORDER BY definition_key, revision",
            [namespace, definition_key, definition_key],
        ).fetchall()
        return [self.definition(namespace, r[0]) for r in rows]

    def latest_release_ms(self, namespace: str) -> int | None:
        if not table_exists(self.conn, "labour_releases"):
            return None
        row = self.conn.execute(
            "SELECT max(retrieved_at_ms) FROM labour_releases WHERE namespace=?", [namespace]
        ).fetchone()
        return None if row is None or row[0] is None else int(row[0])


def _side(value: Mapping[str, Any] | None) -> dict[str, str] | None:
    if value is None:
        return None
    value = dict(value)
    if bool(value.get("series_id")) == bool(value.get("definition_id")):
        raise LabourError("invalid_note", "each side names one series_id or one definition_id")
    return (
        {"kind": "series", "id": str(value["series_id"])}
        if value.get("series_id")
        else {"kind": "definition", "id": str(value["definition_id"])}
    )


def pair_key(left: Mapping[str, str], right: Mapping[str, str] | None) -> str:
    """Order-insensitive: a note on (a, b) is a note on (b, a); a single-sided note keys its one record."""
    sides = [f"{left['kind']}:{left['id']}"] + ([] if right is None else [f"{right['kind']}:{right['id']}"])
    return canonical(sorted(sides))


class LabourComparability:
    """Reviewable comparability notes between labour series or definitions (the demographics pattern)."""

    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = LabourStore(conn, initialize=initialize, now=now)
        self.now = self.store.now

    def _cite(self, namespace: str, side: Mapping[str, str]) -> dict[str, Any]:
        if side["kind"] == "series":
            series = self.store.series(namespace, side["id"])
            if series["current_definition_id"] is None:
                raise LabourError("not_found", "the series has no vintage yet")
            definition = self.store.definition(namespace, series["current_definition_id"])
        else:
            definition = self.store.definition(namespace, side["id"])
            series = None
        content = definition["content"]
        return {
            **dict(side),
            "provider": definition["provider"],
            "definition_id": definition["definition_id"],
            "definition": {k: content.get(k) for k in ("concept", "measure", "basis", "estimate_type", "age_bounds",
                                                       "coverage", "source_text")},
            "seasonal_adjustment": None if series is None else series["seasonal_adjustment"],
            "source_revision": definition["source_revision"],
        }

    def _insert(self, namespace, left, right, relation, statement, periods, *, origin, state, principal_id,
                release_id=None) -> bool:
        key = pair_key(left, right)
        note_id = "lb-comparability:" + digest([namespace, key, relation, statement.strip(), sorted(periods)])[:24]
        if self.conn.execute(
            "SELECT 1 FROM labour_comparability WHERE namespace=? AND note_id=?", [namespace, note_id]
        ).fetchone():
            return False
        cited = [self._cite(namespace, left)] + ([] if right is None else [self._cite(namespace, right)])
        if release_id is not None:
            cited[0]["stated_in"] = self.store.source_revision(namespace, release_id)
        now = self.now()
        self.conn.execute(
            "INSERT INTO labour_comparability VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, note_id, key, canonical(dict(left)), None if right is None else canonical(dict(right)),
             relation, statement.strip(), canonical(sorted(periods)), canonical(sorted(cited, key=canonical)), origin,
             state, canonical([{"state": state, "by": principal_id, "at_ms": now}]), principal_id, now],
        )
        return True

    def record(
        self,
        namespace: str,
        left: Mapping[str, Any],
        right: Mapping[str, Any] | None,
        relation: str,
        statement: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        periods: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Propose a note (idempotent for the same records, relation, statement and periods)."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        if relation not in RELATIONS or not str(statement or "").strip():
            raise LabourError("invalid_note", f"a note has one of {RELATIONS} and a statement")
        a, b = _side(left), _side(right)
        if a is None or (b is None and relation not in SINGLE_SIDED):
            raise LabourError("invalid_note", "a note links two records (breaks and source notes may name one)")
        if a == b:
            raise LabourError("invalid_note", "a note links two different records")
        self._insert(namespace, a, b, relation, statement, [str(p) for p in periods], origin="reviewer",
                     state="proposed", principal_id=principal_id)
        key = pair_key(a, b)
        note_id = "lb-comparability:" + digest([namespace, key, relation, statement.strip(),
                                                sorted(str(p) for p in periods)])[:24]
        return self.note(namespace, note_id, scopes={"operator"})

    def _transition(self, namespace, note, state, principal_id, reason):
        history = note["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}]
        self.conn.execute(
            "UPDATE labour_comparability SET state=?, history_json=? WHERE namespace=? AND note_id=?",
            [state, canonical(history), namespace, note["note_id"]],
        )
        return self.note(namespace, note["note_id"], scopes={"operator"})

    def review(self, namespace, note_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise LabourError("invalid_decision", "accept or reject with a reason")
        note = self.note(namespace, note_id, scopes={"operator"})
        if note["state"] != "proposed":
            raise LabourError("invalid_state", f"note is {note['state']}; only a proposed note is reviewed")
        return self._transition(namespace, note, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip())

    def revert(self, namespace, note_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise LabourError("invalid_decision", "a revert needs a reason")
        note = self.note(namespace, note_id, scopes={"operator"})
        if note["state"] not in {"accepted", "rejected"}:
            raise LabourError("invalid_state", "only an accepted or rejected note can be reverted")
        return self._transition(namespace, note, "reverted", principal_id, reason.strip())

    def note(self, namespace: str, note_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT note_id, pair_key, left_json, right_json, relation, statement, periods_json, cited_json, origin, "
            "state, history_json, created_by, created_at_ms FROM labour_comparability WHERE namespace=? AND note_id=?",
            [namespace, note_id],
        ).fetchone()
        if row is None:
            raise LabourError("not_found", "comparability note is not visible in this namespace")
        return {
            "contract": COMPARABILITY_CONTRACT,
            "record_type": "comparability_note",
            "namespace": namespace,
            "note_id": row[0],
            "pair_key": row[1],
            "left": json.loads(row[2]),
            "right": _load(row[3], None),
            "relation": row[4],
            "statement": row[5],
            "periods": json.loads(row[6]),
            "cited": json.loads(row[7]),
            "origin": row[8],
            "state": row[9],
            "history": json.loads(row[10]),
            "created_by": row[11],
            "created_at_ms": row[12],
        }

    def notes(self, namespace: str, *, scopes: Iterable[str], series_id: str | None = None,
              definition_id: str | None = None, active_only: bool = False) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "labour_comparability"):
            return []
        rows = self.conn.execute(
            "SELECT note_id, left_json, right_json FROM labour_comparability WHERE namespace=? "
            "ORDER BY created_at_ms, note_id",
            [namespace],
        ).fetchall()
        wanted = [{"kind": "series", "id": series_id}] if series_id else []
        wanted += [{"kind": "definition", "id": definition_id}] if definition_id else []
        out = []
        for note_id, left, right in rows:
            sides = [json.loads(left)] + ([] if right is None else [json.loads(right)])
            if wanted and not any(s in wanted for s in sides):
                continue
            note = self.note(namespace, note_id, scopes=scopes)
            if active_only and note["state"] not in ACTIVE_STATES:
                continue
            out.append(note)
        return out

    def notes_between(self, namespace: str, left_series_id: str, right_series_id: str) -> list[dict[str, Any]]:
        """Active notes linking two series (or their current definitions)."""
        if not table_exists(self.conn, "labour_comparability"):
            return []
        store = self.store
        left, right = store.series(namespace, left_series_id), store.series(namespace, right_series_id)
        keys = {
            pair_key(a, b)
            for a in ({"kind": "series", "id": left_series_id},
                      {"kind": "definition", "id": left["current_definition_id"]})
            for b in ({"kind": "series", "id": right_series_id},
                      {"kind": "definition", "id": right["current_definition_id"]})
        }
        rows = self.conn.execute(
            "SELECT note_id, pair_key FROM labour_comparability WHERE namespace=? ORDER BY created_at_ms, note_id",
            [namespace],
        ).fetchall()
        notes = [self.note(namespace, r[0], scopes={"operator"}) for r in rows if r[1] in keys]
        return [{k: n[k] for k in ("note_id", "relation", "statement", "periods", "state", "origin")}
                for n in notes if n["state"] in ACTIVE_STATES]


def comparability_basis(left: Mapping[str, Any], right: Mapping[str, Any]) -> list[dict[str, str]]:
    """Recorded differences between two series' declared attributes (facts of the records, never a harmonisation)."""
    out = []
    for field, kind in (("definition_basis", "different_definition_basis"),
                        ("estimate_type", "different_estimate_type"),
                        ("seasonal_adjustment", "different_seasonal_adjustment"),
                        ("frequency", "different_frequency")):
        if left[field] != right[field]:
            out.append({"kind": kind, "detail": f"{left[field]} ({left['provider']}) against {right[field]} "
                                                f"({right['provider']})"})
    if left["unit"] != right["unit"]:
        out.append({"kind": "different_unit", "detail": f"{left['unit']} against {right['unit']}"})
    return out


class LabourProjector:
    """Source-pack runtime projector for ``noesis-labour-statistics-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = LabourStore(conn)
        LabourComparability(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("labour_statistics") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = dict(item.get("labour_release") or {}), item.get("labour_item")
            if not header or not isinstance(body, Mapping):
                raise LabourError("invalid_record", "page record is not a labour release item")
            groups.setdefault(header["file_sha256"] + canonical(header.get("document")), (header, []))[1].append(
                dict(body)
            )
        namespace = self._namespace(source)
        return [
            self.store.apply_release(namespace, header, items, run_id=run_id, source_id=source["source_id"])
            for header, items in groups.values()
        ]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, published_on FROM labour_releases WHERE namespace=? AND source_id=? "
            "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1",
            [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {"status": status, "latest_release_id": row[0] if row else None,
                "latest_published_on": row[1] if row else None}


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the labour record contract as a schema module in the shared registry."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema" / f"{CONTRACT}.json"
    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "labour-statistics-record",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": json.loads(path.read_text()),
        "owner": "economics.labour",
        "dependencies": [],
        "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [
        SchemaRegistry(conn).register(
            definition, "labour-schema:labour-statistics-record:1.0.0", principal_id=principal_id, scopes=scopes
        )
    ]


def readiness(conn: Any) -> dict[str, Any]:
    store = LabourStore(conn, initialize=False)
    ready = store.ready() and table_exists(conn, "labour_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        if ready:
            releases = int(
                conn.execute("SELECT count(*) FROM labour_releases WHERE provider=?", [provider]).fetchone()[0]
            )
        providers[provider] = {
            "delivers": contract["delivers"],
            "access_decision": contract["access_decision"],
            "live_verification": LIVE_VERIFICATION[provider]["status"],
            "releases": releases,
        }
    return {
        "feature": FEATURE,
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "series_storage": "economic_indicators, economic_series_map, economic_vintages and dataset_observations",
        "providers": providers,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
        "is live until a dated run verifies it",
    }


# ---------------------------------------------------------------------- as-of answers (LB09)


def _next_period(period: str, frequency: str) -> str | None:
    if frequency == "annual" and len(period) == 4:
        return str(int(period) + 1)
    if frequency == "monthly" and len(period) == 7:
        year, month = int(period[:4]), int(period[5:])
        return f"{year + (month == 12)}-{1 if month == 12 else month + 1:02d}"
    if frequency == "quarterly" and "-Q" in period:
        year, quarter = int(period[:4]), int(period[-1])
        return f"{year + (quarter == 4)}-Q{1 if quarter == 4 else quarter + 1}"
    return None


def missing_periods(periods: Sequence[str], frequency: str) -> list[str]:
    """Periods between the first and last published period that the vintage does not state (never filled)."""
    stated = sorted(set(periods))
    if len(stated) < 2:
        return []
    out, current = [], _next_period(stated[0], frequency)
    while current is not None and current < stated[-1] and len(out) < 1000:
        if current not in stated:
            out.append(current)
        current = _next_period(current, frequency)
    return out


class LabourQueries:
    """Labour indicators for a place, sector or occupation as of a release vintage, sources side by side."""

    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        self.store = LabourStore(conn, initialize=False, now=now)

    def _identity(self):
        from src.kb.labour_identity import LabourIdentity

        return LabourIdentity(self.conn, initialize=True, now=self.store.now)

    def _place_codes(self, namespace: str, place: Any) -> dict[str, Any]:
        """Area codes of a place: accepted mappings for a place id, else the native code as given."""
        if isinstance(place, Mapping):
            return {"place": dict(place), "codes": [{"scheme": place["scheme"], "code": str(place["code"]),
                                                    "basis": "native code as requested"}]}
        identity = self._identity()
        codes = [{**c, "basis": "accepted place mapping"} for c in identity.area_codes_for_place(namespace, place)]
        pending = [
            {"scheme": a["subject"]["scheme"], "code": a["subject"]["code"], "state": a["state"],
             "reason": a["reason"]}
            for a in identity.assertions(namespace, scopes={"operator"}, kind="area")
            if a["state"] in {"proposed", "ambiguous"} and (
                (a["target"] or {}).get("place_id") == place
                or any(c["place_id"] == place for c in a["evidence"].get("candidates") or []))
        ]
        return {"place": {"place_id": place}, "codes": codes, "unmapped_codes": pending}

    def _classified(self, namespace: str, kind: str, wanted: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not wanted:
            return None
        wanted = dict(wanted)
        codes = [{"scheme": wanted["scheme"], "version": str(wanted["version"]), "code": str(wanted["code"]),
                  "relation": "exact", "basis": "requested code"}]
        identity = self._identity()
        for mapped in identity.codes_for(namespace, kind, wanted):
            if (mapped["scheme"], mapped["version"], mapped["code"]) != (codes[0]["scheme"], codes[0]["version"],
                                                                         codes[0]["code"]):
                codes.append({**mapped, "basis": "accepted concordance mapping"})
        if wanted["scheme"] == "ISCO":
            # ISCO-08 is hierarchical by digits: an accepted mapping to a unit group lies within its major group.
            for assertion in identity.assertions(namespace, scopes={"operator"}, kind=kind, state="accepted"):
                if assertion["subject"]["target"] != {"scheme": "ISCO", "version": str(wanted["version"])}:
                    continue
                for mapped in assertion["target"]["codes"]:
                    if mapped["code"] != wanted["code"] and mapped["code"].startswith(str(wanted["code"])):
                        codes.append({"scheme": assertion["subject"]["scheme"],
                                      "version": assertion["subject"]["version"], "code": assertion["subject"]["code"],
                                      "relation": "narrower", "via": mapped["code"],
                                      "assertion_id": assertion["assertion_id"],
                                      "basis": "accepted mapping to a code within the requested ISCO group"})
        unmapped = [
            {"scheme": a["subject"]["scheme"], "version": a["subject"]["version"], "code": a["subject"]["code"],
             "state": a["state"], "reason": a["reason"]}
            for a in identity.assertions(namespace, scopes={"operator"}, kind=kind)
            if a["subject"]["target"] == {"scheme": wanted["scheme"], "version": str(wanted["version"])}
            and a["state"] != "accepted"
        ]
        return {"requested": wanted, "codes": codes, "unmapped_codes": unmapped}

    @staticmethod
    def _matches(value: Mapping[str, Any] | None, codes: list[dict[str, Any]]) -> dict[str, Any] | None:
        if value is None:
            return None
        for code in codes:
            if (value["scheme"], str(value["version"]), value["code"]) == (code["scheme"], code["version"],
                                                                          code["code"]):
                return code
        return None

    def indicators(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        place: Any = None,
        sector: Mapping[str, Any] | None = None,
        occupation: Mapping[str, Any] | None = None,
        concept: str | None = None,
        as_of_ms: int | None = None,
        history: bool = False,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> dict[str, Any]:
        """Published values known at ``as_of_ms`` per source, with definitions, seasonal adjustment, vintage and
        comparability notes; nothing blended, re-harmonised or filled."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if place is None and sector is None and occupation is None:
            raise LabourError("invalid_query", "name a place, a sector or an occupation")
        if concept is not None and concept not in CONCEPTS:
            raise LabourError("invalid_query", f"concept is one of {CONCEPTS}")
        places = None if place is None else self._place_codes(namespace, place)
        sectors = self._classified(namespace, "sector", sector)
        occupations = self._classified(namespace, "occupation", occupation)
        area_codes = None if places is None else {(c["scheme"], c["code"]) for c in places["codes"]}
        comparability = LabourComparability(self.conn, now=self.store.now, initialize=False) if table_exists(
            self.conn, "labour_comparability") else None
        results, unavailable = [], []
        for series in self.store.find_series(namespace, concept=concept):
            if area_codes is not None and (series["area"]["scheme"], str(series["area"]["code"])) not in area_codes:
                continue
            sector_match = self._matches(series["sector"], sectors["codes"]) if sectors else None
            if sectors and sector_match is None:
                continue
            occupation_match = self._matches(series["occupation"], occupations["codes"]) if occupations else None
            if occupations and occupation_match is None:
                continue
            vintage, reason = self.store.select_vintage(namespace, series["series_id"], as_of_ms=as_of_ms)
            if vintage is None:
                unavailable.append({"series_id": series["series_id"], "provider": series["provider"],
                                    "native_key": series["native_key"], "reason": reason})
                continue
            revision = self.store.source_revision(namespace, vintage["release_id"])
            cite = {"provider": series["provider"], "source_id": revision["source_id"],
                    "series_key": series["native_key"], "vintage_id": vintage["vintage_id"],
                    "release_at": vintage["release_at"], "release_basis": vintage["release_at_basis"],
                    "retrieved_at": vintage["retrieved_at"], "file_sha256": revision["file_sha256"]}
            observations = self.store.observations(namespace, vintage["vintage_id"], period_from=period_from,
                                                   period_to=period_to)
            values = [{**o, "seasonal_adjustment": series["seasonal_adjustment"], "citation": cite}
                      for o in observations]
            stated = [o["period"] for o in observations]
            results.append({
                "series_id": series["series_id"],
                "provider": series["provider"],
                "native_key": series["native_key"],
                "indicator": series["indicator"],
                "estimate_type": series["estimate_type"],
                "definition_basis": series["definition_basis"],
                "seasonal_adjustment": series["seasonal_adjustment"],
                "frequency": series["frequency"],
                "unit": series["unit"],
                "unit_multiplier": series["unit_multiplier"],
                "area": series["area"],
                "sector": series["sector"],
                "occupation": series["occupation"],
                "matched_by": {"sector": sector_match, "occupation": occupation_match},
                "definition": self.store.definition(namespace, vintage["definition_id"]),
                "vintage": {**vintage, "source_revision": revision},
                "values": values,
                "gaps": {
                    "missing_periods": missing_periods(stated, series["frequency"]),
                    "withheld_periods": [{"period": o["period"], "status": o["status"], "flags": o["flags"]}
                                         for o in observations if o["status"] != "reported"],
                    "latest_published_period": max(stated) if stated else None,
                    "note": "missing and withheld periods are not filled; no nowcast extends the series",
                },
                "source_notes": [] if comparability is None else [
                    {k: n[k] for k in ("relation", "statement", "periods", "state")}
                    for n in comparability.notes(namespace, scopes={"operator"}, series_id=series["series_id"],
                                                 active_only=True) if n["right"] is None],
                **({"revision_history": [
                    {k: v[k] for k in ("vintage_id", "release_at", "release_at_basis", "retrieved_at",
                                       "revision_of", "changes")}
                    for v in self.store.vintage_rows(namespace, series["series_id"])]} if history else {}),
            })
        pairs = []
        for i, left in enumerate(results):
            for right in results[i + 1:]:
                if left["indicator"]["concept"] != right["indicator"]["concept"]:
                    continue
                pairs.append({
                    "series": [left["series_id"], right["series_id"]],
                    "recorded_differences": comparability_basis(left, right),
                    "notes": [] if comparability is None else comparability.notes_between(
                        namespace, left["series_id"], right["series_id"]),
                })
        for pair in pairs:
            if not pair["notes"] and not pair["recorded_differences"]:
                pair["status"] = "comparability_unknown"
            else:
                pair["status"] = "noted"
        return {
            "contract": ANSWER_CONTRACT,
            "namespace": namespace,
            "as_of": iso_from_ms(as_of_ms),
            "subject": {"place": places, "sector": sectors, "occupation": occupations, "concept": concept},
            "status": "reported" if results else "none_published",
            "results": results,
            "unavailable_by_as_of": unavailable,
            "comparability": pairs,
            "side_by_side": True,
            "exclusions": list(EXCLUSIONS),
            "note": "each source's published values side by side; series are never blended, averaged or "
            "re-harmonised and no missing period is filled or nowcast",
        }

    def history(self, namespace: str, series_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        series = self.store.series(namespace, series_id)
        vintages = []
        for vintage in self.store.vintage_rows(namespace, series_id):
            vintages.append({**vintage, "source_revision": self.store.source_revision(namespace, vintage["release_id"]),
                             "observations": self.store.observations(namespace, vintage["vintage_id"])})
        return {"contract": ANSWER_CONTRACT, "series": series, "vintages": vintages,
                "note": "every retained vintage; earlier values are never overwritten"}


__all__ = [
    "LabourQueries",
    "missing_periods",
    "ANSWER_CONTRACT",
    "CHANGE_KINDS",
    "COMPARABILITY_CONTRACT",
    "CONTRACT",
    "FEATURE",
    "LabourComparability",
    "LabourError",
    "LabourProjector",
    "LabourStore",
    "READ_SCOPE",
    "RELATIONS",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "authorize",
    "comparability_basis",
    "feature_enabled",
    "forbidden_keys",
    "readiness",
    "register_schemas",
]
