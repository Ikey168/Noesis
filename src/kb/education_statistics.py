"""Education and research-institution statistics with release vintages: the ``science.education-statistics`` owner.

Records (contract ``noesis-education-statistic-record-v1``, #2227 ED02) follow the vintage pattern of
:mod:`src.kb.demographics` and :mod:`src.kb.demographics_comparability` (release, series, vintage, observation;
release and retrieval clocks with basis labels; ``revision_of`` by release clock) in namespace-scoped ``edu_*``
tables:

* **release** - one acquired publication (an IPEDS data file, an ETER export, a UIS data version, an Education at a
  Glance edition, a Eurostat dataset update) with its release clock, basis label and **release stage**
  (``provisional``/``final`` for IPEDS). Re-acquiring an unchanged publication adds nothing.
* **institution profile** - the name, location and identifiers a source publishes for an institution (IPEDS
  directory, ETER); a changed profile is a new profile revision.
* **series** - an *institution statistic* keyed by the source institution id (UNITID, ETER ID) or an *education
  indicator* keyed by the published country or area code, with indicator code, concept, ISCED level, unit (currency
  and scale as published), dimensions and frequency. The ROR id is attached only through a confirmed identity match
  (:mod:`src.kb.education_identity`), never stored as a guess.
* **vintage** - each release of a series is appended with the definition as published in that release; a revised
  value never overwrites an earlier vintage (a final IPEDS release supersedes the provisional one without deleting
  it), and a publication that changes values without a new release clock is refused (``vintage_conflict``).
* **observation** - period, value text and exact decimal value, status and the publisher's own special code for a
  value that is missing, not applicable, included elsewhere, confidential or suppressed (never zero), flags
  verbatim.
* **comparability note** - the publisher's qualifiers, footnotes, imputation, suppression, status and
  break-in-series flags, quoted per vintage and period. Nothing is harmonised silently.

No ranking, quality or composite score, harmonised or averaged value, currency conversion or derived ratio is ever
stored. Links to Science and Funding records and the as-of query layer live at the end of this module (ED09, ED10).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from src.ingestion.education_sources import (
    CONCEPTS,
    EXCLUSIONS,
    FORMATS,
    LIVE_VERIFICATION,
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    STATUSES,
)

CONTRACT = "noesis-education-statistic-record-v1"
ANSWER_CONTRACT = "noesis-education-statistic-answer-v1"
LINK_CONTRACT = "noesis-education-link-v1"
READ_SCOPE = "knowledge:education:read"
WRITE_SCOPE = "knowledge:education:write"
REVIEW_SCOPE = "knowledge:education:review"
DEFAULT_NAMESPACE = "global"
BUNDLE = "science"
FEATURE = "education-statistics"
RECORD_TYPES = ("release", "institution_profile", "series", "vintage", "observation", "comparability_note")
RECORD_KINDS = ("institution_statistic", "education_indicator")
INSTITUTION_SCHEMES = ("ipeds-unitid", "eter-id")
AREA_SCHEMES = ("uis-geo", "oecd-ref-area", "eurostat-geo")
NOTE_KINDS = ("definition", "qualifier", "magnitude", "footnote", "status", "flag", "imputation", "suppression",
              "special_code")
# Keys that would carry a ranking, a score, a harmonised or derived number.
FORBIDDEN_KEYS = frozenset(
    {
        "rank",
        "ranking",
        "league_table",
        "score",
        "quality_score",
        "performance_score",
        "composite_index",
        "composite_score",
        "harmonised_value",
        "harmonized_value",
        "averaged_value",
        "combined_value",
        "converted_value",
        "per_student",
        "per_staff",
        "per_capita",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS edu_releases (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  document_json TEXT NOT NULL, published_on TEXT, published_at TEXT, release_basis TEXT NOT NULL,
  release_stage TEXT NOT NULL, release_at_ms BIGINT NOT NULL, release_label TEXT, file_sha256 TEXT NOT NULL,
  content_sha256 TEXT NOT NULL, item_count INTEGER NOT NULL, structure_json TEXT NOT NULL,
  evidence_origin TEXT NOT NULL, url TEXT, sequence INTEGER NOT NULL, run_id TEXT NOT NULL, recorded_by TEXT,
  retrieved_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, release_id)
);
CREATE TABLE IF NOT EXISTS edu_profiles (
  namespace TEXT NOT NULL, profile_id TEXT NOT NULL, subject_scheme TEXT NOT NULL, subject_code TEXT NOT NULL,
  release_id TEXT NOT NULL, release_at_ms BIGINT NOT NULL, reference_period TEXT, profile_json TEXT NOT NULL,
  content_hash TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(namespace, profile_id)
);
CREATE TABLE IF NOT EXISTS edu_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, provider TEXT NOT NULL, record_kind TEXT NOT NULL,
  subject_scheme TEXT NOT NULL, subject_code TEXT NOT NULL, subject_json TEXT NOT NULL, country TEXT,
  indicator_code TEXT NOT NULL, concept TEXT NOT NULL, indicator_label TEXT NOT NULL, isced_json TEXT,
  unit_json TEXT NOT NULL, dimensions_json TEXT NOT NULL, frequency TEXT NOT NULL, first_release_id TEXT NOT NULL,
  created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS edu_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL, release_id TEXT NOT NULL,
  release_at_ms BIGINT NOT NULL, release_at_basis TEXT NOT NULL, release_stage TEXT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, definition_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  sequence INTEGER NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS edu_observations (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, period TEXT NOT NULL, value_text TEXT, value TEXT,
  status TEXT NOT NULL, special_code TEXT, flags_json TEXT NOT NULL, extra_json TEXT NOT NULL,
  PRIMARY KEY(namespace, vintage_id, period)
);
CREATE TABLE IF NOT EXISTS edu_notes (
  namespace TEXT NOT NULL, note_id TEXT NOT NULL, vintage_id TEXT NOT NULL, series_id TEXT NOT NULL,
  release_id TEXT NOT NULL, period TEXT, kind TEXT NOT NULL, code TEXT, text TEXT NOT NULL,
  PRIMARY KEY(namespace, note_id)
);
CREATE TABLE IF NOT EXISTS edu_release_members (
  namespace TEXT NOT NULL, release_id TEXT NOT NULL, series_id TEXT NOT NULL, vintage_id TEXT NOT NULL,
  PRIMARY KEY(namespace, release_id, series_id)
);
"""


class EducationError(ValueError):
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
        raise EducationError("unauthorized", f"{required} and namespace access are required")


def require_scope(scopes: Iterable[str], required: str) -> None:
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise EducationError("unauthorized", f"{required} is required for this part of the answer")


def table_exists(conn: Any, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]).fetchone())


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in a value that would carry a ranking, a score or a harmonised or derived number."""
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
    """The release clock in epoch milliseconds (UTC); a date alone is its midnight; no date is the retrieval time."""
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


def as_of_ms(value: Any) -> int | None:
    """An as-of cutoff from epoch milliseconds, an ISO date (end of that day, UTC) or an ISO instant."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    raw = str(value).strip()
    if raw.isdigit():
        return int(raw)
    try:
        if len(raw) == 10:
            day = date.fromisoformat(raw)
            return int(datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc).timestamp() * 1000)
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EducationError("invalid_request", "as_of is epoch milliseconds, an ISO date or an ISO instant") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return int(stamp.timestamp() * 1000)


def selected_features(conn: Any) -> list[str]:
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
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle=?", [BUNDLE]).fetchone()
        if not managed or managed[0] != "composition":
            return []
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return []
    return list((plan.get("features") or {}).get(BUNDLE) or [])


def feature_enabled(conn: Any) -> bool:
    """Whether the Science bundle's optional ``education-statistics`` feature is selected in the active plan."""
    return FEATURE in selected_features(conn)


def _check_item(item: Mapping[str, Any]) -> None:
    if forbidden_keys(dict(item)):
        raise EducationError("invalid_release", "published records carry no ranking, score or derived value")
    kind = item.get("record_kind")
    subject = dict(item.get("subject") or {})
    if not subject.get("scheme") or not subject.get("code"):
        raise EducationError("invalid_release", "a record states its subject scheme and code")
    if kind == "institution_profile":
        if subject["scheme"] not in INSTITUTION_SCHEMES:
            raise EducationError("invalid_release", "a profile describes an institution")
        return
    if kind not in RECORD_KINDS:
        raise EducationError("invalid_release", f"record kind is one of {RECORD_KINDS} or institution_profile")
    expected = INSTITUTION_SCHEMES if kind == "institution_statistic" else AREA_SCHEMES
    if subject["scheme"] not in expected:
        raise EducationError("invalid_release", f"a {kind} is keyed by one of {expected}")
    indicator = dict(item.get("indicator") or {})
    if not indicator.get("code") or indicator.get("concept") not in CONCEPTS or not item.get("unit"):
        raise EducationError("invalid_release", "a series states its indicator code, concept and unit")
    for obs in item.get("observations") or []:
        if obs.get("status") not in STATUSES:
            raise EducationError("invalid_release", "each observation states its status")
        if obs.get("status") != "reported" and obs.get("value") is not None:
            raise EducationError("invalid_release", "a value that is not reported carries no number")
    for note in item.get("notes") or []:
        if note.get("kind") not in NOTE_KINDS or not str(note.get("text") or "").strip():
            raise EducationError("invalid_release", "a comparability note has a kind and the publisher's text")


class EducationStatisticsStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "edu_vintages")

    # ------------------------------------------------------------------ writes

    def _release(self, namespace, header, *, source_id, run_id, retrieved, recorded_by):
        provider, fmt = str(header.get("provider") or ""), header.get("format")
        if fmt not in FORMATS or FORMATS[fmt]["provider"] != provider:
            raise EducationError("invalid_release", "release names a known provider and format")
        document = dict(header.get("document") or {})
        release_id = "edu-release:" + digest([namespace, provider, source_id, header["file_sha256"], document])[:24]
        if self.conn.execute(
            "SELECT 1 FROM edu_releases WHERE namespace=? AND release_id=?", [namespace, release_id]
        ).fetchone():
            return release_id, False
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM edu_releases WHERE namespace=? AND provider=?",
            [namespace, provider],
        ).fetchone()[0]
        origin = header.get("evidence_origin")
        origin = origin if origin in {"fixture", "operator"} else "live"
        self.conn.execute(
            "INSERT INTO edu_releases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                str(header.get("release_stage") or "release"),
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
        """Record one publication: a vintage per stated series and a profile revision per changed profile;
        idempotent by file and document."""
        if int(header.get("item_count", -1)) != len(items):
            raise EducationError("incomplete_release", "a release carries every item it states")
        for item in items:
            _check_item(item)
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        counts = {"series": 0, "vintages": 0, "unchanged_vintages": 0, "profiles": 0, "notes": 0}
        self.conn.execute("BEGIN")
        try:
            release_id, created = self._release(
                namespace, header, source_id=source_id, run_id=run_id, retrieved=retrieved, recorded_by=recorded_by
            )
            if not created:
                self.conn.execute("COMMIT")
                return {"release_id": release_id, "status": "unchanged", **counts}
            clock = release_ms(header.get("published_on"), header.get("published_at"), retrieved)
            basis = str(header.get("release_basis") or "retrieval_time")
            stage = str(header.get("release_stage") or "release")
            vintage_ids, seen = [], set()
            for item in items:
                if item["record_kind"] == "institution_profile":
                    counts["profiles"] += self._profile(namespace, item, release_id, clock)
                    continue
                series_id, new_series = self._series(namespace, header["provider"], item, release_id)
                if series_id in seen:
                    raise EducationError("invalid_release", "a release states the same series twice")
                seen.add(series_id)
                counts["series"] += int(new_series)
                vintage_id, new_vintage, notes = self._vintage(
                    namespace, series_id, item, release_id, clock, basis, stage, retrieved
                )
                vintage_ids.append(vintage_id)
                counts["notes"] += notes
                self.conn.execute(
                    "INSERT INTO edu_release_members VALUES (?,?,?,?)", [namespace, release_id, series_id, vintage_id]
                )
                counts["vintages" if new_vintage else "unchanged_vintages"] += 1
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "release_id": release_id,
            "status": "applied",
            "published_on": header.get("published_on"),
            "release_basis": basis,
            "release_stage": stage,
            "vintage_ids": vintage_ids,
            **counts,
        }

    def _profile(self, namespace, item, release_id, clock) -> int:
        subject = dict(item["subject"])
        content_hash = digest(subject)
        latest = self.conn.execute(
            "SELECT content_hash, revision FROM edu_profiles WHERE namespace=? AND subject_scheme=? AND "
            "subject_code=? ORDER BY release_at_ms DESC, revision DESC LIMIT 1",
            [namespace, subject["scheme"], subject["code"]],
        ).fetchone()
        if latest and latest[0] == content_hash:
            return 0
        revision = 1 if latest is None else int(latest[1]) + 1
        profile_id = "edu-profile:" + digest([namespace, subject["scheme"], subject["code"], release_id])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO edu_profiles VALUES (?,?,?,?,?,?,?,?,?,?)",
            [namespace, profile_id, subject["scheme"], subject["code"], release_id, clock,
             item.get("reference_period"), canonical(subject), content_hash, revision],
        )
        return 1

    @staticmethod
    def series_key(provider: str, item: Mapping[str, Any]) -> list[Any]:
        indicator = dict(item["indicator"])
        return [
            provider,
            item["record_kind"],
            [item["subject"]["scheme"], str(item["subject"]["code"])],
            indicator["code"],
            indicator["concept"],
            item.get("isced"),
            dict(item["unit"]),
            dict(item.get("dimensions") or {}),
            item.get("frequency") or "annual",
        ]

    def _series(self, namespace, provider, item, release_id):
        series_id = "edu-series:" + digest([namespace, *self.series_key(provider, item)])[:24]
        if self.conn.execute(
            "SELECT 1 FROM edu_series WHERE namespace=? AND series_id=?", [namespace, series_id]
        ).fetchone():
            return series_id, False
        subject = dict(item["subject"])
        indicator = dict(item["indicator"])
        self.conn.execute(
            "INSERT INTO edu_series VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                series_id,
                provider,
                item["record_kind"],
                subject["scheme"],
                str(subject["code"]),
                canonical(subject),
                subject.get("country"),
                indicator["code"],
                indicator["concept"],
                indicator.get("label") or indicator["code"],
                None if item.get("isced") is None else canonical(item["isced"]),
                canonical(dict(item["unit"])),
                canonical(dict(item.get("dimensions") or {})),
                item.get("frequency") or "annual",
                release_id,
                self.now(),
            ],
        )
        return series_id, True

    @staticmethod
    def _content(item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "observations": [
                {k: o.get(k) for k in ("period", "value_text", "value", "status", "special_code", "flags")}
                for o in sorted(item.get("observations") or [], key=lambda o: o["period"])
            ],
            "notes": sorted(
                ({k: n.get(k) for k in ("kind", "code", "text", "period")} for n in item.get("notes") or []),
                key=canonical,
            ),
            "definition": dict(item["indicator"]),
        }

    def _vintage(self, namespace, series_id, item, release_id, clock, basis, stage, retrieved):
        content = self._content(item)
        content_hash = digest(content)
        existing = self.conn.execute(
            "SELECT vintage_id, content_hash FROM edu_vintages WHERE namespace=? AND series_id=? AND release_at_ms=? "
            "ORDER BY sequence LIMIT 1",
            [namespace, series_id, clock],
        ).fetchone()
        if existing:
            if existing[1] != content_hash:
                # Values changed without a new release clock: the stored vintage is kept and the publication refused.
                raise EducationError(
                    "vintage_conflict",
                    "the publication changed values without a new release time; the stored vintage is kept",
                    series_id=series_id,
                )
            return existing[0], False, 0
        sequence = 1 + int(
            self.conn.execute(
                "SELECT coalesce(max(sequence), 0) FROM edu_vintages WHERE namespace=? AND series_id=?",
                [namespace, series_id],
            ).fetchone()[0]
        )
        vintage_id = "edu-vintage:" + digest([namespace, series_id, clock, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO edu_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, vintage_id, series_id, release_id, clock, basis, stage, retrieved,
             canonical(dict(item["indicator"])), content_hash, sequence, self.now()],
        )
        for obs in item.get("observations") or []:
            extra = {k: v for k, v in obs.items()
                     if k not in {"period", "value_text", "value", "status", "special_code", "flags"}}
            self.conn.execute(
                "INSERT INTO edu_observations VALUES (?,?,?,?,?,?,?,?,?)",
                [namespace, vintage_id, obs["period"], obs.get("value_text"), obs.get("value"), obs["status"],
                 obs.get("special_code"), canonical(dict(obs.get("flags") or {})), canonical(extra)],
            )
        notes = 0
        for note in item.get("notes") or []:
            note_id = "edu-note:" + digest([namespace, vintage_id, note.get("period"), note["kind"], note.get("code"),
                                            note["text"]])[:24]
            self.conn.execute(
                "INSERT OR IGNORE INTO edu_notes VALUES (?,?,?,?,?,?,?,?,?)",
                [namespace, note_id, vintage_id, series_id, release_id, note.get("period"), note["kind"],
                 note.get("code"), str(note["text"])],
            )
            notes += 1
        return vintage_id, True, notes

    # ------------------------------------------------------------------ reads

    _RELEASE_KEYS = (
        "release_id", "provider", "source_id", "format", "document", "published_on", "published_at", "release_basis",
        "release_stage", "release_at_ms", "release_label", "file_sha256", "content_sha256", "item_count", "structure",
        "evidence_origin", "url", "sequence", "run_id", "recorded_by", "retrieved_at_ms",
    )

    def release(self, namespace: str, release_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            f"SELECT {', '.join(k if k not in {'document', 'structure'} else k + '_json' for k in self._RELEASE_KEYS)}"
            " FROM edu_releases WHERE namespace=? AND release_id=?",
            [namespace, release_id],
        ).fetchone()
        if row is None:
            raise EducationError("not_found", "release is not visible in this namespace")
        view = dict(zip(self._RELEASE_KEYS, row))
        view["document"] = _load(view["document"], {})
        view["structure"] = _load(view["structure"], {})
        return {"contract": CONTRACT, "record_type": "release", "namespace": namespace, **view}

    def source_revision(self, namespace: str, release_id: str) -> dict[str, Any]:
        release = self.release(namespace, release_id)
        keys = ("release_id", "provider", "source_id", "file_sha256", "published_on", "published_at",
                "release_basis", "release_stage", "release_at_ms", "release_label", "url", "evidence_origin",
                "retrieved_at_ms")
        return {k: release[k] for k in keys} | {
            "document": release["document"].get("label"),
            "attribution": PROVIDER_CONTRACTS[release["provider"]]["attribution"],
            "live_verification": LIVE_VERIFICATION.get(release["provider"], {}).get("status"),
        }

    def releases(self, namespace: str, *, provider: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "edu_releases"):
            return []
        rows = self.conn.execute(
            "SELECT release_id FROM edu_releases WHERE namespace=? AND (? IS NULL OR provider=?) "
            "ORDER BY release_at_ms, sequence",
            [namespace, provider, provider],
        ).fetchall()
        return [self.release(namespace, r[0]) for r in rows]

    def profiles(self, namespace: str, scheme: str, code: str, *, as_of: int | None = None) -> list[dict[str, Any]]:
        """Every profile revision of an institution (release order); the last one at the cutoff is current."""
        if not table_exists(self.conn, "edu_profiles"):
            return []
        rows = self.conn.execute(
            "SELECT profile_id, release_id, release_at_ms, reference_period, profile_json, revision FROM edu_profiles "
            "WHERE namespace=? AND subject_scheme=? AND subject_code=? ORDER BY release_at_ms, revision",
            [namespace, scheme, str(code)],
        ).fetchall()
        cutoff = as_of if as_of is not None else 2**62
        return [
            {"contract": CONTRACT, "record_type": "institution_profile", "profile_id": r[0], "revision": r[5],
             "reference_period": r[3], "subject": json.loads(r[4]), "release_at": iso_from_ms(r[2]),
             "source_revision": self.source_revision(namespace, r[1])}
            for r in rows
            if r[2] <= cutoff
        ]

    def institutions(self, namespace: str) -> list[dict[str, Any]]:
        """The current profile of every institution with a profile (latest revision), else its bare subject."""
        if not self.ready():
            return []
        found: dict[tuple[str, str], dict[str, Any]] = {}
        for scheme, code, subject in self.conn.execute(
            "SELECT subject_scheme, subject_code, subject_json FROM edu_series WHERE namespace=? AND "
            "record_kind='institution_statistic' ORDER BY subject_scheme, subject_code",
            [namespace],
        ).fetchall():
            found.setdefault((scheme, code), json.loads(subject))
        if table_exists(self.conn, "edu_profiles"):
            for scheme, code in self.conn.execute(
                "SELECT DISTINCT subject_scheme, subject_code FROM edu_profiles WHERE namespace=?", [namespace]
            ).fetchall():
                profiles = self.profiles(namespace, scheme, code)
                if profiles:
                    found[(scheme, code)] = {**found.get((scheme, code), {}), **profiles[-1]["subject"]}
        return [found[k] for k in sorted(found)]

    _SERIES_COLUMNS = (
        "series_id, provider, record_kind, subject_json, indicator_code, concept, indicator_label, isced_json, "
        "unit_json, dimensions_json, frequency, first_release_id"
    )

    def _series_view(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        (series_id, provider, kind, subject, code, concept, label, isced, unit, dimensions, frequency, first) = row
        vintages = self.vintage_rows(namespace, series_id)
        return {
            "contract": CONTRACT,
            "record_type": "series",
            "namespace": namespace,
            "series_id": series_id,
            "provider": provider,
            "record_kind": kind,
            "subject": _load(subject, {}),
            "indicator": {"code": code, "label": label, "concept": concept},
            "isced": _load(isced, None),
            "unit": _load(unit, {}),
            "dimensions": _load(dimensions, {}),
            "frequency": frequency,
            "first_release_id": first,
            "vintage_count": len(vintages),
            "current_vintage_id": vintages[-1]["vintage_id"] if vintages else None,
        }

    def series(self, namespace: str, series_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            f"SELECT {self._SERIES_COLUMNS} FROM edu_series WHERE namespace=? AND series_id=?", [namespace, series_id]
        ).fetchone()
        if row is None:
            raise EducationError("not_found", "series is not visible in this namespace")
        return self._series_view(namespace, row)

    def find_series(
        self,
        namespace: str,
        *,
        provider: str | None = None,
        record_kind: str | None = None,
        subjects: Iterable[tuple[str, str]] | None = None,
        concept: str | None = None,
        indicator: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        if not self.ready():
            return []
        rows = self.conn.execute(
            f"SELECT {self._SERIES_COLUMNS}, subject_scheme, subject_code FROM edu_series WHERE namespace=? AND "
            "(? IS NULL OR provider=?) AND (? IS NULL OR record_kind=?) AND (? IS NULL OR concept=?) AND "
            "(? IS NULL OR indicator_code=?) ORDER BY concept, provider, subject_scheme, subject_code, indicator_code, "
            "series_id",
            [namespace, provider, provider, record_kind, record_kind, concept, concept, indicator, indicator],
        ).fetchall()
        wanted = None if subjects is None else {(str(s), str(c)) for s, c in subjects}
        out = []
        for row in rows:
            if wanted is not None and (row[-2], row[-1]) not in wanted:
                continue
            out.append(self._series_view(namespace, row[:-2]))
            if len(out) >= limit:
                break
        return out

    def vintage_rows(self, namespace: str, series_id: str) -> list[dict[str, Any]]:
        """Every vintage of a series in release-clock order, each with ``revision_of`` its predecessor."""
        rows = self.conn.execute(
            "SELECT vintage_id, release_id, release_at_ms, release_at_basis, release_stage, retrieved_at_ms, "
            "definition_json, content_hash, sequence FROM edu_vintages WHERE namespace=? AND series_id=? "
            "ORDER BY release_at_ms, sequence",
            [namespace, series_id],
        ).fetchall()
        out, previous = [], None
        for row in rows:
            view = dict(zip(("vintage_id", "release_id", "release_at_ms", "release_at_basis", "release_stage",
                             "retrieved_at_ms", "definition", "content_hash", "sequence"), row))
            view["definition"] = _load(view["definition"], {})
            view["series_id"] = series_id
            view["release_at"] = iso_from_ms(view["release_at_ms"])
            view["revision_of"] = previous["vintage_id"] if previous else None
            view["values_changed"] = previous is None or previous["content_hash"] != view["content_hash"]
            out.append(view)
            previous = view
        return out

    def vintage(self, namespace: str, vintage_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT series_id FROM edu_vintages WHERE namespace=? AND vintage_id=?", [namespace, vintage_id]
        ).fetchone()
        if row is None:
            raise EducationError("not_found", "vintage is not visible in this namespace")
        view = next(v for v in self.vintage_rows(namespace, row[0]) if v["vintage_id"] == vintage_id)
        return {"contract": CONTRACT, "record_type": "vintage", "namespace": namespace, **view,
                "source_revision": self.source_revision(namespace, view["release_id"])}

    def observations(self, namespace: str, vintage_id: str, *, period_from: str | None = None,
                     period_to: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT period, value_text, value, status, special_code, flags_json, extra_json FROM edu_observations "
            "WHERE namespace=? AND vintage_id=? ORDER BY period",
            [namespace, vintage_id],
        ).fetchall()
        out = []
        for period, value_text, value, status, special, flags, extra in rows:
            if period_from and period < period_from:
                continue
            if period_to and period[: len(period_to)] > period_to:
                continue
            out.append({"record_type": "observation", "period": period, "value_text": value_text, "value": value,
                        "status": status, "special_code": special, "flags": _load(flags, {}), **_load(extra, {})})
        return out

    def notes(self, namespace: str, vintage_id: str, *, period: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "edu_notes"):
            return []
        rows = self.conn.execute(
            "SELECT note_id, series_id, release_id, period, kind, code, text FROM edu_notes WHERE namespace=? AND "
            "vintage_id=? AND (? IS NULL OR period IS NULL OR period=?) ORDER BY period, kind, code, note_id",
            [namespace, vintage_id, period, period],
        ).fetchall()
        return [
            {"contract": CONTRACT, "record_type": "comparability_note", "note_id": r[0], "series_id": r[1],
             "vintage_id": vintage_id, "release_id": r[2], "period": r[3], "kind": r[4], "code": r[5], "text": r[6],
             "source": "publisher", "note": "quoted as published; nothing is harmonised"}
            for r in rows
        ]

    def select_vintage(self, namespace: str, series_id: str, *, as_of: int | None = None
                       ) -> tuple[dict[str, Any] | None, str | None]:
        """The vintage released on or before the cutoff (release-cutoff semantics of the economic release store)."""
        vintages = self.vintage_rows(namespace, series_id)
        cutoff = as_of if as_of is not None else 2**62
        eligible = [v for v in vintages if v["release_at_ms"] <= cutoff]
        if eligible:
            return eligible[-1], None
        return None, "no_release_by_as_of" if vintages else "no_vintage"

    def values(self, namespace: str, series_id: str, *, vintage_id: str | None = None, as_of: int | None = None,
               period_from: str | None = None, period_to: str | None = None) -> dict[str, Any]:
        series = self.series(namespace, series_id)
        reason = None
        if vintage_id:
            vintage = next((v for v in self.vintage_rows(namespace, series_id) if v["vintage_id"] == vintage_id), None)
            if vintage is None:
                raise EducationError("not_found", "vintage does not belong to this series")
        else:
            vintage, reason = self.select_vintage(namespace, series_id, as_of=as_of)
        if vintage is None:
            return {"contract": ANSWER_CONTRACT, "series": series, "status": "unavailable", "reason": reason,
                    "observations": []}
        return {
            "contract": ANSWER_CONTRACT,
            "series": series,
            "status": "available",
            "vintage": {**vintage, "source_revision": self.source_revision(namespace, vintage["release_id"])},
            "observations": self.observations(namespace, vintage["vintage_id"], period_from=period_from,
                                              period_to=period_to),
            "comparability_notes": self.notes(namespace, vintage["vintage_id"]),
            "note": "values as published in this vintage; values that are not reported keep the publisher's code "
            "and no number",
        }

    def latest_release_ms(self, namespace: str) -> int | None:
        if not table_exists(self.conn, "edu_releases"):
            return None
        row = self.conn.execute("SELECT max(retrieved_at_ms) FROM edu_releases WHERE namespace=?",
                                [namespace]).fetchone()
        return None if row is None or row[0] is None else int(row[0])


class EducationProjector:
    """Source-pack runtime projector for ``noesis-education-statistic-record-v1`` pages (one release per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = EducationStatisticsStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("education_statistics") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, page_receipt, principal_id
        groups: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        for item in records:
            header, body = dict(item.get("education_release") or {}), item.get("education_item")
            if not header or not isinstance(body, Mapping):
                raise EducationError("invalid_record", "page record is not an education release item")
            groups.setdefault(header["file_sha256"] + canonical(header.get("document")), (header, []))[1].append(
                dict(body))
        namespace = self._namespace(source)
        return [self.store.apply_release(namespace, header, items, run_id=run_id, source_id=source["source_id"])
                for header, items in groups.values()]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT release_id, published_on, release_stage FROM edu_releases WHERE namespace=? AND source_id=? "
            "ORDER BY release_at_ms DESC, sequence DESC LIMIT 1",
            [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {"status": status, "latest_release_id": row[0] if row else None,
                "latest_published_on": row[1] if row else None, "latest_release_stage": row[2] if row else None}


def readiness(conn: Any) -> dict[str, Any]:
    store = EducationStatisticsStore(conn, initialize=False)
    ready = store.ready() and table_exists(conn, "edu_releases")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        releases = 0
        if ready:
            releases = int(conn.execute("SELECT count(*) FROM edu_releases WHERE provider=?", [provider]).fetchone()[0])
        providers[provider] = {"delivers": contract["delivers"], "access_decision": contract["access_decision"],
                               "live_verification": LIVE_VERIFICATION[provider]["status"], "releases": releases}
    return {
        "feature": FEATURE,
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "providers": providers,
        "exclusions": list(EXCLUSIONS),
        "never": NEVER_SENTENCE,
        "note": "offline fixture evidence and live evidence are reported per release (evidence_origin); no provider "
        "is live until a dated run verifies it",
    }


__all__ = [
    "ANSWER_CONTRACT",
    "CONTRACT",
    "EducationError",
    "EducationProjector",
    "EducationStatisticsStore",
    "FEATURE",
    "READ_SCOPE",
    "REVIEW_SCOPE",
    "WRITE_SCOPE",
    "as_of_ms",
    "authorize",
    "feature_enabled",
    "forbidden_keys",
    "readiness",
]
