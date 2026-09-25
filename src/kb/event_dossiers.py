"""Versioned event dossiers assembled from existing event and source records."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Mapping, Sequence
from typing import Any

from src.kb.events import EventKnowledgeStore
from src.osint.independence import origin_summary

CONTRACT = "noesis-event-dossier-v1"
COMPARISON_CONTRACT = "noesis-event-dossier-comparison-v1"
READ_SCOPE = "knowledge:event-dossier:read"
WRITE_SCOPE = "knowledge:event-dossier:write"
MAX_SOURCES = 200
MAX_ACCOUNTS = 200

_DDL = """
CREATE TABLE IF NOT EXISTS event_dossiers(
 dossier_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, event_id TEXT NOT NULL,
 owner TEXT NOT NULL, request_key TEXT NOT NULL, revision BIGINT NOT NULL,
 request_hash TEXT NOT NULL, UNIQUE(namespace,owner,request_key));
CREATE TABLE IF NOT EXISTS event_dossier_revisions(
 dossier_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 content_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(dossier_id,revision));
"""


class EventDossierError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value: Any, name: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise EventDossierError(
            "invalid_request", f"{name} must be nonempty bounded text"
        )
    return value


def _access(
    namespace: str, principal_id: str, scopes: set[str], *, write=False
) -> None:
    _text(namespace, "namespace", 100)
    _text(principal_id, "principal_id", 200)
    if "operator" in scopes:
        return
    required_scopes = {WRITE_SCOPE} if write else {READ_SCOPE, WRITE_SCOPE}
    event_scopes = (
        {"knowledge:event:write"}
        if write
        else {"knowledge:event:read", "knowledge:event:write"}
    )
    namespace_scopes = (
        {f"namespace:{namespace}:write"}
        if write
        else {f"namespace:{namespace}:read", f"namespace:{namespace}:write"}
    )
    if (
        not required_scopes & scopes
        or not event_scopes & scopes
        or not namespace_scopes & scopes
    ):
        raise EventDossierError(
            "unauthorized", "current dossier and namespace access is required"
        )


def _source_access(document_id: str | None, scopes: set[str]) -> None:
    if (
        document_id
        and "operator" not in scopes
        and f"document:{document_id}:read" not in scopes
    ):
        raise EventDossierError(
            "unauthorized", "current source document access is required"
        )


def _ref_from_evidence(evidence: Mapping[str, Any]) -> str | None:
    for key in ("document_revision_id", "source_revision_id", "revision_id"):
        value = evidence.get(key)
        if isinstance(value, str) and 1 <= len(value) <= 500:
            return value
    return None


def _has_revision_table(conn) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='document_revision_records'"
        ).fetchone()
    )


def _time_relation(left: Mapping[str, Any], right: Mapping[str, Any]) -> str:
    a0, a1 = left.get("valid_from_ms"), left.get("valid_to_ms")
    b0, b1 = right.get("valid_from_ms"), right.get("valid_to_ms")
    if None not in (a0, a1, b0, b1) and (a1 < b0 or b1 < a0):
        return "different_time_scope"
    if (
        left["attribute_type"] == "quantity"
        and isinstance(left["value"], Mapping)
        and isinstance(right["value"], Mapping)
    ):
        if left["value"].get("normalized_unit") != right["value"].get(
            "normalized_unit"
        ):
            return "unresolved_comparability"
    if (
        left["attribute_type"] in {"participant", "location"}
        and left["value"] != right["value"]
    ):
        return "unresolved_identity"
    return "contradiction"


def _same_value(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    if left["attribute_type"] == right["attribute_type"] == "quantity":
        first, second = left["value"], right["value"]
        if isinstance(first, Mapping) and isinstance(second, Mapping):
            return (first.get("normalized_unit"), first.get("normalized_value")) == (
                second.get("normalized_unit"),
                second.get("normalized_value"),
            )
    return left["value"] == right["value"]


class EventDossierStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _revision(self, namespace, dossier_id, revision, *, principal_id, scopes):
        _access(namespace, principal_id, scopes)
        row = self.conn.execute(
            "SELECT owner,revision FROM event_dossiers WHERE namespace=? AND dossier_id=?",
            [namespace, dossier_id],
        ).fetchone()
        if not row:
            raise EventDossierError("not_found", "dossier is unavailable")
        if "operator" not in scopes and row[0] != principal_id:
            raise EventDossierError(
                "unauthorized", "current dossier ownership is required"
            )
        selected = row[1] if revision is None else revision
        if type(selected) is not int or selected < 1:
            raise EventDossierError("invalid_request", "revision must be positive")
        content = self.conn.execute(
            "SELECT content_json FROM event_dossier_revisions WHERE dossier_id=? AND revision=?",
            [dossier_id, selected],
        ).fetchone()
        if not content:
            raise EventDossierError("not_found", "dossier revision is unavailable")
        dossier = json.loads(content[0])
        current_availability = []
        for source in dossier["sources"]:
            _source_access(source.get("document_id"), scopes)
            retained = (
                self.conn.execute(
                    "SELECT 1 FROM document_revision_records WHERE revision_id=? AND committed_watermark IS NOT NULL",
                    [source["document_revision_id"]],
                ).fetchone()
                if _has_revision_table(self.conn)
                else None
            )
            current_availability.append(
                {
                    "document_revision_id": source["document_revision_id"],
                    "retained_now": bool(retained),
                }
            )
        # Recheck event access and existence; a deleted/revoked event is not
        # silently replaced by a title match or another event identity.
        if not EventKnowledgeStore(self.conn, initialize=False).get(
            namespace, dossier["event_id"], scopes={"knowledge:event:read"}
        ):
            raise EventDossierError("not_found", "bound event is unavailable")
        return {**dossier, "current_availability": current_availability}

    def create(
        self,
        namespace: str,
        request_key: str,
        event_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        overrides: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        _access(namespace, principal_id, scopes, write=True)
        _text(request_key, "request_key")
        _text(event_id, "event_id")
        request_hash = _hash(
            [namespace, principal_id, request_key, event_id, overrides]
        )
        prior = self.conn.execute(
            "SELECT dossier_id,request_hash FROM event_dossiers WHERE namespace=? AND owner=? AND request_key=?",
            [namespace, principal_id, request_key],
        ).fetchone()
        if prior:
            if prior[1] != request_hash:
                raise EventDossierError(
                    "idempotency_conflict", "request key identifies another dossier"
                )
            return {
                **self._revision(
                    namespace, prior[0], 1, principal_id=principal_id, scopes=scopes
                ),
                "idempotent": True,
            }
        dossier_id = (
            "event-dossier:" + _hash([namespace, principal_id, request_key])[:32]
        )
        value = self._assemble(
            namespace, dossier_id, event_id, 1, overrides, principal_id, scopes
        )
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT INTO event_dossiers VALUES (?,?,?,?,?,?,?)",
                [
                    dossier_id,
                    namespace,
                    event_id,
                    principal_id,
                    request_key,
                    1,
                    request_hash,
                ],
            )
            self.conn.execute(
                "INSERT INTO event_dossier_revisions VALUES (?,?,?,?,?)",
                [dossier_id, 1, _json(value), _hash(value), value["created_at_ms"]],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return value

    def revise(
        self,
        namespace: str,
        dossier_id: str,
        expected_revision: int,
        *,
        principal_id: str,
        scopes: set[str],
        overrides: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        _access(namespace, principal_id, scopes, write=True)
        prior = self._revision(
            namespace, dossier_id, None, principal_id=principal_id, scopes=scopes
        )
        if prior["revision"] != expected_revision:
            raise EventDossierError(
                "revision_conflict", "inspect the current dossier revision"
            )
        value = self._assemble(
            namespace,
            dossier_id,
            prior["event_id"],
            expected_revision + 1,
            overrides,
            principal_id,
            scopes,
        )
        self.conn.execute("BEGIN")
        try:
            changed = self.conn.execute(
                "UPDATE event_dossiers SET revision=revision+1 WHERE dossier_id=? AND revision=? RETURNING revision",
                [dossier_id, expected_revision],
            ).fetchone()
            if not changed:
                raise EventDossierError(
                    "revision_conflict", "dossier changed concurrently"
                )
            self.conn.execute(
                "INSERT INTO event_dossier_revisions VALUES (?,?,?,?,?)",
                [
                    dossier_id,
                    value["revision"],
                    _json(value),
                    _hash(value),
                    value["created_at_ms"],
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return value

    def _assemble(
        self, namespace, dossier_id, event_id, revision, overrides, principal_id, scopes
    ):
        event_store = EventKnowledgeStore(self.conn, initialize=False)
        event = event_store.get(namespace, event_id, scopes={"knowledge:event:read"})
        if not event:
            raise EventDossierError("not_found", "existing event identity is required")
        mentions = self.conn.execute(
            "SELECT mention_id,document_revision_id,language,original_text,lifecycle,confidence FROM event_mentions WHERE namespace=? AND event_id=? ORDER BY mention_id LIMIT ?",
            [namespace, event_id, MAX_SOURCES + 1],
        ).fetchall()
        if len(mentions) > MAX_SOURCES:
            raise EventDossierError(
                "budget_exceeded", "event has more than 200 mentions"
            )
        count = self.conn.execute(
            "SELECT count(*) FROM event_account_current c JOIN event_account_revisions r USING(account_revision_id) WHERE r.namespace=? AND r.event_id=?",
            [namespace, event_id],
        ).fetchone()[0]
        if count > MAX_ACCOUNTS:
            raise EventDossierError(
                "budget_exceeded", "event has more than 200 accounts"
            )
        accounts = event_store.accounts(
            namespace, event_id, scopes={"knowledge:event:read"}, include_retracted=True
        )
        refs = {row[1] for row in mentions}
        account_refs = {}
        for account in accounts:
            resolved = sorted(
                {
                    ref
                    for evidence in account["evidence"]
                    if isinstance(evidence, Mapping)
                    if (ref := _ref_from_evidence(evidence))
                }
            )
            account_refs[account["account_id"]] = resolved
            refs.update(resolved)
        if len(refs) > MAX_SOURCES:
            raise EventDossierError(
                "budget_exceeded", "dossier has more than 200 source revisions"
            )
        if not isinstance(overrides, (list, tuple)) or len(overrides) > MAX_SOURCES:
            raise EventDossierError(
                "invalid_request", "membership overrides must be bounded"
            )
        decisions = {}
        for raw in overrides:
            if not isinstance(raw, Mapping):
                raise EventDossierError(
                    "invalid_request", "membership override must be an object"
                )
            ref = _text(raw.get("document_revision_id"), "document_revision_id")
            if ref not in refs or ref in decisions:
                raise EventDossierError(
                    "invalid_membership",
                    "override must name one linked source revision",
                )
            decision = raw.get("decision")
            if decision not in {"include", "exclude", "ambiguous"}:
                raise EventDossierError(
                    "invalid_membership", "decision must include, exclude or ambiguous"
                )
            rationale = _text(raw.get("rationale"), "membership rationale", 2000)
            reviewer = (
                _text(raw.get("reviewed_by"), "reviewed_by")
                if decision == "ambiguous"
                else raw.get("reviewed_by")
            )
            if decision == "ambiguous" and reviewer != principal_id:
                raise EventDossierError(
                    "invalid_membership",
                    "ambiguous membership review must name the current principal",
                )
            decisions[ref] = {
                "decision": decision,
                "rationale": rationale,
                "reviewed_by": reviewer,
            }
        sources = [
            self._capture_source(ref, decisions.get(ref), scopes)
            for ref in sorted(refs)
        ]
        source_map = {source["document_revision_id"]: source for source in sources}
        included = {
            source["document_revision_id"]
            for source in sources
            if source["membership"]["decision"] == "include"
        }
        captured_accounts = []
        for account in accounts:
            refs_for_account = account_refs[account["account_id"]]
            captured_accounts.append(
                {
                    **account,
                    "source_revision_ids": refs_for_account,
                    "included_source_revision_ids": [
                        ref for ref in refs_for_account if ref in included
                    ],
                    "source_locators": [
                        dict(evidence)
                        for evidence in account["evidence"]
                        if isinstance(evidence, Mapping)
                    ],
                    "coverage": "retained_sources"
                    if refs_for_account
                    and all(
                        source_map[ref]["status"] == "available"
                        for ref in refs_for_account
                    )
                    else "incomplete",
                }
            )
        active_accounts = [
            account
            for account in captured_accounts
            if account["lifecycle"] == "active"
            and (
                not account["source_revision_ids"]
                or set(account["source_revision_ids"]) & included
            )
        ]
        groups = {}
        for account in active_accounts:
            key = (account["attribute_type"], account.get("role"))
            groups.setdefault(key, []).append(account)
        account_groups = []
        for key, group in sorted(groups.items(), key=lambda item: str(item[0])):
            documents = sorted(
                {
                    source_map[ref]["document_id"]
                    for account in group
                    for ref in account["source_revision_ids"]
                    if ref in included and source_map[ref]["document_id"]
                }
            )
            origins = origin_summary(self.conn, documents)
            conflicts = []
            for index, first in enumerate(group):
                for second in group[index + 1 :]:
                    if not _same_value(first, second):
                        conflicts.append(
                            {
                                "left_account_revision_id": first[
                                    "account_revision_id"
                                ],
                                "right_account_revision_id": second[
                                    "account_revision_id"
                                ],
                                "classification": _time_relation(first, second),
                                "left_locators": first["source_locators"],
                                "right_locators": second["source_locators"],
                            }
                        )
            account_groups.append(
                {
                    "attribute_type": key[0],
                    "role": key[1],
                    "accounts": group,
                    "publication_count": origins["publication_count"],
                    "supported_independent_origin_count": origins[
                        "known_independent_count"
                    ],
                    "probable_origin_count": origins["probable_origin_count"],
                    "lineage": origins,
                    "conflicts": conflicts,
                    "uncertainty": "incomplete_lineage_or_sources"
                    if origins["unresolved_count"]
                    or any(a["coverage"] != "retained_sources" for a in group)
                    else "bounded_origin_evidence",
                }
            )
        mention_items = [
            {
                "mention_id": row[0],
                "document_revision_id": row[1],
                "language": row[2],
                "text": row[3],
                "lifecycle": row[4],
                "confidence": row[5],
            }
            for row in mentions
        ]
        history = event_store.get(
            namespace, event_id, scopes={"knowledge:event:read"}, include_history=True
        )["revisions"]
        canonical_operations = []
        try:
            rows = self.conn.execute(
                "SELECT operation_id,action,before_json,after_json,status,created_at_ms,reversed_at_ms FROM canonical_event_operations WHERE namespace=? ORDER BY created_at_ms DESC LIMIT 101",
                [namespace],
            ).fetchall()
            if len(rows) > 100:
                raise EventDossierError(
                    "budget_exceeded", "event resolution history exceeds 100 operations"
                )
            for row in rows:
                if event_id in row[2] or event_id in row[3]:
                    canonical_operations.append(
                        {
                            "operation_id": row[0],
                            "action": row[1],
                            "status": row[4],
                            "created_at_ms": row[5],
                            "reversed_at_ms": row[6],
                        }
                    )
        except Exception as exc:
            if isinstance(exc, EventDossierError):
                raise
        now = self.now()
        return {
            "contract": CONTRACT,
            "dossier_id": dossier_id,
            "namespace": namespace,
            "event_id": event_id,
            "event_revision_id": event["revision_id"],
            "revision": revision,
            "owner": principal_id,
            "event": event,
            "event_history": [
                {
                    "revision_id": item["revision_id"],
                    "revision": item["revision"],
                    "lifecycle": item["lifecycle"],
                    "observed_at_ms": item["observed_at_ms"],
                }
                for item in history
            ],
            "resolution_operations": canonical_operations,
            "membership_overrides": [
                {"document_revision_id": ref, **decision}
                for ref, decision in sorted(decisions.items())
            ],
            "mentions": mention_items,
            "sources": sources,
            "account_groups": account_groups,
            "coverage": "complete_for_retained_sources"
            if all(
                source["status"] == "available"
                and source["membership"]["decision"] != "ambiguous"
                for source in sources
            )
            and all(
                group["uncertainty"] == "bounded_origin_evidence"
                for group in account_groups
            )
            else "incomplete",
            "created_at_ms": now,
            "limitations": [
                "Origin counts are not truth votes",
                "Missing source lineage and partial text coverage remain explicit",
                "Fixture data does not establish live-provider or independent-human validation",
            ],
        }

    def _capture_source(self, revision_id, override, scopes):
        row = (
            self.conn.execute(
                "SELECT document_id,revision,payload_json,observed_at_ms,lifecycle,committed_watermark FROM document_revision_records WHERE revision_id=?",
                [revision_id],
            ).fetchone()
            if _has_revision_table(self.conn)
            else None
        )
        decision = override or {
            "decision": "include",
            "rationale": "linked by existing event/account record",
            "reviewed_by": None,
        }
        if row is None or row[5] is None:
            return {
                "document_revision_id": revision_id,
                "document_id": None,
                "status": "unavailable",
                "unavailable_reason": "retained_revision_missing",
                "membership": decision,
                "representation": None,
                "text_coverage": "unavailable",
            }
        _source_access(row[0], scopes)
        payload = json.loads(row[2])
        metadata = payload.get("metadata") or {}
        representation = (
            metadata.get("acquisition_representation")
            or metadata.get("representation")
            or payload.get("source_type")
            or "unknown"
        )
        coverage = (
            metadata.get("full_text_status")
            or metadata.get("text_coverage")
            or ("full_text" if payload.get("content") else "metadata_only")
        )
        return {
            "document_revision_id": revision_id,
            "document_id": row[0],
            "status": "available",
            "unavailable_reason": None,
            "revision": row[1],
            "observed_at_ms": row[3],
            "lifecycle": row[4],
            "source_id": payload.get("source_id"),
            "url": payload.get("url"),
            "title": payload.get("title"),
            "provider": metadata.get("provider") or metadata.get("source_pack_id"),
            "representation": representation,
            "text_coverage": coverage,
            "membership": decision,
        }

    def inspect(
        self,
        namespace: str,
        dossier_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        revision: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise EventDossierError(
                "invalid_request", "offset and limit must be bounded"
            )
        dossier = self._revision(
            namespace, dossier_id, revision, principal_id=principal_id, scopes=scopes
        )
        return {
            **dossier,
            "sources": dossier["sources"][offset : offset + limit],
            "account_groups": dossier["account_groups"][offset : offset + limit],
            "page": {
                "offset": offset,
                "limit": limit,
                "total_sources": len(dossier["sources"]),
                "total_account_groups": len(dossier["account_groups"]),
            },
        }

    def timeline(
        self,
        namespace: str,
        dossier_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        revision: int | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        dossier = self._revision(
            namespace, dossier_id, revision, principal_id=principal_id, scopes=scopes
        )
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 50
        ):
            raise EventDossierError(
                "invalid_request", "offset and limit must be bounded"
            )
        entries = (
            [{"kind": "event_revision", **item} for item in dossier["event_history"]]
            + [
                {
                    "kind": "source_revision",
                    "document_revision_id": item["document_revision_id"],
                    "document_id": item.get("document_id"),
                    "observed_at_ms": item.get("observed_at_ms"),
                    "status": item["status"],
                }
                for item in dossier["sources"]
            ]
            + [
                {
                    "kind": "account_revision",
                    "account_revision_id": account["account_revision_id"],
                    "observed_at_ms": account["observed_at_ms"],
                    "attribute_type": account["attribute_type"],
                }
                for group in dossier["account_groups"]
                for account in group["accounts"]
            ]
            + [
                {"kind": "resolution_operation", **item}
                for item in dossier["resolution_operations"]
            ]
        )
        entries.sort(
            key=lambda item: (
                item.get("observed_at_ms") or item.get("created_at_ms") or 0,
                item["kind"],
                str(
                    item.get("revision_id")
                    or item.get("document_revision_id")
                    or item.get("account_revision_id")
                    or item.get("operation_id")
                ),
            )
        )
        return {
            "contract": "noesis-event-dossier-timeline-v1",
            "dossier_id": dossier_id,
            "revision": dossier["revision"],
            "items": entries[offset : offset + limit],
            "page": {"offset": offset, "limit": limit, "total": len(entries)},
        }

    def compare(
        self,
        namespace: str,
        dossier_id: str,
        from_revision: int,
        to_revision: int,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        before = self._revision(
            namespace,
            dossier_id,
            from_revision,
            principal_id=principal_id,
            scopes=scopes,
        )
        after = self._revision(
            namespace, dossier_id, to_revision, principal_id=principal_id, scopes=scopes
        )

        def accounts(dossier):
            return {
                account["account_id"]: account
                for group in dossier["account_groups"]
                for account in group["accounts"]
            }

        left, right = accounts(before), accounts(after)
        changed = []
        for identity in sorted(set(left) | set(right)):
            old, new = left.get(identity), right.get(identity)
            if old == new:
                continue
            changed.append(
                {
                    "account_id": identity,
                    "before": old,
                    "after": new,
                    "classification": "addition"
                    if old is None
                    else "removal"
                    if new is None
                    else "correction",
                }
            )
        left_sources = {
            item["document_revision_id"]: item for item in before["sources"]
        }
        right_sources = {
            item["document_revision_id"]: item for item in after["sources"]
        }
        source_changes = [
            {
                "document_revision_id": identity,
                "before": left_sources.get(identity),
                "after": right_sources.get(identity),
            }
            for identity in sorted(set(left_sources) | set(right_sources))
            if left_sources.get(identity) != right_sources.get(identity)
        ]
        conflicts = [
            conflict
            for group in after["account_groups"]
            for conflict in group["conflicts"]
        ]
        core = {
            "contract": COMPARISON_CONTRACT,
            "namespace": namespace,
            "dossier_id": dossier_id,
            "event_id": before["event_id"],
            "from_revision": from_revision,
            "to_revision": to_revision,
            "event_before": before["event"],
            "event_after": after["event"],
            "changed_accounts": changed,
            "source_changes": source_changes,
            "unresolved_conflicts": conflicts,
            "origin_evidence_before": [
                group["lineage"] for group in before["account_groups"]
            ],
            "origin_evidence_after": [
                group["lineage"] for group in after["account_groups"]
            ],
            "coverage_before": before["coverage"],
            "coverage_after": after["coverage"],
        }
        return {**core, "sha256": _hash(core)}

    def export_comparison(
        self,
        namespace: str,
        dossier_id: str,
        from_revision: int,
        to_revision: int,
        *,
        principal_id: str,
        scopes: set[str],
    ) -> dict[str, Any]:
        comparison = self.compare(
            namespace,
            dossier_id,
            from_revision,
            to_revision,
            principal_id=principal_id,
            scopes=scopes,
        )
        lines = [
            f"# Event dossier {dossier_id}: revision {from_revision} to {to_revision}",
            "",
            f"Changed accounts: {len(comparison['changed_accounts'])}; source changes: {len(comparison['source_changes'])}; unresolved conflicts: {len(comparison['unresolved_conflicts'])}.",
            "",
        ]
        for item in comparison["changed_accounts"]:
            lines.append(
                f"- {item['classification']} {item['account_id']}: before={_json(item['before'])}; after={_json(item['after'])}"
            )
        for item in comparison["source_changes"]:
            lines.append(
                f"- Source revision {item['document_revision_id']}: before={_json(item['before'])}; after={_json(item['after'])}"
            )
        lines.extend(
            [
                "",
                "## Origin evidence and unresolved conflicts",
                "",
                _json(comparison["origin_evidence_before"]),
                _json(comparison["origin_evidence_after"]),
                _json(comparison["unresolved_conflicts"]),
                "",
                "Counts describe publications and supported origins, not truth by majority.",
            ]
        )
        return {
            "contract": "noesis-event-dossier-export-v1",
            "comparison": comparison,
            "markdown": "\n".join(lines),
            "sha256": _hash(comparison),
        }

    def create_report(
        self,
        namespace: str,
        request_key: str,
        dossier_id: str,
        *,
        principal_id: str,
        scopes: set[str],
        revision: int | None = None,
    ) -> dict[str, Any]:
        from src.kb.authored_reports import AuthoredReportStore

        dossier = self._revision(
            namespace, dossier_id, revision, principal_id=principal_id, scopes=scopes
        )
        assertions, bibliography, generations = [], {}, []
        dependency_count = 0
        for group in dossier["account_groups"]:
            for account in group["accounts"]:
                if account["lifecycle"] != "active":
                    continue
                refs = account["included_source_revision_ids"]
                if not refs:
                    continue
                dependencies, citations = [], []
                for ref in refs:
                    source = next(
                        (
                            item
                            for item in dossier["sources"]
                            if item["document_revision_id"] == ref
                        ),
                        None,
                    )
                    if not source or source["status"] != "available":
                        raise EventDossierError(
                            "source_revision_unavailable",
                            "report needs retained source revisions",
                        )
                    bibliography[ref] = {
                        "id": ref,
                        "text": f"{source.get('title') or source['document_id']} ({source.get('url') or 'URL unavailable'}), revision {ref}; representation {source['representation']}; text coverage {source['text_coverage']}.",
                    }
                    citations.append(ref)
                    dependencies.append(
                        {
                            "kind": "source",
                            "id": source["document_id"],
                            "revision": ref,
                            "namespace": namespace,
                            "locator": {
                                "document_id": source["document_id"],
                                "revision_id": ref,
                            },
                        }
                    )
                    dependency_count += 1
                    if dependency_count > 1000:
                        raise EventDossierError(
                            "budget_exceeded",
                            "report has more than 1000 source dependencies",
                        )
                    row = self.conn.execute(
                        "SELECT committed_watermark FROM document_revision_records WHERE revision_id=?",
                        [ref],
                    ).fetchone()
                    generations.append(int(row[0]))
                assertions.append(
                    {
                        "id": account["account_id"],
                        "text": f"Recorded account for {account['attribute_type']}: {_json(account['value'])}. This is an attributed account, not an adjudicated fact.",
                        "kind": "sourced",
                        "citations": citations,
                        "dependencies": dependencies,
                    }
                )
        if not assertions:
            raise EventDossierError(
                "source_revision_unavailable",
                "no source-linked accounts are available for report",
            )
        content = {
            "title": f"Event dossier: {dossier['event_id']} revision {dossier['revision']}",
            "snapshot": {
                "id": dossier_id + ":" + str(dossier["revision"]),
                "generations": {namespace: max(generations)},
            },
            "sections": [
                {
                    "id": "accounts",
                    "title": "Attributed accounts",
                    "assertions": assertions,
                }
            ],
            "bibliography": list(bibliography.values()),
            "limitations": [
                *dossier["limitations"],
                "Source dependency changes require explicit assessment and author review.",
            ],
        }
        report = AuthoredReportStore(self.conn).create(
            namespace, request_key, content, principal_id=principal_id, scopes=scopes
        )
        return {
            "report": report,
            "dossier_id": dossier_id,
            "dossier_revision": dossier["revision"],
            "subscription_operations": [
                "subscribe_cited_evidence",
                "evaluate_cited_evidence_subscription",
                "poll_cited_evidence_alerts",
            ],
            "assessment_operation": "assess_authored_report_changes",
        }
