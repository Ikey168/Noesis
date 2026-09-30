"""Campaign-finance records: committees, filings and their amendments, contributions, expenditures and
independent expenditures, versioned as the regulators published them (#2209, CF02).

Acquired ``noesis-campaign-finance-record-v1`` records (from
:mod:`src.ingestion.campaign_finance_sources`) are kept per source and record
key with an append-only revision log, following the
:mod:`src.kb.entity_history` pattern of never rewriting what was recorded:

* a **filing version** is its own record (an FEC ``file_number``; a Commission
  return per regulated entity and reporting period or election). An amendment
  is a *new* record whose ``amendment_chain`` names the versions it amends; the
  amended versions are never overwritten. A change the regulator makes to a
  version's published flags (``most_recent``) is a new revision of that
  version's record, stored as published and never inferred;
* **line items** (contributions, expenditures, independent expenditures)
  reference the filing version they were reported in (``filing_key``) and the
  filing record revision current when they were stored
  (``filing_revision_id``); amounts and dates stay as reported, memo items stay
  labelled;
* **reported totals** live only on the filing version that reported them; any
  derived figure (:mod:`src.kb.campaign_finance_queries`) names the filing
  versions it used;
* a **committee or candidate registration** (FEC Form 1 / Form 2 per two-year
  period) is a dated revision of the committee or candidate record; an older
  registration observed later is logged as ``older-observation`` and never
  becomes current; a replayed response already on record adds nothing. The
  Commission publishes no revision stamp, so its latest acquisition is current.

**Minimisation (CF01) is enforced at write time.** A record whose counterparty
is a natural person must carry no name, address, city, ZIP or postcode,
employer, occupation, donor or contributor id, per-person aggregate or memo
text; :meth:`CampaignFinanceStore.project` refuses it with
``minimisation_violation`` before anything is written. Nothing here scores
influence, infers undisclosed funding or profiles a donor.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.campaign_finance_sources import (
    MINIMISATION,
    RECORD_CONTRACT,
    RECORD_KINDS,
    REVIEW_BOUNDARY,
    is_natural_person,
    minimisation_violations,
)

CONTRACT = RECORD_CONTRACT
READ_SCOPE = "knowledge:political:campaign-finance:read"
WRITE_SCOPE = "knowledge:political:campaign-finance:write"
REVIEW_SCOPE = "knowledge:political:campaign-finance:review"
INDIVIDUAL_SCOPE = "knowledge:political:campaign-finance:individual-items:read"
DEFAULT_NAMESPACE = "global"
FEATURES = {"US": "campaign-finance-us", "GB": "campaign-finance-uk"}
CHANGES = ("new", "revised", "unchanged", "older-observation")
REGISTRATION_KINDS = ("committee", "candidate", "regulated-entity")
ITEM_KINDS = ("contribution", "expenditure", "independent-expenditure")
EXCLUSIONS = ("influence scoring", "dark-money or undisclosed-funding inference",
              "profiling of individual donors beyond the CF01 minimisation decision")
# Keys that would carry an influence reading, an inferred funder or a donor profile; no answer may contain them.
FORBIDDEN_ANSWER_KEYS = frozenset({
    "influence", "influence_score", "score", "rank", "ranking", "dark_money", "undisclosed_funder",
    "inferred_funder", "quid_pro_quo", "risk", "risk_score", "donor_profile", "verdict", "corruption",
})

_DDL = """
CREATE TABLE IF NOT EXISTS campaign_finance_records (
  namespace TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL, provider TEXT NOT NULL,
  jurisdiction TEXT NOT NULL, record_kind TEXT NOT NULL, committee_key TEXT, filing_key TEXT, filing_group TEXT,
  current_revision_id TEXT NOT NULL, revision_count INTEGER NOT NULL, first_run_id TEXT NOT NULL,
  first_observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, source_id, record_key)
);
CREATE TABLE IF NOT EXISTS campaign_finance_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, source_id TEXT NOT NULL, record_key TEXT NOT NULL,
  revision_no INTEGER NOT NULL, previous_revision_id TEXT, change TEXT NOT NULL, content_hash TEXT NOT NULL,
  native_revision TEXT, revision_order TEXT NOT NULL, effective_on TEXT, record_json TEXT NOT NULL,
  filing_revision_id TEXT, late_observation BOOLEAN NOT NULL, individual BOOLEAN NOT NULL,
  evidence_origin TEXT NOT NULL, run_id TEXT NOT NULL, receipt_id TEXT, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS campaign_finance_receipts (
  namespace TEXT NOT NULL, receipt_id TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL,
  receipt_json TEXT NOT NULL, outcome_json TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, receipt_id)
);
"""
_REVISION_COLUMNS = ("revision_id", "source_id", "record_key", "revision_no", "previous_revision_id", "change",
                     "content_hash", "native_revision", "revision_order", "effective_on", "record_json",
                     "filing_revision_id", "late_observation", "individual", "evidence_origin", "run_id",
                     "receipt_id", "observed_at_ms")


class CampaignFinanceError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def authorize(namespace: str, scopes: Iterable[str], required: str, *, write: bool = False) -> None:
    scopes = set(scopes)
    if "operator" in scopes:
        return
    needed = ({f"namespace:{namespace}:write"} if write
              else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"})
    if required not in scopes or not needed & scopes:
        raise CampaignFinanceError("unauthorized", f"{required} and namespace access are required")


def table_exists(conn: Any, name: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def feature_enabled(conn: Any, feature: str) -> bool:
    """Whether the Political bundle's optional ``campaign-finance-us`` / ``campaign-finance-uk`` feature is selected.

    Reads the active composition plan only; defaults to off.
    """
    try:
        if not all(table_exists(conn, t) for t in ("composition_authority", "composition_active",
                                                    "composition_generations", "composition_plans")):
            return False
        managed = conn.execute("SELECT authority FROM composition_authority WHERE bundle='political'").fetchone()
        if not managed or managed[0] != "composition":
            return False
        row = conn.execute(
            "SELECT p.plan_json FROM composition_active a JOIN composition_generations g "
            "ON g.generation_id=a.generation_id JOIN composition_plans p ON p.digest=g.plan_digest WHERE a.slot=1"
        ).fetchone()
        plan = json.loads(row[0]) if row else {}
    except Exception:  # noqa: BLE001 - an unreadable plan never enables a feature
        return False
    return feature in ((plan.get("features") or {}).get("political") or [])


def validate(record: Mapping[str, Any]) -> dict[str, Any]:
    """Structural checks plus the CF01 minimisation guard; returns a canonical copy."""
    record = json.loads(canonical(record))
    if record.get("contract") != CONTRACT or record.get("record_kind") not in RECORD_KINDS:
        raise CampaignFinanceError("invalid_record", "not a campaign-finance record")
    if not str(record.get("record_key") or "").startswith("campaign-finance:"):
        raise CampaignFinanceError("invalid_record", "record keys are campaign-finance:* keys")
    if not str(record.get("locator") or "").startswith("https://"):
        raise CampaignFinanceError("invalid_record", "every record cites an HTTPS locator")
    if record["record_kind"] in ITEM_KINDS and not record.get("filing_key"):
        raise CampaignFinanceError("invalid_record", "a line item names the filing version it was reported in")
    if record["record_kind"] == "filing" and "totals_as_reported" not in (record.get("fields") or {}):
        raise CampaignFinanceError("invalid_record", "a filing states its totals as reported (or none)")
    violations = minimisation_violations(record)
    if violations:
        raise CampaignFinanceError("minimisation_violation", "an individual's personal fields may not be stored "
                                   "(CF01)", paths=violations)
    return record


def _view(row: Sequence[Any], head: Mapping[str, Any]) -> dict[str, Any]:
    revision = dict(zip(_REVISION_COLUMNS, row))
    record = json.loads(revision.pop("record_json"))
    return {
        **head, **revision, "late_observation": bool(revision["late_observation"]),
        "individual": bool(revision["individual"]), "record": record,
        "citation": {
            "source_id": revision["source_id"], "provider": head.get("provider") or record.get("provider"),
            "record_key": revision["record_key"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "native_revision": revision["native_revision"],
            "locator": record.get("locator"), "observed_at_ms": revision["observed_at_ms"],
            "evidence_origin": revision["evidence_origin"], "filing_key": record.get("filing_key"),
            "filing_revision_id": revision["filing_revision_id"],
        },
    }


class CampaignFinanceStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "campaign_finance_revisions")

    # ------------------------------------------------------------------ writes

    def project(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str, source_id: str,
                receipt: Mapping[str, Any] | None = None, observed_at_ms: int | None = None) -> dict[str, Any]:
        """Append revisions for what changed; idempotent (re-projecting an unchanged record adds nothing).

        Registrations and filings are stored before the line items of the same page so that each item can
        reference the filing revision it was reported against.
        """
        checked = [validate(r) for r in records]  # refuse the whole page before writing anything
        keys = [(r["record_key"], r.get("native_revision"), r.get("revision_order")) for r in checked]
        if len(set(keys)) != len(keys):
            raise CampaignFinanceError("invalid_record", "a page repeats a record revision")
        observed = int(observed_at_ms if observed_at_ms is not None else self.now())
        receipt = dict(receipt or {})
        receipt_id = "cf-receipt:" + digest([namespace, source_id, run_id, receipt, [k[0] for k in keys]])[:24]
        counts = dict.fromkeys(CHANGES, 0)
        order = {kind: i for i, kind in enumerate(REGISTRATION_KINDS + ("filing",) + ITEM_KINDS)}
        self.conn.execute("BEGIN")
        try:
            for record in sorted(checked, key=lambda r: (order[r["record_kind"]], r["record_key"],
                                                         r.get("revision_order") or "")):
                counts[self._observe(namespace, record, source_id, run_id, receipt_id, observed)] += 1
            self.conn.execute(
                "INSERT OR IGNORE INTO campaign_finance_receipts VALUES (?,?,?,?,?,?,?)",
                [namespace, receipt_id, run_id, source_id, canonical(receipt), canonical(counts), observed])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {"receipt_id": receipt_id, "counts": counts, "records": len(checked)}

    def _current_filing_revision(self, namespace: str, filing_key: str | None) -> tuple[str | None, str | None]:
        if not filing_key:
            return None, None
        row = self.conn.execute(
            "SELECT current_revision_id, first_run_id FROM campaign_finance_records WHERE namespace=? AND "
            "record_key=? AND record_kind='filing' ORDER BY source_id LIMIT 1", [namespace, filing_key]).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def _observe(self, namespace, record, source_id, run_id, receipt_id, observed) -> str:
        key = record["record_key"]
        origin = "fixture" if record.get("evidence_origin") == "fixture" else "live"
        body = {k: v for k, v in record.items() if k != "evidence_origin"}
        content_hash = digest(body)
        head = self.conn.execute(
            "SELECT r.current_revision_id, r.revision_count, v.content_hash, v.revision_order, r.first_run_id "
            "FROM campaign_finance_records r JOIN campaign_finance_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? AND r.source_id=? AND r.record_key=?",
            [namespace, source_id, key]).fetchone()
        order = str(record.get("revision_order") or "")
        if head is not None:
            if head[2] == content_hash:
                return "unchanged"
            if self.conn.execute(
                    "SELECT 1 FROM campaign_finance_revisions WHERE namespace=? AND source_id=? AND record_key=? AND "
                    "content_hash=?", [namespace, source_id, key, content_hash]).fetchone():
                return "unchanged"  # a replayed older response: already on record, never re-applied
            change = "older-observation" if order < str(head[3] or "") else "revised"
        else:
            change = "new"
        filing_revision_id, filing_first_run = (None, None)
        if record["record_kind"] in ITEM_KINDS:
            filing_revision_id, filing_first_run = self._current_filing_revision(namespace, record.get("filing_key"))
        # A Commission item first observed after its return was first acquired (a late report) is flagged as such.
        late = bool(change == "new" and record["provider"] == "uk-electoral-commission" and filing_first_run
                    and filing_first_run != run_id)
        revision_no = 1 if head is None else int(head[1]) + 1
        revision_id = "cf-rev:" + digest([namespace, source_id, key, revision_no, content_hash])[:24]
        self.conn.execute(
            "INSERT INTO campaign_finance_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, revision_id, source_id, key, revision_no, None if head is None else head[0], change,
             content_hash, record.get("native_revision"), order, record.get("effective_on"), canonical(record),
             filing_revision_id, late, is_natural_person(record), origin, run_id, receipt_id, observed])
        if head is None:
            self.conn.execute(
                "INSERT INTO campaign_finance_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, source_id, key, record["provider"], record["jurisdiction"], record["record_kind"],
                 record.get("committee_key"), record.get("filing_key"), record.get("filing_group"), revision_id,
                 1, run_id, observed])
        elif change == "older-observation":
            self.conn.execute("UPDATE campaign_finance_records SET revision_count=? WHERE namespace=? AND "
                              "source_id=? AND record_key=?", [revision_no, namespace, source_id, key])
        else:
            self.conn.execute(
                "UPDATE campaign_finance_records SET current_revision_id=?, revision_count=?, committee_key=?, "
                "filing_key=?, filing_group=? WHERE namespace=? AND source_id=? AND record_key=?",
                [revision_id, revision_no, record.get("committee_key"), record.get("filing_key"),
                 record.get("filing_group"), namespace, source_id, key])
        return change

    # ------------------------------------------------------------------ reads

    def _heads(self, namespace: str, where: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT r.provider, r.jurisdiction, r.record_kind, r.committee_key, r.filing_key, r.filing_group, "
            "r.revision_count, r.first_observed_at_ms, " + ", ".join(f"v.{c}" for c in _REVISION_COLUMNS) +
            " FROM campaign_finance_records r JOIN campaign_finance_revisions v ON v.namespace=r.namespace AND "
            "v.revision_id=r.current_revision_id WHERE r.namespace=? " + where +
            " ORDER BY r.record_kind, r.record_key, r.source_id", [namespace, *params]).fetchall()
        out = []
        for row in rows:
            head = dict(zip(("provider", "jurisdiction", "record_kind", "committee_key", "filing_key",
                             "filing_group", "revision_count", "first_observed_at_ms"), row[:8]))
            out.append(_view(row[8:], head))
        return out

    def records(self, namespace: str, *, scopes: Iterable[str], kinds: Iterable[str] | None = None,
                committee_key: str | None = None, filing_key: str | None = None, filing_group: str | None = None,
                record_keys: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Current revision of every matching record (per source)."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        where, params = [], []
        for column, value in (("r.committee_key", committee_key), ("r.filing_key", filing_key),
                              ("r.filing_group", filing_group)):
            if value is not None:
                where.append(f"AND {column}=?")
                params.append(value)
        for column, values in (("r.record_kind", kinds), ("r.record_key", record_keys)):
            if values is not None:
                values = sorted(set(values))
                if not values:
                    return []
                where.append(f"AND {column} IN (" + ",".join("?" * len(values)) + ")")
                params += values
        return self._heads(namespace, " ".join(where), params)

    def history(self, namespace: str, record_key: str, *, scopes: Iterable[str], source_id: str | None = None
                ) -> list[dict[str, Any]]:
        """Every revision of a record in arrival order, including older observations that never became current."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM campaign_finance_revisions WHERE namespace=? AND "
            "record_key=? AND (? IS NULL OR source_id=?) ORDER BY source_id, revision_no",
            [namespace, record_key, source_id, source_id]).fetchall()
        return [_view(row, {}) for row in rows]

    def revision(self, namespace: str, revision_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute("SELECT " + ", ".join(_REVISION_COLUMNS) + " FROM campaign_finance_revisions "
                                "WHERE namespace=? AND revision_id=?", [namespace, revision_id]).fetchone()
        if row is None:
            raise CampaignFinanceError("not_found", "revision is not visible in this namespace")
        return _view(row, {})

    def filing_chain(self, namespace: str, filing_group: str, *, scopes: Iterable[str], as_of: str | None = None
                     ) -> dict[str, Any]:
        """Every version of one filing (original, amendments, termination) and the version available on a date.

        The version *selected as of* a date is the latest version the regulator had received on or before it
        (receipt date, then position in the published amendment chain). The regulator's own ``most_recent`` flag is
        reported per version exactly as last published, never inferred; the two can differ and both are shown.
        A version without a published receipt date (a Commission return) is always available and says so.
        """
        rows = self.records(namespace, scopes=scopes, kinds=["filing"], filing_group=filing_group)
        versions = []
        for row in rows:
            fields = row["record"]["fields"]
            versions.append({
                "filing_key": row["record_key"], "file_number": fields.get("file_number"),
                "form_type": fields.get("form_type"), "report_type": fields.get("report_type"),
                "coverage": {"start": fields.get("coverage_start_date"), "end": fields.get("coverage_end_date")},
                "receipt_date": fields.get("receipt_date"),
                "amendment_indicator": fields.get("amendment_indicator"),
                "amendment_chain": fields.get("amendment_chain") or [],
                "most_recent_as_published": fields.get("most_recent"),
                "most_recent_file_number_as_published": fields.get("most_recent_file_number"),
                "totals_as_reported": fields.get("totals_as_reported"), "revision_id": row["revision_id"],
                "revision_no": row["revision_no"], "citation": row["citation"],
            })
        versions.sort(key=lambda v: (v["receipt_date"] or "", len(v["amendment_chain"]), v["file_number"] or 0,
                                     v["filing_key"]))
        day = str(as_of)[:10] if as_of else None
        available = [v for v in versions if day is None or v["receipt_date"] is None or v["receipt_date"] <= day]
        return {"filing_group": filing_group, "as_of": day, "versions": versions,
                "selected": available[-1] if available else None,
                "selection_basis": "the latest version received on or before the as-of date (receipt date, then "
                                   "amendment-chain position); the regulator's most_recent flag is shown as published",
                "later_versions": [v["filing_key"] for v in versions if v not in available]}

    def receipts(self, namespace: str, run_id: str | None = None, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT receipt_id, run_id, source_id, receipt_json, outcome_json, recorded_at_ms FROM "
            "campaign_finance_receipts WHERE namespace=? AND (? IS NULL OR run_id=?) ORDER BY recorded_at_ms, "
            "receipt_id", [namespace, run_id, run_id]).fetchall()
        return [{"receipt_id": r[0], "run_id": r[1], "source_id": r[2], "receipt": json.loads(r[3]),
                 "counts": json.loads(r[4]), "recorded_at_ms": r[5]} for r in rows]


class CampaignFinanceProjector:
    """Source-pack runtime projector for ``noesis-campaign-finance-record-v1`` pages (one selection unit per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = CampaignFinanceStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("campaign_finance") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, documents, principal_id
        items = []
        for item in records:
            record = item.get("campaign_finance_record")
            if not isinstance(record, Mapping):
                raise CampaignFinanceError("invalid_record", "page record is not a campaign-finance record")
            items.append(dict(record))
        return [self.store.project(self._namespace(source), items, run_id=run_id, source_id=source["source_id"],
                                   receipt=dict(page_receipt or {}))]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        rows = self.store.conn.execute(
            "SELECT count(*) FROM campaign_finance_receipts WHERE namespace=? AND run_id=? AND source_id=?",
            [self._namespace(source), run_id, source["source_id"]]).fetchone()
        return {"status": status, "units": int(rows[0])}


def readiness(conn: Any) -> dict[str, Any]:
    from src.ingestion.campaign_finance_sources import LIVE_VERIFICATION, PROVIDER_CONTRACTS

    store = CampaignFinanceStore(conn, initialize=False)
    counts: dict[str, int] = {}
    individual = 0
    if store.ready():
        counts = dict(conn.execute("SELECT provider, count(*) FROM campaign_finance_records GROUP BY provider"
                                   ).fetchall())
        individual = int(conn.execute("SELECT count(*) FROM campaign_finance_revisions WHERE individual"
                                      ).fetchone()[0])
    return {
        "feature": "political.campaign-finance",
        "enabled": {"US": feature_enabled(conn, FEATURES["US"]), "GB": feature_enabled(conn, FEATURES["GB"])},
        "store_ready": store.ready(),
        "providers": {p: {"access_decision": c["access_decision"], "live": LIVE_VERIFICATION[p]["status"],
                          "records": int(counts.get(p, 0))} for p, c in PROVIDER_CONTRACTS.items()},
        "minimisation": {"policy": MINIMISATION["policy"], "minimised_individual_item_revisions": individual},
        "review_boundary": REVIEW_BOUNDARY,
    }


def forbidden_keys(value: Any, path: str = "$") -> list[str]:
    """Keys anywhere in an answer that would carry an influence reading, an inferred funder or a donor profile."""
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in FORBIDDEN_ANSWER_KEYS:
                found.append(f"{path}.{key}")
            found += forbidden_keys(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += forbidden_keys(item, f"{path}[{index}]")
    return found
