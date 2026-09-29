"""Shared plumbing for the Legal pack's courts and justice-statistics features (#2218).

``noesis-court-justice-record-v1`` records arrive through the ``legal-research``
source-pack runtime (connector ``courts-justice``) and are projected by
:class:`CourtsJusticeProjector`: dockets and opinion clusters into
:mod:`src.kb.legal_dockets` (on the existing Legal work/expression/version
model), statistics releases into :mod:`src.kb.justice_statistics`. Both
features are optional Legal features (``courts``, ``justice-statistics``),
default off, selected through the active composition plan.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.legal import READ_SCOPE, WRITE_SCOPE, legal_feature_enabled  # noqa: F401 - re-exported scopes

REVIEW_SCOPE = "knowledge:legal:review"
RECORD_CONTRACT = "noesis-court-justice-record-v1"
DOCKET_ANSWER_CONTRACT = "noesis-court-docket-answer-v1"
STATISTICS_ANSWER_CONTRACT = "noesis-justice-statistics-answer-v1"
DEFAULT_NAMESPACE = "global"
FEATURES = ("courts", "justice-statistics")
# Keys no answer may carry: derived outcomes, predictions, scores, ratings or rankings (#2218 exclusions).
FORBIDDEN_KEYS = frozenset({"outcome_label", "winner", "loser", "won", "lost", "prediction", "risk_score",
                            "recidivism_score", "safety_rating", "safety_score", "rank", "ranking", "legal_advice"})


class CourtsJusticeError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & scopes:
        raise CourtsJusticeError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def day(value: Any) -> str | None:
    match = re.match(r"^(\d{4}-\d{2}-\d{2})", str(value or "").strip())
    return match.group(1) if match else None


def forbidden_keys(value: Any, path: str = "") -> list[str]:
    """Paths of any forbidden (derived-outcome, score, rating, ranking) key in an answer."""
    found = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in FORBIDDEN_KEYS:
                found.append(f"{path}/{key}")
            found += forbidden_keys(item, f"{path}/{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}/{index}")
    return found


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the optional Legal ``courts`` / ``justice-statistics`` feature is selected in the active plan."""
    if feature not in FEATURES:
        raise CourtsJusticeError("invalid_feature", f"feature is one of {FEATURES}")
    return legal_feature_enabled(conn, feature)


class CourtsJusticeProjector:
    """Source-pack runtime projector for ``noesis-court-justice-record-v1``."""

    def __init__(self, conn: Any) -> None:
        from src.kb.justice_statistics import JusticeStatisticsStore
        from src.kb.legal_dockets import LegalDocketStore

        self.conn = conn
        self.dockets = LegalDocketStore(conn)
        self.statistics = JusticeStatisticsStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("courts_justice") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project(self, namespace: str, records: Iterable[Mapping[str, Any]], *, run_id: str, source_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        records = [dict(r) for r in records]
        if any(r.get("contract") != RECORD_CONTRACT for r in records):
            raise CourtsJusticeError("invalid_record", "page record is not a court-justice record")
        dockets = [r for r in records if r["record_kind"] in {"docket", "opinion-cluster"}]
        releases = [r for r in records if r["record_kind"] == "statistics-release"]
        counts: dict[str, int] = {}
        for store, group in ((self.dockets, dockets), (self.statistics, releases)):
            if group:
                for key, value in store.project(namespace, group, run_id=run_id, source_id=source_id,
                                                receipt=receipt, observed_at_ms=observed_at_ms).items():
                    counts[key] = counts.get(key, 0) + value
        return {"counts": counts}

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        namespace = self._namespace(source)
        result = self.project(namespace, [r["court_justice_record"] for r in records], run_id=run_id,
                              source_id=source["source_id"], receipt=dict(page_receipt or {}))
        return [result]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id, run_id
        return {"status": status}


def readiness(conn: Any, *, pack_id: str = "legal-research") -> dict[str, Any]:
    """Whether the features are selected and what each provider has acquired; unverified live access is stated."""
    from src.ingestion.courts_justice_sources import FORMATS, LIVE_VERIFICATION

    counts: dict[str, int] = {}
    for table, column in (("legal_docket_revisions", "provider"), ("legal_opinion_revisions", "provider"),
                          ("justice_vintages", "provider")):
        if table_exists(conn, table):
            for provider, count in conn.execute(f"SELECT {column}, count(*) FROM {table} GROUP BY 1").fetchall():
                counts[provider] = counts.get(provider, 0) + int(count)
    providers = {}
    for fmt, spec in FORMATS.items():
        entry = providers.setdefault(spec["provider"], {"jurisdiction": spec["jurisdiction"], "formats": [],
                                                        "feature": spec["feature"],
                                                        "revisions_acquired": counts.get(spec["provider"], 0),
                                                        "live": LIVE_VERIFICATION[spec["provider"]]})
        entry["formats"].append(fmt)
    return {"pack_id": pack_id, "features": {f: feature_enabled(conn, f) for f in FEATURES},
            "providers": providers,
            "notice": "unverified-live providers have fixture evidence only; a dated live run is outstanding (#2434)"}
