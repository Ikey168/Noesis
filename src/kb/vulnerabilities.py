"""Software vulnerability and advisory evidence: the ``technology.vulnerabilities`` record owner (#1913, V02).

Records (contract ``noesis-vulnerability-record-v1``), namespace-scoped and
revision-addressable:

* **vulnerability** - one identity per CVE id. Non-CVE advisories (GHSA, OSV
  ids) keep their own identifiers and reach a CVE only through an explicit
  alias the source states;
* **advisory** - one *revision series* per source record (NVD CVE record, OSV
  advisory, GitHub advisory, CVE Services record). A revision is appended when
  the source's content digest changes and carries the source, its own
  published/modified times, the observation time and the document it came
  from. Sources about one CVE sit side by side and are never merged; a
  rejected or withdrawn record keeps every earlier revision;
* **affected-version-range** - per revision: ecosystem as the source names it
  (canonicalised only through ``canonical_ecosystem``), range type, ordered
  events or CPE bounds, and the source;
* **weakness-classification** - a CWE id stated by a source, with the CWE list
  version it was resolved against;
* **exploitation-evidence** - a CISA KEV listing as its own revision series
  (catalog version and release kept, not an exploitability verdict);
* **score** - CVSS vectors quoted per publisher and CVSS version from an
  advisory revision, and FIRST EPSS observations as their own dated series
  (score date and model version); nothing is averaged, and a source without a
  score yields no severity;
* **alias** and **change-event** - source-stated aliases per revision and NVD
  change-history events (keyed by ``cveChangeId``).

The existing :class:`~src.domains.technical.advisories.AdvisoryRecord` and
``technical_advisory_ranges`` shapes map onto these records through
:func:`statement_from_advisory_record`; range evaluation reuses the technical
model's helpers rather than duplicating them. No record carries an
exploitability or risk verdict, patch or remediation advice, or an inferred
severity.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

CONTRACT = "noesis-vulnerability-record-v1"
READ_SCOPE = "knowledge:technical:read"
WRITE_SCOPE = "knowledge:technical:write"
REVIEW_SCOPE = "knowledge:technical:review"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = (
    "vulnerability",
    "advisory",
    "affected_version_range",
    "weakness_classification",
    "exploitation_evidence",
    "score",
    "alias",
    "change_event",
)
SERIES_KINDS = ("advisory", "exploitation", "score")
LIFECYCLES = ("active", "rejected", "withdrawn")
REFERENCE_KINDS = ("weakness-definition", "cpe-name")
FORBIDDEN_ANSWER_KEYS = frozenset(
    {
        "risk",
        "risk_score",
        "verdict",
        "exploitable",
        "exploitability_verdict",
        "priority",
        "remediation",
        "patch_advice",
        "aggregate_severity",
        "average_score",
    }
)
CVE_ID = re.compile(r"^CVE-\d{4}-\d{4,}$")

_DDL = """
CREATE TABLE IF NOT EXISTS vuln_identities (
  namespace TEXT NOT NULL, cve_id TEXT NOT NULL, first_series_id TEXT NOT NULL, first_observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, cve_id)
);
CREATE TABLE IF NOT EXISTS vuln_series (
  namespace TEXT NOT NULL, series_id TEXT NOT NULL, record_kind TEXT NOT NULL, source TEXT NOT NULL,
  native_id TEXT NOT NULL, cve_id TEXT, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, series_id)
);
CREATE TABLE IF NOT EXISTS vuln_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, series_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
  previous_revision_id TEXT, content_digest TEXT NOT NULL, lifecycle TEXT NOT NULL, source_status TEXT,
  source_published TEXT, source_modified TEXT, observed_at_ms BIGINT NOT NULL, run_id TEXT, source_id TEXT,
  document_id TEXT, statement_json TEXT NOT NULL, observation_json TEXT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS vuln_aliases (
  namespace TEXT NOT NULL, alias_id TEXT NOT NULL, revision_id TEXT NOT NULL, series_id TEXT NOT NULL,
  source TEXT NOT NULL, alias TEXT NOT NULL, alias_kind TEXT NOT NULL, PRIMARY KEY(namespace, alias_id)
);
CREATE TABLE IF NOT EXISTS vuln_ranges (
  namespace TEXT NOT NULL, range_id TEXT NOT NULL, revision_id TEXT NOT NULL, series_id TEXT NOT NULL,
  source TEXT NOT NULL, ecosystem_source TEXT NOT NULL, ecosystem TEXT, package TEXT NOT NULL, coordinate TEXT,
  range_type TEXT NOT NULL, events_json TEXT NOT NULL, versions_json TEXT NOT NULL, cpe_json TEXT,
  applicability TEXT NOT NULL, PRIMARY KEY(namespace, range_id)
);
CREATE TABLE IF NOT EXISTS vuln_weaknesses (
  namespace TEXT NOT NULL, weakness_id TEXT NOT NULL, revision_id TEXT NOT NULL, series_id TEXT NOT NULL,
  source TEXT NOT NULL, cwe_id TEXT NOT NULL, cwe_version TEXT, stated_by TEXT, stated_type TEXT,
  PRIMARY KEY(namespace, weakness_id)
);
CREATE TABLE IF NOT EXISTS vuln_scores (
  namespace TEXT NOT NULL, score_id TEXT NOT NULL, revision_id TEXT NOT NULL, series_id TEXT NOT NULL,
  source TEXT NOT NULL, publisher TEXT, score_kind TEXT NOT NULL, cvss_version TEXT, vector TEXT,
  base_score DOUBLE, base_severity TEXT, stated_type TEXT, epss TEXT, percentile TEXT, score_date TEXT,
  model_version TEXT, PRIMARY KEY(namespace, score_id)
);
CREATE TABLE IF NOT EXISTS vuln_exploitation (
  namespace TEXT NOT NULL, evidence_id TEXT NOT NULL, revision_id TEXT NOT NULL, series_id TEXT NOT NULL,
  source TEXT NOT NULL, cve_id TEXT NOT NULL, date_added TEXT, vendor_project TEXT, product TEXT,
  catalog_version TEXT, catalog_released TEXT, evidence_json TEXT NOT NULL, PRIMARY KEY(namespace, evidence_id)
);
CREATE TABLE IF NOT EXISTS vuln_change_events (
  namespace TEXT NOT NULL, change_id TEXT NOT NULL, cve_id TEXT NOT NULL, source TEXT NOT NULL, event_name TEXT,
  created TEXT, source_identifier TEXT, details_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, run_id TEXT,
  document_id TEXT, PRIMARY KEY(namespace, change_id)
);
CREATE TABLE IF NOT EXISTS vuln_reference_staging (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, kind TEXT NOT NULL, native_id TEXT NOT NULL,
  statement_json TEXT NOT NULL, PRIMARY KEY(namespace, run_id, source_id, kind, native_id)
);
"""


class VulnerabilityError(ValueError):
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
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                               f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise VulnerabilityError("unauthorized", f"{required} and namespace access are required")


def source_time_ms(value: Any) -> int | None:
    """Milliseconds of a source timestamp or date, or None when absent or unparseable."""
    if value in (None, ""):
        return None
    from src.kb.temporal import parse_source_time

    try:
        text = str(value)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?", text):
            text += "Z"  # NVD timestamps are UTC without an offset
        return parse_source_time(text, field="source_time")[0]
    except Exception:  # noqa: BLE001 - an unparseable source time orders nothing
        return None


def series_id(namespace: str, kind: str, source: str, native_id: str) -> str:
    return "vuln-series:" + digest([namespace, kind, source, native_id])[:24]


def statement_from_advisory_record(record: Any, *, source: str) -> dict[str, Any]:
    """Map an existing :class:`AdvisoryRecord` (technical advisories) onto an advisory statement.

    The OSV and CVE connectors parse through the existing adapters and then
    this shape; ranges keep the adapters' ordered events unchanged.
    """
    from src.domains.technical.model import TechnicalModelError, canonical_ecosystem

    ranges = []
    for affected in record.affected:
        try:
            ecosystem = canonical_ecosystem(str(affected.ecosystem)) if affected.ecosystem else None
        except TechnicalModelError:
            ecosystem = None
        package = str(affected.coordinate or "").split(":", 2)[-1] if affected.coordinate else ""
        for item in affected.ranges:
            ranges.append({"ecosystem_source": str(affected.ecosystem or ""), "ecosystem": ecosystem,
                           "package": package, "coordinate": affected.coordinate if ecosystem else None,
                           "range_type": str(item.get("type") or "ECOSYSTEM").upper(),
                           "events": [dict(e) for e in item.get("events") or []], "versions": [],
                           "applicability": "direct"})
    withdrawn = bool(record.withdrawn_at)
    rejected = str(record.metadata.get("state") or "") == "rejected"
    return {
        "contract": CONTRACT, "kind": "advisory", "source": source, "native_id": record.advisory_id,
        "cve_id": record.advisory_id if CVE_ID.match(record.advisory_id) else None,
        "published": record.published_at, "modified": record.modified_at,
        "lifecycle": "withdrawn" if withdrawn and not rejected else "rejected" if rejected else "active",
        "source_status": record.metadata.get("state") or ("withdrawn" if withdrawn else "published"),
        "summary": record.summary, "aliases": sorted(set(record.aliases)), "ranges": ranges, "weaknesses": [],
        "scores": [], "references": [{"url": url} for url in record.references],
    }


def feature_enabled(conn: Any, namespace: str | None = None) -> bool:
    """Whether the Technology bundle's optional ``vulnerabilities`` feature is selected in the active plan.

    Defaults to off; there is no separate enablement flag. Reads only.
    """
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
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='technology'").fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return "vulnerabilities" in ((plan.get("features") or {}).get("technology") or [])


class VulnerabilityStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='vuln_revisions'").fetchone())

    # ------------------------------------------------------------------ writes

    def apply(
        self,
        namespace: str,
        statement: Mapping[str, Any],
        *,
        run_id: str | None = None,
        source_id: str | None = None,
        document_id: str | None = None,
        observed_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record one source statement; idempotent by content digest, whatever the fetch time.

        Returns ``created`` (first revision of a series), ``revised`` (a new
        revision), ``unchanged`` or ``stale`` (older than the latest revision by
        the source's own modified time; never rewrites the chain).
        """
        statement = dict(statement)
        kind = str(statement.get("kind") or "")
        if statement.get("contract") != CONTRACT:
            raise VulnerabilityError("invalid_record", "statement is not a noesis-vulnerability-record-v1")
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        if kind == "change-event":
            return self._change_event(namespace, statement, observed, run_id, document_id)
        if kind not in SERIES_KINDS:
            raise VulnerabilityError("invalid_record", f"unsupported record kind {kind!r}")
        source, native = str(statement.get("source") or ""), str(statement.get("native_id") or "")
        if not source or not native:
            raise VulnerabilityError("invalid_record", "a statement needs its source and native id")
        cve_id = statement.get("cve_id")
        if cve_id is not None and not CVE_ID.match(str(cve_id)):
            raise VulnerabilityError("invalid_record", "cve_id must be a CVE identifier")
        lifecycle = str(statement.get("lifecycle") or "active")
        if lifecycle not in LIFECYCLES:
            raise VulnerabilityError("invalid_record", f"lifecycle must be one of {LIFECYCLES}")
        observation = dict(statement.pop("_observation", None) or {})
        content_digest = digest(statement)
        sid = series_id(namespace, kind, source, native)
        latest = self._latest(namespace, sid)
        if latest and latest["content_digest"] == content_digest:
            return {"status": "unchanged", "series_id": sid, "revision_id": latest["revision_id"]}
        modified = source_time_ms(statement.get("modified") or observation.get("catalog_released"))
        if latest and modified is not None:
            previous = source_time_ms(latest["source_modified"] or latest["catalog_released"])
            if previous is not None and modified < previous:
                return {"status": "stale", "series_id": sid, "revision_id": latest["revision_id"],
                        "reason": "older than the latest revision by the source's own date"}
        number = 1 if latest is None else latest["revision_no"] + 1
        revision_id = "vuln-revision:" + digest([namespace, sid, number, content_digest])[:24]
        self.conn.execute("BEGIN")
        try:
            if latest is None:
                self.conn.execute("INSERT OR IGNORE INTO vuln_series VALUES (?,?,?,?,?,?,?)",
                                  [namespace, sid, kind, source, native, cve_id, observed])
            if cve_id:
                self._identity(namespace, str(cve_id), sid, observed)
            self.conn.execute(
                "INSERT INTO vuln_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, revision_id, sid, number, latest["revision_id"] if latest else None, content_digest,
                 lifecycle, statement.get("source_status"),
                 None if statement.get("published") is None else str(statement["published"]),
                 None if statement.get("modified") is None else str(statement["modified"]), observed, run_id,
                 source_id, document_id, canonical(statement), canonical(observation)])
            self._derived(namespace, revision_id, sid, statement, observation, observed)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"status": "created" if latest is None else "revised", "series_id": sid, "revision_id": revision_id,
                "revision_no": number}

    def _identity(self, namespace: str, cve_id: str, sid: str, observed: int) -> None:
        self.conn.execute("INSERT OR IGNORE INTO vuln_identities VALUES (?,?,?,?)", [namespace, cve_id, sid, observed])

    def _derived(self, namespace, revision_id, sid, statement, observation, observed) -> None:
        source = statement["source"]

        def rid(prefix: str, value: Any) -> str:
            return f"{prefix}:" + digest([revision_id, value])[:24]

        for alias in statement.get("aliases") or []:
            kind = "cve" if CVE_ID.match(str(alias)) else "advisory"
            self.conn.execute("INSERT OR IGNORE INTO vuln_aliases VALUES (?,?,?,?,?,?,?)",
                              [namespace, rid("vuln-alias", alias), revision_id, sid, source, str(alias), kind])
            if kind == "cve":
                self._identity(namespace, str(alias), sid, observed)
        for index, item in enumerate(statement.get("ranges") or []):
            self.conn.execute(
                "INSERT OR IGNORE INTO vuln_ranges VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, rid("vuln-range", [index, item]), revision_id, sid, source,
                 str(item.get("ecosystem_source") or ""), item.get("ecosystem"), str(item.get("package") or ""),
                 item.get("coordinate"), str(item.get("range_type") or ""), canonical(item.get("events") or []),
                 canonical(item.get("versions") or []), canonical(item["cpe"]) if item.get("cpe") else None,
                 str(item.get("applicability") or "direct")])
        cwe_versions: dict[str, str | None] = {}
        for item in statement.get("weaknesses") or []:
            cwe = str(item["cwe_id"])
            if cwe not in cwe_versions:
                cwe_versions[cwe] = self.cwe_version(cwe)
            self.conn.execute(
                "INSERT OR IGNORE INTO vuln_weaknesses VALUES (?,?,?,?,?,?,?,?,?)",
                [namespace, rid("vuln-weakness", item), revision_id, sid, source, cwe, cwe_versions[cwe],
                 item.get("stated_by"), item.get("type")])
        for item in statement.get("scores") or []:
            self.conn.execute(
                "INSERT OR IGNORE INTO vuln_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, rid("vuln-score", item), revision_id, sid, source, item.get("publisher"), "cvss",
                 item.get("cvss_version"), item.get("vector"),
                 None if item.get("base_score") is None else float(item["base_score"]), item.get("base_severity"),
                 item.get("type"), None, None, None, None])
        score = statement.get("score")
        if score:
            self.conn.execute(
                "INSERT OR IGNORE INTO vuln_scores VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, rid("vuln-score", score), revision_id, sid, source, score.get("publisher"),
                 str(score.get("kind") or "epss"), None, None, None, None, None, score.get("epss"),
                 score.get("percentile"), score.get("score_date"), score.get("model_version")])
        evidence = statement.get("evidence")
        if evidence:
            self.conn.execute(
                "INSERT OR IGNORE INTO vuln_exploitation VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, rid("vuln-exploitation", evidence), revision_id, sid, source, statement["cve_id"],
                 evidence.get("date_added"), evidence.get("vendor_project"), evidence.get("product"),
                 observation.get("catalog_version"), observation.get("catalog_released"), canonical(evidence)])

    def _change_event(self, namespace, statement, observed, run_id, document_id) -> dict[str, Any]:
        change = dict(statement.get("change") or {})
        change_id = str(statement.get("native_id") or "")
        if not change_id or not CVE_ID.match(str(statement.get("cve_id") or "")):
            raise VulnerabilityError("invalid_record", "a change event needs its change id and CVE id")
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO vuln_change_events VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING change_id",
            [namespace, change_id, statement["cve_id"], statement["source"], change.get("event_name"),
             change.get("created"), change.get("source_identifier"), canonical(change.get("details") or []),
             observed, run_id, document_id]).fetchall()
        return {"status": "created" if inserted else "unchanged", "change_id": change_id}

    def stage_reference(self, namespace: str, statement: Mapping[str, Any], *, run_id: str, source_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO vuln_reference_staging VALUES (?,?,?,?,?,?)",
            [namespace, run_id, source_id, statement["kind"], statement["native_id"], canonical(statement)])

    def staged_references(self, namespace: str, run_id: str, source_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT statement_json FROM vuln_reference_staging WHERE namespace=? AND run_id=? AND source_id=? "
            "ORDER BY kind, native_id", [namespace, run_id, source_id]).fetchall()
        return [json.loads(r[0]) for r in rows]

    def cwe_version(self, cwe_id: str) -> str | None:
        """The highest published ``technology-cwe`` version that defines ``cwe_id`` (None when none does)."""
        from src.kb.vulnerability_reference import cwe_version_for

        return cwe_version_for(self.conn, cwe_id)

    # ------------------------------------------------------------------ reads

    def _latest(self, namespace: str, sid: str, as_of_ms: int | None = None) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT revision_id, revision_no, content_digest, source_modified, observation_json FROM vuln_revisions "
            "WHERE namespace=? AND series_id=? AND (? IS NULL OR observed_at_ms<=?) "
            "ORDER BY revision_no DESC LIMIT 1", [namespace, sid, as_of_ms, as_of_ms]).fetchone()
        if row is None:
            return None
        return {**dict(zip(("revision_id", "revision_no", "content_digest", "source_modified"), row[:4])),
                "catalog_released": _load(row[4], {}).get("catalog_released")}

    def latest_revision_id(self, namespace: str, sid: str, as_of_ms: int | None = None) -> str | None:
        latest = self._latest(namespace, sid, as_of_ms)
        return latest["revision_id"] if latest else None

    def series(self, namespace: str, sid: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT series_id, record_kind, source, native_id, cve_id, created_at_ms FROM vuln_series "
            "WHERE namespace=? AND series_id=?", [namespace, sid]).fetchone()
        if row is None:
            raise VulnerabilityError("not_found", "vulnerability record series is not in this namespace")
        return dict(zip(("series_id", "record_kind", "source", "native_id", "cve_id", "created_at_ms"), row))

    def series_list(self, namespace: str, *, cve_id: str | None = None, source: str | None = None,
                    native_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT series_id FROM vuln_series WHERE namespace=? AND (? IS NULL OR cve_id=?) AND (? IS NULL OR "
            "source=?) AND (? IS NULL OR native_id=?) ORDER BY record_kind, source, native_id",
            [namespace, cve_id, cve_id, source, source, native_id, native_id]).fetchall()
        return [self.series(namespace, r[0]) for r in rows]

    def history(self, namespace: str, sid: str, *, as_of_ms: int | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision_id FROM vuln_revisions WHERE namespace=? AND series_id=? AND (? IS NULL OR "
            "observed_at_ms<=?) ORDER BY revision_no", [namespace, sid, as_of_ms, as_of_ms]).fetchall()
        return [self.revision(namespace, r[0], detail=False) for r in rows]

    def revision(self, namespace: str, revision_id: str, *, detail: bool = True) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT r.revision_id, r.series_id, r.revision_no, r.previous_revision_id, r.content_digest, r.lifecycle, "
            "r.source_status, r.source_published, r.source_modified, r.observed_at_ms, r.run_id, r.source_id, "
            "r.document_id, r.statement_json, r.observation_json, s.record_kind, s.source, s.native_id, s.cve_id "
            "FROM vuln_revisions r JOIN vuln_series s ON s.namespace=r.namespace AND s.series_id=r.series_id "
            "WHERE r.namespace=? AND r.revision_id=?", [namespace, revision_id]).fetchone()
        if row is None:
            raise VulnerabilityError("not_found", "vulnerability revision is not in this namespace")
        value = dict(zip(("revision_id", "series_id", "revision_no", "previous_revision_id", "content_digest",
                          "lifecycle", "source_status", "source_published", "source_modified", "observed_at_ms",
                          "run_id", "source_id", "document_id"), row[:13]))
        value.update({"contract": CONTRACT, "record_type": {"advisory": "advisory", "exploitation":
                                                             "exploitation_evidence", "score": "score"}[row[15]],
                      "source": row[16], "native_id": row[17], "cve_id": row[18],
                      "observation": _load(row[14], {})})
        if detail:
            value["statement"] = _load(row[13], {})
            value["aliases"] = self.aliases(namespace, revision_id)
            value["ranges"] = self.ranges(namespace, revision_id=revision_id)
            value["weaknesses"] = self.weaknesses(namespace, revision_id)
            value["scores"] = self.scores(namespace, revision_id=revision_id)
            value["exploitation"] = self.exploitation(namespace, revision_id=revision_id)
        return value

    def aliases(self, namespace: str, revision_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT alias, alias_kind, source FROM vuln_aliases WHERE namespace=? AND revision_id=? ORDER BY alias",
            [namespace, revision_id]).fetchall()
        return [{"record_type": "alias", "alias": r[0], "alias_kind": r[1], "asserted_by": r[2],
                 "revision_id": revision_id} for r in rows]

    def ranges(self, namespace: str, *, revision_id: str | None = None, coordinate: str | None = None,
               package: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT range_id, revision_id, series_id, source, ecosystem_source, ecosystem, package, coordinate, "
            "range_type, events_json, versions_json, cpe_json, applicability FROM vuln_ranges WHERE namespace=? "
            "AND (? IS NULL OR revision_id=?) AND (? IS NULL OR coordinate=?) AND (? IS NULL OR package=?) "
            "ORDER BY source, ecosystem_source, package, range_id",
            [namespace, revision_id, revision_id, coordinate, coordinate, package, package]).fetchall()
        return [{"record_type": "affected_version_range", "range_id": r[0], "revision_id": r[1],
                 "series_id": r[2], "source": r[3], "ecosystem_source": r[4], "ecosystem": r[5], "package": r[6],
                 "coordinate": r[7], "range_type": r[8], "events": _load(r[9], []), "versions": _load(r[10], []),
                 "cpe": _load(r[11], None), "applicability": r[12]} for r in rows]

    def weaknesses(self, namespace: str, revision_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT cwe_id, cwe_version, stated_by, stated_type, source FROM vuln_weaknesses WHERE namespace=? "
            "AND revision_id=? ORDER BY cwe_id, stated_by", [namespace, revision_id]).fetchall()
        result = []
        for cwe, version, stated_by, stated_type, source in rows:
            resolved = version or self.cwe_version(cwe)
            result.append({"record_type": "weakness_classification", "cwe_id": cwe, "cwe_version": resolved,
                           "cwe_version_resolved": "at-acquisition" if version else
                           ("at-read" if resolved else "unresolved"),
                           "stated_by": stated_by, "stated_type": stated_type, "source": source,
                           "revision_id": revision_id})
        return result

    def scores(self, namespace: str, *, revision_id: str | None = None, series: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT s.revision_id, s.source, s.publisher, s.score_kind, s.cvss_version, s.vector, s.base_score, "
            "s.base_severity, s.stated_type, s.epss, s.percentile, s.score_date, s.model_version, r.observed_at_ms "
            "FROM vuln_scores s JOIN vuln_revisions r ON r.namespace=s.namespace AND r.revision_id=s.revision_id "
            "WHERE s.namespace=? AND (? IS NULL OR s.revision_id=?) AND (? IS NULL OR s.series_id=?) "
            "ORDER BY s.score_kind, s.score_date, s.publisher, s.cvss_version, s.vector",
            [namespace, revision_id, revision_id, series, series]).fetchall()
        result = []
        for row in rows:
            if row[3] == "cvss":
                result.append({"record_type": "score", "score_kind": "cvss", "revision_id": row[0], "source": row[1],
                               "publisher": row[2], "cvss_version": row[4], "vector": row[5], "base_score": row[6],
                               "base_severity_as_stated": row[7], "stated_type": row[8]})
            else:
                result.append({"record_type": "score", "score_kind": row[3], "revision_id": row[0], "source": row[1],
                               "publisher": row[2], "epss": row[9], "percentile": row[10], "score_date": row[11],
                               "model_version": row[12], "model_version_known": row[12] is not None,
                               "observed_at_ms": row[13]})
        return result

    def exploitation(self, namespace: str, *, revision_id: str | None = None,
                     cve_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision_id, source, cve_id, date_added, vendor_project, product, catalog_version, "
            "catalog_released, evidence_json FROM vuln_exploitation WHERE namespace=? AND (? IS NULL OR revision_id=?) "
            "AND (? IS NULL OR cve_id=?) ORDER BY cve_id, revision_id",
            [namespace, revision_id, revision_id, cve_id, cve_id]).fetchall()
        return [{"record_type": "exploitation_evidence", "revision_id": r[0], "source": r[1], "cve_id": r[2],
                 "date_added": r[3], "vendor_project": r[4], "product": r[5], "catalog_version": r[6],
                 "catalog_released": r[7], "evidence": _load(r[8], {}),
                 "meaning": "catalog listing on date_added; not an exploitability verdict"} for r in rows]

    def change_events(self, namespace: str, cve_id: str, *, as_of_ms: int | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT change_id, source, event_name, created, source_identifier, details_json, observed_at_ms, "
            "document_id FROM vuln_change_events WHERE namespace=? AND cve_id=? AND (? IS NULL OR observed_at_ms<=?) "
            "ORDER BY created, change_id", [namespace, cve_id, as_of_ms, as_of_ms]).fetchall()
        return [{"record_type": "change_event", "change_id": r[0], "source": r[1], "event_name": r[2],
                 "created": r[3], "source_identifier": r[4], "details": _load(r[5], []), "observed_at_ms": r[6],
                 "document_id": r[7]} for r in rows]

    def revision_digests(self, namespace: str, revision_ids: Iterable[str]) -> list[dict[str, Any]]:
        ids = sorted(set(revision_ids))
        if not ids:
            return []
        rows = self.conn.execute(
            "SELECT r.revision_id, r.content_digest, r.series_id, s.source, s.native_id FROM vuln_revisions r "
            "JOIN vuln_series s ON s.namespace=r.namespace AND s.series_id=r.series_id WHERE r.namespace=? AND "
            "r.revision_id IN (" + ",".join("?" * len(ids)) + ") ORDER BY r.revision_id", [namespace, *ids]).fetchall()
        return [dict(zip(("revision_id", "content_digest", "series_id", "source", "native_id"), r)) for r in rows]


class VulnerabilityProjector:
    """Source-pack runtime projector for ``noesis-vulnerability-record-v1`` pages.

    Advisory, exploitation, score and change-event statements are applied as
    revisions before the page checkpoint advances (idempotent on replay). CWE
    and CPE reference statements are staged per run and published as ontology
    modules when the source finishes complete, so a paged dictionary becomes
    one module version rather than one per page.
    """

    SERVICE_SCOPES = frozenset({"knowledge:schema:read", "knowledge:schema:register", "knowledge:schema:deprecate"})

    def __init__(self, conn: Any) -> None:
        self.store = VulnerabilityStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("vulnerabilities") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, page_receipt, principal_id
        namespace = self._namespace(source)
        by_record = {str(d["metadata"].get("source_pack_record_id")): d for d in documents or []}
        results = []
        for record in records:
            statement = record.get("vulnerability_record")
            if not isinstance(statement, Mapping):
                raise VulnerabilityError("invalid_record", "page record is not a vulnerability statement")
            if statement.get("kind") in REFERENCE_KINDS:
                self.store.stage_reference(namespace, statement, run_id=run_id, source_id=source["source_id"])
                results.append({"status": "staged", "native_id": statement["native_id"]})
                continue
            document = by_record.get(str(record.get("id")))
            results.append(self.store.apply(
                namespace, statement, run_id=run_id, source_id=source["source_id"],
                document_id=document["document_id"] if document else None,
                observed_at_ms=int(document["ingested_at"]) if document and document.get("ingested_at") else None))
        return results

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest
        namespace = self._namespace(source)
        staged = self.store.staged_references(namespace, run_id, source["source_id"])
        published = None
        if staged and status == "complete":
            from src.kb.vulnerability_reference import VulnerabilityReference

            published = VulnerabilityReference(self.store.conn).publish_statements(
                staged, principal_id=principal_id, scopes=set(self.SERVICE_SCOPES),
                selection=dict(dict(source.get("vulnerabilities") or {}).get("selection") or {}))
        return {"status": status, "namespace": namespace, "reference_published": published,
                "reference_staged": len(staged),
                **({"reference_skipped": "source did not finish complete"} if staged and status != "complete"
                   else {})}


def register_schemas(conn: Any, *, principal_id: str, scopes: Any) -> list[dict[str, Any]]:
    """Register the vulnerability record contract as a schema module in the shared registry."""
    from pathlib import Path

    from src.kb.schema_registry import SchemaRegistry

    path = Path(__file__).resolve().parents[2] / "contracts/schemas/jsonschema" / f"{CONTRACT}.json"
    definition = {
        "contract": "noesis-schema-module-v1", "name": "vulnerability-record", "kind": "schema",
        "semantic_version": "1.0.0", "content": json.loads(path.read_text()), "owner": "technology.vulnerabilities",
        "dependencies": [], "compatibility_policy": "backward",
        "provenance": {"kind": "imported", "source": f"contracts/schemas/jsonschema/{CONTRACT}.json"},
        "actor": {"principal_id": principal_id, "kind": "service"},
    }
    return [SchemaRegistry(conn).register(definition, "vulnerability-schema:vulnerability-record:1.0.0",
                                          principal_id=principal_id, scopes=scopes)]


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.vulnerability_sources import PROVIDER_CONTRACTS

    store = VulnerabilityStore(conn, initialize=False)
    counts: dict[str, int] = {}
    if store.ready():
        counts = {r[0]: int(r[1]) for r in conn.execute(
            "SELECT source, count(*) FROM vuln_series GROUP BY source").fetchall()}
    return {
        "feature": "technology.vulnerabilities",
        "enabled": feature_enabled(conn),
        "store_ready": store.ready(),
        "series_by_source": counts,
        "providers": {name: {"access_decision": c["access_decision"], "reason": c["reason"],
                             "live": "outstanding"} for name, c in PROVIDER_CONTRACTS.items()},
        "evidence": "offline fixtures only until a dated live run; live coverage is reported separately",
    }


__all__ = [
    "CONTRACT",
    "DEFAULT_NAMESPACE",
    "READ_SCOPE",
    "RECORD_TYPES",
    "REVIEW_SCOPE",
    "VulnerabilityError",
    "VulnerabilityProjector",
    "VulnerabilityStore",
    "WRITE_SCOPE",
    "authorize",
    "feature_enabled",
    "readiness",
    "register_schemas",
    "series_id",
    "source_time_ms",
    "statement_from_advisory_record",
]
