"""Public-health surveillance series, case definitions, reporting-delay notes and indicator vintages (#1917, I02).

The ``clinical.surveillance`` record owner (contract ``noesis-surveillance-record-v1``).
Records live in namespace-scoped ``surveillance_*`` tables and follow the C01.2
store rules: content-derived ids, immutable numbered revisions and a
native-revision link (release tag or commit, dataset update stamp, table
``Updated`` stamp, export extraction date) on every record.

* **release** - one acquired publication or operator-supplied export: provider,
  document, native revision, the publication's own release clock with its basis
  label, digests and evidence origin (``fixture``, ``operator`` or ``live``).
  Re-acquiring an unchanged publication adds nothing.
* **surveillance-series** - source, condition (scheme and code), geography code
  and code system, indicator, unit, interval and **kind** (``observation``,
  ``estimate`` or ``model-output``). A series without a kind is refused; the
  same condition, place and period from two sources or of two kinds is always
  two series.
* **case-definition** and **case-definition-revision** - the source's case
  definition (for example an RKI Falldefinition edition) with valid-from,
  valid-to, text or locator, ICD scope and its predecessor. Every value
  references the revision in force on its reference date; a change of edition
  is a marked series break and is never applied to earlier values. A corrected
  declaration of an edition is a new revision of that edition (a correction),
  never a conflict.
* **reporting-delay-note** - the source's own statement about reporting delay
  (for example that the most recent weeks are incomplete).
* **indicator-vintage** - one release of a series, on the release/retrieval
  clock and ``revision_of`` pattern of the economic and environment vintages:
  earlier vintages stay addressable, the order follows the source's release
  clock in whatever order releases arrived, and a republication that repeats
  the vintage in force adds nothing.

Every value carries ``reference_period`` and ``reporting_date`` as separate
fields and says which is unknown. Nothing here predicts, nowcasts, completes,
corrects, suggests a threshold or advises: keys that would carry such a value
are refused.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from src.ingestion.surveillance_sources import (
    BOUNDARY,
    FORMATS,
    KINDS,
    UNITS,
    SurveillanceFormatError,
    check_item,
    parse_ecdc_export,
    period_bounds,
)
from src.kb.clinical_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    ClinicalRecordError,
)

CONTRACT = "noesis-surveillance-record-v1"
ANSWER_CONTRACT = "noesis-surveillance-answer-v1"
DEFAULT_NAMESPACE = "clinical"
RECORD_TYPES = (
    "release",
    "surveillance-series",
    "case-definition",
    "case-definition-revision",
    "reporting-delay-note",
    "indicator-vintage",
    "surveillance-value",
    "series-break",
)
BREAK_KINDS = ("case-definition", "publisher-flag", "geography")
NEVER = (
    "predict an outbreak or a future value",
    "nowcast or estimate completeness",
    "derive, suggest or adjust a threshold",
    "give health advice",
    "correct a value silently across a case-definition change",
    "present an estimate or model output as an observation",
    "merge sources or conflate reporting date and reference date",
)
NEVER_SENTENCE = (
    "Published surveillance values as each source released them: no outbreak prediction, nowcast, completeness "
    "estimate, threshold suggestion or health advice; case-definition changes are marked breaks, never corrections."
)
# Keys that would carry a prediction, nowcast, advice, a derived threshold or a corrected value.
FORBIDDEN_KEYS = frozenset(
    {
        "prediction",
        "predicted",
        "forecast",
        "forecasted",
        "nowcast",
        "nowcasted",
        "nowcasting",
        "projection",
        "projected",
        "advice",
        "health_advice",
        "recommendation",
        "recommended",
        "corrected_value",
        "corrected",
        "adjusted_value",
        "imputed",
        "imputed_value",
        "completeness_estimate",
        "estimated_completeness",
        "expected_value",
        "expected_count",
        "derived_threshold",
        "suggested_threshold",
        "outbreak_probability",
        "risk_score",
        "cause",
        "causal",
        "merged_value",
        "combined_value",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS surveillance_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_json TEXT NOT NULL, native_revision TEXT, published_on TEXT NOT NULL, published_at TEXT,
  release_basis TEXT NOT NULL, release_at_ms BIGINT NOT NULL, file_sha256 TEXT NOT NULL, content_sha256 TEXT NOT NULL,
  item_count INTEGER NOT NULL, structure_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, url TEXT,
  sequence INTEGER NOT NULL, run_id TEXT NOT NULL, recorded_by TEXT, retrieved_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS surveillance_case_definitions (
  namespace TEXT NOT NULL, definition_key TEXT NOT NULL, provider TEXT NOT NULL, condition_json TEXT NOT NULL,
  first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, definition_key)
);
CREATE TABLE IF NOT EXISTS surveillance_case_definition_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, definition_key TEXT NOT NULL, version TEXT NOT NULL,
  revision_no INTEGER NOT NULL, content_json TEXT NOT NULL, content_hash TEXT NOT NULL, valid_from TEXT NOT NULL,
  valid_to TEXT, declared_on TEXT NOT NULL, release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS surveillance_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, condition_json TEXT NOT NULL,
  condition_scheme TEXT NOT NULL, condition_code TEXT NOT NULL, indicator_code TEXT NOT NULL,
  geography_system TEXT NOT NULL, geography_code TEXT NOT NULL, unit_label TEXT NOT NULL, interval TEXT NOT NULL,
  kind TEXT NOT NULL, dimensions_json TEXT NOT NULL, definition_key TEXT, first_release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS surveillance_delay_notes (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, provider TEXT NOT NULL, content_json TEXT NOT NULL,
  first_release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, note_id)
);
CREATE TABLE IF NOT EXISTS surveillance_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, release_basis TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  native_revision TEXT, content_hash TEXT NOT NULL, metadata_json TEXT NOT NULL, sequence INTEGER NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS surveillance_values (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, value_key TEXT NOT NULL, reference_period TEXT,
  reporting_date TEXT, value_text TEXT, value TEXT, lower_value TEXT, upper_value TEXT, flags_json TEXT NOT NULL,
  normalized_json TEXT, case_definition_revision_id TEXT, extra_json TEXT NOT NULL,
  PRIMARY KEY(namespace, vintage_id, value_key)
);
CREATE TABLE IF NOT EXISTS surveillance_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  PRIMARY KEY(namespace, release_id, series_id)
);
CREATE TABLE IF NOT EXISTS surveillance_breaks (
  namespace TEXT NOT NULL, break_id TEXT NOT NULL, series_id TEXT NOT NULL, kind TEXT NOT NULL, period TEXT,
  from_ref TEXT, to_ref TEXT, detail_json TEXT NOT NULL, note TEXT NOT NULL, first_vintage_id TEXT,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, break_id)
);
CREATE TABLE IF NOT EXISTS surveillance_pins (
  namespace TEXT NOT NULL, pin_id TEXT NOT NULL, view_key TEXT NOT NULL, series_id TEXT NOT NULL,
  vintage_id TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, pin_id)
);
"""


class SurveillanceError(ClinicalRecordError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(code, message)
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
        raise SurveillanceError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes: Iterable[str], required: str) -> None:
    """A scope needed only for an optional part of an answer, checked when that part is requested."""
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise SurveillanceError(
            "unauthorized", f"{required} is required for this part of the answer"
        )


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
    )


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
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
        raise SurveillanceError(
            "invalid_date", f"{value!r} is not an ISO date"
        ) from exc


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
    """A count in cases through the pint owner (exact decimal fallback); a rate stays a rate."""
    if value is None:
        return None
    pint_unit, factor, kind = UNITS[unit_label]
    if kind != "count" or factor == 1:
        return {
            "value": str(Decimal(str(value))),
            "unit": unit_label,
            "method": "as published",
        }
    try:
        from src.integrations.units import convert_physical

        receipt = convert_physical(
            str(Decimal(str(value)) * factor), pint_unit, "count", precision=0
        )
        return {
            "value": receipt["result"]["value"],
            "unit": "count",
            "method": f"pint convert_physical x{factor} {pint_unit} -> count",
        }
    except ModuleNotFoundError:
        exact = (Decimal(str(value)) * factor).quantize(
            Decimal(1), rounding=ROUND_HALF_EVEN
        )
        return {
            "value": str(exact),
            "unit": "count",
            "method": f"exact decimal scale x{factor} (pint not installed)",
        }


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Clinical Evidence bundle's optional ``surveillance`` feature is selected in the active plan."""
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
            "SELECT authority FROM composition_authority WHERE bundle='clinical-evidence'"
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
    return "surveillance" in (
        (plan.get("features") or {}).get("clinical-evidence") or []
    )


def reference_start(period: str | None) -> str | None:
    return None if period is None else period_bounds(period)[0].isoformat()


def period_end(period: str | None) -> str | None:
    return None if period is None else period_bounds(period)[1].isoformat()


class SurveillanceStore:
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
        return table_exists(self.conn, "surveillance_vintages")

    # ------------------------------------------------------------------ writes

    def _release(self, namespace, header, *, source_id, run_id, retrieved, recorded_by):
        provider = str(header.get("provider") or "")
        if (
            header.get("format") not in FORMATS
            or FORMATS[header["format"]]["provider"] != provider
        ):
            raise SurveillanceError(
                "invalid_release", "release names a known provider and format"
            )
        document = dict(header.get("document") or {})
        # The release id changes whenever the publication or its declared reading changes.
        release_id = (
            "sv-release:"
            + digest([namespace, provider, source_id, header["file_sha256"], document])[
                :24
            ]
        )
        if self.conn.execute(
            "SELECT 1 FROM surveillance_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone():
            return release_id, False
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM surveillance_releases WHERE namespace=? AND provider=?",
            [namespace, provider],
        ).fetchone()[0]
        published = _day(header["published_on"])
        origin = header.get("evidence_origin")
        origin = origin if origin in {"fixture", "operator"} else "live"
        self.conn.execute(
            "INSERT INTO surveillance_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                release_id,
                provider,
                source_id,
                header["format"],
                canonical(document),
                header.get("native_revision"),
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
        """Record one publication: case-definition revisions, series, a vintage per series, notes and breaks."""
        if int(header.get("item_count", -1)) != len(items):
            raise SurveillanceError(
                "incomplete_release", "a release carries every series it states"
            )
        for item in items:
            found = forbidden_keys(dict(item))
            if found:
                raise SurveillanceError(
                    "outside_boundary",
                    "surveillance records carry no prediction, nowcast, "
                    "advice, derived threshold or corrected value",
                    keys=found,
                )
            try:
                check_item(item)
            except SurveillanceFormatError as exc:
                raise SurveillanceError(exc.code, str(exc)) from exc
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        counts = {
            "series": 0,
            "vintages": 0,
            "definition_revisions": 0,
            "breaks": 0,
            "delay_notes": 0,
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
            vintage_ids, seen = [], set()
            for item in items:
                revisions, added = self._case_definition(
                    namespace,
                    header["provider"],
                    item,
                    published,
                    release_id,
                    retrieved,
                )
                counts["definition_revisions"] += added
                note_id, new_note = self._delay_note(
                    namespace, header["provider"], item, release_id, retrieved
                )
                counts["delay_notes"] += int(new_note)
                series_id, new_series = self._series(
                    namespace, header["provider"], item, release_id, retrieved
                )
                if series_id in seen:
                    raise SurveillanceError(
                        "invalid_release", "a release states the same series twice"
                    )
                seen.add(series_id)
                counts["series"] += int(new_series)
                vintage_id, new_vintage = self._vintage(
                    namespace,
                    series_id,
                    item,
                    revisions,
                    note_id,
                    release_id,
                    header,
                    clock,
                    basis,
                    retrieved,
                )
                vintage_ids.append(vintage_id)
                self.conn.execute(
                    "INSERT INTO surveillance_release_members VALUES (?,?,?,?)",
                    [namespace, release_id, series_id, vintage_id],
                )
                counts["vintages"] += int(new_vintage)
                if new_vintage:
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

    def import_export(
        self,
        namespace: str,
        export: Mapping[str, Any],
        *,
        principal_id: str,
        scopes: Iterable[str],
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Record an operator-supplied ECDC Surveillance Atlas export (the atlas is never fetched); idempotent."""
        from src.ingestion.surveillance_sources import EXPORT_CONTRACT, finish_release

        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        if not isinstance(export, Mapping) or export.get("format") != "ecdc-atlas-csv":
            raise SurveillanceError(
                "invalid_export",
                "exports are ecdc-atlas-csv files with their declaration",
            )
        document = dict(export.get("document") or {})
        if forbidden_keys(document):
            raise SurveillanceError(
                "outside_boundary",
                "an export declaration carries no prediction or advice",
            )
        content = export.get("content")
        if not isinstance(content, str) or not content.strip():
            raise SurveillanceError(
                "invalid_export", "the export content is the CSV text as downloaded"
            )
        raw = content.encode()
        try:
            release = finish_release(
                "ecdc-atlas-csv", parse_ecdc_export(raw, document=document), raw
            )
        except SurveillanceFormatError as exc:
            raise SurveillanceError(exc.code, str(exc)) from exc
        header = {
            "contract": EXPORT_CONTRACT,
            "provider": release["provider"],
            "format": "ecdc-atlas-csv",
            "document": document,
            "native_revision": release["native_revision"],
            "published_on": release["published_on"],
            "published_at": None,
            "release_basis": release["release_basis"],
            "file_sha256": release["file_sha256"],
            "content_sha256": release["content_sha256"],
            "item_count": release["item_count"],
            "structure": release["structure"],
            "evidence_origin": "operator",
            "url": document.get("url"),
        }
        return self.apply_release(
            namespace,
            header,
            release["series"],
            run_id=run_id or f"operator:{principal_id}",
            source_id="operator-export:ecdc-atlas",
            recorded_by=principal_id,
        )

    def _case_definition(
        self, namespace, provider, item, published, release_id, retrieved
    ):
        """The declared revisions of the series' case definition, each resolved to its stored revision id."""
        declared = item.get("case_definition")
        if not declared:
            return [], 0
        key = f"{provider}:{declared['key']}"
        if not self.conn.execute(
            "SELECT 1 FROM surveillance_case_definitions WHERE namespace=? AND definition_key=?",
            [namespace, key],
        ).fetchone():
            self.conn.execute(
                "INSERT INTO surveillance_case_definitions VALUES (?,?,?,?,?,?)",
                [
                    namespace,
                    key,
                    provider,
                    canonical(item["condition"]),
                    release_id,
                    retrieved,
                ],
            )
        resolved, added = [], 0
        for revision in declared["revisions"]:
            content = {
                k: revision.get(k)
                for k in (
                    "version",
                    "valid_from",
                    "valid_to",
                    "text",
                    "locator",
                    "icd_scope",
                )
            }
            content_hash = digest(content)
            history = self.conn.execute(
                "SELECT revision_id, content_hash, declared_on FROM surveillance_case_definition_revisions WHERE "
                "namespace=? AND definition_key=? AND version=? ORDER BY declared_on, revision_no",
                [namespace, key, content["version"]],
            ).fetchall()
            in_force = [row for row in history if row[2] <= published]
            if in_force and in_force[-1][1] == content_hash:
                resolved.append({**content, "revision_id": in_force[-1][0]})
                continue
            if not in_force and history and history[0][1] == content_hash:
                # An earlier publication declaring the same wording: that revision was already declared then.
                self.conn.execute(
                    "UPDATE surveillance_case_definition_revisions SET declared_on=? WHERE namespace=? "
                    "AND revision_id=?",
                    [published, namespace, history[0][0]],
                )
                resolved.append({**content, "revision_id": history[0][0]})
                continue
            # A new edition, or a corrected declaration of an edition (dedupe only against the one in force, so a
            # reversion to earlier wording is itself a new revision).
            number = 1 + len(history)
            revision_id = (
                "sv-casedef:"
                + digest(
                    [
                        namespace,
                        key,
                        content["version"],
                        number,
                        content_hash,
                        published,
                    ]
                )[:24]
            )
            self.conn.execute(
                "INSERT INTO surveillance_case_definition_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    revision_id,
                    key,
                    content["version"],
                    number,
                    canonical(content),
                    content_hash,
                    content["valid_from"],
                    content.get("valid_to"),
                    published,
                    release_id,
                    retrieved,
                ],
            )
            resolved.append({**content, "revision_id": revision_id})
            added += 1
        return resolved, added

    def _delay_note(self, namespace, provider, item, release_id, retrieved):
        note = item.get("delay_note")
        if not note:
            return None, False
        content = {k: note.get(k) for k in ("text", "locator", "incomplete_recent")}
        note_id = "sv-delay:" + digest([namespace, provider, content])[:24]
        if self.conn.execute(
            "SELECT 1 FROM surveillance_delay_notes WHERE namespace=? AND note_id=?",
            [namespace, note_id],
        ).fetchone():
            return note_id, False
        self.conn.execute(
            "INSERT INTO surveillance_delay_notes VALUES (?,?,?,?,?,?)",
            [namespace, note_id, provider, canonical(content), release_id, retrieved],
        )
        return note_id, True

    def _series(self, namespace, provider, item, release_id, retrieved):
        condition, geography = item["condition"], item["geography"]
        dims = dict(item.get("dimensions") or {})
        identity = [
            namespace,
            provider,
            condition["scheme"],
            condition["code"],
            item["indicator"]["code"],
            geography["system"],
            geography["code"],
            item["unit"]["label"],
            item["interval"],
            item["kind"],
            dims,
        ]
        series_id = "sv-series:" + digest(identity)[:24]
        if self.conn.execute(
            "SELECT 1 FROM surveillance_series WHERE namespace=? AND series_id=?",
            [namespace, series_id],
        ).fetchone():
            return series_id, False
        definition_key = (
            f"{provider}:{item['case_definition']['key']}"
            if item.get("case_definition")
            else None
        )
        self.conn.execute(
            "INSERT INTO surveillance_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                series_id,
                provider,
                canonical(condition),
                condition["scheme"],
                condition["code"],
                item["indicator"]["code"],
                geography["system"],
                geography["code"],
                item["unit"]["label"],
                item["interval"],
                item["kind"],
                canonical(dims),
                definition_key,
                release_id,
                retrieved,
            ],
        )
        return series_id, True

    @staticmethod
    def _revision_for(revisions, reference):
        """The declared revision in force on a reference date (the latest valid-from on or before it)."""
        start = reference_start(reference)
        if start is None:
            return None
        fitting = [
            r
            for r in revisions
            if r["valid_from"] <= start
            and (r.get("valid_to") is None or start <= r["valid_to"])
        ]
        return max(fitting, key=lambda r: r["valid_from"]) if fitting else None

    def _vintage(
        self,
        namespace,
        series_id,
        item,
        revisions,
        note_id,
        release_id,
        header,
        clock,
        basis,
        retrieved,
    ):
        values = [
            {k: v for k, v in value.items() if k != "locator"}
            for value in item["values"]
        ]
        metadata = {
            "indicator": item["indicator"],
            "condition_label": item["condition"].get("label"),
            "geography_label": item["geography"].get("label"),
            "code_list_version": item["geography"].get("code_list_version"),
            "unit_published": item["unit"].get("published"),
            "denominator": item.get("denominator"),
            "citations": list(item.get("citations") or []),
            "delay_note_id": note_id,
            "case_definition_revisions": [r["revision_id"] for r in revisions],
            "values_hash": digest(values),
        }
        content_hash = digest({"values": values, "metadata": metadata})
        # Every new release stating the series is a vintage (the earlier one stays addressable). A publication with
        # the same release clock and the same content as the vintage in force adds nothing (an unchanged re-cut of
        # the same release); dedupe is only against that vintage, so a return to earlier values is a new vintage.
        rows = self.conn.execute(
            "SELECT vintage_id, content_hash, release_at_ms FROM surveillance_vintages WHERE namespace=? AND "
            "series_id=? AND release_at_ms<=? ORDER BY release_at_ms DESC, sequence DESC LIMIT 1",
            [namespace, series_id, clock],
        ).fetchone()
        if rows and rows[1] == content_hash and rows[2] == clock:
            return rows[0], False
        sequence = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(sequence), 0) FROM surveillance_vintages WHERE namespace=? AND series_id=?",
                [namespace, series_id],
            ).fetchone()[0]
        )
        vintage_id = (
            "sv-vintage:"
            + digest([namespace, series_id, clock, content_hash, release_id])[:24]
        )
        self.conn.execute(
            "INSERT INTO surveillance_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                vintage_id,
                series_id,
                release_id,
                clock,
                basis,
                retrieved,
                header.get("native_revision"),
                content_hash,
                canonical(metadata),
                sequence,
                retrieved,
            ],
        )
        for value in item["values"]:
            revision = self._revision_for(revisions, value.get("reference_period"))
            extra = {
                k: v
                for k, v in value.items()
                if k
                not in {
                    "reference_period",
                    "reporting_date",
                    "value_text",
                    "value",
                    "lower",
                    "upper",
                    "flags",
                }
            }
            key = f"{value.get('reference_period') or ''}|{value.get('reporting_date') or ''}"
            self.conn.execute(
                "INSERT INTO surveillance_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    vintage_id,
                    key,
                    value.get("reference_period"),
                    value.get("reporting_date"),
                    value.get("value_text"),
                    value.get("value"),
                    value.get("lower"),
                    value.get("upper"),
                    canonical(list(value.get("flags") or [])),
                    None
                    if value.get("value") is None
                    else canonical(
                        normalise_value(value["value"], item["unit"]["label"])
                    ),
                    None if revision is None else revision["revision_id"],
                    canonical(extra),
                ],
            )
        return vintage_id, True

    def add_break(
        self,
        namespace,
        series_id,
        kind,
        *,
        period,
        from_ref,
        to_ref,
        detail,
        note,
        vintage_id,
    ) -> int:
        if kind not in BREAK_KINDS:
            raise SurveillanceError(
                "invalid_break", f"break kind is one of {BREAK_KINDS}"
            )
        break_id = (
            "sv-break:"
            + digest([namespace, series_id, kind, period, from_ref, to_ref])[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM surveillance_breaks WHERE namespace=? AND break_id=?",
            [namespace, break_id],
        ).fetchone():
            return 0
        self.conn.execute(
            "INSERT INTO surveillance_breaks VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                break_id,
                series_id,
                kind,
                period,
                from_ref,
                to_ref,
                canonical(detail),
                note,
                vintage_id,
                self.now(),
            ],
        )
        return 1

    def _breaks(self, namespace: str, series_id: str, vintage_id: str) -> int:
        """Mark case-definition changes, publisher break flags and code-list changes; nothing is restated."""
        added = 0
        previous = None
        for value in self.value_rows(namespace, vintage_id):
            if (
                any(str(flag).startswith("b:") for flag in value["flags"])
                and value["reference_period"]
            ):
                added += self.add_break(
                    namespace,
                    series_id,
                    "publisher-flag",
                    period=value["reference_period"],
                    from_ref=None,
                    to_ref=None,
                    detail={"flag": "b"},
                    note="the publisher flags a break in the series at this period",
                    vintage_id=vintage_id,
                )
            revision_id = value["case_definition_revision_id"]
            if revision_id is None or value["reference_period"] is None:
                continue
            if previous and previous["case_definition_revision_id"] != revision_id:
                before = self.definition_revision(
                    namespace, previous["case_definition_revision_id"]
                )
                after = self.definition_revision(namespace, revision_id)
                if before["version"] != after["version"]:
                    # What the new edition changes besides its validity dates.
                    changed = sorted(
                        k
                        for k in ("text", "locator", "icd_scope")
                        if before["content"].get(k) != after["content"].get(k)
                    )
                    added += self.add_break(
                        namespace,
                        series_id,
                        "case-definition",
                        period=value["reference_period"],
                        from_ref=before["version"],
                        to_ref=after["version"],
                        detail={
                            "from_revision_id": before["revision_id"],
                            "to_revision_id": after["revision_id"],
                            "changed": changed,
                            "icd_scope": {
                                "from": before["content"].get("icd_scope"),
                                "to": after["content"].get("icd_scope"),
                            },
                        },
                        note="the case definition changed; values before this period stay under the earlier "
                        "definition and are not restated",
                        vintage_id=vintage_id,
                    )
            previous = value
        ordered = self.vintage_rows(namespace, series_id)
        for before, after in zip(ordered, ordered[1:]):
            old, new = (
                before["metadata"].get("code_list_version"),
                after["metadata"].get("code_list_version"),
            )
            if old and new and old != new:
                added += self.add_break(
                    namespace,
                    series_id,
                    "geography",
                    period=None,
                    from_ref=old,
                    to_ref=new,
                    detail={"basis": "code-list version declared by the release"},
                    note="the geography code list changed between these vintages; nothing is "
                    "re-aggregated",
                    vintage_id=after["vintage_id"],
                )
        return added

    # ------------------------------------------------------------------ reads

    _RELEASE_KEYS = (
        "release_id",
        "provider",
        "source_id",
        "format",
        "document",
        "native_revision",
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
            "SELECT release_id, provider, source_id, format, document_json, native_revision, published_on, "
            "published_at, release_basis, release_at_ms, file_sha256, content_sha256, item_count, structure_json, "
            "evidence_origin, url, sequence, run_id, recorded_by, retrieved_at_ms FROM surveillance_releases WHERE "
            "namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone()
        if row is None:
            raise SurveillanceError(
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
                "native_revision",
                "file_sha256",
                "published_on",
                "published_at",
                "release_basis",
                "url",
                "evidence_origin",
                "retrieved_at_ms",
            )
        } | {"document": release["document"].get("label")}

    def releases(
        self, namespace: str, *, provider: str | None = None
    ) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT release_id FROM surveillance_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY release_at_ms, sequence",
            [namespace, provider, provider],
        ).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def release_series(self, namespace: str, release_id: str) -> list[dict[str, str]]:
        return [
            {"series_id": r[0], "vintage_id": r[1]}
            for r in self.conn.execute(
                "SELECT series_id, vintage_id FROM surveillance_release_members WHERE namespace=? AND release_id=? "
                "ORDER BY series_id",
                [namespace, release_id],
            ).fetchall()
        ]

    def definition_revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT revision_id, definition_key, version, revision_no, content_json, content_hash, valid_from, "
            "valid_to, declared_on, release_id FROM surveillance_case_definition_revisions WHERE namespace=? AND "
            "revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if row is None:
            raise SurveillanceError(
                "not_found", "case-definition revision is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "revision_id",
                    "definition_key",
                    "version",
                    "revision_no",
                    "content",
                    "content_hash",
                    "valid_from",
                    "valid_to",
                    "declared_on",
                    "release_id",
                ),
                row,
            )
        )
        view["content"] = _load(view["content"], {})
        return {
            "contract": CONTRACT,
            "record_type": "case-definition-revision",
            "namespace": namespace,
            **view,
            "source_revision": self.source_revision(namespace, view["release_id"]),
        }

    def definition_history(self, namespace: str, definition_key: str) -> dict[str, Any]:
        """Every revision of a case definition, editions by valid-from; corrections of an edition by declaration."""
        head = self.conn.execute(
            "SELECT provider, condition_json, first_release_id FROM surveillance_case_definitions WHERE namespace=? "
            "AND definition_key=?",
            [namespace, definition_key],
        ).fetchone()
        if head is None:
            raise SurveillanceError(
                "not_found", "case definition is not visible in this namespace"
            )
        rows = self.conn.execute(
            "SELECT revision_id FROM surveillance_case_definition_revisions WHERE namespace=? AND definition_key=? "
            "ORDER BY valid_from, declared_on, revision_no",
            [namespace, definition_key],
        ).fetchall()
        revisions = [self.definition_revision(namespace, r[0]) for r in rows]
        editions: dict[str, list[dict[str, Any]]] = {}
        for revision in revisions:
            editions.setdefault(revision["version"], []).append(revision)
        out, predecessor = [], None
        for version in sorted(
            editions, key=lambda v: (editions[v][0]["valid_from"], v)
        ):
            declared = sorted(
                editions[version], key=lambda r: (r["declared_on"], r["revision_no"])
            )
            for index, revision in enumerate(declared):
                out.append(
                    {
                        **revision,
                        "predecessor_version": predecessor,
                        "correction_of": declared[index - 1]["revision_id"]
                        if index
                        else None,
                        "current_for_edition": index == len(declared) - 1,
                    }
                )
            predecessor = version
        return {
            "contract": CONTRACT,
            "record_type": "case-definition",
            "namespace": namespace,
            "definition_key": definition_key,
            "provider": head[0],
            "condition": _load(head[1], {}),
            "first_release_id": head[2],
            "revisions": out,
            "note": "each edition applies from its valid-from date; values keep the edition in force on their "
            "reference date and are never restated",
        }

    def delay_note(self, namespace: str, note_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT note_id, provider, content_json, first_release_id FROM surveillance_delay_notes WHERE "
            "namespace=? AND note_id=?",
            [namespace, note_id],
        ).fetchone()
        if row is None:
            raise SurveillanceError(
                "not_found", "reporting-delay note is not visible in this namespace"
            )
        return {
            "contract": CONTRACT,
            "record_type": "reporting-delay-note",
            "namespace": namespace,
            "note_id": row[0],
            "provider": row[1],
            **_load(row[2], {}),
            "source_revision": self.source_revision(namespace, row[3]),
            "note": "the source's own statement; no completeness is estimated from it",
        }

    _SERIES_KEYS = (
        "series_id",
        "provider",
        "condition",
        "indicator_code",
        "geography_system",
        "geography_code",
        "unit_label",
        "interval",
        "kind",
        "dimensions",
        "definition_key",
        "first_release_id",
    )

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT series_id, provider, condition_json, indicator_code, geography_system, geography_code, "
            "unit_label, interval, kind, dimensions_json, definition_key, first_release_id FROM surveillance_series "
            "WHERE namespace=? AND series_id=?",
            [namespace, series_id],
        ).fetchone()
        if row is None:
            raise SurveillanceError(
                "not_found", "series is not visible in this namespace"
            )
        view = dict(zip(self._SERIES_KEYS, row))
        view["condition"] = _load(view["condition"], {})
        view["dimensions"] = _load(view["dimensions"], {})
        vintages = self.vintage_rows(namespace, series_id)
        current = vintages[-1] if vintages else None
        metadata = current["metadata"] if current else {}
        view.update(
            geography={
                "system": view["geography_system"],
                "code": view["geography_code"],
                "label": metadata.get("geography_label"),
                "code_list_version": metadata.get("code_list_version"),
            },
            indicator=metadata.get("indicator") or {"code": view["indicator_code"]},
            unit={
                "label": view["unit_label"],
                "published": metadata.get("unit_published"),
                "kind": UNITS[view["unit_label"]][2],
            },
            citations=metadata.get("citations") or [],
            current_vintage_id=current["vintage_id"] if current else None,
            vintage_count=len(vintages),
            breaks=self.breaks(namespace, series_id),
            delay_note=self.delay_note(namespace, metadata["delay_note_id"])
            if metadata.get("delay_note_id")
            else None,
        )
        return {
            "contract": CONTRACT,
            "record_type": "surveillance-series",
            "namespace": namespace,
            **view,
        }

    def find_series(
        self,
        namespace: str,
        *,
        provider: str | None = None,
        condition_scheme: str | None = None,
        condition_codes: Iterable[str] | None = None,
        geography_system: str | None = None,
        geography_code: str | None = None,
        kind: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        if kind is not None and kind not in KINDS:
            raise SurveillanceError("invalid_kind", f"kind is one of {KINDS}")
        codes = (
            None
            if condition_codes is None
            else sorted({str(c) for c in condition_codes})
        )
        rows = self.conn.execute(
            "SELECT series_id, condition_code FROM surveillance_series WHERE namespace=? AND (? IS NULL OR "
            "provider=?) AND (? IS NULL OR condition_scheme=?) AND (? IS NULL OR geography_system=?) AND (? IS NULL "
            "OR geography_code=?) AND (? IS NULL OR kind=?) ORDER BY provider, condition_scheme, condition_code, "
            "geography_code, kind, series_id",
            [
                namespace,
                provider,
                provider,
                condition_scheme,
                condition_scheme,
                geography_system,
                geography_system,
                geography_code,
                geography_code,
                kind,
                kind,
            ],
        ).fetchall()
        out = []
        for series_id, code in rows:
            if codes is not None and code not in codes:
                continue
            out.append(self.series(namespace, series_id))
            if len(out) >= limit:
                break
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        """Every vintage in release-clock order, each with ``revision_of`` its predecessor by that clock."""
        rows = self.conn.execute(
            "SELECT vintage_id, release_id, release_at_ms, release_basis, retrieved_at_ms, native_revision, "
            "content_hash, metadata_json, sequence FROM surveillance_vintages WHERE namespace=? AND series_id=? "
            "ORDER BY release_at_ms, sequence",
            [namespace, series_id],
        ).fetchall()
        out, previous = [], None
        for row in rows:
            view = dict(
                zip(
                    (
                        "vintage_id",
                        "release_id",
                        "release_at_ms",
                        "release_basis",
                        "retrieved_at_ms",
                        "native_revision",
                        "content_hash",
                        "metadata",
                        "sequence",
                    ),
                    row,
                )
            )
            view["metadata"] = _load(view["metadata"], {})
            view["series_id"] = series_id
            view["retrieved_at_basis"] = "acquisition_time"
            view["revision_of"] = previous["vintage_id"] if previous else None
            view["values_changed"] = previous is None or previous["metadata"].get(
                "values_hash"
            ) != view["metadata"].get("values_hash")
            # Two releases with the same release clock: the later-recorded one corrects the earlier.
            view["correction_of_same_release_clock"] = (
                previous["vintage_id"]
                if previous and previous["release_at_ms"] == view["release_at_ms"]
                else None
            )
            out.append(view)
            previous = view
        return out

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT series_id FROM surveillance_vintages WHERE namespace=? AND vintage_id=?",
            [namespace, vintage_id],
        ).fetchone()
        if row is None:
            raise SurveillanceError(
                "not_found", "vintage is not visible in this namespace"
            )
        view = next(
            v
            for v in self.vintage_rows(namespace, row[0])
            if v["vintage_id"] == vintage_id
        )
        return {
            "contract": CONTRACT,
            "record_type": "indicator-vintage",
            "namespace": namespace,
            **view,
            "source_revision": self.source_revision(namespace, view["release_id"]),
        }

    def value_rows(self, namespace: str, vintage_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT value_key, reference_period, reporting_date, value_text, value, lower_value, upper_value, "
            "flags_json, normalized_json, case_definition_revision_id, extra_json FROM surveillance_values WHERE "
            "namespace=? AND vintage_id=?",
            [namespace, vintage_id],
        ).fetchall()
        out = []
        for (
            key,
            reference,
            reporting,
            text,
            value,
            lower,
            upper,
            flags,
            normalized,
            revision,
            extra,
        ) in rows:
            unknown = [
                name
                for name, present in (
                    ("reference_period", reference),
                    ("reporting_date", reporting),
                )
                if present is None
            ]
            out.append(
                {
                    "record_type": "surveillance-value",
                    "value_key": key,
                    "reference_period": reference,
                    "reporting_date": reporting,
                    "unknown": unknown,
                    "value_text": text,
                    "value": value,
                    "lower": lower,
                    "upper": upper,
                    "flags": _load(flags, []),
                    "normalized": _load(normalized, None),
                    "case_definition_revision_id": revision,
                    **_load(extra, {}),
                }
            )
        return sorted(
            out,
            key=lambda v: (
                reference_start(v["reference_period"]) or "",
                v["reference_period"] or "",
                v["reporting_date"] or "",
            ),
        )

    def breaks(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT break_id, kind, period, from_ref, to_ref, detail_json, note, first_vintage_id FROM "
            "surveillance_breaks WHERE namespace=? AND series_id=? ORDER BY coalesce(period, ''), kind, break_id",
            [namespace, series_id],
        ).fetchall()
        return [
            {
                "record_type": "series-break",
                "series_id": series_id,
                **dict(
                    zip(
                        (
                            "break_id",
                            "kind",
                            "period",
                            "from",
                            "to",
                            "detail",
                            "note",
                            "first_vintage_id",
                        ),
                        row[:5] + (_load(row[5], {}),) + row[6:],
                    )
                ),
                "handling": "marked; values are never restated, re-aggregated or spliced across it",
            }
            for row in rows
        ]

    def select_vintage(
        self, namespace: str, series_id: str, *, as_of_ms: int | None = None
    ) -> tuple[dict[str, Any] | None, str | None]:
        """The vintage released on or before the cutoff (the release-cutoff semantics of the economic store)."""
        vintages = self.vintage_rows(namespace, series_id)
        cutoff = as_of_ms if as_of_ms is not None else 2**62
        eligible = [v for v in vintages if v["release_at_ms"] <= cutoff]
        if eligible:
            return eligible[-1], None
        return None, "historical_vintage_unavailable"

    def answer(
        self,
        namespace: str,
        series_id: str,
        *,
        scopes: Iterable[str],
        vintage_id: str | None = None,
        reporting_as_of: str | None = None,
        period_from: str | None = None,
        period_to: str | None = None,
    ) -> dict[str, Any]:
        """A series' values in one vintage, each with both dates, kind, case definition and source revision.

        ``reporting_as_of`` (an ISO date) selects the vintage released by the end of that day and returns only the
        values whose reporting date lies on or before it; values without a reporting date are listed apart as
        ``reporting_date_unknown``, never counted as reported by then. Nothing is interpolated.
        """
        authorize(namespace, set(scopes), READ_SCOPE)
        series = self.series(namespace, series_id)
        cutoff_day = None if reporting_as_of is None else _day(reporting_as_of)
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
                raise SurveillanceError(
                    "not_found", "vintage does not belong to this series"
                )
            reason = None
        else:
            as_of_ms = (
                None
                if cutoff_day is None
                else release_ms(cutoff_day, None) + 86_400_000 - 1
            )
            vintage, reason = self.select_vintage(
                namespace, series_id, as_of_ms=as_of_ms
            )
        base = {
            "contract": ANSWER_CONTRACT,
            "series": series,
            "kind": series["kind"],
            "boundary": NEVER_SENTENCE,
            "request": {
                "vintage_id": vintage_id,
                "reporting_as_of": cutoff_day,
                "period_from": period_from,
                "period_to": period_to,
            },
        }
        if vintage is None:
            return {
                **base,
                "status": "unavailable",
                "reason": reason,
                "values": [],
                "reporting_date_unknown": [],
            }
        values, unknown = [], []
        revisions: dict[str, dict[str, Any]] = {}
        for value in self.value_rows(namespace, vintage["vintage_id"]):
            start = reference_start(value["reference_period"])
            if period_from and (start is None or start < _day(period_from)):
                continue
            if period_to and (start is None or start > _day(period_to)):
                continue
            revision_id = value["case_definition_revision_id"]
            if revision_id and revision_id not in revisions:
                revisions[revision_id] = self.definition_revision(
                    namespace, revision_id
                )
            value = {
                **value,
                "kind": series["kind"],
                "case_definition": None
                if not revision_id
                else {
                    k: revisions[revision_id][k]
                    for k in (
                        "revision_id",
                        "definition_key",
                        "version",
                        "valid_from",
                        "valid_to",
                    )
                },
            }
            if cutoff_day is not None:
                if value["reporting_date"] is None:
                    unknown.append(value)
                    continue
                if period_end(value["reporting_date"]) > cutoff_day:
                    continue
            values.append(value)
        return {
            **base,
            "status": "available",
            "vintage": {
                **vintage,
                "source_revision": self.source_revision(
                    namespace, vintage["release_id"]
                ),
            },
            "values": values,
            "reporting_date_unknown": unknown,
            "breaks": series["breaks"],
            "delay_note": series["delay_note"],
            "note": "values as published under their case-definition revision; reporting date and reference "
            "date are separate fields; nothing interpolated, completed or merged",
        }


class SurveillanceProjector:
    """Source-pack runtime projector for ``noesis-surveillance-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        from src.kb.clinical_records import ClinicalRecordStore

        self.store = SurveillanceStore(conn)
        # Provider freshness is kept by the clinical owner's provider state (the bundle's readiness reads it).
        self.state = ClinicalRecordStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("surveillance") or {}).get("namespace") or DEFAULT_NAMESPACE
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
        del manifest, documents, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = (
                dict(item.get("surveillance_release") or {}),
                item.get("surveillance_series"),
            )
            if not header or not isinstance(body, Mapping):
                raise SurveillanceError(
                    "invalid_record", "page record is not a surveillance series"
                )
            groups.setdefault(
                header["file_sha256"] + canonical(header.get("document")), (header, [])
            )[1].append(dict(body))
        namespace = self._namespace(source)
        results = [
            self.store.apply_release(
                namespace, header, items, run_id=run_id, source_id=source["source_id"]
            )
            for header, items in groups.values()
        ]
        provider = dict(source.get("surveillance") or {}).get("provider")
        if provider:
            self.state.record_success(
                namespace,
                provider,
                observation_id=run_id,
                observed_at_ms=self.store.now(),
                execution=(page_receipt or {}).get("execution") or "unrecorded",
            )
        return results

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        provider = dict(source.get("surveillance") or {}).get("provider")
        if status != "complete" and provider:
            row = (
                self.store.conn.execute(
                    "SELECT failure_json FROM source_pack_source_runs WHERE run_id=? AND source_id=?",
                    [run_id, source["source_id"]],
                ).fetchone()
                if table_exists(self.store.conn, "source_pack_source_runs")
                else None
            )
            code = (
                (json.loads(row[0]) or {}).get("code")
                if row and row[0]
                else "source_failed"
            )
            self.state.record_failure(
                self._namespace(source),
                provider,
                observation_id=run_id,
                failure_code=code,
                observed_at_ms=self.store.now(),
                internal=True,
            )
        row = self.store.conn.execute(
            "SELECT release_id, published_on FROM surveillance_releases WHERE namespace=? AND source_id=? "
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
    """Register the surveillance record contract as a schema module in the shared registry."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = (
        Path(__file__).resolve().parents[2]
        / "contracts/schemas/jsonschema"
        / f"{CONTRACT}.json"
    )
    definition = {
        "contract": "noesis-schema-module-v1",
        "name": "surveillance-record",
        "kind": "schema",
        "semantic_version": "1.0.0",
        "content": json.loads(path.read_text()),
        "owner": "clinical.surveillance",
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
            "surveillance-schema:surveillance-record:1.0.0",
            principal_id=principal_id,
            scopes=scopes,
        )
    ]


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.surveillance_sources import PROVIDER_CONTRACTS

    store = SurveillanceStore(conn, initialize=False)
    ready = store.ready() and table_exists(conn, "surveillance_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        origins: list[str] = []
        if ready:
            releases = int(
                conn.execute(
                    "SELECT count(*) FROM surveillance_releases WHERE provider=?",
                    [provider],
                ).fetchone()[0]
            )
            origins = sorted(
                {
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT evidence_origin FROM surveillance_releases WHERE provider=?",
                        [provider],
                    ).fetchall()
                }
            )
        providers[provider] = {
            "delivers": contract["delivers"],
            "access_decision": contract["access_decision"],
            "reason": contract["reason"],
            "releases": releases,
            "evidence_origins": origins,
        }
    return {
        "feature": "surveillance",
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "providers": providers,
        "boundary": BOUNDARY,
        "never": list(NEVER),
        "note": "offline fixture, operator and live evidence are reported per release (evidence_origin); no "
        "provider is live until a dated run verifies it",
    }


__all__ = [
    "ANSWER_CONTRACT",
    "BREAK_KINDS",
    "CONTRACT",
    "FORBIDDEN_KEYS",
    "NEVER",
    "NEVER_SENTENCE",
    "READ_SCOPE",
    "REVIEW_SCOPE",
    "SurveillanceError",
    "SurveillanceProjector",
    "SurveillanceStore",
    "WRITE_SCOPE",
    "authorize",
    "feature_enabled",
    "readiness",
    "register_schemas",
]
