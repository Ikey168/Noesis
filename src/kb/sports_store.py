"""The Sports record owner: append-only, source-attributed revisions (#2135, SP02).

Two namespace-scoped tables hold every ``noesis-sports-record-v1`` record:

* ``sports_acquisitions`` - one acquired publication (an API response, a file
  at a pinned commit, an operator-recorded official decision): provider,
  source id, file digest, release, the source's own publication time,
  attribution and licence, evidence origin and retrieval time. Re-acquiring an
  unchanged publication adds nothing.
* ``sports_revisions`` - one revision of one record (``record_type``,
  ``record_key``) with its normalised body, content hash, the source's
  published-at (falling back to retrieval time when the source states none),
  the acquisition it came from and an observation sequence.

Revision rules (order-independent):

* the revision id is content-addressed (record, normalised content, source
  date), so a replayed or late-arriving publication never changes a key;
* an observation is deduplicated only against the *current* revision of the
  same source at its publication date: unchanged data adds nothing, a
  reversion to earlier content is a new revision, a late older publication
  lands as history without displacing later ones;
* "current" is chosen per source by the source's own date (observation order
  breaks ties), then filtered, then the latest across sources is taken;
* derived labels (``rescheduled``, ``corrected``) are computed at read time
  from the source-ordered history, never stored, so they do not depend on
  arrival order.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.ingestion.sports_sources import (
    ACQUISITION_CONTRACT,
    FORMATS,
    compact,
    iso_datetime,
    to_ms,
)
from src.kb.sports_records import (
    CONTRACT,
    DEFAULT_NAMESPACE,
    OFFICIAL_CLASS,
    SportsError,
    canonical,
    digest,
    parent_key,
    table_exists,
    validate_body,
)

DECISION_PROVIDER = "official-decision"
MANUAL_FORMATS = {
    DECISION_PROVIDER: "official-decision",
    "transfers-official": "transfer-announcement",
}
_DDL = """
CREATE TABLE IF NOT EXISTS sports_acquisitions (
  namespace TEXT NOT NULL, acquisition_id TEXT NOT NULL, provider TEXT NOT NULL, source_id TEXT, format TEXT NOT NULL,
  path TEXT, url TEXT, file_sha256 TEXT NOT NULL, release TEXT, published_at TEXT, header_json TEXT NOT NULL,
  evidence_origin TEXT NOT NULL, record_count INTEGER NOT NULL, run_id TEXT NOT NULL, sequence BIGINT NOT NULL,
  retrieved_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, acquisition_id)
);
CREATE TABLE IF NOT EXISTS sports_revisions (
  namespace TEXT NOT NULL, revision_id TEXT NOT NULL, record_type TEXT NOT NULL, record_key TEXT NOT NULL,
  parent_key TEXT, provider TEXT NOT NULL, source_id TEXT, source_record_id TEXT NOT NULL, locator TEXT,
  content_hash TEXT NOT NULL, body_json TEXT NOT NULL, published_at TEXT, published_ms BIGINT NOT NULL,
  acquisition_id TEXT NOT NULL, evidence_origin TEXT NOT NULL, retrieved_at_ms BIGINT NOT NULL,
  observed_seq BIGINT NOT NULL, PRIMARY KEY(namespace, revision_id)
);
CREATE TABLE IF NOT EXISTS sports_acquisition_members (
  namespace TEXT NOT NULL, acquisition_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  PRIMARY KEY(namespace, acquisition_id, revision_id)
);
"""
# The source's own order: its publication date, then observation order.
SOURCE_ORDER = "published_ms, observed_seq"
_COLUMNS = (
    "revision_id",
    "record_type",
    "record_key",
    "parent_key",
    "provider",
    "source_id",
    "source_record_id",
    "locator",
    "content_hash",
    "body_json",
    "published_at",
    "published_ms",
    "acquisition_id",
    "evidence_origin",
    "retrieved_at_ms",
    "observed_seq",
)


def outcome_content(body: Mapping[str, Any]) -> Any:
    """What a result states, independent of the source's structure (for correction comparisons)."""
    return {
        k: body.get(k)
        for k in ("score", "score_text", "ranking", "winner")
        if k in body
    }


class SportsStore:
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

    def ready(self, namespace: str | None = None) -> bool:
        if not table_exists(self.conn, "sports_revisions"):
            return False
        if namespace is None:
            return True
        return bool(
            self.conn.execute(
                "SELECT 1 FROM sports_revisions WHERE namespace=? LIMIT 1", [namespace]
            ).fetchone()
        )

    def require_ready(self, namespace: str) -> None:
        if not self.ready(namespace):
            raise SportsError(
                "not_ready",
                "no sports source has been acquired in this namespace yet; run the sports source pack",
            )

    # ------------------------------------------------------------------ acquisition

    def apply(
        self,
        namespace: str,
        header: Mapping[str, Any],
        observations: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        source_id: str | None,
        retrieved_at_ms: int | None = None,
    ) -> dict[str, Any]:
        """Record one acquired publication and append a revision for every record whose content is new.

        Idempotent by (provider, source, file digest, path, release, parsed records); an unchanged record adds
        nothing.
        """
        provider = str(header.get("provider") or "")
        format_id = str(header.get("format") or "")
        known = (
            FORMATS.get(format_id, {}).get("provider") == provider
            or MANUAL_FORMATS.get(provider) == format_id
        )
        if not known:
            raise SportsError(
                "invalid_acquisition",
                "an acquisition names a known provider and format",
            )
        if int(header.get("record_count", -1)) != len(observations):
            raise SportsError(
                "incomplete_acquisition",
                "an acquisition carries every record it states; a partial page is refused",
            )
        acquisition_id = (
            "sports-acq:"
            + digest(
                [
                    namespace,
                    provider,
                    source_id,
                    header.get("path"),
                    header["file_sha256"],
                    header.get("release"),
                    # The same file parsed under another declaration (competition, season) is another acquisition.
                    digest([dict(o) for o in observations]),
                ]
            )[:24]
        )
        if self.conn.execute(
            "SELECT 1 FROM sports_acquisitions WHERE namespace=? AND acquisition_id=?",
            [namespace, acquisition_id],
        ).fetchone():
            return {
                "acquisition_id": acquisition_id,
                "status": "unchanged",
                "revisions": 0,
            }
        retrieved = int(retrieved_at_ms if retrieved_at_ms is not None else self.now())
        origin = (
            header.get("evidence_origin")
            if header.get("evidence_origin") in {"fixture", "operator"}
            else "live"
        )
        sequence = self.conn.execute(
            "SELECT coalesce(max(sequence), 0) FROM sports_acquisitions WHERE namespace=?",
            [namespace],
        ).fetchone()[0]
        counts: dict[str, int] = {}
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO sports_acquisitions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    acquisition_id,
                    provider,
                    source_id,
                    format_id,
                    header.get("path"),
                    header.get("url"),
                    header["file_sha256"],
                    header.get("release"),
                    iso_datetime(header.get("published_at")),
                    canonical(compact(dict(header))),
                    origin,
                    len(observations),
                    run_id,
                    int(sequence) + 1,
                    retrieved,
                ],
            )
            for observation in observations:
                created = self._observe(
                    namespace,
                    observation,
                    header,
                    acquisition_id,
                    provider,
                    source_id,
                    origin,
                    retrieved,
                )
                if created:
                    counts[observation["record_type"]] = (
                        counts.get(observation["record_type"], 0) + 1
                    )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {
            "acquisition_id": acquisition_id,
            "status": "applied",
            "evidence_origin": origin,
            "revisions": sum(counts.values()),
            "by_type": dict(sorted(counts.items())),
        }

    def _observe(
        self,
        namespace,
        observation,
        header,
        acquisition_id,
        provider,
        source_id,
        origin,
        retrieved,
    ) -> bool:
        record_type, key = (
            str(observation["record_type"]),
            str(observation["record_key"]),
        )
        body = validate_body(record_type, observation.get("body") or {})
        content_hash = digest([record_type, body])
        published = iso_datetime(
            observation.get("published_at") or header.get("published_at")
        )
        published_ms = to_ms(published) if published else retrieved
        revision_id = (
            "sports-rev:"
            + digest(
                [namespace, record_type, key, provider, content_hash, published_ms]
            )[:24]
        )
        link = "INSERT OR IGNORE INTO sports_acquisition_members VALUES (?,?,?)"
        if self.conn.execute(
            "SELECT 1 FROM sports_revisions WHERE namespace=? AND revision_id=?",
            [namespace, revision_id],
        ).fetchone():
            self.conn.execute(link, [namespace, acquisition_id, revision_id])
            return False
        # Deduplicate only against this source's current revision at the publication date.
        current = self.conn.execute(
            "SELECT revision_id, content_hash FROM sports_revisions WHERE namespace=? AND record_type=? AND "
            f"record_key=? AND provider=? AND published_ms<=? ORDER BY {SOURCE_ORDER.replace(',', ' DESC,')} DESC "
            "LIMIT 1",
            [namespace, record_type, key, provider, published_ms],
        ).fetchone()
        if current and current[1] == content_hash:
            self.conn.execute(link, [namespace, acquisition_id, current[0]])
            return False
        seq = self.conn.execute(
            "SELECT coalesce(max(observed_seq), 0) FROM sports_revisions WHERE namespace=?",
            [namespace],
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO sports_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                revision_id,
                record_type,
                key,
                parent_key(record_type, key, body),
                provider,
                source_id,
                str(observation.get("source_record_id") or key),
                observation.get("locator"),
                content_hash,
                canonical(body),
                published,
                published_ms,
                acquisition_id,
                origin,
                retrieved,
                int(seq) + 1,
            ],
        )
        self.conn.execute(link, [namespace, acquisition_id, revision_id])
        return True

    def record_manual(
        self,
        namespace: str,
        provider: str,
        observations: Sequence[Mapping[str, Any]],
        *,
        attribution: str,
        citation: Mapping[str, Any],
        principal_id: str,
    ) -> dict[str, Any]:
        """Append operator-recorded official statements (a governing-body decision, a transfer announcement)."""
        payload = [dict(o) for o in observations]
        header = compact(
            {
                "contract": ACQUISITION_CONTRACT,
                "provider": provider,
                "format": MANUAL_FORMATS[provider],
                "url": citation.get("url"),
                "file_sha256": digest([payload, dict(citation)]),
                "published_at": citation.get("published_at"),
                "record_count": len(payload),
                "attribution": attribution,
                "licence": {
                    "id": "facts-with-citation",
                    "terms_url": citation.get("url"),
                },
                "evidence_origin": "operator",
                "recorded_by": principal_id,
            }
        )
        return self.apply(
            namespace,
            header,
            payload,
            run_id=f"manual:{principal_id}",
            source_id=f"operator:{provider}",
        )

    # ------------------------------------------------------------------ views

    def _row(self, namespace: str, row: Sequence[Any]) -> dict[str, Any]:
        view = dict(zip(_COLUMNS, row))
        body = json.loads(view.pop("body_json"))
        acquisition = self.acquisition(namespace, view["acquisition_id"])
        return compact(
            {
                "contract": CONTRACT,
                "namespace": namespace,
                "record_type": view["record_type"],
                "record_key": view["record_key"],
                "revision_id": view["revision_id"],
                "published_at": view["published_at"],
                "published_ms": view["published_ms"],
                "retrieved_at_ms": view["retrieved_at_ms"],
                "observed_seq": view["observed_seq"],
                "body": body,
                "source": {
                    "provider": view["provider"],
                    "source_id": view["source_id"],
                    "source_record_id": view["source_record_id"],
                    "locator": view["locator"],
                    "acquisition_id": view["acquisition_id"],
                    "url": acquisition.get("url"),
                    "release": acquisition.get("release"),
                    "file_sha256": acquisition.get("file_sha256"),
                    "attribution": acquisition.get("attribution"),
                    "licence": acquisition.get("licence"),
                    "evidence_origin": view["evidence_origin"],
                },
            }
        )

    def acquisition(self, namespace: str, acquisition_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT header_json, source_id, run_id, retrieved_at_ms, sequence FROM sports_acquisitions WHERE "
            "namespace=? AND acquisition_id=?",
            [namespace, acquisition_id],
        ).fetchone()
        if row is None:
            raise SportsError(
                "not_found", "acquisition is not visible in this namespace"
            )
        return {
            **json.loads(row[0]),
            "acquisition_id": acquisition_id,
            "source_id": row[1],
            "run_id": row[2],
            "retrieved_at_ms": row[3],
            "sequence": row[4],
        }

    def revision(self, namespace: str, revision_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM sports_revisions WHERE namespace=? AND revision_id=?",
            [namespace, revision_id],
        ).fetchone()
        if row is None:
            raise SportsError("not_found", "revision is not visible in this namespace")
        return self._row(namespace, row)

    def history(
        self,
        namespace: str,
        record_type: str,
        record_key: str,
        *,
        cutoff_ms: int | None = None,
        acquired_by_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Every revision of a record in the sources' own date order (published by the cutoff, acquired by then)."""
        rows = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM sports_revisions WHERE namespace=? AND record_type=? AND "
            "record_key=? AND (? IS NULL OR published_ms<=?) AND (? IS NULL OR retrieved_at_ms<=?) "
            f"ORDER BY {SOURCE_ORDER}",
            [
                namespace,
                record_type,
                record_key,
                cutoff_ms,
                cutoff_ms,
                acquired_by_ms,
                acquired_by_ms,
            ],
        ).fetchall()
        # A later-dated revision equal to its source's previous one restates it: when the older publication arrived
        # after it, both are stored, so the restatement is skipped here and the answer is the same in every order.
        kept, last = [], {}
        for row in rows:
            provider, content_hash = (
                row[_COLUMNS.index("provider")],
                row[_COLUMNS.index("content_hash")],
            )
            if last.get(provider) == content_hash:
                continue
            last[provider] = content_hash
            kept.append(row)
        return [self._row(namespace, r) for r in kept]

    def current(
        self,
        namespace: str,
        record_type: str,
        record_key: str,
        *,
        cutoff_ms: int | None = None,
        acquired_by_ms: int | None = None,
        statuses: Iterable[str] | None = None,
    ) -> dict[str, Any] | None:
        """The current revision: per source by its own date first, then filtered, then the latest across sources."""
        per_source: dict[str, dict[str, Any]] = {}
        for revision in self.history(
            namespace,
            record_type,
            record_key,
            cutoff_ms=cutoff_ms,
            acquired_by_ms=acquired_by_ms,
        ):
            per_source[revision["source"]["provider"]] = revision
        candidates = list(per_source.values())
        if statuses is not None:
            wanted = set(statuses)
            candidates = [c for c in candidates if c["body"].get("status") in wanted]
        if not candidates:
            return None
        return max(candidates, key=lambda r: (r["published_ms"], r["observed_seq"]))

    def records(
        self,
        namespace: str,
        record_type: str,
        *,
        parent: str | None = None,
        provider: str | None = None,
    ) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT record_key FROM sports_revisions WHERE namespace=? AND record_type=? AND "
            "(? IS NULL OR parent_key=?) AND (? IS NULL OR provider=?) ORDER BY record_key",
            [namespace, record_type, parent, parent, provider, provider],
        ).fetchall()
        return [r[0] for r in rows]

    def body(
        self, namespace: str, record_type: str, record_key: str, **kwargs: Any
    ) -> dict[str, Any] | None:
        current = self.current(namespace, record_type, record_key, **kwargs)
        return None if current is None else current["body"]

    def labelled_history(
        self, namespace: str, record_type: str, record_key: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """History with read-time labels: ``rescheduled`` and ``corrected`` from the source-ordered predecessors."""
        history = self.history(namespace, record_type, record_key, **kwargs)
        out = []
        for index, revision in enumerate(history):
            body = revision["body"]
            earlier = history[:index]
            label = body.get("status")
            previous = earlier[-1] if earlier else None
            if record_type == "fixture_schedule_revision" and previous is not None:
                moved = previous["body"].get("kickoff") != body.get("kickoff")
                if label == "scheduled" and (
                    moved or previous["body"].get("status") != "scheduled"
                ):
                    label = "rescheduled"
            if record_type == "match_result_revision" and label == "official":
                official = [
                    e for e in earlier if e["body"].get("status") in OFFICIAL_CLASS
                ]
                if official and outcome_content(
                    official[-1]["body"]
                ) != outcome_content(body):
                    label = "corrected"
            out.append(
                compact(
                    {
                        **revision,
                        "revision_no": index + 1,
                        "status": label,
                        "previous_revision_id": None
                        if previous is None
                        else previous["revision_id"],
                    }
                )
            )
        return out

    def generation(self, namespace: str) -> str:
        """Changes whenever any source changes (a new acquisition or a new revision)."""
        if not table_exists(self.conn, "sports_revisions"):
            return "sports-gen:empty"
        rows = self.conn.execute(
            "SELECT (SELECT string_agg(acquisition_id, ',' ORDER BY acquisition_id) FROM sports_acquisitions "
            "WHERE namespace=?), (SELECT string_agg(revision_id, ',' ORDER BY revision_id) FROM sports_revisions "
            "WHERE namespace=?)",
            [namespace, namespace],
        ).fetchone()
        return "sports-gen:" + digest(list(rows))[:24]


class SportsProjector:
    """Source-pack runtime projector for ``noesis-sports-record-v1`` pages (one acquisition per page)."""

    def __init__(self, conn: Any) -> None:
        self.store = SportsStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(
            dict(source.get("sports") or {}).get("namespace") or DEFAULT_NAMESPACE
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
            header, record = (
                dict(item.get("sports_acquisition") or {}),
                item.get("sports_record"),
            )
            if not header or not isinstance(record, Mapping):
                raise SportsError(
                    "invalid_record", "page record is not a sports record"
                )
            groups.setdefault(
                canonical([header.get("path"), header["file_sha256"]]), (header, [])
            )[1].append(dict(record))
        return [
            self.store.apply(
                self._namespace(source),
                header,
                observations,
                run_id=run_id,
                source_id=source["source_id"],
            )
            for header, observations in groups.values()
        ]

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        row = self.store.conn.execute(
            "SELECT acquisition_id, published_at FROM sports_acquisitions WHERE namespace=? AND source_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            [self._namespace(source), source["source_id"]],
        ).fetchone()
        return {
            "status": status,
            "latest_acquisition_id": row[0] if row else None,
            "latest_published_at": row[1] if row else None,
        }


def record_result_decision(
    conn: Any,
    namespace: str,
    fixture_key: str,
    *,
    status: str,
    deciding_body: str,
    decision: Mapping[str, Any],
    published_at: str,
    principal_id: str,
    score: Mapping[str, int] | None = None,
    now: Callable[[], int] | None = None,
) -> dict[str, Any]:
    """A governing body's decision on a result (forfeit awarded, annulment, official correction), as a revision.

    It changes answers only from its own publication time and always names the deciding body and its citation.
    """
    if status not in {"forfeit_awarded", "annulled", "official"}:
        raise SportsError(
            "invalid_decision",
            "a decision awards a forfeit, annuls a result or states the official one",
        )
    if (
        not str(decision.get("url") or "").startswith("https://")
        or not str(deciding_body or "").strip()
    ):
        raise SportsError(
            "invalid_decision",
            "a decision names the deciding body and cites its decision (https)",
        )
    store = SportsStore(conn, initialize=False, now=now)
    store.require_ready(namespace)
    if store.current(namespace, "fixture", fixture_key) is None:
        raise SportsError("not_found", "fixture is not visible in this namespace")
    published = iso_datetime(published_at)
    body = compact(
        {
            "status": status,
            "score": None
            if score is None
            else {"home": int(score["home"]), "away": int(score["away"])},
            "deciding_body": deciding_body.strip(),
            "decision": {**dict(decision), "body": deciding_body.strip()},
        }
    )
    return store.record_manual(
        namespace,
        DECISION_PROVIDER,
        [
            {
                "record_type": "match_result_revision",
                "record_key": fixture_key,
                "source_record_id": decision["url"],
                "locator": decision["url"],
                "published_at": published,
                "body": body,
            }
        ],
        attribution=deciding_body.strip(),
        citation={"url": decision["url"], "published_at": published},
        principal_id=principal_id,
    )


def record_table_rule(
    conn: Any,
    namespace: str,
    season_key: str,
    rule_id: str,
    body: Mapping[str, Any],
    *,
    published_at: str,
    citation_url: str,
    attribution: str,
    principal_id: str,
    now: Callable[[], int] | None = None,
) -> dict[str, Any]:
    """A stated scoring rule or a points deduction (deciding body and decision cited) for one season."""
    store = SportsStore(conn, initialize=False, now=now)
    store.require_ready(namespace)
    if store.current(namespace, "season", season_key) is None:
        raise SportsError("not_found", "season is not visible in this namespace")
    if not str(citation_url or "").startswith("https://"):
        raise SportsError(
            "invalid_rule", "a rule or deduction cites its source (https)"
        )
    published = iso_datetime(published_at)
    return store.record_manual(
        namespace,
        DECISION_PROVIDER,
        [
            {
                "record_type": "table_rule",
                "record_key": f"{season_key}|rule|{rule_id}",
                "source_record_id": rule_id,
                "locator": citation_url,
                "published_at": published,
                "body": {**dict(body), "season_key": season_key},
            }
        ],
        attribution=attribution,
        citation={"url": citation_url, "published_at": published},
        principal_id=principal_id,
    )
