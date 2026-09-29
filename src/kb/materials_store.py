"""Versioned material records, dataset releases and their source-pack projection (MT02-MT07, MT11).

One store owns ``noesis-material-record-v1`` (namespace-scoped, one owner):

* **Releases** (``materials_releases``) are keyed by provider and release
  label, with the source's own release date when it states one (ISO date,
  compared as a date) and the first time Noesis saw the release. A corrected
  release date is recorded as a correction with its history, never a conflict.
* **Entries** are keyed by ``namespace + provider + native id``; each
  release of an entry is versioned separately (``materials_entry_versions``).
* **Value series** are keyed by every distinguishing field - entry, property,
  condition set, method and release - digested from normalised,
  source-independent representations, so a key never depends on arrival order.
  Each series has versions: a re-acquisition identical to the *current*
  version adds nothing, a changed value (including a reversion to an earlier
  value) is a new ``correction`` version, and a late-arriving older release is
  its own series and never a correction of a newer one.
* **Current** values come from the provider's newest release by its stated
  release date, falling back to observation order when a release date is
  missing, then the newest version within that release. As-of reads use only
  releases and versions acquired by the cutoff.

Measured, computed and evaluated values are stored side by side with their
method; nothing here merges, averages or reconciles them.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from src.kb import materials_records as mr
from src.kb import materials_units as mu
from src.kb.materials_records import READ_SCOPE, WRITE_SCOPE, canonical, digest

_DDL = """
CREATE TABLE IF NOT EXISTS materials_releases(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, release_label TEXT NOT NULL, released_on TEXT, release_basis TEXT NOT NULL,
 first_seen_ms BIGINT NOT NULL, run_id TEXT NOT NULL, history_json TEXT NOT NULL, sequence BIGINT,
 PRIMARY KEY(namespace, provider, release_label));
CREATE TABLE IF NOT EXISTS materials_entries(
 entry_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, native_id TEXT NOT NULL,
 reduced_formula TEXT NOT NULL, created_at_ms BIGINT NOT NULL, UNIQUE(namespace, provider, native_id));
CREATE TABLE IF NOT EXISTS materials_entry_versions(
 entry_id TEXT NOT NULL, release_label TEXT NOT NULL, version BIGINT NOT NULL, repr_hash TEXT NOT NULL,
 content_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, run_id TEXT NOT NULL, document_id TEXT, change TEXT NOT NULL,
 source_updated_at TEXT, PRIMARY KEY(entry_id, release_label, version));
CREATE TABLE IF NOT EXISTS materials_values(
 series_key TEXT PRIMARY KEY, namespace TEXT NOT NULL, entry_id TEXT NOT NULL, provider TEXT NOT NULL,
 native_id TEXT NOT NULL, property TEXT NOT NULL, condition_key TEXT NOT NULL, method_key TEXT NOT NULL,
 method_class TEXT NOT NULL, release_label TEXT NOT NULL, identity_key TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS materials_value_versions(
 series_key TEXT NOT NULL, version BIGINT NOT NULL, repr_hash TEXT NOT NULL, value_json TEXT NOT NULL,
 observed_at_ms BIGINT NOT NULL, run_id TEXT NOT NULL, document_id TEXT, change TEXT NOT NULL,
 source_updated_at TEXT, PRIMARY KEY(series_key, version));
CREATE TABLE IF NOT EXISTS materials_provider_state(
 namespace TEXT NOT NULL, provider TEXT NOT NULL, last_success_ms BIGINT, last_failure_ms BIGINT,
 last_failure_code TEXT, last_execution TEXT, last_run_id TEXT, PRIMARY KEY(namespace, provider));
"""
TABLES = (
    "materials_releases",
    "materials_entries",
    "materials_entry_versions",
    "materials_values",
    "materials_value_versions",
    "materials_provider_state",
)


class MaterialsError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def authorize(namespace, scopes, required, *, write=False):
    """Materials scope plus current namespace access (operator bypasses)."""

    scopes = set(scopes or ())
    if "operator" in scopes:
        return
    needed = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if required not in scopes or not needed & scopes:
        raise MaterialsError(
            "unauthorized", f"{required} and namespace access are required"
        )


def require_scope(scopes, required):
    if "operator" not in set(scopes or ()) and required not in set(scopes or ()):
        raise MaterialsError("unauthorized", f"{required} scope is required")


def table_exists(conn, table):
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [table]
        ).fetchone()
    )


def entry_id(namespace, provider, native_id):
    return "mat:" + digest([namespace, provider, str(native_id)])[:24]


def record_key(provider, native_id):
    """The identity-subject key of one source entry (shared reviewable identity, MT08)."""

    return f"materials:entry:{provider}:{native_id}"


def _number_text(value):
    """One source-independent spelling of a decimal ('1.20' and '1.2' are the same value)."""

    text = format(Decimal(str(value)).normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _reference_repr(reference):
    return {
        "level": reference["level"],
        "doi": reference.get("doi"),
        "standard": (reference.get("standard") or "").casefold() or None,
        "text": " ".join(reference["text"].split()).casefold(),
    }


def value_repr(item):
    """Normalised, source-independent representation of one value (what a correction is compared on)."""

    uncertainty = item.get("uncertainty")
    return {
        "value": _number_text(item["value"]),
        "unit": mu.unit_key(item.get("unit")),
        "uncertainty": None
        if uncertainty is None
        else {
            "value": _number_text(uncertainty["value"]),
            "unit": mu.unit_key(uncertainty.get("unit")),
            "kind": uncertainty["kind"].casefold(),
        },
        "status": item["status"],
        "references": sorted(
            (_reference_repr(r) for r in item.get("references") or []), key=canonical
        ),
    }


def entry_repr(record):
    structure = record.get("structure") or {}
    lattice = {
        k: _number_text(v)
        for k, v in (structure.get("lattice") or {}).items()
        if k != "unit"
    }
    return {
        "material": {
            "reduced_formula": record["material"]["reduced_formula"],
            "composition": record["material"]["composition"],
            **{
                k: record["material"][k]
                for k in ("name", "cas", "inchi")
                if k in record["material"]
            },
        },
        "structure": None
        if not structure
        else {
            "space_group": (structure.get("space_group") or {}).get("number"),
            "lattice": lattice,
            "method_class": structure["method_class"],
            "measurement": structure.get("measurement"),
        },
        "status": record["status"],
        "identifiers": record.get("identifiers") or {},
        "references": sorted(
            (_reference_repr(r) for r in record.get("references") or []), key=canonical
        ),
    }


def identity_key(namespace, provider, native_id, prop, condition, method):
    """A value's identity across releases (the series key without the release)."""

    return (
        "matv:"
        + digest([namespace, provider, str(native_id), prop, condition, method])[:28]
    )


def series_key(identity, release_label):
    return "mats:" + digest([identity, release_label])[:28]


def _load(value, default):
    return default if value in (None, "") else json.loads(value)


def instant(value):
    """An ISO date or instant as an aware datetime (dates are UTC midnight) - compared as time, never as text."""

    text = str(value).replace("Z", "+00:00")
    parsed = datetime.fromisoformat(
        text + ("T00:00:00+00:00" if len(text) == 10 else "")
    )
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def current_row(rows):
    """The current version among (version, repr, source_updated_at, change, ...) rows.

    When every version carries the source's own update time the newest by that
    time wins; otherwise the newest version that is not a late-arriving older
    capture.
    """

    if not rows:
        return None
    if all(r[2] is not None for r in rows):
        return max(rows, key=lambda r: (instant(r[2]), int(r[0])))
    return [r for r in rows if r[3] != "late-older"][-1]


class MaterialsStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def ready(self):
        """True once a materials source has run (its tables exist and hold a release)."""

        return all(table_exists(self.conn, t) for t in TABLES) and bool(
            self.conn.execute("SELECT 1 FROM materials_releases LIMIT 1").fetchone()
        )

    def require_ready(self):
        if not self.ready():
            raise MaterialsError(
                "not_ready", "no materials source has run yet; acquire a source first"
            )

    # ----------------------------------------------------------------- writes

    def apply(
        self,
        namespace,
        records,
        *,
        run_id,
        principal_id,
        scopes,
        execution="injected",
        observed_at_ms=None,
        documents=None,
    ):
        """Apply validated entries; unchanged content is a no-op, changes are new versions (never overwrites)."""

        del principal_id
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        observed = int(observed_at_ms or self.now())
        counts = {
            "entries": 0,
            "entry_versions": 0,
            "series": 0,
            "value_versions": 0,
            "corrections": 0,
            "unchanged": 0,
            "releases": 0,
            "release_corrections": 0,
        }
        documents = dict(documents or {})
        providers = set()
        for raw in records:
            record = mr.validate(dict(raw))
            providers.add(record["provider"])
            self._apply_one(
                namespace,
                record,
                run_id=run_id,
                observed=observed,
                counts=counts,
                document_id=documents.get(record["native_id"]),
            )
        self._provider_state(
            namespace, providers, success=observed, execution=execution, run_id=run_id
        )
        return counts

    def _release(self, namespace, record, *, run_id, observed, counts):
        release = record["release"]
        row = self.conn.execute(
            "SELECT released_on, release_basis, history_json FROM materials_releases WHERE namespace=? AND provider=? "
            "AND release_label=?",
            [namespace, record["provider"], release["label"]],
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO materials_releases VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    record["provider"],
                    release["label"],
                    release.get("released_on"),
                    release["basis"],
                    observed,
                    run_id,
                    "[]",
                    release.get("sequence"),
                ],
            )
            counts["releases"] += 1
            return
        stated = release.get("released_on")
        if stated is not None and (stated != row[0] or release["basis"] != row[1]):
            # Corrected release metadata is a correction with history, never a blocking conflict.
            history = _load(row[2], []) + [
                {
                    "released_on": row[0],
                    "release_basis": row[1],
                    "corrected_at_ms": observed,
                    "run_id": run_id,
                }
            ]
            history = [{k: v for k, v in h.items() if v is not None} for h in history]
            self.conn.execute(
                "UPDATE materials_releases SET released_on=?, release_basis=?, history_json=? "
                "WHERE namespace=? AND provider=? AND release_label=?",
                [
                    stated,
                    release["basis"],
                    canonical(history),
                    namespace,
                    record["provider"],
                    release["label"],
                ],
            )
            counts["release_corrections"] += 1

    def _apply_one(self, namespace, record, *, run_id, observed, counts, document_id):
        provider, native = record["provider"], record["native_id"]
        release_label = record["release"]["label"]
        self._release(
            namespace, record, run_id=run_id, observed=observed, counts=counts
        )
        eid = entry_id(namespace, provider, native)
        if not self.conn.execute(
            "SELECT 1 FROM materials_entries WHERE entry_id=?", [eid]
        ).fetchone():
            self.conn.execute(
                "INSERT INTO materials_entries VALUES (?,?,?,?,?,?)",
                [
                    eid,
                    namespace,
                    provider,
                    native,
                    record["material"]["reduced_formula"],
                    observed,
                ],
            )
            counts["entries"] += 1
        content = {k: v for k, v in record.items() if k != "values"}
        updated = record.get("source_updated_at")
        if self._version(
            "materials_entry_versions",
            ["entry_id", "release_label"],
            [eid, release_label],
            digest(entry_repr(record)),
            canonical(content),
            observed,
            run_id,
            document_id,
            updated,
        ):
            counts["entry_versions"] += 1
        apfu = mr.atoms_per_formula_unit(record["material"])
        for item in record["values"]:
            condition, method = (
                mr.condition_key(item["conditions"]),
                mr.method_key(item["method"]),
            )
            identity = identity_key(
                namespace, provider, native, item["property"], condition, method
            )
            key = series_key(identity, release_label)
            if not self.conn.execute(
                "SELECT 1 FROM materials_values WHERE series_key=?", [key]
            ).fetchone():
                self.conn.execute(
                    "INSERT INTO materials_values VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    [
                        key,
                        namespace,
                        eid,
                        provider,
                        native,
                        item["property"],
                        condition,
                        method,
                        item["method"]["class"],
                        release_label,
                        identity,
                        observed,
                    ],
                )
                counts["series"] += 1
            normalized = mu.normalise(
                item["value"],
                item.get("unit"),
                item["property"],
                atoms_per_formula_unit=apfu,
            )
            uncertainty = None
            if item.get("uncertainty"):
                u = item["uncertainty"]
                uncertainty = mu.normalise(
                    u["value"],
                    u.get("unit") or item.get("unit"),
                    item["property"],
                    atoms_per_formula_unit=apfu,
                    difference=True,
                )
            payload = {
                **item,
                "normalized": normalized,
                **({"normalized_uncertainty": uncertainty} if uncertainty else {}),
            }
            change = self._version(
                "materials_value_versions",
                ["series_key"],
                [key],
                digest(value_repr(item)),
                canonical(payload),
                observed,
                run_id,
                document_id,
                updated,
            )
            if change is None:
                counts["unchanged"] += 1
            else:
                counts["value_versions"] += 1
                counts["corrections"] += change == "correction"

    def _version(
        self,
        table,
        key_columns,
        key_values,
        repr_hash,
        content,
        observed,
        run_id,
        document_id,
        updated,
    ):
        where = " AND ".join(f"{c}=?" for c in key_columns)
        rows = self.conn.execute(
            f"SELECT version, repr_hash, source_updated_at, change FROM {table} WHERE {where} "
            "ORDER BY version",
            key_values,
        ).fetchall()
        current = current_row(rows)
        if current is not None and current[1] == repr_hash:
            return None  # deduplicated against the current version only
        if (
            updated is not None
            and current is not None
            and current[2] is not None
            and instant(updated) < instant(current[2])
        ):
            # A capture the source dated before the current version arrived late: kept as history, never a correction.
            if any(r[1] == repr_hash and r[2] == updated for r in rows):
                return None
            change = "late-older"
        else:
            change = "correction" if current is not None else "initial"
        version = int(rows[-1][0]) + 1 if rows else 1
        placeholders = ",".join("?" * (len(key_values) + 8))
        self.conn.execute(
            f"INSERT INTO {table} VALUES ({placeholders})",
            [
                *key_values,
                version,
                repr_hash,
                content,
                observed,
                run_id,
                document_id,
                change,
                updated,
            ],
        )
        return change

    def _provider_state(
        self,
        namespace,
        providers,
        *,
        success=None,
        failure=None,
        code=None,
        execution=None,
        run_id=None,
    ):
        for provider in providers:
            self.conn.execute(
                "INSERT INTO materials_provider_state VALUES (?,?,?,?,?,?,?) ON CONFLICT (namespace, provider) DO UPDATE SET "
                "last_success_ms=coalesce(excluded.last_success_ms, materials_provider_state.last_success_ms), "
                "last_failure_ms=coalesce(excluded.last_failure_ms, materials_provider_state.last_failure_ms), "
                "last_failure_code=CASE WHEN excluded.last_failure_ms IS NULL THEN materials_provider_state.last_failure_code "
                "ELSE excluded.last_failure_code END, "
                "last_execution=coalesce(excluded.last_execution, materials_provider_state.last_execution), "
                "last_run_id=coalesce(excluded.last_run_id, materials_provider_state.last_run_id)",
                [namespace, provider, success, failure, code, execution, run_id],
            )

    def record_failure(self, namespace, provider, *, code, run_id, scopes):
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        row = self.conn.execute(
            "SELECT last_success_ms FROM materials_provider_state WHERE namespace=? AND provider=?",
            [namespace, provider],
        ).fetchone()
        # A failure is recorded after the success it follows, whichever clock stamped that success.
        now = max(self.now(), int(row[0]) + 1 if row and row[0] is not None else 0)
        self._provider_state(
            namespace, [provider], failure=now, code=code, run_id=run_id
        )
        return {
            "provider": provider,
            "failure_code": code,
            "recorded_at_ms": now,
            "effect": "stored values and releases unchanged; the provider reads as stale",
        }

    # ------------------------------------------------------------------ reads

    def provider_state(self, namespace, provider):
        if not table_exists(self.conn, "materials_provider_state"):
            return {"provider": provider, "stale": True, "reason": "never acquired"}
        row = self.conn.execute(
            "SELECT last_success_ms, last_failure_ms, last_failure_code, last_execution, last_run_id "
            "FROM materials_provider_state WHERE namespace=? AND provider=?",
            [namespace, provider],
        ).fetchone()
        if row is None:
            return {"provider": provider, "stale": True, "reason": "never acquired"}
        stale = row[0] is None or (row[1] is not None and row[1] > row[0])
        state = {
            "provider": provider,
            "last_success_ms": row[0],
            "last_failure_ms": row[1],
            "last_failure_code": row[2],
            "last_execution": row[3],
            "last_run_id": row[4],
            "stale": stale,
        }
        return {k: v for k, v in state.items() if v is not None}

    def releases(self, namespace, provider, *, as_of_ms=None):
        """Releases oldest -> newest, in the source's own order when it gives one.

        By the stated release date when every release states one, else by the
        source's ordinal (e.g. COD revision numbers) when every release has one,
        else by first observation.
        """

        rows = self.conn.execute(
            "SELECT release_label, released_on, release_basis, first_seen_ms, history_json, sequence FROM materials_releases "
            "WHERE namespace=? AND provider=? AND (? IS NULL OR first_seen_ms<=?)",
            [namespace, provider, as_of_ms, as_of_ms],
        ).fetchall()
        items = [
            {
                k: v
                for k, v in {
                    "label": r[0],
                    "released_on": r[1],
                    "basis": r[2],
                    "first_seen_ms": int(r[3]),
                    "corrections": _load(r[4], []) or None,
                    "sequence": None if r[5] is None else int(r[5]),
                }.items()
                if v is not None
            }
            for r in rows
        ]
        if all("released_on" in i for i in items):
            basis = "source release date"
            items.sort(
                key=lambda i: (
                    date.fromisoformat(i["released_on"]),
                    i["first_seen_ms"],
                    i["label"],
                )
            )
        elif all("sequence" in i for i in items):
            basis = "source release ordinal"
            items.sort(key=lambda i: (i["sequence"], i["first_seen_ms"], i["label"]))
        else:
            basis = "observation order (a release date is missing)"
            items.sort(key=lambda i: (i["first_seen_ms"], i["label"]))
        for index, item in enumerate(items):
            item["order"] = index
            item["order_basis"] = basis
        return items

    def entries(
        self,
        namespace,
        *,
        provider=None,
        reduced_formula=None,
        entry_ids=None,
        as_of_ms=None,
    ):
        rows = self.conn.execute(
            "SELECT entry_id, provider, native_id, reduced_formula FROM materials_entries WHERE namespace=? "
            "AND (? IS NULL OR provider=?) AND (? IS NULL OR reduced_formula=?) AND (? IS NULL OR created_at_ms<=?) "
            "ORDER BY provider, native_id",
            [
                namespace,
                provider,
                provider,
                reduced_formula,
                reduced_formula,
                as_of_ms,
                as_of_ms,
            ],
        ).fetchall()
        wanted = None if entry_ids is None else set(entry_ids)
        return [
            {
                "entry_id": r[0],
                "provider": r[1],
                "native_id": r[2],
                "reduced_formula": r[3],
            }
            for r in rows
            if wanted is None or r[0] in wanted
        ]

    def entry(self, namespace, eid, *, as_of_ms=None):
        """The entry's current content (newest release containing it, newest version) and every release it is in."""

        head = self.conn.execute(
            "SELECT provider, native_id, reduced_formula FROM materials_entries WHERE entry_id=? "
            "AND namespace=?",
            [eid, namespace],
        ).fetchone()
        if head is None:
            raise MaterialsError(
                "not_found", "material entry is not visible in this namespace"
            )
        order = {
            r["label"]: r for r in self.releases(namespace, head[0], as_of_ms=as_of_ms)
        }
        rows = self.conn.execute(
            "SELECT version, content_json, source_updated_at, change, observed_at_ms, run_id, document_id, release_label "
            "FROM materials_entry_versions WHERE entry_id=? AND (? IS NULL OR observed_at_ms<=?) ORDER BY version",
            [eid, as_of_ms, as_of_ms],
        ).fetchall()
        per_release: dict[str, list[Any]] = {}
        for row in rows:
            if row[7] in order:
                per_release.setdefault(row[7], []).append(row)
        if not per_release:
            raise MaterialsError(
                "not_found", "no version of this entry was acquired by then"
            )
        label = max(per_release, key=lambda label: order[label]["order"])
        row = current_row(per_release[label])
        return {
            "entry_id": eid,
            "provider": head[0],
            "native_id": head[1],
            "reduced_formula": head[2],
            "record_key": record_key(head[0], head[1]),
            "release": order[label],
            "version": int(row[0]),
            "content": json.loads(row[1]),
            "observed_at_ms": int(row[4]),
            "run_id": row[5],
            **({"document_id": row[6]} if row[6] else {}),
            "in_releases": sorted(per_release, key=lambda label: order[label]["order"]),
        }

    def _versions(self, key, as_of_ms=None):
        """(version, value_json, source_updated_at, change, observed_at_ms, run_id, document_id) rows by the cutoff."""

        return self.conn.execute(
            "SELECT version, value_json, source_updated_at, change, observed_at_ms, run_id, document_id "
            "FROM materials_value_versions WHERE series_key=? AND (? IS NULL OR observed_at_ms<=?) ORDER BY version",
            [key, as_of_ms, as_of_ms],
        ).fetchall()

    def _value_view(self, row_series, version_row, release, namespace):
        payload = json.loads(version_row[1])
        view = {
            "series_key": row_series["series_key"],
            "identity_key": row_series["identity_key"],
            "entry_id": row_series["entry_id"],
            "provider": row_series["provider"],
            "native_id": row_series["native_id"],
            "record_key": record_key(row_series["provider"], row_series["native_id"]),
            "property": row_series["property"],
            "method_class": row_series["method_class"],
            "condition_key": row_series["condition_key"],
            "method_key": row_series["method_key"],
            "release": release,
            "version": int(version_row[0]),
            "change": version_row[3],
            "observed_at_ms": int(version_row[4]),
            "run_id": version_row[5],
            **payload,
            "namespace": namespace,
        }
        if version_row[2]:
            view["source_updated_at"] = version_row[2]
        if version_row[6]:
            view["document_id"] = version_row[6]
        return view

    def current_values(
        self, namespace, *, as_of_ms=None, provider=None, entry_ids=None, prop=None
    ):
        """The current value per (entry, property, conditions, method), selected *before* any filtering.

        Per provider the newest release (by stated release date, else observation
        order) that holds the value identity wins, then its newest version; only
        releases and versions acquired by ``as_of_ms`` count. A value absent from
        a newer release of an entry that release does contain is flagged, never
        treated as withdrawn unless the source says so.
        """

        rows = self.conn.execute(
            "SELECT series_key, entry_id, provider, native_id, property, condition_key, method_key, method_class, "
            "release_label, identity_key FROM materials_values WHERE namespace=? AND (? IS NULL OR provider=?) "
            "AND (? IS NULL OR property=?) ORDER BY series_key",
            [namespace, provider, provider, prop, prop],
        ).fetchall()
        wanted = None if entry_ids is None else set(entry_ids)
        orders: dict[str, dict[str, dict[str, Any]]] = {}
        by_identity: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            if wanted is not None and r[1] not in wanted:
                continue
            series = dict(
                zip(
                    (
                        "series_key",
                        "entry_id",
                        "provider",
                        "native_id",
                        "property",
                        "condition_key",
                        "method_key",
                        "method_class",
                        "release_label",
                        "identity_key",
                    ),
                    r,
                )
            )
            if series["provider"] not in orders:
                orders[series["provider"]] = {
                    x["label"]: x
                    for x in self.releases(
                        namespace, series["provider"], as_of_ms=as_of_ms
                    )
                }
            if series["release_label"] not in orders[series["provider"]]:
                continue
            by_identity.setdefault(series["identity_key"], []).append(series)
        entry_releases: dict[str, set[str]] = {}
        result = []
        for identity, candidates in sorted(by_identity.items()):
            order = orders[candidates[0]["provider"]]
            chosen = None
            for series in sorted(
                candidates,
                key=lambda s: order[s["release_label"]]["order"],
                reverse=True,
            ):
                versions = self._versions(series["series_key"], as_of_ms)
                if versions:
                    chosen = (series, current_row(versions))
                    break
            if chosen is None:
                continue
            series, version = chosen
            view = self._value_view(
                series, version, order[series["release_label"]], namespace
            )
            eid = series["entry_id"]
            if eid not in entry_releases:
                entry_releases[eid] = {
                    r[0]
                    for r in self.conn.execute(
                        "SELECT DISTINCT release_label FROM materials_entry_versions WHERE entry_id=? "
                        "AND (? IS NULL OR observed_at_ms<=?)",
                        [eid, as_of_ms, as_of_ms],
                    ).fetchall()
                }
            newer = [
                label
                for label in entry_releases[eid]
                if label in order
                and order[label]["order"] > order[series["release_label"]]["order"]
            ]
            if newer:
                view["absent_from_newer_releases"] = sorted(
                    newer, key=lambda label: order[label]["order"]
                )
                view["absence_note"] = (
                    "absent from a newer release that contains the entry; not stated as withdrawn"
                )
            result.append(view)
        return result

    def value_history(self, namespace, identity):
        """Every release and version of one value identity, oldest release first."""

        rows = self.conn.execute(
            "SELECT series_key, entry_id, provider, native_id, property, condition_key, method_key, method_class, "
            "release_label, identity_key FROM materials_values WHERE namespace=? AND identity_key=?",
            [namespace, identity],
        ).fetchall()
        if not rows:
            raise MaterialsError("not_found", "value is not visible in this namespace")
        order = {r["label"]: r for r in self.releases(namespace, rows[0][2])}
        history = []
        for r in sorted(rows, key=lambda r: order[r[8]]["order"]):
            series = dict(
                zip(
                    (
                        "series_key",
                        "entry_id",
                        "provider",
                        "native_id",
                        "property",
                        "condition_key",
                        "method_key",
                        "method_class",
                        "release_label",
                        "identity_key",
                    ),
                    r,
                )
            )
            for version in self._versions(series["series_key"]):
                history.append(
                    self._value_view(
                        series, version, order[series["release_label"]], namespace
                    )
                )
        return history

    def snapshot_id(self, namespace, *, as_of_ms=None, providers=None):
        """Changes whenever any source adds or corrects a release, entry or value by the cutoff."""

        wanted = None if providers is None else sorted(set(providers))
        values = self.conn.execute(
            "SELECT v.series_key, vv.version FROM materials_values v JOIN materials_value_versions vv USING(series_key) "
            "WHERE v.namespace=? AND (? IS NULL OR vv.observed_at_ms<=?) ORDER BY 1, 2",
            [namespace, as_of_ms, as_of_ms],
        ).fetchall()
        entries = self.conn.execute(
            "SELECT e.entry_id, ev.release_label, ev.version, e.provider FROM materials_entries e JOIN "
            "materials_entry_versions ev USING(entry_id) WHERE e.namespace=? AND (? IS NULL OR ev.observed_at_ms<=?) "
            "ORDER BY 1, 2, 3",
            [namespace, as_of_ms, as_of_ms],
        ).fetchall()
        releases = self.conn.execute(
            "SELECT provider, release_label, released_on, history_json FROM materials_releases WHERE namespace=? "
            "AND (? IS NULL OR first_seen_ms<=?) ORDER BY 1, 2",
            [namespace, as_of_ms, as_of_ms],
        ).fetchall()
        if wanted is not None:
            entries = [e for e in entries if e[3] in wanted]
            releases = [r for r in releases if r[0] in wanted]
        return (
            "materials-snapshot:"
            + digest(
                [
                    [list(v) for v in values],
                    [list(e) for e in entries],
                    [list(r) for r in releases],
                    wanted,
                ]
            )[:32]
        )


class MaterialsProjector:
    """Source-pack runtime projector for ``noesis-material-record-v1`` pages (every materials connector)."""

    SCOPES_FOR = staticmethod(
        lambda namespace: {
            WRITE_SCOPE,
            READ_SCOPE,
            f"namespace:{namespace}:write",
            f"namespace:{namespace}:read",
        }
    )

    def __init__(self, conn):
        self.store = MaterialsStore(conn)

    @staticmethod
    def _namespace(source):
        return str(dict(source.get("materials") or {}).get("namespace") or "materials")

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
        from src.ingestion.source_packs import _digest

        del page_receipt
        namespace = self._namespace(source)
        payload = [
            dict(item["material_entry"])
            for item in records
            if item.get("material_entry")
        ]
        stored = {d["document_id"] for d in documents}
        # The runtime's document id for a record, so every value cites the document it was read from.
        docs = {}
        for item in records:
            if item.get("material_entry"):
                document_id = (
                    "spdoc:" + _digest([source["source_id"], str(item.get("id"))])[:28]
                )
                if document_id in stored:
                    docs[item["material_entry"]["native_id"]] = document_id
        observed = max(
            (
                int(d["ingested_at"])
                for d in documents
                if d.get("ingested_at") is not None
            ),
            default=None,
        )
        return self.store.apply(
            namespace,
            payload,
            run_id=run_id,
            principal_id=principal_id,
            scopes=self.SCOPES_FOR(namespace),
            execution="source-pack",
            observed_at_ms=observed,
            documents=docs,
        )

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        namespace = self._namespace(source)
        provider = str(dict(source.get("materials") or {}).get("provider"))
        if status != "complete":
            self.store.record_failure(
                namespace,
                provider,
                code="source_run_" + status,
                run_id=run_id,
                scopes=self.SCOPES_FOR(namespace),
            )
        return {
            "status": status,
            "provider": provider,
            "namespace": namespace,
            "provider_state": self.store.provider_state(namespace, provider),
        }
