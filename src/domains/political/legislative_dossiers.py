"""Versioned legislative dossiers over committed official document revisions.

The dossier records source assertions. It does not determine legal effect from
document titles, inferred dates, or similarity between jurisdictions.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from src.kb.evidence_changes import EvidenceResolver
from src.kb.temporal import parse_source_time

DOSSIER_CONTRACT = "noesis-legislative-dossier-v1"
TIMELINE_CONTRACT = "noesis-legislative-timeline-v1"
COMPARISON_CONTRACT = "noesis-legislative-comparison-v1"
READ_SCOPE = "knowledge:political:dossier:read"
WRITE_SCOPE = "knowledge:political:dossier:write"
STAGES = ("proposal", "amendment", "vote", "adoption", "publication", "commencement", "repeal")
_TYPE_STAGE = {
    "proposal": "proposal", "amendment": "amendment", "roll_call": "vote",
    "plenary_record": "vote", "regulation": "publication",
    "directive": "publication", "decision": "publication",
}
_DDL = """
CREATE TABLE IF NOT EXISTS legislative_dossiers(
 dossier_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 request_hash TEXT NOT NULL, revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS legislative_dossier_revisions(
 dossier_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 content_hash TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(dossier_id,revision));
"""


class DossierError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value: Any, field: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DossierError("invalid_input", f"{field} must be nonempty text within {limit} characters")
    return value.strip()


def _identifier(value: Any, field: str) -> str:
    raw = _text(value, field, 256)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:./_-]*", raw):
        raise DossierError("invalid_identifier", f"{field} contains unsupported characters")
    return raw.casefold()


def _date(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    at_ms, provenance = parse_source_time(value, field=field)
    return {"at_ms": at_ms, "source_time": provenance}


def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _political(metadata: Mapping[str, Any]) -> dict[str, Any]:
    value = metadata.get("political") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


class LegislativeDossierStore:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(namespace: str, owner: str, principal_id: str, scopes: set[str], *, write=False) -> None:
        needed = WRITE_SCOPE if write else READ_SCOPE
        ns = f"namespace:{namespace}:{'write' if write else 'read'}"
        if not principal_id or ("operator" not in scopes and (owner != principal_id or needed not in scopes or ns not in scopes)):
            raise DossierError("unauthorized", "dossier ownership and namespace scope are required")

    def _source(self, ref: Mapping[str, Any], scopes: set[str]) -> dict[str, Any]:
        if not isinstance(ref, Mapping) or set(ref) != {"document_id", "revision_id"}:
            raise DossierError("invalid_source", "source requires document_id and revision_id")
        document_id = _text(ref["document_id"], "document_id", 512)
        revision_id = _text(ref["revision_id"], "revision_id", 512)
        if "operator" not in scopes and f"document:{document_id}:read" not in scopes:
            raise DossierError("unauthorized", "current document read access is required")
        row = self.conn.execute(
            "SELECT revision,source_id,payload_json,content_hash,payload_hash,observed_at_ms,lifecycle "
            "FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
            [document_id, revision_id],
        ).fetchone()
        if row is None:
            raise DossierError("source_unavailable", "committed source revision is unavailable")
        if row[6] in {"deleted", "retracted"}:
            raise DossierError("source_unavailable", "withdrawn source revision cannot support a new stage")
        payload = json.loads(row[2])
        metadata = _metadata(payload)
        source_id = str(row[1] or metadata.get("source_manifest_id") or "")
        if source_id not in {"de-bundestag-dip", "eu-eurlex-regulatory"}:
            raise DossierError("unsupported_source", "dossier needs acquired Bundestag DIP or EUR-Lex evidence")
        jurisdiction = str(metadata.get("jurisdiction") or "").upper()
        if (source_id == "de-bundestag-dip" and jurisdiction != "DE") or (source_id == "eu-eurlex-regulatory" and jurisdiction != "EU"):
            raise DossierError("source_mismatch", "provider and jurisdiction disagree")
        raw_identifier = _text(metadata.get("official_identifier"), "official identifier", 256)
        locator = _text(payload.get("url") or metadata.get("canonical_url"), "source locator", 8192)
        if not locator.startswith("https://"):
            raise DossierError("invalid_source", "source locator must be HTTPS")
        return {
            "document_id": document_id, "revision_id": revision_id,
            "revision": int(row[0]), "source_id": source_id,
            "content_hash": row[3], "payload_hash": row[4],
            "observed_at_ms": int(row[5]), "jurisdiction": jurisdiction,
            "raw_identifier": raw_identifier,
            "normalized_document_id": _identifier(raw_identifier, "official identifier"),
            "source_locator": locator,
            "title": str(payload.get("title") or ""),
            "content": str(payload.get("content") or ""),
            "document_type": str(metadata.get("document_type") or ""),
            "published_at": _date(payload.get("created_at"), "published_at"),
            "asserted_effective_from": _date(metadata.get("effective_from"), "effective_from"),
            "political": _political(metadata),
        }

    def _stage(self, source: Mapping[str, Any], procedure_id: str) -> tuple[dict[str, Any], bool]:
        political = source["political"]
        typed_identifiers = [
            ("procedure_id", political.get("procedure_id")),
            ("proposal_id", political.get("proposal_id")),
            ("instrument_id", political.get("instrument_id")),
        ]
        linked_ids = political.get("linked_procedure_ids") or []
        if not isinstance(linked_ids, list) or len(linked_ids) > 20:
            raise DossierError("invalid_source", "linked procedure identifiers must be bounded")
        typed_identifiers.extend(("linked_procedure_id", value) for value in linked_ids)
        normalized = []
        link_basis = None
        for kind, raw in typed_identifiers:
            if raw:
                value = _identifier(raw, "procedure identifier")
                normalized.append(value)
                if value == procedure_id and link_basis is None:
                    link_basis = kind
        linked = link_basis is not None
        stage = _TYPE_STAGE.get(source["document_type"])
        if not stage:
            raise DossierError("unsupported_stage", "document type has no legislative stage mapping")
        citation = {
            "namespace": None, "document_id": source["document_id"],
            "revision_id": source["revision_id"], "revision": source["revision"],
            "source_id": source["source_id"], "url": source["source_locator"],
            "content_hash": source["content_hash"], "payload_hash": source["payload_hash"],
        }
        legal_events = political.get("legal_events") or []
        if not isinstance(legal_events, list) or len(legal_events) > 20:
            raise DossierError("invalid_source", "legal events must be a bounded list")
        related = political.get("related_procedures") or []
        if not isinstance(related, list) or len(related) > 20:
            raise DossierError("invalid_source", "related procedures must be a bounded list")
        relationships = []
        for relation in related:
            if not isinstance(relation, Mapping) or relation.get("kind") not in {"implements", "transposes", "amends", "cites"}:
                raise DossierError("invalid_source", "related procedure kind is unsupported")
            quote = _text(relation.get("source_quote"), "relationship source quote", 1000)
            if quote not in source["content"]:
                raise DossierError("unsupported_relation", "relationship quote is absent from pinned source text")
            target_jurisdiction = _text(relation.get("jurisdiction"), "related jurisdiction", 16).upper()
            if target_jurisdiction not in {"DE", "EU"}:
                raise DossierError("unsupported_relation", "related jurisdiction must be DE or EU")
            relationships.append({
                "kind": relation["kind"], "jurisdiction": target_jurisdiction,
                "procedure_id": _identifier(relation.get("procedure_id"), "related procedure identifier"),
                "source_quote": quote, "citation": citation,
            })
        supported_events = []
        for event in legal_events:
            if not isinstance(event, Mapping) or event.get("kind") not in {"adoption", "commencement", "amendment", "repeal"}:
                raise DossierError("invalid_source", "legal event kind is unsupported")
            quote = _text(event.get("source_quote"), "source quote", 1000)
            if quote not in source["content"]:
                raise DossierError("unsupported_legal_fact", "legal event quote is absent from pinned source text")
            if str(event.get("jurisdiction") or "").upper() != source["jurisdiction"]:
                raise DossierError("unsupported_legal_fact", "legal event jurisdiction is unbound")
            supported_events.append({
                "kind": event["kind"], "date": _date(event.get("date"), "legal event date"),
                "section": event.get("section"), "source_quote": quote,
                "jurisdiction": source["jurisdiction"], "citation": citation,
            })
        result = {
            "stage_id": _hash([source["document_id"], source["revision_id"], stage])[:32],
            "stage": stage, "jurisdiction": source["jurisdiction"],
            "procedure_identifiers": normalized,
            "link_basis": link_basis,
            "raw_instrument_id": political.get("instrument_id"),
            "normalized_instrument_id": (
                _identifier(political["instrument_id"], "instrument identifier")
                if political.get("instrument_id") else None
            ),
            "raw_document_id": source["raw_identifier"],
            "normalized_document_id": source["normalized_document_id"],
            "title": source["title"], "event_at": _date(political.get("event_at"), "event_at"),
            "published_at": source["published_at"],
            "observed_at_ms": source["observed_at_ms"],
            "asserted_effective_from": source["asserted_effective_from"],
            "legal_events": supported_events,
            "attributable_relationships": relationships,
            "citation": citation,
        }
        return result, linked

    def _state(self, namespace: str, dossier_id: str, revision: int | None = None) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT r.content_json FROM legislative_dossiers d JOIN legislative_dossier_revisions r "
            "ON d.dossier_id=r.dossier_id WHERE d.namespace=? AND d.dossier_id=? "
            "AND r.revision=coalesce(?,d.revision)", [namespace, dossier_id, revision],
        ).fetchone()
        if not row:
            raise DossierError("dossier_unavailable", "dossier revision is unavailable")
        return json.loads(row[0])

    def save(
        self, namespace: str, request_key: str, jurisdiction: str, procedure_id: str,
        source_refs: Sequence[Mapping[str, Any]], *, principal_id: str,
        scopes: set[str], dossier_id: str | None = None, expected_revision: int | None = None,
    ) -> dict[str, Any]:
        namespace = _text(namespace, "namespace", 128)
        principal_id = _text(principal_id, "principal", 256)
        request_key = _text(request_key, "request key", 256)
        jurisdiction = _text(jurisdiction, "jurisdiction", 16).upper()
        if jurisdiction not in {"DE", "EU"}:
            raise DossierError("invalid_jurisdiction", "legislative dossiers support DE or EU jurisdiction")
        raw_procedure_id = _text(procedure_id, "procedure identifier", 256)
        procedure_id = _identifier(raw_procedure_id, "procedure identifier")
        if not isinstance(source_refs, (list, tuple)) or not 1 <= len(source_refs) <= 100:
            raise DossierError("invalid_sources", "one to 100 pinned source revisions are required")
        self._authorize(namespace, principal_id, principal_id, scopes, write=True)
        sources = [self._source(ref, scopes) for ref in source_refs]
        if len({s["document_id"] for s in sources}) != len(sources):
            raise DossierError("duplicate_source", "each document can appear once in a dossier revision")
        if any(s["jurisdiction"] != jurisdiction for s in sources):
            raise DossierError("jurisdiction_mismatch", "cross-jurisdiction evidence requires a separate attributable relation")
        fixture_count = sum(bool(s["political"].get("fixture")) for s in sources)
        stages, candidates = [], []
        for source in sources:
            stage, linked = self._stage(source, procedure_id)
            stage["citation"]["namespace"] = namespace
            for event in stage["legal_events"]:
                event["citation"]["namespace"] = namespace
            (stages if linked else candidates).append(stage)
        stages.sort(key=lambda x: (x["observed_at_ms"], x["stage"], x["citation"]["document_id"]))
        candidates.sort(key=lambda x: (x["observed_at_ms"], x["citation"]["document_id"]))
        if not stages:
            raise DossierError("unlinked_sources", "at least one source must explicitly identify the procedure")
        present_stages = {s["stage"] for s in stages}
        present_stages.update(event["kind"] for stage in stages for event in stage["legal_events"])
        if any(stage["asserted_effective_from"] is not None for stage in stages):
            present_stages.add("commencement")
        request = [namespace, principal_id, jurisdiction, procedure_id, request_key]
        dossier_id = dossier_id or "legislative-dossier:" + _hash(request)[:32]
        prior = self.conn.execute(
            "SELECT owner,request_hash,revision FROM legislative_dossiers WHERE namespace=? AND dossier_id=?",
            [namespace, dossier_id],
        ).fetchone()
        if prior:
            self._authorize(namespace, prior[0], principal_id, scopes, write=True)
            if prior[1] != _hash(request):
                raise DossierError("identity_conflict", "dossier identity belongs to another procedure")
            if expected_revision is not None and expected_revision != prior[2]:
                raise DossierError("revision_conflict", "dossier revision changed")
        elif expected_revision is not None:
            raise DossierError("revision_conflict", "new dossier has no expected revision")
        payload = {
            "contract": DOSSIER_CONTRACT, "dossier_id": dossier_id,
            "namespace": namespace, "owner": principal_id, "jurisdiction": jurisdiction,
            "procedure_id": procedure_id, "raw_procedure_id": raw_procedure_id,
            "procedure_identity_state": (
                "instrument_anchor_only" if all(s["link_basis"] == "instrument_id" for s in stages)
                else "source_linked_procedure"
            ),
            "stages": stages, "review_candidates": candidates,
            "missing_stages": [stage for stage in STAGES if stage not in present_stages],
            "source_count": len(sources),
            "evidence_origin": (
                "fixture" if fixture_count == len(sources)
                else "mixed" if fixture_count else "acquired_official_records"
            ),
        }
        digest = _hash(payload)
        if prior:
            current = self._state(namespace, dossier_id)
            if _hash({key: current[key] for key in payload}) == digest:
                return {**current, "idempotent": True}
        revision = int(prior[2]) + 1 if prior else 1
        state = {**payload, "revision": revision, "created_at_ms": self.now()}
        self.conn.execute("BEGIN")
        try:
            if prior:
                updated = self.conn.execute(
                    "UPDATE legislative_dossiers SET revision=? WHERE dossier_id=? AND revision=? RETURNING revision",
                    [revision, dossier_id, prior[2]],
                ).fetchone()
                if not updated:
                    raise DossierError("revision_conflict", "dossier revision changed")
            else:
                self.conn.execute(
                    "INSERT INTO legislative_dossiers VALUES (?,?,?,?,?)",
                    [dossier_id, namespace, principal_id, _hash(request), revision],
                )
            self.conn.execute(
                "INSERT INTO legislative_dossier_revisions VALUES (?,?,?,?,?)",
                [dossier_id, revision, _json(state), digest, state["created_at_ms"]],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**state, "idempotent": False}

    def inspect(self, namespace: str, dossier_id: str, *, principal_id: str, scopes: set[str],
                revision: int | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise DossierError("invalid_page", "limit must be 1–100 and offset nonnegative")
        current = self._state(namespace, dossier_id)
        self._authorize(namespace, current["owner"], principal_id, scopes)
        state = self._state(namespace, dossier_id, revision)
        for stage in [*state["stages"], *state["review_candidates"]]:
            self._source({k: stage["citation"][k] for k in ("document_id", "revision_id")}, scopes)
        return {**state, "stages": state["stages"][offset:offset + limit],
                "review_candidates": state["review_candidates"][offset:offset + limit],
                "limit": limit, "offset": offset, "total_stages": len(state["stages"]),
                "total_review_candidates": len(state["review_candidates"])}

    def _full(self, namespace: str, dossier_id: str, revision: int, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        state = self._state(namespace, dossier_id, revision)
        self._authorize(namespace, state["owner"], principal_id, scopes)
        for stage in [*state["stages"], *state["review_candidates"]]:
            self._source({k: stage["citation"][k] for k in ("document_id", "revision_id")}, scopes)
        return state

    def timeline(self, namespace: str, dossier_id: str, *, principal_id: str, scopes: set[str],
                 revision: int | None = None, observed_as_of_ms: int | None = None,
                 limit: int = 50, offset: int = 0) -> dict[str, Any]:
        current = self._state(namespace, dossier_id)
        state = self._full(namespace, dossier_id, revision or current["revision"], principal_id, scopes)
        if observed_as_of_ms is not None and (type(observed_as_of_ms) is not int or observed_as_of_ms < 0):
            raise DossierError("invalid_time", "observation cutoff must be nonnegative epoch milliseconds")
        entries = []
        for stage in state["stages"]:
            if observed_as_of_ms is not None and stage["observed_at_ms"] > observed_as_of_ms:
                continue
            entries.append({"event_kind": stage["stage"], "date": stage["event_at"],
                            "published_at": stage["published_at"], "observed_at_ms": stage["observed_at_ms"],
                            "jurisdiction": stage["jurisdiction"], "citation": stage["citation"],
                            "source_stage_id": stage["stage_id"], "legal_fact": False})
            if stage["asserted_effective_from"] is not None:
                entries.append({"event_kind": "commencement", "date": stage["asserted_effective_from"],
                                "published_at": stage["published_at"], "observed_at_ms": stage["observed_at_ms"],
                                "jurisdiction": stage["jurisdiction"], "citation": stage["citation"],
                                "source_stage_id": stage["stage_id"], "legal_fact": True,
                                "source_field": "metadata.effective_from"})
            for event in stage["legal_events"]:
                entries.append({"event_kind": event["kind"], "date": event["date"],
                                "published_at": stage["published_at"], "observed_at_ms": stage["observed_at_ms"],
                                "jurisdiction": event["jurisdiction"], "citation": event["citation"],
                                "source_stage_id": stage["stage_id"], "section": event["section"],
                                "source_quote": event["source_quote"], "legal_fact": True})
        entries.sort(key=lambda e: (e["date"]["at_ms"] if e["date"] else 2**63 - 1,
                                    e["published_at"]["at_ms"] if e["published_at"] else 2**63 - 1,
                                    e["observed_at_ms"], e["event_kind"], e["source_stage_id"]))
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise DossierError("invalid_page", "limit must be 1–100 and offset nonnegative")
        return {"contract": TIMELINE_CONTRACT, "dossier_id": dossier_id, "revision": state["revision"],
                "observed_as_of_ms": observed_as_of_ms, "jurisdiction": state["jurisdiction"],
                "entries": entries[offset:offset + limit], "total": len(entries),
                "limit": limit, "offset": offset,
                "legal_effect_state": "source_supported_events_only" if any(e["legal_fact"] for e in entries) else "unknown"}

    def compare(self, namespace: str, dossier_id: str, before_revision: int, after_revision: int,
                *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        if type(before_revision) is not int or type(after_revision) is not int or before_revision >= after_revision:
            raise DossierError("invalid_revision", "ordered dossier revisions are required")
        before = self._full(namespace, dossier_id, before_revision, principal_id, scopes)
        after = self._full(namespace, dossier_id, after_revision, principal_id, scopes)
        left = {stage["citation"]["document_id"]: stage for stage in before["stages"]}
        right = {stage["citation"]["document_id"]: stage for stage in after["stages"]}
        changes = []
        for document_id in sorted(set(left) | set(right)):
            old, new = left.get(document_id), right.get(document_id)
            if old == new:
                continue
            kind = "added" if old is None else "unavailable" if new is None else "changed"
            changes.append({"kind": kind, "stage": (new or old)["stage"], "document_id": document_id,
                            "before_citation": old["citation"] if old else None,
                            "after_citation": new["citation"] if new else None,
                            "before": old, "after": new})
        return {"contract": COMPARISON_CONTRACT, "dossier_id": dossier_id,
                "before_revision": before_revision, "after_revision": after_revision,
                "changes": changes, "counts": {k: sum(c["kind"] == k for c in changes)
                                              for k in ("added", "changed", "unavailable")},
                "interpretation": None, "comparison_basis": "pinned_official_record_revisions"}

    def export_change_summary(self, namespace: str, dossier_id: str, before_revision: int,
                              after_revision: int, *, principal_id: str, scopes: set[str]) -> dict[str, Any]:
        comparison = self.compare(namespace, dossier_id, before_revision, after_revision,
                                  principal_id=principal_id, scopes=scopes)
        return {**comparison, "report_ready": [
            {"text": f"{change['stage'].capitalize()} stage {change['kind']} in dossier revision {after_revision}.",
             "kind": "recorded_change", "before_citation": change["before_citation"],
             "after_citation": change["after_citation"]}
            for change in comparison["changes"]
        ], "limitations": ["A dossier comparison records acquired source changes; legal interpretation requires review."]}

    def dependencies(self, namespace: str, dossier_id: str, *, principal_id: str,
                     scopes: set[str], revision: int | None = None) -> dict[str, Any]:
        current = self._state(namespace, dossier_id)
        state = self._full(namespace, dossier_id, revision or current["revision"], principal_id, scopes)
        report = []
        project = []
        for stage in state["stages"]:
            citation = stage["citation"]
            locator = {"document_id": citation["document_id"], "revision_id": citation["revision_id"]}
            report.append({"kind": "source", "namespace": namespace, "id": citation["document_id"],
                           "revision": citation["revision_id"], "locator": locator})
            project.append({"kind": "evidence", "namespace": namespace, "id": citation["document_id"],
                            "revision": citation["revision"], "locator": locator})
        evaluations = [EvidenceResolver(self.conn, scopes).compare(dep) for dep in report]
        return {"contract": "noesis-legislative-dependencies-v1", "dossier_id": dossier_id,
                "revision": state["revision"], "report_dependencies": report,
                "project_links": project, "change_evaluations": evaluations,
                "existing_alerts": "use CitationAlertStore on an authored report or project revision"}
