"""Engineering Safety record owner: records, immutable revisions and their parts (ES02, #2063).

Tables are namespace-scoped and prefixed ``es_``. One record per provider,
record kind and native identifier (``es_records``); every distinct statement
the source published for it is an immutable revision (``es_revisions``) with
its parts: applicability clauses, verbatim statements (findings, probable
causes, required actions, compliance times), stated relations (supersession,
revision, cross-references, upgrades, recommendations, cited recalls),
subjects as published, the occurrence, dated recommendation responses,
quantities with native units and extracted citations.

Revision rules (shared by every provider):

* replaying the current statement adds nothing; the digest is taken over the
  normalised statement, never over provider-specific payload structure;
* a changed statement is a new revision. It becomes current when its rank is
  at least the current one's: report status first (a final report ranks above
  a preliminary one), then the source's own date (publication, issue, report,
  status or file-vintage date), then observation order;
* an older statement delivered late is kept as history and never becomes
  current, and one already on record adds nothing;
* returning to earlier content with a date at least as new is a new revision
  (a reversion), never a silent no-op, because only the *current* revision is
  deduplicated against.

Record identity never depends on arrival order: the key is the provider, the
kind and the native identifier (an EASA AD without its revision suffix).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date
from typing import Any

from src.kb.engineering_safety_citations import extract_all
from src.kb.engineering_safety_records import (
    AUTHORITIES,
    DEFAULT_NAMESPACE,
    READ_SCOPE,
    EngineeringSafetyError,
    authorize,
    canonical,
    content_view,
    date_rank,
    digest,
    iso_date,
    iso_text,
    load,
    record_id_for,
    subject_key,
    validate_statement,
)

_DDL = """
CREATE SEQUENCE IF NOT EXISTS es_seq;
CREATE TABLE IF NOT EXISTS es_records (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, provider TEXT NOT NULL, record_kind TEXT NOT NULL,
  native_id TEXT NOT NULL, authority TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS es_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, revision_no INTEGER NOT NULL,
  seq BIGINT NOT NULL, content_digest TEXT NOT NULL, revision_date DATE, revision_date_basis TEXT,
  revision_label TEXT, report_status TEXT, effective_date DATE, title TEXT, language TEXT, access TEXT NOT NULL,
  url TEXT, observed_at_ms BIGINT NOT NULL, run_id TEXT, source_id TEXT, document_id TEXT, payload_pointer TEXT,
  statement_json TEXT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS es_current (
  namespace TEXT NOT NULL, record_id TEXT NOT NULL, revision_id TEXT NOT NULL, updated_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, record_id)
);
CREATE TABLE IF NOT EXISTS es_applicability (
  namespace TEXT NOT NULL, applicability_id TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, text TEXT NOT NULL, parse_state TEXT NOT NULL, parsed_json TEXT, locator_json TEXT NOT NULL,
  PRIMARY KEY(namespace, applicability_id)
);
CREATE TABLE IF NOT EXISTS es_statements (
  namespace TEXT NOT NULL, statement_id TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, kind TEXT NOT NULL, label TEXT, text TEXT NOT NULL, language TEXT,
  locator_json TEXT NOT NULL, PRIMARY KEY(namespace, statement_id)
);
CREATE TABLE IF NOT EXISTS es_relations (
  namespace TEXT NOT NULL, relation_id TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL,
  relation TEXT NOT NULL, target_provider TEXT NOT NULL, target_native_id TEXT NOT NULL, target_revision_label TEXT,
  text TEXT, locator_json TEXT NOT NULL, PRIMARY KEY(namespace, relation_id)
);
CREATE TABLE IF NOT EXISTS es_subjects (
  namespace TEXT NOT NULL, subject_id TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, kind TEXT NOT NULL, subject_key TEXT, source_string TEXT NOT NULL, role TEXT,
  fields_json TEXT NOT NULL, locator_json TEXT NOT NULL, PRIMARY KEY(namespace, subject_id)
);
CREATE TABLE IF NOT EXISTS es_occurrences (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL, occurred_on DATE,
  occurred_declared TEXT, place TEXT, country TEXT, latitude DOUBLE, longitude DOUBLE, severity TEXT,
  consequences_json TEXT NOT NULL, locator_json TEXT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS es_responses (
  namespace TEXT NOT NULL, response_id TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL, status TEXT NOT NULL, status_date DATE, status_date_declared TEXT, addressee TEXT,
  locator_json TEXT NOT NULL, PRIMARY KEY(namespace, response_id)
);
CREATE TABLE IF NOT EXISTS es_quantities (
  namespace TEXT NOT NULL, quantity_id TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL,
  name TEXT NOT NULL, value TEXT NOT NULL, unit TEXT, locator_json TEXT NOT NULL, PRIMARY KEY(namespace, quantity_id)
);
CREATE TABLE IF NOT EXISTS es_citations (
  namespace TEXT NOT NULL, citation_id TEXT NOT NULL, revision_id TEXT NOT NULL, record_id TEXT NOT NULL,
  kind TEXT NOT NULL, raw TEXT NOT NULL, reference_key TEXT NOT NULL, identifiers_json TEXT NOT NULL,
  locator_json TEXT NOT NULL, PRIMARY KEY(namespace, citation_id)
);
CREATE TABLE IF NOT EXISTS es_citation_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, citation_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_namespace TEXT NOT NULL, target_id TEXT NOT NULL, target_revision_id TEXT,
  target_label TEXT, basis TEXT NOT NULL, identifier TEXT NOT NULL, principal_id TEXT NOT NULL,
  linked_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
CREATE TABLE IF NOT EXISTS es_selection (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, selection_index INTEGER NOT NULL,
  selector_json TEXT NOT NULL, outcome TEXT NOT NULL, records INTEGER NOT NULL, out_of_scope INTEGER NOT NULL,
  response_sha256 TEXT, observed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, run_id, source_id, selection_index)
);
CREATE TABLE IF NOT EXISTS es_source_runs (
  namespace TEXT NOT NULL, run_id TEXT NOT NULL, source_id TEXT NOT NULL, status TEXT NOT NULL,
  cutoff_seq BIGINT NOT NULL, finished_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, run_id, source_id)
);
CREATE TABLE IF NOT EXISTS es_generation (
  namespace TEXT NOT NULL, generation BIGINT NOT NULL, PRIMARY KEY(namespace)
);
"""

REVISION_COLUMNS = (
    "revision_id",
    "record_id",
    "revision_no",
    "seq",
    "content_digest",
    "revision_date",
    "revision_date_basis",
    "revision_label",
    "report_status",
    "effective_date",
    "title",
    "language",
    "access",
    "url",
    "observed_at_ms",
    "run_id",
    "source_id",
    "document_id",
    "payload_pointer",
)
_STATUS_RANK = {"final": 1}


def table_exists(conn: Any, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]
        ).fetchone()
    )


def rank(revision: Mapping[str, Any]) -> tuple[int, date]:
    """Current-selection order: report status (final above preliminary), then the source's own date."""
    return _STATUS_RANK.get(str(revision.get("report_status") or ""), 0), date_rank(
        revision.get("revision_date")
    )


def _ms(value: Any) -> int | None:
    return None if value is None else int(value)


class EngineeringSafetyStore:
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

    # ------------------------------------------------------------ readiness

    def ready(self) -> bool:
        """Whether a source has run (or a record was imported); empty tables created by a write are not enough."""
        if not table_exists(self.conn, "es_revisions"):
            return False
        if self.conn.execute("SELECT 1 FROM es_revisions LIMIT 1").fetchone():
            return True
        return table_exists(self.conn, "es_source_runs") and bool(
            self.conn.execute("SELECT 1 FROM es_source_runs LIMIT 1").fetchone()
        )

    def require_ready(self) -> None:
        if not self.ready():
            raise EngineeringSafetyError(
                "not_ready",
                "no engineering-safety records are stored yet; run the engineering-safety source pack first",
            )

    def generation(self, namespace: str) -> int:
        if not table_exists(self.conn, "es_generation"):
            return 0
        row = self.conn.execute(
            "SELECT generation FROM es_generation WHERE namespace=?", [namespace]
        ).fetchone()
        return int(row[0]) if row else 0

    def bump(self, namespace: str) -> None:
        """Advance the namespace's review generation (matches, reviews, links): append-only and monotone."""
        self.conn.execute(
            "INSERT INTO es_generation VALUES (?, 1) ON CONFLICT (namespace) "
            "DO UPDATE SET generation=es_generation.generation+1",
            [namespace],
        )

    # ------------------------------------------------------------ writes

    def observe_page(
        self,
        run_id: str,
        source: Mapping[str, Any],
        namespace: str,
        records: Sequence[Mapping[str, Any]],
        *,
        documents: Mapping[str, str],
        page_receipt: Mapping[str, Any],
    ) -> dict[str, int]:
        """Project one runtime page in one transaction; replays add nothing."""
        statements = []
        for item in records:
            statement = item.get("engineering_safety_record")
            if not isinstance(statement, Mapping):
                raise EngineeringSafetyError(
                    "invalid_record",
                    "page record lacks an engineering-safety statement",
                )
            statements.append((dict(statement), documents.get(str(item.get("id")))))
        counts = {
            "created": 0,
            "revised": 0,
            "reverted": 0,
            "history": 0,
            "unchanged": 0,
        }
        now = self.now()
        self.conn.execute("BEGIN")
        try:
            for statement, document_id in statements:
                result = self._apply(
                    namespace,
                    statement,
                    run_id=run_id,
                    source_id=source["source_id"],
                    document_id=document_id,
                    observed_at_ms=now,
                )
                counts[result["status"]] += 1
            if page_receipt.get("selection_index") is not None:
                self.conn.execute(
                    "INSERT OR IGNORE INTO es_selection VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [
                        namespace,
                        run_id,
                        source["source_id"],
                        int(page_receipt["selection_index"]),
                        canonical(page_receipt.get("selector") or {}),
                        str(page_receipt.get("selector_outcome") or "returned"),
                        len(statements),
                        int(page_receipt.get("out_of_scope") or 0),
                        page_receipt.get("response_sha256"),
                        now,
                    ],
                )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return counts

    def apply(
        self,
        namespace: str,
        statement: Mapping[str, Any],
        *,
        run_id: str | None = None,
        source_id: str | None = None,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        """Record one statement outside a runtime page (tests, imports); the same rules as a page."""
        self.conn.execute("BEGIN")
        try:
            result = self._apply(
                namespace,
                dict(statement),
                run_id=run_id,
                source_id=source_id,
                document_id=document_id,
                observed_at_ms=self.now(),
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return result

    def _apply(
        self,
        namespace: str,
        statement: dict[str, Any],
        *,
        run_id,
        source_id,
        document_id,
        observed_at_ms: int,
    ) -> dict[str, Any]:
        pointer = statement.pop("payload_pointer", None)
        value = validate_statement(statement)
        provider, kind, native = (
            value["provider"],
            value["record_kind"],
            value["native_id"],
        )
        content_digest = digest(content_view(value))
        record_id = record_id_for(namespace, provider, kind, native)
        current = self.conn.execute(
            "SELECT r.revision_id, r.content_digest, r.revision_date, r.report_status FROM es_current c "
            "JOIN es_revisions r ON r.namespace=c.namespace AND r.revision_id=c.revision_id "
            "WHERE c.namespace=? AND c.record_id=?",
            [namespace, record_id],
        ).fetchone()
        if current and current[1] == content_digest:
            return {
                "status": "unchanged",
                "record_id": record_id,
                "revision_id": current[0],
            }
        incoming = {
            "report_status": value.get("report_status"),
            "revision_date": value.get("revision_date"),
        }
        newer = current is None or rank(incoming) >= rank(
            {"report_status": current[3], "revision_date": current[2]}
        )
        known = self.conn.execute(
            "SELECT revision_id FROM es_revisions WHERE namespace=? AND record_id=? AND content_digest=? "
            "ORDER BY seq DESC LIMIT 1",
            [namespace, record_id, content_digest],
        ).fetchone()
        if known and not newer:
            # An older statement already kept, delivered again late: nothing new happened at the source.
            return {
                "status": "unchanged",
                "record_id": record_id,
                "revision_id": known[0],
            }
        if current is None:
            self.conn.execute(
                "INSERT OR IGNORE INTO es_records VALUES (?,?,?,?,?,?,?)",
                [
                    namespace,
                    record_id,
                    provider,
                    kind,
                    native,
                    AUTHORITIES[provider][1],
                    observed_at_ms,
                ],
            )
        revision_no = (
            int(
                self.conn.execute(
                    "SELECT coalesce(max(revision_no), 0) FROM es_revisions WHERE namespace=? AND record_id=?",
                    [namespace, record_id],
                ).fetchone()[0]
            )
            + 1
        )
        seq = int(self.conn.execute("SELECT nextval('es_seq')").fetchone()[0])
        revision_id = (
            "es-revision:"
            + digest([namespace, record_id, revision_no, content_digest])[:24]
        )
        self.conn.execute(
            "INSERT INTO es_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                revision_id,
                record_id,
                revision_no,
                seq,
                content_digest,
                iso_date(value.get("revision_date")),
                value.get("revision_date_basis")
                or ("observation" if not value.get("revision_date") else None),
                value.get("revision_label"),
                value.get("report_status"),
                iso_date(value.get("effective_date")),
                value.get("title"),
                value.get("language"),
                str(value.get("access") or "acquired"),
                value.get("url"),
                observed_at_ms,
                run_id,
                source_id,
                document_id,
                pointer,
                canonical(value),
            ],
        )
        self._parts(namespace, record_id, revision_id, value)
        if newer:
            self.conn.execute(
                "INSERT OR REPLACE INTO es_current VALUES (?,?,?,?)",
                [namespace, record_id, revision_id, observed_at_ms],
            )
            status = (
                "created" if current is None else "reverted" if known else "revised"
            )
        else:
            status = "history"
        return {
            "status": status,
            "record_id": record_id,
            "revision_id": revision_id,
            "revision_no": revision_no,
        }

    def _parts(
        self, namespace: str, record_id: str, revision_id: str, value: Mapping[str, Any]
    ) -> None:
        def rid(prefix: str, index: int, item: Any) -> str:
            return f"{prefix}:" + digest([revision_id, index, item])[:24]

        texts: list[tuple[str, dict[str, Any]]] = []
        for i, item in enumerate(value.get("applicability") or []):
            locator = dict(item["locator"])
            self.conn.execute(
                "INSERT OR IGNORE INTO es_applicability VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("es-applicability", i, item),
                    revision_id,
                    record_id,
                    i,
                    item["text"],
                    item["parse_state"],
                    canonical(item["parsed"]) if item.get("parsed") else None,
                    canonical(locator),
                ],
            )
            texts.append((item["text"], locator))
        for i, item in enumerate(value.get("statements") or []):
            locator = dict(item["locator"])
            self.conn.execute(
                "INSERT OR IGNORE INTO es_statements VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("es-statement", i, item),
                    revision_id,
                    record_id,
                    i,
                    item["kind"],
                    item.get("label"),
                    item["text"],
                    item.get("language") or value.get("language"),
                    canonical(locator),
                ],
            )
            texts.append((item["text"], locator))
        for i, item in enumerate(value.get("relations") or []):
            self.conn.execute(
                "INSERT OR IGNORE INTO es_relations VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("es-relation", i, item),
                    revision_id,
                    record_id,
                    item["relation"],
                    item["target_provider"],
                    item["target_native_id"],
                    item.get("target_revision_label"),
                    item.get("text"),
                    canonical(item.get("locator") or {}),
                ],
            )
        for i, item in enumerate(value.get("subjects") or []):
            fields = dict(item.get("fields") or {})
            self.conn.execute(
                "INSERT OR IGNORE INTO es_subjects VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("es-subject", i, item),
                    revision_id,
                    record_id,
                    i,
                    item["kind"],
                    subject_key(item["kind"], fields),
                    item["source_string"],
                    item.get("role"),
                    canonical(fields),
                    canonical(item.get("locator") or {}),
                ],
            )
        occurrence = value.get("occurrence")
        if occurrence:
            self.conn.execute(
                "INSERT OR IGNORE INTO es_occurrences VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    revision_id,
                    record_id,
                    iso_date(occurrence.get("occurred_on")),
                    occurrence.get("occurred_declared"),
                    occurrence.get("place"),
                    occurrence.get("country"),
                    occurrence.get("latitude"),
                    occurrence.get("longitude"),
                    occurrence.get("severity"),
                    canonical(occurrence.get("consequences") or {}),
                    canonical(occurrence.get("locator") or {}),
                ],
            )
        for i, item in enumerate(value.get("responses") or []):
            self.conn.execute(
                "INSERT OR IGNORE INTO es_responses VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("es-response", i, item),
                    revision_id,
                    record_id,
                    i,
                    item["status"],
                    iso_date(item.get("status_date")),
                    item.get("status_date_declared"),
                    item.get("addressee"),
                    canonical(item.get("locator") or {}),
                ],
            )
        for i, item in enumerate(value.get("quantities") or []):
            self.conn.execute(
                "INSERT OR IGNORE INTO es_quantities VALUES (?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    rid("es-quantity", i, item),
                    revision_id,
                    record_id,
                    item["name"],
                    str(item["value"]),
                    item.get("unit"),
                    canonical(item.get("locator") or {}),
                ],
            )
        own = f"{value['provider']}:{value['native_id']}"
        for citation in extract_all(texts):
            if (
                citation["kind"] in {"directive", "recommendation"}
                and citation["reference_key"] == own
            ):
                continue  # a record naming itself is not a citation
            self.conn.execute(
                "INSERT OR IGNORE INTO es_citations VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    "es-citation:"
                    + digest(
                        [
                            revision_id,
                            citation["reference_key"],
                            citation["raw"],
                            citation["locator"],
                        ]
                    )[:24],
                    revision_id,
                    record_id,
                    citation["kind"],
                    citation["raw"],
                    citation["reference_key"],
                    canonical(citation["identifiers"]),
                    canonical(citation["locator"]),
                ],
            )

    def finish_source(
        self, run_id: str, source_id: str, namespace: str, status: str
    ) -> dict[str, Any]:
        """Record a source's run outcome; monitors only advance past runs where every source completed."""
        cutoff = int(
            self.conn.execute(
                "SELECT coalesce(max(seq), 0) FROM es_revisions WHERE namespace=?",
                [namespace],
            ).fetchone()[0]
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO es_source_runs VALUES (?,?,?,?,?,?)",
            [namespace, run_id, source_id, status, cutoff, self.now()],
        )
        return {
            "source_id": source_id,
            "status": status,
            "complete": status == "complete",
            "cutoff_seq": cutoff,
        }

    # ------------------------------------------------------------ reads

    def record(self, namespace: str, record_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT record_id, provider, record_kind, native_id, authority FROM es_records "
            "WHERE namespace=? AND record_id=?",
            [namespace, record_id],
        ).fetchone()
        if row is None:
            raise EngineeringSafetyError(
                "not_found", "record is not visible in this namespace"
            )
        return dict(
            zip(("record_id", "provider", "record_kind", "native_id", "authority"), row)
        )

    def find(
        self, namespace: str, provider: str, kind: str, native_id: str
    ) -> str | None:
        record_id = record_id_for(namespace, provider, kind, native_id)
        row = self.conn.execute(
            "SELECT 1 FROM es_records WHERE namespace=? AND record_id=?",
            [namespace, record_id],
        ).fetchone()
        return record_id if row else None

    def resolve(self, namespace: str, reference: str, kind: str | None = None) -> str:
        """A record id, or ``provider:native_id`` (with the kind when the provider publishes several)."""
        if reference.startswith("es-record:"):
            self.record(namespace, reference)
            return reference
        provider, _, native = reference.partition(":")
        from src.kb.engineering_safety_records import PROVIDER_KINDS

        if provider not in PROVIDER_KINDS or not native:
            raise EngineeringSafetyError(
                "invalid_request", "name a record id or provider:native_id"
            )
        kinds = [kind] if kind else sorted(PROVIDER_KINDS[provider])
        for candidate in kinds:
            found = self.find(namespace, provider, candidate, native)
            if found:
                return found
        raise EngineeringSafetyError(
            "not_found", f"{reference} is not on record in this namespace"
        )

    def records(
        self, namespace: str, kinds: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        kinds = sorted(set(kinds or []))
        sql = "SELECT record_id, provider, record_kind, native_id, authority FROM es_records WHERE namespace=?"
        params: list[Any] = [namespace]
        if kinds:
            sql += " AND record_kind IN (" + ",".join("?" * len(kinds)) + ")"
            params += kinds
        return [
            dict(
                zip(
                    ("record_id", "provider", "record_kind", "native_id", "authority"),
                    r,
                )
            )
            for r in self.conn.execute(
                sql + " ORDER BY provider, native_id", params
            ).fetchall()
        ]

    def revisions(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT "
            + ", ".join(REVISION_COLUMNS)
            + " FROM es_revisions WHERE namespace=? AND record_id=? "
            "ORDER BY seq",
            [namespace, record_id],
        ).fetchall()
        result = []
        for row in rows:
            item = dict(zip(REVISION_COLUMNS, row))
            for key in ("revision_date", "effective_date"):
                item[key] = iso_text(item[key])
            result.append({k: v for k, v in item.items() if v is not None})
        return result

    def current_revision_id(self, namespace: str, record_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT revision_id FROM es_current WHERE namespace=? AND record_id=?",
            [namespace, record_id],
        ).fetchone()
        return row[0] if row else None

    def revision_as_of(
        self,
        namespace: str,
        record_id: str,
        as_of: date | None = None,
        acquired_by_ms: int | None = None,
        *,
        cutoff_seq: int | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """The revision current as of a date (and an acquisition cutoff), and the later revisions.

        Only revisions acquired by the cutoff count. Among them, the current one is chosen by report status
        and the source's own date, falling back to observation order; with neither cutoff it is the stored
        current revision.
        """
        revisions = self.revisions(namespace, record_id)
        if as_of is None and acquired_by_ms is None and cutoff_seq is None:
            current = self.current_revision_id(namespace, record_id)
            return next((r for r in revisions if r["revision_id"] == current), None), []
        acquired = [
            r
            for r in revisions
            if (acquired_by_ms is None or r["observed_at_ms"] <= acquired_by_ms)
            and (cutoff_seq is None or r["seq"] <= cutoff_seq)
        ]
        eligible = [
            r
            for r in acquired
            if as_of is None or date_rank(r.get("revision_date")) <= as_of
        ]
        later = [
            r
            for r in acquired
            if as_of is not None and date_rank(r.get("revision_date")) > as_of
        ]
        chosen = None
        for revision in sorted(eligible, key=lambda r: r["seq"]):
            # Replaying arrival order reproduces the store's own current selection among eligible revisions.
            if chosen is None or rank(revision) >= rank(chosen):
                chosen = revision
        return chosen, later

    def statement(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT statement_json FROM es_revisions WHERE namespace=? AND revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if row is None:
            raise EngineeringSafetyError(
                "not_found", "revision is not visible in this namespace"
            )
        return load(row[0], {})

    def parts(self, namespace: str, revision_id: str) -> dict[str, Any]:
        def rows(sql: str, keys: Sequence[str]) -> list[dict[str, Any]]:
            result = []
            for r in self.conn.execute(sql, [namespace, revision_id]).fetchall():
                item = dict(zip(keys, r))
                for key in (
                    "locator",
                    "parsed",
                    "fields",
                    "consequences",
                    "identifiers",
                ):
                    if key in item:
                        item[key] = load(item[key], {} if key != "parsed" else None)
                for key in ("status_date", "occurred_on"):
                    if key in item:
                        item[key] = iso_text(item[key])
                result.append({k: v for k, v in item.items() if v is not None})
            return result

        applicability = rows(
            "SELECT applicability_id, ordinal, text, parse_state, parsed_json, locator_json FROM es_applicability "
            "WHERE namespace=? AND revision_id=? ORDER BY ordinal",
            ("applicability_id", "ordinal", "text", "parse_state", "parsed", "locator"),
        )
        statements = rows(
            "SELECT statement_id, ordinal, kind, label, text, language, locator_json FROM es_statements "
            "WHERE namespace=? AND revision_id=? ORDER BY ordinal",
            ("statement_id", "ordinal", "kind", "label", "text", "language", "locator"),
        )
        for item in statements:
            item["quoted"] = True
        relations = rows(
            "SELECT relation_id, relation, target_provider, target_native_id, target_revision_label, text, "
            "locator_json FROM es_relations WHERE namespace=? AND revision_id=? ORDER BY relation, target_native_id",
            (
                "relation_id",
                "relation",
                "target_provider",
                "target_native_id",
                "target_revision_label",
                "text",
                "locator",
            ),
        )
        subjects = rows(
            "SELECT subject_id, ordinal, kind, subject_key, source_string, role, fields_json, locator_json "
            "FROM es_subjects WHERE namespace=? AND revision_id=? ORDER BY ordinal",
            (
                "subject_id",
                "ordinal",
                "kind",
                "subject_key",
                "source_string",
                "role",
                "fields",
                "locator",
            ),
        )
        occurrence = rows(
            "SELECT occurred_on, occurred_declared, place, country, latitude, longitude, severity, "
            "consequences_json, locator_json FROM es_occurrences WHERE namespace=? AND revision_id=?",
            (
                "occurred_on",
                "occurred_declared",
                "place",
                "country",
                "latitude",
                "longitude",
                "severity",
                "consequences",
                "locator",
            ),
        )
        responses = rows(
            "SELECT response_id, ordinal, status, status_date, status_date_declared, addressee, locator_json "
            "FROM es_responses WHERE namespace=? AND revision_id=? ORDER BY ordinal",
            (
                "response_id",
                "ordinal",
                "status",
                "status_date",
                "status_date_declared",
                "addressee",
                "locator",
            ),
        )
        quantities = rows(
            "SELECT quantity_id, name, value, unit, locator_json FROM es_quantities WHERE namespace=? AND "
            "revision_id=? ORDER BY name",
            ("quantity_id", "name", "value", "unit", "locator"),
        )
        citations = rows(
            "SELECT citation_id, kind, raw, reference_key, identifiers_json, locator_json FROM es_citations "
            "WHERE namespace=? AND revision_id=? ORDER BY kind, raw, citation_id",
            ("citation_id", "kind", "raw", "reference_key", "identifiers", "locator"),
        )
        for citation in citations:
            citation["links"] = self.citation_links(namespace, citation["citation_id"])
            citation["resolution"] = (
                "resolved"
                if citation["links"]
                else "unresolved (kept as the cited text)"
            )
        if occurrence:
            item = occurrence[0]
            if item.get("latitude") is not None and item.get("longitude") is not None:
                # Coordinates as published only; nothing is geocoded from a place name.
                item["geometry"] = {
                    "type": "Point",
                    "coordinates": [item["longitude"], item["latitude"]],
                    "basis": "coordinates as published",
                }
        return {
            "applicability": applicability,
            "statements": statements,
            "relations": relations,
            "subjects": subjects,
            "occurrence": occurrence[0] if occurrence else None,
            "responses": responses,
            "quantities": [
                dict(q, normalized=normalize_quantity(q)) for q in quantities
            ],
            "citations": citations,
        }

    def citation_links(self, namespace: str, citation_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "es_citation_links"):
            return []
        keys = (
            "link_id",
            "target_kind",
            "target_namespace",
            "target_id",
            "target_revision_id",
            "target_label",
            "basis",
            "identifier",
            "linked_at_ms",
        )
        return [
            {k: v for k, v in zip(keys, r) if v is not None}
            for r in self.conn.execute(
                "SELECT "
                + ", ".join(keys)
                + " FROM es_citation_links WHERE namespace=? AND citation_id=? "
                "ORDER BY linked_at_ms, link_id",
                [namespace, citation_id],
            ).fetchall()
        ]

    def response_history(
        self,
        namespace: str,
        record_id: str,
        acquired_by_ms: int | None = None,
        cutoff_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """Every published status of a recommendation across its acquired revisions, deduplicated and dated."""
        rows = self.conn.execute(
            "SELECT s.status, s.status_date, s.status_date_declared, s.addressee, s.locator_json, r.revision_id, "
            "r.observed_at_ms, r.seq FROM es_responses s JOIN es_revisions r ON r.namespace=s.namespace AND "
            "r.revision_id=s.revision_id WHERE s.namespace=? AND s.record_id=? ORDER BY r.seq, s.ordinal",
            [namespace, record_id],
        ).fetchall()
        seen: dict[tuple[Any, ...], dict[str, Any]] = {}
        for (
            status,
            status_date,
            declared,
            addressee,
            locator,
            revision_id,
            observed,
            seq,
        ) in rows:
            if acquired_by_ms is not None and observed > acquired_by_ms:
                continue
            if cutoff_seq is not None and seq > cutoff_seq:
                continue
            key = (status, iso_text(status_date), addressee)
            if key not in seen:
                seen[key] = {
                    k: v
                    for k, v in {
                        "status": status,
                        "status_date": iso_text(status_date),
                        "status_date_declared": declared,
                        "addressee": addressee,
                        "locator": load(locator, {}),
                        "first_published_in": revision_id,
                        "first_observed_at_ms": observed,
                    }.items()
                    if v is not None
                }
        return sorted(
            seen.values(),
            key=lambda r: (date_rank(r.get("status_date")), r["first_observed_at_ms"]),
        )

    def sources_consulted(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "es_source_runs"):
            return []
        rows = self.conn.execute(
            "SELECT source_id, max(finished_at_ms) FILTER (WHERE status='complete'), max(finished_at_ms), "
            "arg_max(status, finished_at_ms) FROM es_source_runs WHERE namespace=? GROUP BY source_id "
            "ORDER BY source_id",
            [namespace],
        ).fetchall()
        return [
            {
                k: v
                for k, v in zip(
                    (
                        "source_id",
                        "last_complete_run_at_ms",
                        "last_run_at_ms",
                        "last_run_status",
                    ),
                    r,
                )
                if v is not None
            }
            for r in rows
        ]

    def selection_outcomes(self, namespace: str, run_id: str) -> list[dict[str, Any]]:
        self.require_ready()
        keys = (
            "source_id",
            "selection_index",
            "selector",
            "outcome",
            "records",
            "out_of_scope",
        )
        result = []
        for r in self.conn.execute(
            "SELECT source_id, selection_index, selector_json, outcome, records, out_of_scope FROM es_selection "
            "WHERE namespace=? AND run_id=? ORDER BY source_id, selection_index",
            [namespace, run_id],
        ).fetchall():
            item = dict(zip(keys, r))
            item["selector"] = load(item["selector"], {})
            result.append(item)
        return result


def normalize_quantity(quantity: Mapping[str, Any]) -> dict[str, Any]:
    """A published quantity in SI through ``src.integrations.units`` when its unit is explicit; else unconverted."""
    targets = {
        "bbl": ("oil_barrel", "meter ** 3"),
        "mscf": ("1000 * foot ** 3", "meter ** 3"),
        "gal": ("gallon", "meter ** 3"),
    }
    unit = str(quantity.get("unit") or "").casefold()
    if unit not in targets:
        return {
            "status": "not_converted",
            "reason": "no explicit convertible unit; the published value stands",
        }
    try:
        from src.integrations.units import convert_physical

        receipt = convert_physical(
            quantity["value"], targets[unit][0], targets[unit][1]
        )
    except ImportError:
        return {
            "status": "not_converted",
            "reason": "unit library unavailable; the published value stands",
        }
    except Exception as exc:  # noqa: BLE001 - a failed conversion never replaces the published value
        return {"status": "not_converted", "reason": str(exc)[:120]}
    output = dict(receipt.get("output") or receipt.get("result") or {})
    return {
        "status": "converted",
        "value": output.get("value"),
        "unit": "m^3",
        "via": "src.integrations.units",
    }


class EngineeringSafetyProjector:
    """Source-pack runtime projector for ``noesis-engineering-safety-record-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = EngineeringSafetyStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("engineering_safety") or {}).get("namespace")
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
        del manifest, principal_id
        document_ids = {
            str(dict(item.get("metadata") or {}).get("source_pack_record_id")): str(
                item["document_id"]
            )
            for item in documents
        }
        return self.store.observe_page(
            run_id,
            source,
            self._namespace(source),
            records,
            documents=document_ids,
            page_receipt=page_receipt,
        )

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del manifest, principal_id
        return self.store.finish_source(
            run_id, source["source_id"], self._namespace(source), status
        )


def inspect_record(
    conn: Any,
    namespace: str,
    reference: str,
    *,
    scopes: Iterable[str],
    as_of: str | None = None,
    acquired_by_ms: int | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    """One record with the revision current as of a date, its parts and every other revision's parts."""
    from src.kb.engineering_safety_records import BOUNDARY, CONTRACT

    authorize(namespace, scopes, READ_SCOPE)
    store = EngineeringSafetyStore(conn, initialize=False)
    store.require_ready()
    record_id = store.resolve(namespace, reference, kind)
    head = store.record(namespace, record_id)
    cutoff = iso_date(as_of)
    if as_of and cutoff is None:
        raise EngineeringSafetyError("invalid_request", "as_of must be an ISO date")
    chosen, later = store.revision_as_of(namespace, record_id, cutoff, acquired_by_ms)
    history = store.revisions(namespace, record_id)
    return {
        "contract": CONTRACT,
        "namespace": namespace,
        **head,
        "as_of": None if cutoff is None else cutoff.isoformat(),
        "acquired_by_ms": _ms(acquired_by_ms),
        "revision": None
        if chosen is None
        else {**chosen, **store.parts(namespace, chosen["revision_id"])},
        "status": "published" if chosen else "not yet published as of this date",
        "later_revisions": [
            {
                "revision_id": r["revision_id"],
                "revision_date": r.get("revision_date"),
                "note": "published after as_of",
            }
            for r in later
        ],
        "revision_history": [
            {**r, "parts": store.parts(namespace, r["revision_id"])} for r in history
        ],
        "responses": store.response_history(namespace, record_id, acquired_by_ms)
        if head["record_kind"] == "safety_recommendation"
        else [],
        "boundary": BOUNDARY,
    }
