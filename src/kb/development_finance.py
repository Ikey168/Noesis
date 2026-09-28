"""Aid activities, publishers, CRS aggregates and World Bank projects: the ``funding.development-finance`` record owner.

Records (contract ``noesis-development-finance-record-v1``, #1932 D02) are
namespace-scoped and revision-addressable in ``devfin_*`` tables. Every
revision carries its source, the dataset version (capture digest) it came
from and its observation time:

* **publisher** - the organisation that *reports* (an IATI reporting-org, the
  OECD for CRS, the World Bank for its projects). It is a record of its own,
  separate from the organisations it reports on, and publishers are never
  merged: one reference is one publisher.
* **dataset** - one acquisition (query, capture digests, evidence origin); the
  dataset version every revision points to.
* **activity** and **activity revision** - one IATI activity *as one publisher
  reports it* (the IATI identifier and reporting-org reference stored exactly
  as given). The same identifier reported by two publishers is two activities,
  side by side. A changed activity is a new revision, never an overwrite; the
  revision in force is chosen by the publisher's own ``last-updated-datetime``
  (the observation time only when the stamp is absent), so a late-arriving
  older version lands as history and an unchanged re-acquisition adds nothing.
* **participating organisation**, **transaction**, **allocation** (sector,
  recipient country, recipient region - activity or transaction level) and
  **result** - children of an activity revision, addressable by revision.
  Transactions keep value text, currency (stated or the activity default),
  value date, type and provider/receiver organisations as reported; an amount
  without a currency or value date stays unknown, and no aggregate is stored
  on an activity.
* **publisher coverage** - what a selection requested, what came back per
  publisher, how many pages were read and why reading stopped; a failed or
  partial refresh is stale coverage, and only a complete selection can record
  that a publisher no longer publishes an activity (a *withdrawal*, which never
  means the activity ended).
* **CRS cell** and **CRS vintage** - an OECD CRS aggregate (donor, recipient,
  sector, flow, channel, price basis) with every release kept as a vintage.
* **World Bank project** and **project revision** - the World Bank's own
  project records.

Nothing here totals amounts across publishers, converts a currency, averages
conflicting values or judges effectiveness.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from src.ingestion.development_finance_sources import (
    NEVER_SENTENCE,
    PROVIDER_CONTRACTS,
    identifier_key,
)

CONTRACT = "noesis-development-finance-record-v1"
ANSWER_CONTRACT = "noesis-development-finance-answer-v1"
READ_SCOPE = "knowledge:funding:development-finance:read"
WRITE_SCOPE = "knowledge:funding:development-finance:write"
REVIEW_SCOPE = "knowledge:funding:development-finance:review"
INGEST_SCOPE = "knowledge:ingestion:execute"
DEFAULT_NAMESPACE = "global"
RECORD_TYPES = (
    "publisher",
    "dataset",
    "activity",
    "activity_revision",
    "participating_organisation",
    "transaction",
    "allocation",
    "result",
    "publisher_coverage",
    "crs_cell",
    "crs_vintage",
    "world_bank_project",
)
# Keys that would carry a judgement, a cross-publisher total or an averaged value.
FORBIDDEN_KEYS = frozenset(
    {
        "effectiveness",
        "impact_score",
        "impact",
        "achievement",
        "achieved",
        "success",
        "rating",
        "verdict",
        "total_all_publishers",
        "combined_total",
        "grand_total",
        "average",
        "averaged",
        "merged_value",
    }
)

_DDL = """
CREATE TABLE IF NOT EXISTS devfin_publishers (
  namespace TEXT NOT NULL, publisher_id TEXT NOT NULL, provider TEXT NOT NULL, publisher_ref TEXT,
  name TEXT, first_dataset_id TEXT, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, publisher_id)
);
CREATE TABLE IF NOT EXISTS devfin_datasets (
  namespace TEXT NOT NULL, dataset_id TEXT NOT NULL, provider TEXT NOT NULL, query_json TEXT NOT NULL,
  source_url TEXT, capture_digests_json TEXT NOT NULL, evidence_origin TEXT NOT NULL, execution TEXT,
  run_id TEXT, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, dataset_id)
);
CREATE TABLE IF NOT EXISTS devfin_activities (
  namespace TEXT NOT NULL, activity_key TEXT NOT NULL, publisher_id TEXT NOT NULL, iati_identifier TEXT NOT NULL,
  identifier_key TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, activity_key)
);
CREATE TABLE IF NOT EXISTS devfin_activity_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, activity_key TEXT NOT NULL, publisher_id TEXT NOT NULL,
  iati_identifier TEXT NOT NULL, revision_no INTEGER NOT NULL, supersedes TEXT, arrival TEXT NOT NULL,
  last_updated_at TEXT, last_updated_text TEXT, order_stamp TEXT NOT NULL, stamp_basis TEXT NOT NULL,
  body_json TEXT NOT NULL, content_hash TEXT NOT NULL, dataset_id TEXT NOT NULL, capture_sha256 TEXT NOT NULL,
  source_url TEXT, locator_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS devfin_participating_orgs (
  namespace TEXT NOT NULL, org_record_id TEXT NOT NULL, revision_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  role TEXT, ref TEXT, org_type TEXT, name TEXT, activity_id TEXT, PRIMARY KEY(namespace, org_record_id)
);
CREATE TABLE IF NOT EXISTS devfin_transactions (
  namespace TEXT NOT NULL, transaction_id TEXT NOT NULL, revision_id TEXT NOT NULL, activity_key TEXT NOT NULL,
  publisher_id TEXT NOT NULL, ordinal INTEGER NOT NULL, transaction_key TEXT NOT NULL, transaction_ref TEXT,
  transaction_type TEXT, transaction_date TEXT, value_text TEXT, value TEXT, currency TEXT, currency_source TEXT,
  value_date TEXT, provider_org_json TEXT, receiver_org_json TEXT, body_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  PRIMARY KEY(namespace, transaction_id)
);
CREATE TABLE IF NOT EXISTS devfin_allocations (
  namespace TEXT NOT NULL, allocation_id TEXT NOT NULL, revision_id TEXT NOT NULL, level TEXT NOT NULL,
  transaction_ordinal INTEGER, kind TEXT NOT NULL, vocabulary TEXT, code TEXT, percentage_text TEXT,
  percentage TEXT, narrative TEXT, PRIMARY KEY(namespace, allocation_id)
);
CREATE TABLE IF NOT EXISTS devfin_results (
  namespace TEXT NOT NULL, result_id TEXT NOT NULL, revision_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  result_type TEXT, title TEXT, body_json TEXT NOT NULL, content_hash TEXT NOT NULL, PRIMARY KEY(namespace, result_id)
);
CREATE TABLE IF NOT EXISTS devfin_sightings (
  namespace TEXT NOT NULL, activity_key TEXT NOT NULL, dataset_id TEXT NOT NULL, selection_key TEXT NOT NULL,
  revision_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, activity_key, dataset_id)
);
CREATE TABLE IF NOT EXISTS devfin_withdrawals (
  namespace TEXT NOT NULL, activity_key TEXT NOT NULL, coverage_id TEXT NOT NULL, selection_key TEXT NOT NULL,
  last_revision_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, activity_key, coverage_id)
);
CREATE TABLE IF NOT EXISTS devfin_coverage (
  namespace TEXT NOT NULL, coverage_id TEXT NOT NULL, provider TEXT NOT NULL, selection_key TEXT NOT NULL,
  requested_json TEXT NOT NULL, returned_json TEXT NOT NULL, pages_read INTEGER NOT NULL, stop_reason TEXT,
  complete BOOLEAN NOT NULL, failure_code TEXT, dataset_id TEXT, observed_at_ms BIGINT NOT NULL,
  rejected_json TEXT NOT NULL, PRIMARY KEY(namespace, coverage_id)
);
CREATE TABLE IF NOT EXISTS devfin_coverage_observations (
  namespace TEXT NOT NULL, coverage_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, coverage_id, observed_at_ms)
);
CREATE TABLE IF NOT EXISTS devfin_crs_cells (
  namespace TEXT NOT NULL, cell_id TEXT NOT NULL, dataflow TEXT NOT NULL, dimensions_json TEXT NOT NULL,
  donor TEXT, recipient TEXT, recipient_kind TEXT NOT NULL, sector TEXT, flow_type TEXT, channel TEXT, measure TEXT,
  price_basis TEXT NOT NULL, price_base_code TEXT, unit TEXT, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, cell_id)
);
CREATE TABLE IF NOT EXISTS devfin_crs_vintages (
  namespace TEXT NOT NULL, vintage_id TEXT NOT NULL, cell_id TEXT NOT NULL, vintage_no INTEGER NOT NULL,
  supersedes TEXT, arrival TEXT NOT NULL, published_on TEXT, release_label TEXT, vintage_basis TEXT NOT NULL,
  order_stamp TEXT NOT NULL, dataflow_version TEXT, base_year TEXT, base_year_state TEXT NOT NULL, unit_mult TEXT,
  observations_json TEXT NOT NULL, content_hash TEXT NOT NULL, dataset_id TEXT NOT NULL, file_sha256 TEXT NOT NULL,
  source_id TEXT, url TEXT, evidence_origin TEXT NOT NULL, observed_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, vintage_id)
);
CREATE TABLE IF NOT EXISTS devfin_wb_projects (
  namespace TEXT NOT NULL, project_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, project_id)
);
CREATE TABLE IF NOT EXISTS devfin_wb_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, project_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
  supersedes TEXT, body_json TEXT NOT NULL, content_hash TEXT NOT NULL, dataset_id TEXT NOT NULL,
  capture_sha256 TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS devfin_generation (
  namespace TEXT NOT NULL PRIMARY KEY, generation BIGINT NOT NULL
);
"""


class DevelopmentFinanceError(ValueError):
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


def table_exists(conn: Any, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
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
        raise DevelopmentFinanceError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes: Iterable[str], required: str) -> None:
    """A scope needed only for an optional part of an answer, checked when that part is requested."""
    scopes = set(scopes)
    if "operator" not in scopes and required not in scopes:
        raise DevelopmentFinanceError(
            "unauthorized", f"{required} is required for this part of the answer"
        )


def iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat(
        timespec="seconds"
    )


def as_of_ms(value: Any) -> int | None:
    """An as-of date (``YYYY-MM-DD``, inclusive through the end of that UTC day) or an ISO instant, in ms."""
    if value in (None, ""):
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            day = date.fromisoformat(raw)
            return int(
                datetime(
                    day.year,
                    day.month,
                    day.day,
                    23,
                    59,
                    59,
                    999000,
                    tzinfo=timezone.utc,
                ).timestamp()
                * 1000
            )
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DevelopmentFinanceError(
            "invalid_date", f"{value!r} is not an ISO date"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


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


def name_key(value: Any) -> str:
    return " ".join(re.sub(r"[^0-9a-zà-ɏ]+", " ", str(value or "").casefold()).split())


def publisher_id(provider: str, ref: Any, name: Any = None) -> str:
    """One publisher per provider and reported reference; a publisher without a reference is keyed by its name."""
    if ref:
        return f"{provider}:ref:{identifier_key(ref)}"
    return f"{provider}:name:{name_key(name).replace(' ', '-') or 'unnamed'}"


def activity_key(publisher: str, iati_identifier: str) -> str:
    return f"{publisher}|{iati_identifier}"


def transaction_keys(transactions: Sequence[Mapping[str, Any]]) -> list[str]:
    """A transaction's identity within its activity: its ``ref`` when reported, else type, date and parties.

    Two transactions that share every one of those fields are told apart by their order of occurrence.
    """
    counts: dict[str, int] = {}
    keys = []
    for tx in transactions:
        if tx.get("ref"):
            base = "ref:" + identifier_key(tx["ref"])
        else:
            base = "pos:" + "|".join(
                [
                    str(tx.get("type") or ""),
                    str(tx.get("date") or ""),
                    identifier_key((tx.get("provider_org") or {}).get("ref")),
                    identifier_key((tx.get("receiver_org") or {}).get("ref")),
                ]
            )
        number = counts.get(base, 0)
        counts[base] = number + 1
        keys.append(base if number == 0 else f"{base}#{number}")
    return keys


def transaction_digest(tx: Mapping[str, Any]) -> str:
    return digest(
        {
            k: tx.get(k)
            for k in (
                "type",
                "date",
                "value_text",
                "currency",
                "value_date",
                "provider_org",
                "receiver_org",
                "flow_type",
                "aid_type",
            )
        }
    )


def activity_content(activity: Mapping[str, Any]) -> dict[str, Any]:
    """The reported content of an activity (without where it was found in the capture)."""
    return {k: v for k, v in activity.items() if k != "locator"}


def order_key(row: Mapping[str, Any]) -> tuple:
    return (
        row["order_stamp"],
        int(row["observed_at_ms"]),
        int(row.get("revision_no") or row.get("vintage_no") or 0),
    )


class DevelopmentFinanceStore:
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
        return table_exists(self.conn, "devfin_activity_revisions")

    def require_ready(self) -> None:
        if not self.ready():
            raise DevelopmentFinanceError(
                "not_ready",
                "no development-finance records are stored yet; run an IATI, World Bank or OECD CRS "
                "acquisition first",
            )

    # ------------------------------------------------------------------ generation

    def _bump(self, namespace: str) -> None:
        self.conn.execute(
            "INSERT INTO devfin_generation VALUES (?, 1) ON CONFLICT (namespace) "
            "DO UPDATE SET generation=devfin_generation.generation+1",
            [namespace],
        )

    def generation(self, namespace: str) -> int:
        if not table_exists(self.conn, "devfin_generation"):
            return 0
        row = self.conn.execute(
            "SELECT generation FROM devfin_generation WHERE namespace=?", [namespace]
        ).fetchone()
        return int(row[0]) if row else 0

    def latest_observation_ms(self, namespace: str) -> int | None:
        """The newest observation of any source in the namespace (datasets, coverage and CRS vintages)."""
        if not self.ready():
            return None
        row = self.conn.execute(
            "SELECT max(t) FROM (SELECT max(observed_at_ms) t FROM devfin_datasets WHERE namespace=? UNION ALL "
            "SELECT max(observed_at_ms) FROM devfin_coverage_observations WHERE namespace=? UNION ALL "
            "SELECT max(observed_at_ms) FROM devfin_crs_vintages WHERE namespace=?)",
            [namespace, namespace, namespace],
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------------ publishers and datasets

    def _publisher(
        self, namespace: str, provider: str, ref: Any, name: Any, dataset_id: str | None
    ) -> str:
        pid = publisher_id(provider, ref, name)
        created = self.conn.execute(
            "INSERT INTO devfin_publishers VALUES (?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING publisher_id",
            [namespace, pid, provider, ref, name, dataset_id, self.now()],
        ).fetchone()
        if created:
            self._bump(namespace)
        return pid

    def record_dataset(
        self,
        namespace: str,
        *,
        provider: str,
        query: Mapping[str, Any],
        source_url: str | None,
        capture_digests: Sequence[str],
        evidence_origin: str,
        execution: str | None,
        observed_at_ms: int,
        run_id: str | None = None,
    ) -> str:
        """One acquisition; re-acquiring identical bytes for the same query is the same dataset version."""
        if evidence_origin not in {"fixture", "live", "operator"}:
            raise DevelopmentFinanceError(
                "invalid_dataset", "evidence origin is fixture, live or operator"
            )
        dataset_id = (
            "devfin-ds:"
            + digest([namespace, provider, dict(query), sorted(capture_digests)])[:24]
        )
        created = self.conn.execute(
            "INSERT INTO devfin_datasets VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING dataset_id",
            [
                namespace,
                dataset_id,
                provider,
                canonical(dict(query)),
                source_url,
                canonical(sorted(capture_digests)),
                evidence_origin,
                execution,
                run_id,
                int(observed_at_ms),
            ],
        ).fetchone()
        if created:
            self._bump(namespace)
        return dataset_id

    def dataset(self, namespace: str, dataset_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT provider, query_json, source_url, capture_digests_json, evidence_origin, execution, run_id, "
            "observed_at_ms FROM devfin_datasets WHERE namespace=? AND dataset_id=?",
            [namespace, dataset_id],
        ).fetchone()
        if row is None:
            raise DevelopmentFinanceError(
                "not_found", "dataset is not visible in this namespace"
            )
        return {
            "dataset_id": dataset_id,
            "provider": row[0],
            "query": json.loads(row[1]),
            "source_url": row[2],
            "capture_digests": json.loads(row[3]),
            "evidence_origin": row[4],
            "execution": row[5],
            "run_id": row[6],
            "observed_at_ms": int(row[7]),
        }

    # ------------------------------------------------------------------ IATI activities

    def _current_revision_row(
        self, namespace: str, key: str, as_of: int | None = None
    ) -> dict[str, Any] | None:
        rows = self._revision_rows(namespace, key, as_of=as_of)
        return max(rows, key=order_key) if rows else None

    def _revision_rows(
        self, namespace: str, key: str, *, as_of: int | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision_id, revision_no, order_stamp, observed_at_ms, content_hash, last_updated_at "
            "FROM devfin_activity_revisions WHERE namespace=? AND activity_key=? AND (? IS NULL OR observed_at_ms<=?)",
            [namespace, key, as_of, as_of],
        ).fetchall()
        return [
            dict(
                zip(
                    (
                        "revision_id",
                        "revision_no",
                        "order_stamp",
                        "observed_at_ms",
                        "content_hash",
                        "last_updated_at",
                    ),
                    r,
                )
            )
            for r in rows
        ]

    def apply_activity(
        self,
        namespace: str,
        activity: Mapping[str, Any],
        *,
        dataset_id: str,
        capture_sha256: str,
        source_url: str | None,
        selection_key: str,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        """Store one reported activity; unchanged content adds nothing and a changed one is a new revision."""
        reporting = dict(activity.get("reporting_org") or {})
        publisher = self._publisher(
            namespace, "iati", reporting.get("ref"), reporting.get("name"), dataset_id
        )
        identifier = str(activity["iati_identifier"])
        key = activity_key(publisher, identifier)
        content = activity_content(activity)
        content_hash = digest(content)
        stamp = activity.get("last_updated_at")
        if self.conn.execute(
            "INSERT INTO devfin_activities VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING "
            "RETURNING activity_key",
            [
                namespace,
                key,
                publisher,
                identifier,
                identifier_key(identifier),
                self.now(),
            ],
        ).fetchone():
            self._bump(namespace)
        rows = self._revision_rows(namespace, key)
        current = max(rows, key=order_key) if rows else None
        order_stamp = stamp or iso_from_ms(observed_at_ms)
        outcome = None
        if (
            current is not None
            and current["content_hash"] == content_hash
            and current["last_updated_at"] == stamp
        ):
            outcome, revision_id = "unchanged", current["revision_id"]
        elif stamp is not None and any(
            r["content_hash"] == content_hash and r["last_updated_at"] == stamp
            for r in rows
        ):
            # The same version under the same publisher stamp is already history: a late replay adds nothing.
            outcome = "known-history"
            revision_id = next(
                r["revision_id"]
                for r in rows
                if r["content_hash"] == content_hash and r["last_updated_at"] == stamp
            )
        if outcome is not None:
            self._sighting(
                namespace, key, dataset_id, selection_key, revision_id, observed_at_ms
            )
            return {
                "activity_key": key,
                "publisher_id": publisher,
                "revision_id": revision_id,
                "outcome": outcome,
            }
        if current is None:
            arrival = "first"
        elif order_stamp > current["order_stamp"]:
            arrival = (
                "reversion"
                if any(r["content_hash"] == content_hash for r in rows)
                else "newer"
            )
        elif order_stamp == current["order_stamp"]:
            arrival = "same-stamp-conflict"
        else:
            arrival = "late-older"
        revision_id = (
            "devfin-rev:"
            + digest(
                [namespace, key, content_hash, stamp, None if stamp else observed_at_ms]
            )[:24]
        )
        number = 1 + max((r["revision_no"] for r in rows), default=0)
        supersedes = (
            current["revision_id"]
            if current is not None and arrival in {"newer", "reversion"}
            else None
        )
        self.conn.execute(
            "INSERT INTO devfin_activity_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                revision_id,
                key,
                publisher,
                identifier,
                number,
                supersedes,
                arrival,
                stamp,
                activity.get("last_updated_text"),
                order_stamp,
                "last-updated-datetime"
                if stamp
                else "observed-at (last-updated-datetime absent)",
                canonical(content),
                content_hash,
                dataset_id,
                capture_sha256,
                source_url,
                canonical(dict(activity.get("locator") or {})),
                int(observed_at_ms),
            ],
        )
        self._children(namespace, revision_id, key, publisher, activity)
        self._sighting(
            namespace, key, dataset_id, selection_key, revision_id, observed_at_ms
        )
        self._bump(namespace)
        return {
            "activity_key": key,
            "publisher_id": publisher,
            "revision_id": revision_id,
            "outcome": arrival,
        }

    def _children(self, namespace, revision_id, key, publisher, activity) -> None:
        for org in activity.get("participating_orgs") or []:
            self.conn.execute(
                "INSERT INTO devfin_participating_orgs VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    f"{revision_id}:org:{org['ordinal']}",
                    revision_id,
                    org["ordinal"],
                    org.get("role"),
                    org.get("ref"),
                    org.get("type"),
                    org.get("name"),
                    org.get("activity_id"),
                ],
            )
        for scope, allocations, ordinal in [
            (
                "activity",
                (activity.get("sectors") or [])
                + (activity.get("recipient_countries") or [])
                + (activity.get("recipient_regions") or []),
                None,
            )
        ] + [
            (
                "transaction",
                (tx.get("sectors") or [])
                + (tx.get("recipient_countries") or [])
                + (tx.get("recipient_regions") or []),
                tx["ordinal"],
            )
            for tx in activity.get("transactions") or []
        ]:
            for number, allocation in enumerate(allocations):
                allocation_id = f"{revision_id}:{scope}:{'' if ordinal is None else ordinal}:alloc:{number}"
                self.conn.execute(
                    "INSERT INTO devfin_allocations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        allocation_id,
                        revision_id,
                        scope,
                        ordinal,
                        allocation["kind"],
                        allocation.get("vocabulary"),
                        allocation.get("code"),
                        allocation.get("percentage_text"),
                        allocation.get("percentage"),
                        allocation.get("narrative"),
                    ],
                )
        transactions = list(activity.get("transactions") or [])
        for tx, tx_key in zip(transactions, transaction_keys(transactions)):
            self.conn.execute(
                "INSERT INTO devfin_transactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    f"{revision_id}:tx:{tx['ordinal']}",
                    revision_id,
                    key,
                    publisher,
                    tx["ordinal"],
                    tx_key,
                    tx.get("ref"),
                    tx.get("type"),
                    tx.get("date"),
                    tx.get("value_text"),
                    tx.get("value"),
                    tx.get("currency"),
                    tx.get("currency_source"),
                    tx.get("value_date"),
                    None
                    if tx.get("provider_org") is None
                    else canonical(tx["provider_org"]),
                    None
                    if tx.get("receiver_org") is None
                    else canonical(tx["receiver_org"]),
                    canonical(tx),
                    transaction_digest(tx),
                ],
            )
        for result in activity.get("results") or []:
            self.conn.execute(
                "INSERT INTO devfin_results VALUES (?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    f"{revision_id}:result:{result['ordinal']}",
                    revision_id,
                    result["ordinal"],
                    result.get("type"),
                    result.get("title"),
                    canonical(result),
                    digest(result),
                ],
            )

    def _sighting(
        self, namespace, key, dataset_id, selection_key, revision_id, observed_at_ms
    ) -> None:
        self.conn.execute(
            "INSERT INTO devfin_sightings VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [
                namespace,
                key,
                dataset_id,
                selection_key,
                revision_id,
                int(observed_at_ms),
            ],
        )

    def apply_iati(
        self,
        namespace: str,
        collected: Mapping[str, Any],
        *,
        dataset: Mapping[str, Any],
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Apply one bounded IATI selection (every page) with its publisher coverage, in one transaction."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        coverage = dict(collected["coverage"])
        selection_key = "iati:" + digest(coverage["requested"])[:16]
        observed = int(dataset["observed_at_ms"])
        self.conn.execute("BEGIN")
        try:
            dataset_id = self.record_dataset(
                namespace,
                provider="iati-datastore",
                query=coverage["requested"],
                source_url=dataset.get("source_url"),
                capture_digests=dataset["capture_digests"],
                evidence_origin=dataset["evidence_origin"],
                execution=dataset.get("execution"),
                observed_at_ms=observed,
                run_id=dataset.get("run_id"),
            )
            outcomes, returned = [], {}
            for page in collected["pages"]:
                for activity in page["activities"]:
                    result = self.apply_activity(
                        namespace,
                        activity,
                        dataset_id=dataset_id,
                        capture_sha256=page["sha256"],
                        source_url=dataset.get("source_url"),
                        selection_key=selection_key,
                        observed_at_ms=observed,
                    )
                    outcomes.append(result)
                    returned[result["publisher_id"]] = (
                        returned.get(result["publisher_id"], 0) + 1
                    )
            coverage_id = self._coverage(
                namespace,
                "iati-datastore",
                selection_key,
                coverage,
                returned,
                dataset_id=dataset_id,
                failure_code=None,
                observed_at_ms=observed,
            )
            withdrawn = []
            if coverage["complete"]:
                seen = {o["activity_key"] for o in outcomes}
                withdrawn = self._withdrawals(
                    namespace, selection_key, seen, coverage_id, observed
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        counts: dict[str, int] = {}
        for outcome in outcomes:
            counts[outcome["outcome"]] = counts.get(outcome["outcome"], 0) + 1
        return {
            "dataset_id": dataset_id,
            "coverage_id": coverage_id,
            "selection_key": selection_key,
            "activities": outcomes,
            "counts": counts,
            "withdrawn": withdrawn,
            "coverage": coverage,
        }

    def _coverage(
        self,
        namespace,
        provider,
        selection_key,
        coverage,
        returned,
        *,
        dataset_id,
        failure_code,
        observed_at_ms,
    ) -> str:
        body = [
            namespace,
            provider,
            selection_key,
            dataset_id,
            failure_code,
            bool(coverage.get("complete")),
            coverage.get("pages_read"),
            coverage.get("stop_reason"),
            sorted(returned.items()),
            coverage.get("rejected") or [],
        ]
        if dataset_id is None:
            body.append(observed_at_ms)  # every failed refresh is its own observation
        coverage_id = "devfin-cov:" + digest(body)[:24]
        created = self.conn.execute(
            "INSERT INTO devfin_coverage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING RETURNING coverage_id",
            [
                namespace,
                coverage_id,
                provider,
                selection_key,
                canonical(coverage.get("requested") or {}),
                canonical(returned),
                int(coverage.get("pages_read") or 0),
                coverage.get("stop_reason"),
                bool(coverage.get("complete")),
                failure_code,
                dataset_id,
                int(observed_at_ms),
                canonical(coverage.get("rejected") or []),
            ],
        ).fetchone()
        if created:
            self._bump(namespace)
        self.conn.execute(
            "INSERT INTO devfin_coverage_observations VALUES (?,?,?) ON CONFLICT DO NOTHING",
            [namespace, coverage_id, int(observed_at_ms)],
        )
        return coverage_id

    def _withdrawals(
        self, namespace, selection_key, seen, coverage_id, observed_at_ms
    ) -> list[str]:
        """Activities an earlier run of this complete selection returned and this one did not: withdrawn by the
        publisher from publication (never "ended"). Recorded once until the activity is seen again."""
        withdrawn = []
        later = self.conn.execute(
            "SELECT 1 FROM devfin_coverage c JOIN devfin_coverage_observations o ON o.namespace=c.namespace AND "
            "o.coverage_id=c.coverage_id WHERE c.namespace=? AND c.selection_key=? AND o.observed_at_ms>? LIMIT 1",
            [namespace, selection_key, observed_at_ms],
        ).fetchone()
        if later:
            return []  # a late-arriving older run never withdraws what a newer run of the selection saw
        rows = self.conn.execute(
            "SELECT DISTINCT activity_key FROM devfin_sightings WHERE namespace=? AND selection_key=? "
            "AND observed_at_ms<? ORDER BY activity_key",
            [namespace, selection_key, observed_at_ms],
        ).fetchall()
        for (key,) in rows:
            if (
                key in seen
                or self.publication_state(namespace, key)["state"]
                == "withdrawn-by-publisher"
            ):
                continue
            current = self._current_revision_row(namespace, key)
            self.conn.execute(
                "INSERT INTO devfin_withdrawals VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                [
                    namespace,
                    key,
                    coverage_id,
                    selection_key,
                    current["revision_id"],
                    observed_at_ms,
                ],
            )
            withdrawn.append(key)
            self._bump(namespace)
        return withdrawn

    def record_failure(
        self,
        namespace: str,
        provider: str,
        requested: Mapping[str, Any],
        *,
        failure_code: str,
        observed_at_ms: int,
        scopes: Iterable[str],
        pages_read: int = 0,
    ) -> dict[str, Any]:
        """A failed or partial refresh: stale coverage, never a closed, ended or withdrawn activity."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        prefix = {"iati-datastore": "iati", "world-bank-projects": "wb"}.get(
            provider, provider
        )
        selection_key = f"{prefix}:" + digest(dict(requested))[:16]
        coverage = {
            "requested": dict(requested),
            "complete": False,
            "pages_read": pages_read,
            "stop_reason": f"failed: {failure_code}",
        }
        coverage_id = self._coverage(
            namespace,
            provider,
            selection_key,
            coverage,
            {},
            dataset_id=None,
            failure_code=failure_code,
            observed_at_ms=observed_at_ms,
        )
        return {
            "provider": provider,
            "coverage_id": coverage_id,
            "state": "stale",
            "failure_code": failure_code,
            "note": "the last revisions stay current; nothing is marked ended",
        }

    # ------------------------------------------------------------------ World Bank projects

    def apply_world_bank(
        self,
        namespace: str,
        collected: Mapping[str, Any],
        *,
        dataset: Mapping[str, Any],
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        coverage = dict(collected["coverage"])
        selection_key = "wb:" + digest(coverage["requested"])[:16]
        observed = int(dataset["observed_at_ms"])
        self.conn.execute("BEGIN")
        try:
            dataset_id = self.record_dataset(
                namespace,
                provider="world-bank-projects",
                query=coverage["requested"],
                source_url=dataset.get("source_url"),
                capture_digests=dataset["capture_digests"],
                evidence_origin=dataset["evidence_origin"],
                execution=dataset.get("execution"),
                observed_at_ms=observed,
                run_id=dataset.get("run_id"),
            )
            publisher = self._publisher(
                namespace, "world-bank-projects", "world-bank", "World Bank", dataset_id
            )
            outcomes = []
            for page, capture in zip(collected["pages"], dataset["capture_digests"]):
                for project in page["projects"]:
                    outcomes.append(
                        self._apply_project(
                            namespace, project, dataset_id, capture, observed
                        )
                    )
            coverage_id = self._coverage(
                namespace,
                "world-bank-projects",
                selection_key,
                coverage,
                {publisher: len(outcomes)},
                dataset_id=dataset_id,
                failure_code=None,
                observed_at_ms=observed,
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "dataset_id": dataset_id,
            "coverage_id": coverage_id,
            "projects": outcomes,
            "coverage": coverage,
        }

    def _apply_project(
        self, namespace, project, dataset_id, capture, observed
    ) -> dict[str, Any]:
        pid = project["project_id"]
        content_hash = digest(project)
        if self.conn.execute(
            "INSERT INTO devfin_wb_projects VALUES (?,?,?) ON CONFLICT DO NOTHING "
            "RETURNING project_id",
            [namespace, pid, self.now()],
        ).fetchone():
            self._bump(namespace)
        rows = self.conn.execute(
            "SELECT revision_id, revision_no, content_hash, observed_at_ms FROM devfin_wb_revisions "
            "WHERE namespace=? AND project_id=?",
            [namespace, pid],
        ).fetchall()
        current = max(rows, key=lambda r: (r[3], r[1])) if rows else None
        if current is not None and current[2] == content_hash:
            return {
                "project_id": pid,
                "revision_id": current[0],
                "outcome": "unchanged",
            }
        if current is not None and observed < current[3]:
            if any(r[2] == content_hash for r in rows):
                return {
                    "project_id": pid,
                    "revision_id": next(r[0] for r in rows if r[2] == content_hash),
                    "outcome": "known-history",
                }
        revision_id = (
            "devfin-wbrev:" + digest([namespace, pid, content_hash, observed])[:24]
        )
        number = 1 + max((r[1] for r in rows), default=0)
        self.conn.execute(
            "INSERT INTO devfin_wb_revisions VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
            [
                namespace,
                revision_id,
                pid,
                number,
                current[0] if current else None,
                canonical(project),
                content_hash,
                dataset_id,
                capture,
                observed,
            ],
        )
        self._bump(namespace)
        return {
            "project_id": pid,
            "revision_id": revision_id,
            "outcome": "first"
            if current is None
            else "late-older"
            if observed < current[3]
            else "newer",
        }

    def world_bank_projects(
        self, namespace: str, *, as_of: int | None = None
    ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "devfin_wb_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT revision_id, project_id, revision_no, supersedes, body_json, dataset_id, capture_sha256, "
            "observed_at_ms FROM devfin_wb_revisions WHERE namespace=? AND (? IS NULL OR observed_at_ms<=?)",
            [namespace, as_of, as_of],
        ).fetchall()
        current: dict[str, tuple] = {}
        for row in rows:
            if row[1] not in current or (row[7], row[2]) > (
                current[row[1]][7],
                current[row[1]][2],
            ):
                current[row[1]] = row
        return [
            {
                "record_type": "world_bank_project",
                "project_id": r[1],
                "revision_id": r[0],
                "revision_no": r[2],
                "supersedes": r[3],
                "project": json.loads(r[4]),
                "dataset_id": r[5],
                "capture_sha256": r[6],
                "observed_at_ms": int(r[7]),
                "publisher_id": publisher_id("world-bank-projects", "world-bank"),
            }
            for _, r in sorted(current.items())
        ]

    # ------------------------------------------------------------------ OECD CRS

    def apply_crs(
        self,
        namespace: str,
        header: Mapping[str, Any],
        cells: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        """Store CRS cells as vintaged statistics with the OECD as publisher; never as activities."""
        observed = int(observed_at_ms)
        dataset_id = self.record_dataset(
            namespace,
            provider="oecd-crs",
            query=dict(header.get("document") or {}),
            source_url=header.get("url"),
            capture_digests=[header["file_sha256"]],
            evidence_origin=header["evidence_origin"],
            execution="runtime",
            observed_at_ms=observed,
            run_id=run_id,
        )
        self._publisher(
            namespace,
            "oecd-crs",
            "OECD",
            "OECD Development Assistance Committee (CRS)",
            dataset_id,
        )
        outcomes = []
        for cell in cells:
            outcomes.append(
                self._apply_cell(
                    namespace,
                    header,
                    cell,
                    dataset_id=dataset_id,
                    source_id=source_id,
                    observed=observed,
                )
            )
        return {"dataset_id": dataset_id, "cells": outcomes}

    def _apply_cell(
        self, namespace, header, cell, *, dataset_id, source_id, observed
    ) -> dict[str, Any]:
        dataflow = str(cell.get("dataflow") or cell.get("dataflow_id") or "")
        flow_base = re.sub(r"\(.*\)$", "", dataflow)
        version = re.search(r"\(([^)]*)\)$", dataflow)
        dims = dict(cell["dimensions"])
        cell_id = "devfin-crs:" + digest([namespace, flow_base, dims])[:24]
        recipient = dict(cell.get("recipient") or {})
        if self.conn.execute(
            "INSERT INTO devfin_crs_cells VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING "
            "RETURNING cell_id",
            [
                namespace,
                cell_id,
                flow_base,
                canonical(dims),
                cell.get("donor"),
                recipient.get("code"),
                recipient.get("kind") or "unknown",
                cell.get("sector"),
                cell.get("flow_type"),
                cell.get("channel"),
                cell.get("measure"),
                cell.get("price_basis") or "unknown",
                cell.get("price_base_code"),
                cell.get("unit"),
                self.now(),
            ],
        ).fetchone():
            self._bump(namespace)
        content = {
            "observations": [
                {k: o.get(k) for k in ("period", "value_text", "value", "attributes")}
                for o in cell.get("observations") or []
            ],
            "unit": cell.get("unit"),
            "unit_mult": cell.get("unit_mult"),
            "base_year": cell.get("base_year"),
            "price_basis": cell.get("price_basis"),
        }
        content_hash = digest(content)
        published_on = header.get("published_on")
        label = header.get("release_label")
        rows = self.conn.execute(
            "SELECT vintage_id, vintage_no, order_stamp, observed_at_ms, content_hash, published_on, release_label "
            "FROM devfin_crs_vintages WHERE namespace=? AND cell_id=?",
            [namespace, cell_id],
        ).fetchall()
        rows = [
            dict(
                zip(
                    (
                        "vintage_id",
                        "vintage_no",
                        "order_stamp",
                        "observed_at_ms",
                        "content_hash",
                        "published_on",
                        "release_label",
                    ),
                    r,
                )
            )
            for r in rows
        ]
        current = max(rows, key=order_key) if rows else None
        if (
            current is not None
            and current["content_hash"] == content_hash
            and (
                published_on is None
                or (current["published_on"], current["release_label"])
                == (published_on, label)
            )
        ):
            return {
                "cell_id": cell_id,
                "vintage_id": current["vintage_id"],
                "outcome": "unchanged",
            }
        if published_on is not None:
            known = next(
                (
                    r
                    for r in rows
                    if r["content_hash"] == content_hash
                    and r["published_on"] == published_on
                    and r["release_label"] == label
                ),
                None,
            )
            if known is not None:
                return {
                    "cell_id": cell_id,
                    "vintage_id": known["vintage_id"],
                    "outcome": "known-history",
                }
        order_stamp = published_on or iso_from_ms(observed)[:10]
        if current is None:
            arrival = "first"
        elif order_stamp > current["order_stamp"]:
            arrival = "newer"
        elif order_stamp == current["order_stamp"]:
            arrival = "same-stamp-conflict"
        else:
            arrival = "late-older"
        vintage_id = (
            "devfin-crsv:"
            + digest(
                [
                    namespace,
                    cell_id,
                    content_hash,
                    published_on,
                    label,
                    None if published_on else observed,
                ]
            )[:24]
        )
        number = 1 + max((r["vintage_no"] for r in rows), default=0)
        self.conn.execute(
            "INSERT INTO devfin_crs_vintages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT DO NOTHING",
            [
                namespace,
                vintage_id,
                cell_id,
                number,
                current["vintage_id"]
                if current is not None and arrival == "newer"
                else None,
                arrival,
                published_on,
                label,
                header.get("vintage_basis") or "retrieval_time",
                order_stamp,
                version.group(1) if version else None,
                cell.get("base_year"),
                cell.get("base_year_state") or "not-stated",
                cell.get("unit_mult"),
                canonical(cell.get("observations") or []),
                content_hash,
                dataset_id,
                header["file_sha256"],
                source_id,
                header.get("url"),
                header["evidence_origin"],
                observed,
            ],
        )
        self._bump(namespace)
        return {"cell_id": cell_id, "vintage_id": vintage_id, "outcome": arrival}

    def crs_cells(
        self,
        namespace: str,
        *,
        donor: str | None = None,
        recipient: str | None = None,
        sector: str | None = None,
        flow_type: str | None = None,
        price_basis: str | None = None,
    ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "devfin_crs_cells"):
            return []
        rows = self.conn.execute(
            "SELECT cell_id, dataflow, dimensions_json, donor, recipient, recipient_kind, sector, flow_type, channel, "
            "measure, price_basis, price_base_code, unit FROM devfin_crs_cells WHERE namespace=? "
            "AND (? IS NULL OR upper(donor)=upper(?)) AND (? IS NULL OR upper(recipient)=upper(?)) "
            "AND (? IS NULL OR sector=?) AND (? IS NULL OR flow_type=?) AND (? IS NULL OR price_basis=?) "
            "ORDER BY cell_id",
            [
                namespace,
                donor,
                donor,
                recipient,
                recipient,
                sector,
                sector,
                flow_type,
                flow_type,
                price_basis,
                price_basis,
            ],
        ).fetchall()
        return [
            {
                "record_type": "crs_cell",
                "cell_id": r[0],
                "dataflow": r[1],
                "dimensions": json.loads(r[2]),
                "donor": r[3],
                "recipient": {"code": r[4], "kind": r[5]},
                "sector": r[6],
                "flow_type": r[7],
                "channel": r[8],
                "measure": r[9],
                "price_basis": r[10],
                "price_base_code": r[11],
                "unit": r[12],
                "publisher_id": publisher_id("oecd-crs", "OECD"),
            }
            for r in rows
        ]

    def crs_vintages(
        self, namespace: str, cell_id: str, *, as_of: int | None = None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT vintage_id, vintage_no, supersedes, arrival, published_on, release_label, vintage_basis, "
            "order_stamp, dataflow_version, base_year, base_year_state, unit_mult, observations_json, content_hash, "
            "dataset_id, file_sha256, source_id, url, evidence_origin, observed_at_ms FROM devfin_crs_vintages "
            "WHERE namespace=? AND cell_id=? AND (? IS NULL OR observed_at_ms<=?)",
            [namespace, cell_id, as_of, as_of],
        ).fetchall()
        keys = (
            "vintage_id",
            "vintage_no",
            "supersedes",
            "arrival",
            "published_on",
            "release_label",
            "vintage_basis",
            "order_stamp",
            "dataflow_version",
            "base_year",
            "base_year_state",
            "unit_mult",
            "observations",
            "content_hash",
            "dataset_id",
            "file_sha256",
            "source_id",
            "url",
            "evidence_origin",
            "observed_at_ms",
        )
        out = []
        for row in rows:
            item = dict(zip(keys, row))
            item["observations"] = json.loads(item["observations"])
            item["observed_at_ms"] = int(item["observed_at_ms"])
            item["record_type"] = "crs_vintage"
            item["cell_id"] = cell_id
            out.append(item)
        return sorted(out, key=order_key)

    # ------------------------------------------------------------------ reads

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT revision_id, activity_key, publisher_id, iati_identifier, revision_no, supersedes, arrival, "
            "last_updated_at, last_updated_text, order_stamp, stamp_basis, body_json, content_hash, dataset_id, "
            "capture_sha256, source_url, locator_json, observed_at_ms FROM devfin_activity_revisions "
            "WHERE namespace=? AND revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if row is None:
            raise DevelopmentFinanceError(
                "not_found", "activity revision is not visible in this namespace"
            )
        keys = (
            "revision_id",
            "activity_key",
            "publisher_id",
            "iati_identifier",
            "revision_no",
            "supersedes",
            "arrival",
            "last_updated_at",
            "last_updated_text",
            "order_stamp",
            "stamp_basis",
            "activity",
            "content_hash",
            "dataset_id",
            "capture_sha256",
            "source_url",
            "locator",
            "observed_at_ms",
        )
        item = dict(zip(keys, row))
        item["activity"] = json.loads(item["activity"])
        item["locator"] = json.loads(item["locator"])
        item["observed_at_ms"] = int(item["observed_at_ms"])
        item["record_type"] = "activity_revision"
        item["contract"] = CONTRACT
        item["namespace"] = namespace
        item["source"] = {
            "provider": "iati-datastore",
            "dataset_id": item["dataset_id"],
            "capture_sha256": item["capture_sha256"],
            "source_url": item["source_url"],
            "locator": item["locator"],
            "observed_at_ms": item["observed_at_ms"],
        }
        item["transactions"] = self.transactions(namespace, revision_id)
        return item

    def transactions(self, namespace: str, revision_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT transaction_id, ordinal, transaction_key, transaction_ref, transaction_type, transaction_date, "
            "value_text, value, currency, currency_source, value_date, provider_org_json, receiver_org_json, "
            "body_json, content_hash, activity_key, publisher_id FROM devfin_transactions "
            "WHERE namespace=? AND revision_id=? ORDER BY ordinal",
            [namespace, revision_id],
        ).fetchall()
        out = []
        for r in rows:
            body = json.loads(r[13])
            out.append(
                {
                    "record_type": "transaction",
                    "transaction_id": r[0],
                    "revision_id": revision_id,
                    "ordinal": r[1],
                    "transaction_key": r[2],
                    "ref": r[3],
                    "type": r[4],
                    "date": r[5],
                    "value_text": r[6],
                    "value": r[7],
                    "currency": r[8],
                    "currency_source": r[9],
                    "value_date": r[10],
                    "provider_org": _load(r[11], None),
                    "receiver_org": _load(r[12], None),
                    "sectors": body.get("sectors") or [],
                    "recipient_countries": body.get("recipient_countries") or [],
                    "flow_type": body.get("flow_type"),
                    "aid_type": body.get("aid_type"),
                    "content_hash": r[14],
                    "activity_key": r[15],
                    "publisher_id": r[16],
                    "amount_state": "unknown"
                    if r[7] is None or r[8] is None or r[10] is None
                    else "reported",
                }
            )
        return out

    def activity_keys(self, namespace: str) -> list[str]:
        if not self.ready():
            return []
        return [
            r[0]
            for r in self.conn.execute(
                "SELECT activity_key FROM devfin_activities WHERE namespace=? ORDER BY activity_key",
                [namespace],
            ).fetchall()
        ]

    def history(
        self, namespace: str, key: str, *, as_of: int | None = None
    ) -> list[dict[str, Any]]:
        """Every revision of one publisher's activity observed by ``as_of``, in the publisher's stamp order."""
        rows = self._revision_rows(namespace, key, as_of=as_of)
        return [
            self.revision(namespace, r["revision_id"])
            for r in sorted(rows, key=order_key)
        ]

    def current(
        self, namespace: str, *, as_of: int | None = None
    ) -> dict[str, dict[str, Any]]:
        """Each activity's revision in force as of a time - selected per publisher *before* any filtering."""
        if not self.ready():
            return {}
        rows = self.conn.execute(
            "SELECT activity_key, revision_id, revision_no, order_stamp, observed_at_ms FROM devfin_activity_revisions "
            "WHERE namespace=? AND (? IS NULL OR observed_at_ms<=?)",
            [namespace, as_of, as_of],
        ).fetchall()
        best: dict[str, tuple] = {}
        for key, revision_id, number, stamp, observed in rows:
            candidate = (stamp, int(observed), int(number), revision_id)
            if key not in best or candidate > best[key]:
                best[key] = candidate
        return {
            key: self.revision(namespace, value[3])
            for key, value in sorted(best.items())
        }

    def publication_state(
        self, namespace: str, key: str, *, as_of: int | None = None
    ) -> dict[str, Any]:
        """Published, or withdrawn by the publisher (from a complete selection), as of a time; never "ended"."""
        seen = self.conn.execute(
            "SELECT max(observed_at_ms) FROM devfin_sightings WHERE namespace=? AND activity_key=? "
            "AND (? IS NULL OR observed_at_ms<=?)",
            [namespace, key, as_of, as_of],
        ).fetchone()[0]
        withdrawal = self.conn.execute(
            "SELECT coverage_id, observed_at_ms, last_revision_id FROM devfin_withdrawals WHERE namespace=? AND "
            "activity_key=? AND (? IS NULL OR observed_at_ms<=?) ORDER BY observed_at_ms DESC LIMIT 1",
            [namespace, key, as_of, as_of],
        ).fetchone()
        if withdrawal is not None and (seen is None or int(withdrawal[1]) > int(seen)):
            return {
                "state": "withdrawn-by-publisher",
                "coverage_id": withdrawal[0],
                "observed_at_ms": int(withdrawal[1]),
                "last_revision_id": withdrawal[2],
                "note": "the publisher's complete selection no longer returns this activity; that does not mean "
                "the activity ended",
            }
        return {
            "state": "published",
            "last_seen_at_ms": None if seen is None else int(seen),
        }

    def publishers(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "devfin_publishers"):
            return []
        rows = self.conn.execute(
            "SELECT publisher_id, provider, publisher_ref, name, first_dataset_id FROM devfin_publishers "
            "WHERE namespace=? ORDER BY publisher_id",
            [namespace],
        ).fetchall()
        return [
            {
                "record_type": "publisher",
                "publisher_id": r[0],
                "provider": r[1],
                "publisher_ref": r[2],
                "name": r[3],
                "first_dataset_id": r[4],
            }
            for r in rows
        ]

    def coverage_rows(
        self, namespace: str, *, as_of: int | None = None
    ) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "devfin_coverage"):
            return []
        rows = self.conn.execute(
            "SELECT c.coverage_id, c.provider, c.selection_key, c.requested_json, c.returned_json, c.pages_read, "
            "c.stop_reason, c.complete, c.failure_code, c.dataset_id, max(o.observed_at_ms), c.rejected_json "
            "FROM devfin_coverage c "
            "JOIN devfin_coverage_observations o ON o.namespace=c.namespace AND o.coverage_id=c.coverage_id "
            "WHERE c.namespace=? AND (? IS NULL OR o.observed_at_ms<=?) GROUP BY ALL",
            [namespace, as_of, as_of],
        ).fetchall()
        out = []
        for r in rows:
            try:
                out.append(
                    {
                        "record_type": "publisher_coverage",
                        "coverage_id": r[0],
                        "provider": r[1],
                        "selection_key": r[2],
                        "requested": json.loads(r[3]),
                        "returned": json.loads(r[4]),
                        "pages_read": int(r[5]),
                        "stop_reason": r[6],
                        "complete": bool(r[7]),
                        "failure_code": r[8],
                        "dataset_id": r[9],
                        "observed_at_ms": int(r[10]),
                        "rejected": json.loads(r[11]),
                    }
                )
            except (TypeError, ValueError):
                continue  # a listing survives one unreadable row
        return sorted(
            out,
            key=lambda c: (c["selection_key"], c["observed_at_ms"], c["coverage_id"]),
        )

    def latest_coverage(
        self, namespace: str, *, as_of: int | None = None
    ) -> dict[str, dict[str, Any]]:
        """The latest coverage observation per selection (a failure after a success makes the selection stale)."""
        latest: dict[str, dict[str, Any]] = {}
        for row in self.coverage_rows(namespace, as_of=as_of):
            prior = latest.get(row["selection_key"])
            # At the same instant a failure outranks a success, so the selection reads as stale.
            rank = (row["observed_at_ms"], row["failure_code"] is not None)
            if prior is None or rank > (
                prior["observed_at_ms"],
                prior["failure_code"] is not None,
            ):
                latest[row["selection_key"]] = row
        return latest


# ------------------------------------------------------------------ source-pack projector


class DevelopmentFinanceProjector:
    """Source-pack runtime projector for ``noesis-development-finance-record-v1`` pages (OECD CRS series)."""

    def __init__(self, conn: Any) -> None:
        self.store = DevelopmentFinanceStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("development_finance") or {}).get("namespace")
            or DEFAULT_NAMESPACE
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
                dict(item.get("development_finance_release") or {}),
                item.get("development_finance_item"),
            )
            if not header or not isinstance(body, Mapping):
                raise DevelopmentFinanceError(
                    "invalid_record", "page record is not a development-finance item"
                )
            groups.setdefault(
                header["file_sha256"] + canonical(header.get("document")), (header, [])
            )[1].append(dict(body))
        namespace = self._namespace(source)
        return [
            self.store.apply_crs(
                namespace,
                header,
                cells,
                run_id=run_id,
                source_id=source["source_id"],
                observed_at_ms=self.store.now(),
            )
            for header, cells in groups.values()
        ]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT max(observed_at_ms), count(*) FROM devfin_crs_vintages WHERE namespace=? AND source_id=?",
            [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {
            "status": status,
            "latest_vintage_observed_at_ms": row[0],
            "vintages": int(row[1]),
        }


# ------------------------------------------------------------------ feature and readiness


def feature_enabled(conn: Any) -> bool:
    """Whether the Funding & Grants bundle's optional ``development-finance`` feature is in the active plan."""
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
            "SELECT authority FROM composition_authority WHERE bundle='funding-grants'"
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
    return "development-finance" in (
        (plan.get("features") or {}).get("funding-grants") or []
    )


STORE_TABLES = (
    "devfin_publishers",
    "devfin_datasets",
    "devfin_activities",
    "devfin_activity_revisions",
    "devfin_participating_orgs",
    "devfin_transactions",
    "devfin_allocations",
    "devfin_results",
    "devfin_sightings",
    "devfin_withdrawals",
    "devfin_coverage",
    "devfin_coverage_observations",
    "devfin_crs_cells",
    "devfin_crs_vintages",
    "devfin_wb_projects",
    "devfin_wb_revisions",
    "devfin_generation",
)


def readiness(conn: Any, namespace: str | None = None) -> dict[str, Any]:
    ready = table_exists(conn, "devfin_activity_revisions")
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        count = 0
        if ready and namespace is not None:
            count = int(
                conn.execute(
                    "SELECT count(*) FROM devfin_datasets WHERE namespace=? AND provider=?",
                    [namespace, provider],
                ).fetchone()[0]
            )
        providers[provider] = {
            "delivers": contract["delivers"],
            "access_decision": contract["access_decision"],
            "reason": contract["reason"],
            "datasets": count,
            "live": "not-implemented"
            if contract["access_decision"] == "not-implemented"
            else "outstanding",
        }
    return {
        "feature": "funding.development-finance",
        "selected": feature_enabled(conn),
        "stores_ready": ready,
        "stores": {t: table_exists(conn, t) for t in STORE_TABLES},
        "providers": providers,
        "boundary": NEVER_SENTENCE,
    }
