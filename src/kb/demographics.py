"""Population and migration series, definitions, geography levels and vintages: the ``economics.demographics`` owner.

Records (contract ``noesis-demographic-series-v1``, #1914 M02), each carrying
its publisher, source id, source revision (the acquired release) and retrieval
time, in namespace-scoped, revision-addressable ``demographic_*`` tables:

* **release** - one acquired publication (or an operator's figure sheet for a
  PDF-only publication): provider, document, the publication's own release
  clock (Eurostat ``updated``, GENESIS table ``Updated``, a declared publication
  date or ``Last-Modified``) with its basis label, digests, the publication's
  structure (dimensions, code lists, flag labels) as evidence and the evidence
  origin. Re-acquiring an unchanged publication adds nothing, whatever the
  fetch time.
* **definition revision** - what a series measures, first-class: concept,
  stock or flow, citizenship / country of birth / country of origin, application
  or decision, reference date or period, population base (register, census
  2011, census 2022...), the publisher's own wording and code labels, and the
  date it is valid from. A series cannot be stored without one; a changed
  definition is a new revision plus a marked series break, and a value always
  stays with the definition revision it was published under.
* **geography level** - a code list (NUTS, country codes, AGS, Berlin Bezirk,
  COD-AB p-codes), the level within it and the code-list version.
* **series** - publisher, dataset or table code, indicator, dimensions,
  geography code and level, definition identity, unit and frequency. Two
  publishers, definitions or geography levels are always two series.
* **vintage** - one release of a series, following the ``economic_vintages``
  pattern (:mod:`src.domains.economic.model`): release and retrieval clocks
  with basis labels, ``revision_of`` (the previous vintage by the release
  clock, whatever order the releases arrived in), the definition revision, the
  publisher's coverage notes and every observation with its value text, parsed
  value, flags and a pint-normalised value. Earlier vintages stay addressable.
* **break** - a publisher-declared break (a Eurostat ``b`` flag), a definition
  revision or a census-base change, marked on the series; nothing is chained,
  rebased or spliced across it.

Nothing here projects a population, claims a cause of migration, or merges,
averages, nets or apportions values across publishers, definitions or
geography levels.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from src.ingestion.demographic_sources import (
    FORMATS,
    PROVIDER_CONTRACTS,
    REVIEW_BOUNDARY,
    UNITS,
    DemographicFormatError,
    check_definition,
    check_geography,
    check_unit,
    parse_operator_sheet,
)

CONTRACT = "noesis-demographic-series-v1"
ANSWER_CONTRACT = "noesis-demographic-answer-v1"
READ_SCOPE = "knowledge:demographics:read"
WRITE_SCOPE = "knowledge:demographics:write"
REVIEW_SCOPE = "knowledge:demographics:review"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = (
    "release",
    "definition_revision",
    "geography_level",
    "series",
    "vintage",
    "observation",
    "series_break",
)
BREAK_KINDS = ("publisher_flag", "definition_revision", "census_base_change")
# Keys that would carry a projection, a causal claim or a merged, averaged, netted or apportioned number.
FORBIDDEN_KEYS = frozenset(
    {
        "projection",
        "projected",
        "forecast",
        "prediction",
        "predicted",
        "cause",
        "causes",
        "driver",
        "drivers",
        "effect",
        "policy_effect",
        "merged_value",
        "merged",
        "average",
        "averaged",
        "net",
        "netted",
        "net_migration_estimate",
        "chained",
        "rebased",
        "spliced",
        "apportioned",
        "combined_total",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS demographic_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_json TEXT NOT NULL, published_on TEXT NOT NULL, published_at TEXT, release_basis TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL,
  structure_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, url TEXT, sequence INTEGER NOT NULL,
  run_id TEXT NOT NULL, recorded_by TEXT, retrieved_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS demographic_geography_levels (
  namespace TEXT NOT NULL, level_id TEXT NOT NULL, scheme TEXT NOT NULL, level TEXT NOT NULL,
  code_list_version TEXT, first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, level_id)
);
CREATE TABLE IF NOT EXISTS demographic_definitions (
  namespace TEXT NOT NULL, definition_id TEXT NOT NULL, definition_key TEXT NOT NULL, revision_no INTEGER NOT NULL,
  provider TEXT NOT NULL, series_code TEXT NOT NULL, indicator TEXT NOT NULL, dimensions_json TEXT NOT NULL,
  content_json TEXT NOT NULL, content_hash TEXT NOT NULL, valid_from TEXT NOT NULL, release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, definition_id)
);
CREATE TABLE IF NOT EXISTS demographic_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, series_code TEXT NOT NULL,
  indicator TEXT NOT NULL, dimensions_json TEXT NOT NULL, geography_code TEXT NOT NULL, geography_label TEXT,
  level_id TEXT NOT NULL, definition_key TEXT NOT NULL, unit_code TEXT NOT NULL, unit_label TEXT NOT NULL,
  frequency TEXT NOT NULL, first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS demographic_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, release_at_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  retrieved_at_basis TEXT NOT NULL, definition_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  notes_json TEXT NOT NULL, locator_json TEXT NOT NULL, sequence INTEGER NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS demographic_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  flags_json TEXT NOT NULL, normalized_json TEXT, definition_id TEXT NOT NULL, extra_json TEXT NOT NULL,
  PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS demographic_vintage_readings (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, definition_id TEXT NOT NULL, release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id, definition_id, release_id)
);
CREATE TABLE IF NOT EXISTS demographic_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  PRIMARY KEY(namespace, release_id, series_id)
);
CREATE TABLE IF NOT EXISTS demographic_breaks (
  namespace TEXT NOT NULL, break_id TEXT NOT NULL, series_id TEXT NOT NULL, kind TEXT NOT NULL, period TEXT,
  from_definition_id TEXT, to_definition_id TEXT, flag TEXT, note TEXT NOT NULL, first_vintage_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, break_id)
);
"""


class DemographicError(ValueError):
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
    return default if value in (None, "") else json.loads(value)


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
        raise DemographicError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes: Iterable[str], required: str) -> None:
    """A scope needed only for an optional part of an answer, checked when that part is requested."""
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise DemographicError(
            "unauthorized", f"{required} is required for this part of the answer"
        )


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
    )


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry a projection, a causal claim or a merged number."""
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


def _day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise DemographicError("invalid_date", f"{value!r} is not an ISO date") from exc


def release_ms(published_on: str, published_at: str | None) -> int:
    """The release clock in epoch milliseconds (UTC); a date alone is its midnight."""
    if published_at:
        stamp = datetime.fromisoformat(str(published_at))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return int(stamp.timestamp() * 1000)
    return int(
        datetime.combine(
            date.fromisoformat(published_on), datetime.min.time(), tzinfo=timezone.utc
        ).timestamp()
        * 1000
    )


def normalise_value(value: Any, unit_label: str) -> dict[str, Any] | None:
    """A count in persons through the pint owner (exact decimal fallback); a rate stays a rate."""
    if value is None:
        return None
    pint_unit, scale, kind = UNITS[unit_label]
    if kind == "rate":
        return {
            "value": str(Decimal(str(value))),
            "unit": unit_label,
            "scale": "1",
            "method": "rate as published; never converted to a count (no denominator is inferred)",
            "receipt_sha256": None,
        }
    if scale == 1:
        return {
            "value": str(Decimal(str(value))),
            "unit": "persons",
            "scale": "1",
            "method": "identity (published in persons)",
            "receipt_sha256": None,
        }
    try:
        from src.integrations.units import convert_physical

        receipt = convert_physical(str(value), pint_unit, "count", precision=0)
    except ModuleNotFoundError:
        # Without the optional pint dependency the published scale (a declared power of ten) is applied exactly,
        # with the rounding pint's receipt uses, so values stay comparable across deployments.
        exact = (Decimal(str(value)) * scale).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
        return {
            "value": str(exact),
            "unit": "persons",
            "scale": str(scale),
            "method": f"exact decimal scale x{scale} (pint not installed)",
            "receipt_sha256": None,
        }
    return {
        "value": receipt["result"]["value"],
        "unit": "persons",
        "scale": str(scale),
        "method": f"pint convert_physical {pint_unit} -> count",
        "receipt_sha256": receipt.get("sha256"),
    }


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Economics bundle's optional ``demographics`` feature is selected in the active plan."""
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
            "SELECT authority FROM composition_authority WHERE bundle='economics'"
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
    return "demographics" in ((plan.get("features") or {}).get("economics") or [])


class DemographicStore:
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
        return table_exists(self.conn, "demographic_vintages")

    # ------------------------------------------------------------------ writes

    def _release(self, namespace, header, *, source_id, run_id, retrieved, recorded_by):
        provider = str(header.get("provider") or "")
        if (
            header.get("format") not in FORMATS
            or FORMATS[header["format"]]["provider"] != provider
        ):
            raise DemographicError(
                "invalid_release", "release names a known provider and format"
            )
        document = dict(header.get("document") or {})
        # The release id changes whenever the publication or its declared reading (definitions, levels) changes.
        release_id = (
            "dm-release:"
            + digest([namespace, provider, source_id, header["file_sha256"], document])[
                :24
            ]
        )
        if self.conn.execute(
            "SELECT 1 FROM demographic_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone():
            return release_id, False
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM demographic_releases WHERE namespace=? AND provider=?",
            [namespace, provider],
        ).fetchone()[0]
        published = _day(header["published_on"])
        origin = header.get("evidence_origin")
        origin = origin if origin in {"fixture", "operator"} else "live"
        self.conn.execute(
            "INSERT INTO demographic_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                release_id,
                provider,
                source_id,
                header["format"],
                canonical(document),
                published,
                header.get("published_at"),
                str(header.get("release_basis") or "declared_publication"),
                release_ms(published, header.get("published_at")),
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
        """Record one publication: levels, definition revisions, series, a vintage per series and its breaks."""
        if int(header.get("item_count", -1)) != len(items):
            raise DemographicError(
                "incomplete_release", "a release carries every series it states"
            )
        for item in items:
            if forbidden_keys(dict(item)):
                raise DemographicError(
                    "invalid_release",
                    "published series carry no derived projection or merged value",
                )
            try:
                check_definition(item.get("definition") or {})
                check_geography(item.get("geography") or {})
                check_unit(item.get("unit") or {})
            except DemographicFormatError as exc:
                raise DemographicError("invalid_release", str(exc)) from exc
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        counts = {
            "series": 0,
            "definitions": 0,
            "vintages": 0,
            "breaks": 0,
            "definition_corrections": 0,
        }
        self.conn.execute("BEGIN")
        try:
            release_id, created = self._release(
                namespace,
                header,
                source_id=source_id,
                run_id=run_id,
                retrieved=retrieved,
                recorded_by=recorded_by,
            )
            if not created:
                self.conn.execute("COMMIT")
                return {"release_id": release_id, "status": "unchanged", **counts}
            published = _day(header["published_on"])
            clock = release_ms(published, header.get("published_at"))
            basis = str(header.get("release_basis") or "declared_publication")
            vintage_ids = []
            seen = set()
            for item in items:
                level_id = self._level(
                    namespace, item["geography"], release_id, retrieved
                )
                definition_id, key, new_definition = self._definition(
                    namespace,
                    header["provider"],
                    item,
                    published,
                    release_id,
                    retrieved,
                )
                counts["definitions"] += int(new_definition)
                series_id, new_series = self._series(
                    namespace,
                    header["provider"],
                    item,
                    level_id,
                    key,
                    release_id,
                    retrieved,
                )
                if series_id in seen:
                    raise DemographicError(
                        "invalid_release", "a release states the same series twice"
                    )
                seen.add(series_id)
                counts["series"] += int(new_series)
                vintage_id, new_vintage, corrected = self._vintage(
                    namespace,
                    series_id,
                    item,
                    definition_id,
                    release_id,
                    clock,
                    basis,
                    retrieved,
                )
                vintage_ids.append(vintage_id)
                # Every release that states a series is recorded, also when it repeats a stored vintage.
                self.conn.execute(
                    "INSERT INTO demographic_release_members VALUES (?,?,?,?)",
                    [namespace, release_id, series_id, vintage_id],
                )
                counts["vintages"] += int(new_vintage)
                counts["definition_corrections"] += int(corrected)
                if new_vintage or corrected:
                    counts["breaks"] += self._breaks(namespace, series_id, vintage_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "release_id": release_id,
            "status": "applied",
            "published_on": published,
            "release_basis": basis,
            "vintage_ids": vintage_ids,
            **counts,
        }

    def import_sheet(
        self,
        namespace: str,
        sheet: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an operator's figure sheet for a PDF-only publication (BAMF); idempotent by sheet content."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        if forbidden_keys(dict(sheet)):
            raise DemographicError(
                "invalid_sheet",
                "a figure sheet carries no projection, cause or merged value",
            )
        try:
            release = parse_operator_sheet(sheet)
        except DemographicFormatError as exc:
            raise DemographicError(exc.code, str(exc)) from exc
        raw = canonical(dict(sheet)).encode()
        header = {
            "provider": sheet["provider"],
            "format": "operator-figure-sheet",
            "document": {
                **dict(sheet.get("release") or {}),
                "references": list(sheet.get("references") or []),
                "geography_level": sheet.get("geography_level"),
            },
            "published_on": release["published_on"],
            "published_at": None,
            "release_basis": "declared_publication",
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "content_sha256": digest(release["series"]),
            "item_count": len(release["series"]),
            "structure": release["structure"],
            "evidence_origin": "operator",
            "url": dict(sheet["release"]).get("url"),
        }
        return self.apply_release(
            namespace,
            header,
            release["series"],
            run_id=run_id or f"operator:{principal_id}",
            source_id=f"operator-sheet:{sheet['provider']}",
            recorded_by=principal_id,
        )

    def _level(self, namespace, geography, release_id, retrieved) -> str:
        level_id = (
            "dm-level:"
            + digest(
                [
                    namespace,
                    geography["scheme"],
                    geography["level"],
                    geography.get("code_list_version"),
                ]
            )[:24]
        )
        if not self.conn.execute(
            "SELECT 1 FROM demographic_geography_levels WHERE namespace=? AND level_id=?",
            [namespace, level_id],
        ).fetchone():
            self.conn.execute(
                "INSERT INTO demographic_geography_levels VALUES (?,?,?,?,?,?,?)",
                [
                    namespace,
                    level_id,
                    geography["scheme"],
                    geography["level"],
                    geography.get("code_list_version"),
                    release_id,
                    retrieved,
                ],
            )
        return level_id

    def _definition(self, namespace, provider, item, published, release_id, retrieved):
        dims = dict(item.get("dimensions") or {})
        key = (
            "dm-definition:"
            + digest(
                [namespace, provider, item["series_code"], item["indicator"], dims]
            )[:24]
        )
        content = dict(item["definition"])
        content_hash = digest(content)
        same = self.conn.execute(
            "SELECT definition_id FROM demographic_definitions WHERE namespace=? AND definition_key=? AND "
            "content_hash=? AND valid_from=? ORDER BY revision_no LIMIT 1",
            [namespace, key, content_hash, published],
        ).fetchone()
        if same:
            return same[0], key, False
        # Dedupe only against the revision in force at this publication date: a return to earlier wording after
        # a change is itself a new revision.
        current = self.conn.execute(
            "SELECT definition_id, content_hash FROM demographic_definitions WHERE namespace=? AND "
            "definition_key=? AND valid_from<=? ORDER BY valid_from DESC, revision_no DESC LIMIT 1",
            [namespace, key, published],
        ).fetchone()
        if current and current[1] == content_hash:
            return current[0], key, False
        if current is None:
            # An older publication arriving late: the same wording as the earliest known revision means that
            # revision was already in force at this earlier date (its valid-from moves back), never a new one.
            following = self.conn.execute(
                "SELECT definition_id, content_hash FROM demographic_definitions WHERE namespace=? AND "
                "definition_key=? AND valid_from>? ORDER BY valid_from, revision_no LIMIT 1",
                [namespace, key, published],
            ).fetchone()
            if following and following[1] == content_hash:
                self.conn.execute(
                    "UPDATE demographic_definitions SET valid_from=? WHERE namespace=? AND definition_id=?",
                    [published, namespace, following[0]],
                )
                return following[0], key, False
        number = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(revision_no), 0) FROM demographic_definitions WHERE namespace=? AND "
                "definition_key=?",
                [namespace, key],
            ).fetchone()[0]
        )
        definition_id = "dm-definition-rev:" + digest([key, number, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO demographic_definitions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                definition_id,
                key,
                number,
                provider,
                item["series_code"],
                item["indicator"],
                canonical(dims),
                canonical(content),
                content_hash,
                published,
                release_id,
                retrieved,
            ],
        )
        return definition_id, key, True

    def _series(
        self, namespace, provider, item, level_id, definition_key, release_id, retrieved
    ):
        geography = item["geography"]
        unit = item["unit"]
        dims = dict(item.get("dimensions") or {})
        series_id = (
            "dm-series:"
            + digest(
                [
                    namespace,
                    provider,
                    item["series_code"],
                    item["indicator"],
                    dims,
                    geography["code"],
                    level_id,
                    definition_key,
                    unit["code"],
                    unit["label"],
                    item["frequency"],
                ]
            )[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM demographic_series WHERE namespace=? AND series_id=?",
            [namespace, series_id],
        ).fetchone():
            return series_id, False
        self.conn.execute(
            "INSERT INTO demographic_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                series_id,
                provider,
                item["series_code"],
                item["indicator"],
                canonical(dims),
                geography["code"],
                geography.get("label"),
                level_id,
                definition_key,
                unit["code"],
                unit["label"],
                item["frequency"],
                release_id,
                retrieved,
            ],
        )
        return series_id, True

    def _vintage(
        self,
        namespace,
        series_id,
        item,
        definition_id,
        release_id,
        clock,
        basis,
        retrieved,
    ):
        observations = list(item["observations"])
        # The content is the published values; the declared definition is a reading of them, recorded separately,
        # so a corrected declaration of an already stored publication is a correction, never a conflict.
        content = {
            "observations": [
                {k: o.get(k) for k in ("period", "value_text", "value", "flags")}
                for o in observations
            ],
        }
        content_hash = digest(content)
        existing = self.conn.execute(
            "SELECT vintage_id, content_hash FROM demographic_vintages WHERE namespace=? AND series_id=? AND "
            "release_at_ms=? ORDER BY sequence LIMIT 1",
            [namespace, series_id, clock],
        ).fetchone()
        if existing:
            if existing[1] != content_hash:
                # The provider changed values without a new release time: the stored vintage is kept and the
                # publication is refused rather than silently replacing it.
                raise DemographicError(
                    "vintage_conflict",
                    "the publication changed values without a new release time; the stored vintage is kept",
                    series_id=series_id,
                )
            if self._current_definition(namespace, existing[0]) != definition_id:
                # The declared reading changed (e.g. a corrected population base): the stored values stay, the new
                # definition revision is recorded as a correction made by this release.
                self.conn.execute(
                    "INSERT INTO demographic_vintage_readings VALUES (?,?,?,?,?)",
                    [namespace, existing[0], definition_id, release_id, self.now()],
                )
                return existing[0], False, True
            return existing[0], False, False
        sequence = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(sequence), 0) FROM demographic_vintages WHERE namespace=? AND series_id=?",
                [namespace, series_id],
            ).fetchone()[0]
        )
        vintage_id = (
            "dm-vintage:" + digest([namespace, series_id, clock, content_hash])[:24]
        )
        self.conn.execute(
            "INSERT INTO demographic_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                vintage_id,
                series_id,
                release_id,
                clock,
                basis,
                retrieved,
                "acquisition_time",
                definition_id,
                content_hash,
                canonical(list(item.get("coverage_notes") or [])),
                canonical(dict(item.get("locator") or {})),
                sequence,
                retrieved,
            ],
        )
        unit_label = item["unit"]["label"]
        for obs in observations:
            extra = {
                k: v
                for k, v in obs.items()
                if k not in {"period", "value_text", "value", "flags"}
            }
            self.conn.execute(
                "INSERT INTO demographic_observations VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    vintage_id,
                    obs["period"],
                    obs.get("value_text"),
                    obs.get("value"),
                    canonical(list(obs.get("flags") or [])),
                    None
                    if obs.get("value") is None
                    else canonical(normalise_value(obs["value"], unit_label)),
                    definition_id,
                    canonical(extra),
                ],
            )
        return vintage_id, True, False

    def _add_break(
        self,
        namespace,
        series_id,
        kind,
        *,
        period,
        from_id,
        to_id,
        flag,
        note,
        vintage_id,
    ):
        break_id = (
            "dm-break:"
            + digest([namespace, series_id, kind, period, from_id, to_id])[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM demographic_breaks WHERE namespace=? AND break_id=?",
            [namespace, break_id],
        ).fetchone():
            return 0
        self.conn.execute(
            "INSERT INTO demographic_breaks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                break_id,
                series_id,
                kind,
                period,
                from_id,
                to_id,
                flag,
                note,
                vintage_id,
                self.now(),
            ],
        )
        return 1

    def _breaks(self, namespace: str, series_id: str, vintage_id: str) -> int:
        """Mark publisher break flags and definition changes against the neighbouring vintages by release clock."""
        added = 0
        for period, flags in self.conn.execute(
            "SELECT period, flags_json FROM demographic_observations WHERE namespace=? AND vintage_id=? "
            "ORDER BY period",
            [namespace, vintage_id],
        ).fetchall():
            if "b" in _load(flags, []):
                added += self._add_break(
                    namespace,
                    series_id,
                    "publisher_flag",
                    period=period,
                    from_id=None,
                    to_id=None,
                    flag="b",
                    note="the publisher flags a break in the series at this period",
                    vintage_id=vintage_id,
                )
        ordered = self.vintage_rows(namespace, series_id)
        for previous, following in zip(ordered, ordered[1:]):
            if previous["definition_id"] == following["definition_id"]:
                continue
            before = self.definition(namespace, previous["definition_id"])
            after = self.definition(namespace, following["definition_id"])
            if before["content_hash"] == after["content_hash"]:
                continue  # two revisions with the same wording (e.g. after a corrected declaration) are no break
            base_changed = before["content"].get("population_base") != after[
                "content"
            ].get("population_base")
            added += self._add_break(
                namespace,
                series_id,
                "census_base_change" if base_changed else "definition_revision",
                period=None,
                from_id=previous["definition_id"],
                to_id=following["definition_id"],
                flag=None,
                note=(
                    "the population base changed "
                    f"({before['content'].get('population_base')} -> {after['content'].get('population_base')})"
                    if base_changed
                    else "the publisher's definition changed between these vintages"
                ),
                vintage_id=following["vintage_id"],
            )
        return added

    # ------------------------------------------------------------------ reads

    _RELEASE_KEYS = (
        "release_id",
        "provider",
        "source_id",
        "format",
        "document",
        "published_on",
        "published_at",
        "release_basis",
        "release_at_ms",
        "file_sha256",
        "content_sha256",
        "item_count",
        "structure",
        "evidence_origin",
        "url",
        "sequence",
        "run_id",
        "recorded_by",
        "retrieved_at_ms",
    )

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT release_id, provider, source_id, format, document_json, published_on, published_at, "
            "release_basis, release_at_ms, file_sha256, content_sha256, item_count, structure_json, evidence_origin, "
            "url, sequence, run_id, recorded_by, retrieved_at_ms FROM demographic_releases WHERE namespace=? AND "
            "release_id=?",
            [namespace, release_id],
        ).fetchone()
        if row is None:
            raise DemographicError(
                "not_found", "release is not visible in this namespace"
            )
        view = dict(zip(self._RELEASE_KEYS, row))
        view["document"] = _load(view["document"], {})
        view["structure"] = _load(view["structure"], {})
        return {
            "contract": CONTRACT,
            "record_type": "release",
            "namespace": namespace,
            **view,
        }

    def source_revision(self, namespace: str, release_id: str) -> dict[str, Any]:
        release = self.release(namespace, release_id)
        return {
            k: release[k]
            for k in (
                "release_id",
                "provider",
                "source_id",
                "file_sha256",
                "published_on",
                "published_at",
                "release_basis",
                "url",
                "evidence_origin",
                "retrieved_at_ms",
            )
        } | {
            "document": release["document"].get("label")
            or release["document"].get("document")
        }

    def releases(
        self, namespace: str, *, provider: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT release_id FROM demographic_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY release_at_ms, sequence",
            [namespace, provider, provider],
        ).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def geography_level(self, namespace: str, level_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT level_id, scheme, level, code_list_version, first_release_id FROM demographic_geography_levels "
            "WHERE namespace=? AND level_id=?",
            [namespace, level_id],
        ).fetchone()
        if row is None:
            raise DemographicError(
                "not_found", "geography level is not visible in this namespace"
            )
        return {
            "contract": CONTRACT,
            "record_type": "geography_level",
            "namespace": namespace,
            **dict(
                zip(
                    (
                        "level_id",
                        "scheme",
                        "level",
                        "code_list_version",
                        "first_release_id",
                    ),
                    row,
                )
            ),
        }

    def definition(self, namespace: str, definition_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT definition_id, definition_key, revision_no, provider, series_code, indicator, dimensions_json, "
            "content_json, content_hash, valid_from, release_id FROM demographic_definitions WHERE namespace=? AND "
            "definition_id=?",
            [namespace, definition_id],
        ).fetchone()
        if row is None:
            raise DemographicError(
                "not_found", "definition revision is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "definition_id",
                    "definition_key",
                    "revision_no",
                    "provider",
                    "series_code",
                    "indicator",
                    "dimensions",
                    "content",
                    "content_hash",
                    "valid_from",
                    "release_id",
                ),
                row,
            )
        )
        view["dimensions"] = _load(view["dimensions"], {})
        view["content"] = _load(view["content"], {})
        view["source_revision"] = self.source_revision(namespace, view["release_id"])
        return {
            "contract": CONTRACT,
            "record_type": "definition_revision",
            "namespace": namespace,
            **view,
        }

    def definition_history(
        self, namespace: str, definition_key: str
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT definition_id FROM demographic_definitions WHERE namespace=? AND definition_key=? "
            "ORDER BY valid_from, revision_no",
            [namespace, definition_key],
        ).fetchall()
        return [self.definition(namespace, r[0]) for r in rows]

    _SERIES_KEYS = (
        "series_id",
        "provider",
        "series_code",
        "indicator",
        "dimensions",
        "geography_code",
        "geography_label",
        "level_id",
        "definition_key",
        "unit_code",
        "unit_label",
        "frequency",
        "first_release_id",
    )

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT "
            + ", ".join(
                "dimensions_json" if k == "dimensions" else k for k in self._SERIES_KEYS
            )
            + " FROM demographic_series WHERE namespace=? AND series_id=?",
            [namespace, series_id],
        ).fetchone()
        if row is None:
            raise DemographicError(
                "not_found", "series is not visible in this namespace"
            )
        view = dict(zip(self._SERIES_KEYS, row))
        view["dimensions"] = _load(view["dimensions"], {})
        view["geography_level"] = self.geography_level(namespace, view["level_id"])
        vintages = self.vintage_rows(namespace, series_id)
        current = vintages[-1] if vintages else None
        view["current_vintage_id"] = current["vintage_id"] if current else None
        view["definition"] = (
            self.definition(namespace, current["definition_id"]) if current else None
        )
        view["coverage_notes"] = current["notes"] if current else []
        view["vintage_count"] = len(vintages)
        view["breaks"] = self.breaks(namespace, series_id)
        view["unit"] = {"code": view["unit_code"], "label": view["unit_label"]}
        return {
            "contract": CONTRACT,
            "record_type": "series",
            "namespace": namespace,
            **view,
        }

    def find_series(
        self,
        namespace: str,
        *,
        provider: str | None = None,
        concept: str | None = None,
        geography_code: str | None = None,
        scheme: str | None = None,
        level: str | None = None,
        series_code: str | None = None,
        definition_key: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT s.series_id FROM demographic_series s JOIN demographic_geography_levels l ON "
            "l.namespace=s.namespace AND l.level_id=s.level_id WHERE s.namespace=? AND (? IS NULL OR s.provider=?) "
            "AND (? IS NULL OR s.geography_code=?) AND (? IS NULL OR l.scheme=?) AND (? IS NULL OR l.level=?) AND "
            "(? IS NULL OR s.series_code=?) AND (? IS NULL OR s.definition_key=?) ORDER BY s.provider, "
            "s.series_code, s.indicator, s.geography_code, s.series_id",
            [
                namespace,
                provider,
                provider,
                geography_code,
                geography_code,
                scheme,
                scheme,
                level,
                level,
                series_code,
                series_code,
                definition_key,
                definition_key,
            ],
        ).fetchall()
        out = []
        for (series_id,) in rows:
            # Select the current definition first, then filter on it.
            view = self.series(namespace, series_id)
            if (
                concept
                and (view["definition"] or {}).get("content", {}).get("concept")
                != concept
            ):
                continue
            out.append(view)
            if len(out) >= limit:
                break
        return out

    def _readings(self, namespace: str, vintage_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "demographic_vintage_readings"):
            return []
        return [
            {"definition_id": r[0], "release_id": r[1], "recorded_at_ms": r[2]}
            for r in self.conn.execute(
                "SELECT definition_id, release_id, created_at_ms FROM demographic_vintage_readings WHERE namespace=? "
                "AND vintage_id=? ORDER BY created_at_ms, definition_id",
                [namespace, vintage_id],
            ).fetchall()
        ]

    def _current_definition(self, namespace: str, vintage_id: str) -> str:
        readings = self._readings(namespace, vintage_id)
        if readings:
            return readings[-1]["definition_id"]
        return self.conn.execute(
            "SELECT definition_id FROM demographic_vintages WHERE namespace=? AND vintage_id=?",
            [namespace, vintage_id],
        ).fetchone()[0]

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        """Every vintage of a series in release-clock order, each with ``revision_of`` its predecessor."""
        rows = self.conn.execute(
            "SELECT vintage_id, release_id, release_at_ms, release_at_basis, retrieved_at_ms, retrieved_at_basis, "
            "definition_id, content_hash, notes_json, locator_json, sequence FROM demographic_vintages WHERE "
            "namespace=? AND series_id=? ORDER BY release_at_ms, sequence",
            [namespace, series_id],
        ).fetchall()
        out = []
        previous = None
        for row in rows:
            view = dict(
                zip(
                    (
                        "vintage_id",
                        "release_id",
                        "release_at_ms",
                        "release_at_basis",
                        "retrieved_at_ms",
                        "retrieved_at_basis",
                        "definition_id",
                        "content_hash",
                        "notes",
                        "locator",
                        "sequence",
                    ),
                    row,
                )
            )
            view["notes"] = _load(view["notes"], [])
            view["locator"] = _load(view["locator"], {})
            view["series_id"] = series_id
            # The definition as first declared stays addressable; a later correction of the declaration is the
            # reading in force.
            view["published_definition_id"] = view["definition_id"]
            view["definition_corrections"] = self._readings(
                namespace, view["vintage_id"]
            )
            if view["definition_corrections"]:
                view["definition_id"] = view["definition_corrections"][-1][
                    "definition_id"
                ]
            view["revision_of"] = previous["vintage_id"] if previous else None
            view["values_changed"] = (
                previous is None or previous["content_hash"] != view["content_hash"]
            )
            out.append(view)
            previous = view
        return out

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT series_id FROM demographic_vintages WHERE namespace=? AND vintage_id=?",
            [namespace, vintage_id],
        ).fetchone()
        if row is None:
            raise DemographicError(
                "not_found", "vintage is not visible in this namespace"
            )
        view = next(
            v
            for v in self.vintage_rows(namespace, row[0])
            if v["vintage_id"] == vintage_id
        )
        return {
            "contract": CONTRACT,
            "record_type": "vintage",
            "namespace": namespace,
            **view,
            "source_revision": self.source_revision(namespace, view["release_id"]),
        }

    def observations(
        self,
        namespace: str,
        vintage_id: str,
        *,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT period, value_text, value, flags_json, normalized_json, definition_id, extra_json FROM "
            "demographic_observations WHERE namespace=? AND vintage_id=? ORDER BY period",
            [namespace, vintage_id],
        ).fetchall()
        out = []
        for period, text, value, flags, normalized, definition_id, extra in rows:
            if period_from and period < period_from:
                continue
            if period_to and period[: len(period_to)] > period_to:
                continue
            out.append(
                {
                    "record_type": "observation",
                    "period": period,
                    "value_text": text,
                    "value": value,
                    "flags": _load(flags, []),
                    "normalized": _load(normalized, None),
                    "definition_id": definition_id,
                    **_load(extra, {}),
                }
            )
        return out

    def release_series(self, namespace: str, release_id: str) -> list[dict[str, str]]:
        """The series (and the vintage) each release states, whether or not it added the vintage."""
        return [
            {"series_id": r[0], "vintage_id": r[1]}
            for r in self.conn.execute(
                "SELECT series_id, vintage_id FROM demographic_release_members WHERE namespace=? AND release_id=? "
                "ORDER BY series_id",
                [namespace, release_id],
            ).fetchall()
        ]

    def breaks(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT break_id, kind, period, from_definition_id, to_definition_id, flag, note, first_vintage_id FROM "
            "demographic_breaks WHERE namespace=? AND series_id=? ORDER BY coalesce(period, ''), kind, break_id",
            [namespace, series_id],
        ).fetchall()
        ordered = self.vintage_rows(namespace, series_id)
        adjacent = {
            (a["definition_id"], b["definition_id"])
            for a, b in zip(ordered, ordered[1:])
        }
        # A definition break whose revisions are no longer adjacent (a corrected declaration removed the change)
        # stays stored but is not reported as a break of the series.
        rows = [r for r in rows if r[3] is None or (r[3], r[4]) in adjacent]
        return [
            {
                "record_type": "series_break",
                "series_id": series_id,
                **dict(
                    zip(
                        (
                            "break_id",
                            "kind",
                            "period",
                            "from_definition_id",
                            "to_definition_id",
                            "flag",
                            "note",
                            "first_vintage_id",
                        ),
                        row,
                    )
                ),
                "handling": "marked; no chaining, rebasing or splicing across it",
            }
            for row in rows
        ]

    def select_vintage(
        self,
        namespace: str,
        series_id: str,
        *,
        as_of_ms: int | None = None,
        acquired_cutoff_ms: int | None = None,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The vintage released on or before the cutoff (release-cutoff semantics of the economic release store)."""
        vintages = self.vintage_rows(namespace, series_id)
        release_cutoff = as_of_ms if as_of_ms is not None else 2**62
        acquired_cutoff = (
            acquired_cutoff_ms if acquired_cutoff_ms is not None else 2**62
        )
        eligible = [
            v
            for v in vintages
            if v["release_at_ms"] <= release_cutoff
            and v["retrieved_at_ms"] <= acquired_cutoff
        ]
        if eligible:
            return eligible[-1], None
        if not vintages:
            return None, "historical_vintage_unavailable"
        if any(v["release_at_ms"] <= release_cutoff for v in vintages):
            return None, "late_acquired"
        return None, "historical_vintage_unavailable"

    def values(
        self,
        namespace: str,
        series_id: str,
        *,
        vintage_id: str | None = None,
        as_of_ms: int | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> dict[str, Any]:
        """A series' values in one vintage with definition, unit, level, notes, breaks and source revision."""
        series = self.series(namespace, series_id)
        reason = None
        if vintage_id:
            vintage = next(
                (
                    v
                    for v in self.vintage_rows(namespace, series_id)
                    if v["vintage_id"] == vintage_id
                ),
                None,
            )
            if vintage is None:
                raise DemographicError(
                    "not_found", "vintage does not belong to this series"
                )
        else:
            vintage, reason = self.select_vintage(
                namespace, series_id, as_of_ms=as_of_ms
            )
        if vintage is None:
            return {
                "contract": ANSWER_CONTRACT,
                "series": series,
                "status": "unavailable",
                "reason": reason,
                "observations": [],
            }
        return {
            "contract": ANSWER_CONTRACT,
            "series": series,
            "status": "available",
            "vintage": {
                **vintage,
                "source_revision": self.source_revision(
                    namespace, vintage["release_id"]
                ),
            },
            "definition": self.definition(namespace, vintage["definition_id"]),
            "unit": series["unit"],
            "geography_level": series["geography_level"],
            "coverage_notes": vintage["notes"],
            "breaks": series["breaks"],
            "observations": self.observations(
                namespace,
                vintage["vintage_id"],
                period_from=period_from,
                period_to=period_to,
            ),
            "note": "values as published under this vintage's definition revision; nothing merged or recomputed",
        }


class DemographicProjector:
    """Source-pack runtime projector for ``noesis-demographic-series-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = DemographicStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("demographics") or {}).get("namespace") or DEFAULT_NAMESPACE
        )

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
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = (
                dict(item.get("demographic_release") or {}),
                item.get("demographic_series"),
            )
            if not header or not isinstance(body, Mapping):
                raise DemographicError(
                    "invalid_record", "page record is not a demographic series"
                )
            groups.setdefault(
                header["file_sha256"] + canonical(header.get("document")), (header, [])
            )[1].append(dict(body))
        namespace = self._namespace(source)
        return [
            self.store.apply_release(
                namespace, header, items, run_id=run_id, source_id=source["source_id"]
            )
            for header, items in groups.values()
        ]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, published_on FROM demographic_releases WHERE namespace=? AND source_id=? "
            "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1",
            [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {
            "status": status,
            "latest_release_id": row[0] if row else None,
            "latest_published_on": row[1] if row else None,
        }


def register_schemas(
    conn: Any, *, principal_id: str, scopes: Any
) -> list[dict[str, Any]]:
    """Register the demographic record contract as a schema module in the shared registry."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = (
        Path(__file__).resolve().parents[2]
        / "contracts/schemas/jsonschema"
        / f"{CONTRACT}.json"
    )
    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "demographic-series",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": json.loads(path.read_text()),
        "owner": "economics.demographics",
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
            "demographics-schema:demographic-series:1.0.0",
            principal_id=principal_id,
            scopes=scopes,
        )
    ]


def readiness(conn: Any) -> dict[str, Any]:
    store = DemographicStore(conn, initialize=False)
    ready = store.ready() and table_exists(conn, "demographic_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        if ready:
            releases = int(
                conn.execute(
                    "SELECT count(*) FROM demographic_releases WHERE provider=?",
                    [provider],
                ).fetchone()[0]
            )
        providers[provider] = {
            "delivers": contract["delivers"],
            "access_decision": contract["access_decision"],
            "reason": contract["reason"],
            "releases": releases,
        }
    return {
        "feature": "demographics",
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "providers": providers,
        "review_boundary": REVIEW_BOUNDARY,
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
        "is live until a dated run verifies it",
    }
