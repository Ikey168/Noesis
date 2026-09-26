"""Public OpenReview round snapshots and source-bound concern comparisons."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any

from src.kb.review_inbox import ReviewInboxStore

SNAPSHOT_CONTRACT = "noesis-openreview-rounds-v1"
ROUND_CONTRACT = "noesis-openreview-round-v1"
COMPARISON_CONTRACT = "noesis-openreview-round-comparison-v1"
READ_SCOPE = "knowledge:openreview:read"
WRITE_SCOPE = "knowledge:openreview:write"
NOTE_TYPES = {"submission", "review", "rebuttal", "decision", "unknown"}
_DDL = """
CREATE TABLE IF NOT EXISTS openreview_round_sets(
 round_set_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 forum_id TEXT NOT NULL,revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS openreview_round_set_revisions(
 round_set_id TEXT NOT NULL,revision BIGINT NOT NULL,content_hash TEXT NOT NULL,
 state_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(round_set_id,revision));
CREATE TABLE IF NOT EXISTS openreview_concern_revisions(
 concern_revision_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,concern_id TEXT NOT NULL,
 revision BIGINT NOT NULL,content_hash TEXT NOT NULL,payload_json TEXT NOT NULL,
 UNIQUE(namespace,concern_id,revision));
"""


class OpenReviewError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value: Any, field: str, limit: int = 500) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise OpenReviewError("invalid_input", f"{field} must be bounded nonempty text")
    return value.strip()


def _metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("metadata") or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise OpenReviewError("source_unavailable", "source metadata is malformed") from exc
    if not isinstance(value, Mapping):
        raise OpenReviewError("source_unavailable", "source metadata is malformed")
    return dict(value)


def _round_from_invitations(invitations: Sequence[str]) -> tuple[str | None, str]:
    matches = set()
    for invitation in invitations:
        for value in re.findall(r"(?:^|[/_-])(?:review[/_-]?)?round[/_-]?(\d+)(?=$|[/_-])", invitation, re.I):
            matches.add(int(value))
    if len(matches) == 1:
        return f"round:{next(iter(matches))}", "invitation"
    return None, "conflicting_invitations" if matches else "round_unspecified"


class OpenReviewRoundStore:
    def __init__(self, conn: Any, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(namespace: str, owner: str, principal_id: str, scopes: set[str], *, write=False):
        needed = WRITE_SCOPE if write else READ_SCOPE
        ns = f"namespace:{namespace}:{'write' if write else 'read'}"
        if not principal_id or ("operator" not in scopes and (principal_id != owner or needed not in scopes or ns not in scopes)):
            raise OpenReviewError("unauthorized", "current OpenReview and namespace access required")

    def _source(self, ref: Mapping[str, Any], scopes: set[str]) -> dict[str, Any]:
        if not isinstance(ref, Mapping) or set(ref) != {"document_id", "revision_id"}:
            raise OpenReviewError("invalid_source", "exact document and revision are required")
        document_id = _text(ref["document_id"], "document ID")
        revision_id = _text(ref["revision_id"], "revision ID")
        if "operator" not in scopes and f"document:{document_id}:read" not in scopes:
            raise OpenReviewError("unauthorized", "current document read access required")
        row = self.conn.execute(
            "SELECT payload_json,content_hash,payload_hash,observed_at_ms,lifecycle "
            "FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
            [document_id, revision_id],
        ).fetchone()
        if not row:
            raise OpenReviewError("source_unavailable", "committed public note revision is unavailable")
        payload = json.loads(row[0])
        if payload.get("_payload_reclaimed"):
            raise OpenReviewError("source_unavailable", "public note text was reclaimed")
        metadata = _metadata(payload)
        if payload.get("source_id") != "openreview-notes" or metadata.get("source_pack_id") != "openreview-research":
            raise OpenReviewError("unsupported_source", "source is not an acquired OpenReview public note")
        raw = metadata.get("source_pack_native_json")
        try:
            native = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as exc:
            raise OpenReviewError("source_unavailable", "captured native note is malformed") from exc
        if not isinstance(native, Mapping) or not isinstance(native.get("provider_record"), Mapping):
            raise OpenReviewError("source_unavailable", "captured native note is unavailable")
        provider = native["provider_record"]
        note_id = _text(native.get("id"), "provider note ID")
        if provider.get("id") != note_id or native.get("note_type") not in NOTE_TYPES:
            raise OpenReviewError("source_mismatch", "captured note identity or type disagrees")
        forum = _text(native.get("forum"), "forum ID")
        invitations = provider.get("invitations") or []
        if not isinstance(invitations, list) or len(invitations) > 30 or any(not isinstance(v, str) for v in invitations):
            raise OpenReviewError("source_unavailable", "captured invitations are malformed")
        content = payload.get("content")
        if not isinstance(content, str):
            raise OpenReviewError("source_unavailable", "public note text is unavailable")
        locator = {
            "document_id": document_id, "revision_id": revision_id,
            "url": payload.get("url"), "note_id": note_id,
            "content_hash": row[1], "payload_hash": row[2],
            "native_version": native.get("version"),
        }
        return {
            "note_id": note_id, "forum_id": forum, "replyto": native.get("replyto"),
            "note_type": native["note_type"], "invitations": invitations,
            "signatures": provider.get("signatures") or [],
            "provider_revision": {key: provider.get(key) for key in ("number", "original", "referent") if key in provider},
            "observed_at_ms": int(row[3]), "published_at_ms": native.get("published_at"),
            "edited_at_ms": native.get("updated_at"),
            "availability": "removed" if native.get("deleted") or row[4] in {"deleted", "retracted"} else "public",
            "content": content, "citation": locator,
        }

    @staticmethod
    def _membership(notes: list[dict[str, Any]]) -> None:
        by_id: dict[str, list[dict[str, Any]]] = {}
        for note in notes:
            by_id.setdefault(note["note_id"], []).append(note)
            round_id, basis = _round_from_invitations(note["invitations"])
            note["round_id"] = round_id
            note["membership_basis"] = basis
        for note in notes:
            if note["round_id"] is not None or not note["replyto"]:
                continue
            parent = by_id.get(note["replyto"], [])
            inherited = {p["round_id"] for p in parent if p["round_id"] is not None}
            if len(inherited) == 1:
                note["round_id"] = next(iter(inherited))
                note["membership_basis"] = "reply_parent"
            elif len(inherited) > 1:
                note["membership_basis"] = "ambiguous_reply_parent"
        for note in notes:
            note["membership_state"] = "supported" if note["round_id"] else "ambiguous"

    def _concern(self, spec: Mapping[str, Any], notes: list[dict[str, Any]], namespace: str,
                 round_set_id: str, revision: int) -> dict[str, Any]:
        if not isinstance(spec, Mapping) or set(spec) - {"key", "review_note_id", "review_revision_id", "quote", "response_note_id", "response_revision_id", "response_quote", "manuscript_before", "manuscript_after", "correspondence_origin", "claimed_scope"}:
            raise OpenReviewError("invalid_concern", "concern has unsupported fields")
        key = _text(spec.get("key"), "concern key", 200)
        quote = _text(spec.get("quote"), "review quote", 2000)
        review = next((n for n in notes if n["note_type"] == "review" and n["note_id"] == spec.get("review_note_id") and n["citation"]["revision_id"] == spec.get("review_revision_id")), None)
        if review is None or quote not in review["content"]:
            raise OpenReviewError("invalid_concern", "concern must quote a pinned public review")
        response = None
        response_quote = spec.get("response_quote")
        if spec.get("response_note_id") is not None:
            response = next((n for n in notes if n["note_type"] == "rebuttal" and n["note_id"] == spec.get("response_note_id") and n["citation"]["revision_id"] == spec.get("response_revision_id")), None)
            if response is None or not isinstance(response_quote, str) or response_quote not in response["content"]:
                raise OpenReviewError("invalid_concern", "response must quote a pinned public rebuttal")
        elif response_quote is not None:
            raise OpenReviewError("invalid_concern", "response quote needs a rebuttal")
        before = spec.get("manuscript_before")
        after = spec.get("manuscript_after")
        manuscript_notes = []
        for ref in (before, after):
            if ref is None:
                manuscript_notes.append(None)
                continue
            if not isinstance(ref, Mapping) or set(ref) != {"document_id", "revision_id"}:
                raise OpenReviewError("invalid_concern", "manuscript version needs exact source reference")
            note = next((n for n in notes if n["note_type"] == "submission" and all(n["citation"][k] == ref[k] for k in ref)), None)
            if note is None:
                raise OpenReviewError("invalid_concern", "manuscript version is outside the pinned public thread")
            manuscript_notes.append(note)
        old, new = manuscript_notes
        origin = spec.get("correspondence_origin", "none")
        if origin not in {"none", "author_claimed", "machine_proposed"}:
            raise OpenReviewError("invalid_concern", "correspondence origin is unsupported")
        claimed_scope = spec.get("claimed_scope", "full")
        if claimed_scope not in {"full", "partial"}:
            raise OpenReviewError("invalid_concern", "claimed scope must be full or partial")
        if origin == "author_claimed" and response is None:
            raise OpenReviewError("invalid_concern", "author claim requires an attributable rebuttal")
        changed = bool(old and new and old["citation"]["content_hash"] != new["citation"]["content_hash"])
        if old is None or new is None or not old["content"] or not new["content"]:
            status = "unassessable"
        elif not response or not changed:
            status = "unresolved"
        elif origin == "author_claimed" and claimed_scope == "partial":
            status = "author_claimed_partial"
        else:
            status = origin if origin != "none" else "unresolved"
        concern_id = "openreview-concern:" + _hash([namespace, round_set_id, key])[:24]
        revision_id = "openreview-concern-revision:" + _hash([concern_id, revision])[:24]
        start = review["content"].find(quote)
        response_start = response["content"].find(response_quote) if response else None
        return {
            "concern_id": concern_id, "concern_revision_id": revision_id,
            "key": key, "round_id": review["round_id"], "status": status,
            "review_quote": quote,
            "review_locator": {**review["citation"], "start": start, "end": start + len(quote)},
            "response_quote": response_quote if response else None,
            "response_locator": ({**response["citation"], "start": response_start,
                                  "end": response_start + len(response_quote)} if response else None),
            "manuscript_before": old["citation"] if old else None,
            "manuscript_after": new["citation"] if new else None,
            "manuscript_changed": changed,
            "correspondence_origin": origin,
            "claimed_scope": claimed_scope if origin == "author_claimed" else None,
            "independently_verified": False,
        }

    def _state(self, namespace: str, round_set_id: str, revision: int | None = None) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT r.state_json FROM openreview_round_sets s JOIN openreview_round_set_revisions r "
            "ON s.round_set_id=r.round_set_id WHERE s.namespace=? AND s.round_set_id=? "
            "AND r.revision=coalesce(?,s.revision)", [namespace, round_set_id, revision],
        ).fetchone()
        if row is None:
            raise OpenReviewError("round_set_unavailable", "pinned round set revision is unavailable")
        return json.loads(row[0])

    def save(self, namespace: str, request_key: str, forum_id: str,
             source_refs: Sequence[Mapping[str, Any]], *, principal_id: str,
             scopes: set[str], concerns: Sequence[Mapping[str, Any]] = (),
             round_set_id: str | None = None, expected_revision: int | None = None,
             paper_family: Mapping[str, Any] | None = None) -> dict[str, Any]:
        namespace, request_key, forum_id = (_text(namespace, "namespace", 128),
                                            _text(request_key, "request key", 256),
                                            _text(forum_id, "forum ID", 256))
        self._authorize(namespace, principal_id, principal_id, scopes, write=True)
        if not isinstance(source_refs, (list, tuple)) or not 1 <= len(source_refs) <= 100:
            raise OpenReviewError("invalid_sources", "one to 100 pinned public notes are required")
        if not isinstance(concerns, (list, tuple)) or len(concerns) > 100:
            raise OpenReviewError("invalid_concerns", "at most 100 concerns are supported")
        unique_refs = list({(ref.get("document_id"), ref.get("revision_id")): ref for ref in source_refs if isinstance(ref, Mapping)}.values())
        if len(unique_refs) == 0 or len(unique_refs) > 100 or any(not isinstance(ref, Mapping) for ref in source_refs):
            raise OpenReviewError("invalid_sources", "source references must be exact objects")
        notes = [self._source(ref, scopes) for ref in unique_refs]
        if any(note["forum_id"] != forum_id for note in notes):
            raise OpenReviewError("forum_mismatch", "all notes must belong to the selected forum")
        notes.sort(key=lambda n: (n["observed_at_ms"], n["note_id"], n["citation"]["revision_id"]))
        self._membership(notes)
        if paper_family is not None:
            from src.domains.research.paper_families import PaperFamilyStore

            if not isinstance(paper_family, Mapping) or set(paper_family) != {"family_id", "revision"}:
                raise OpenReviewError("invalid_family", "paper family needs exact ID and revision")
            family = PaperFamilyStore(self.conn, initialize=False).inspect(
                namespace, paper_family["family_id"], revision=paper_family["revision"],
                principal_id=principal_id, scopes=scopes,
            )
            sources = {(n["citation"]["document_id"], n["citation"]["revision_id"]) for n in notes if n["note_type"] == "submission"}
            member_sources = {(m["source"]["document_id"], m["source"]["revision_id"]) for m in family["members"] if m["status"] == "active"}
            if not sources & member_sources or not any(r["status"] in {"accepted_provider", "accepted_review"} for r in family["relations"]):
                raise OpenReviewError("unaccepted_family", "paper family needs an accepted identifier relation and matching submission")
            family_ref = dict(paper_family)
        else:
            family_ref = None
        identity = [namespace, principal_id, request_key, forum_id]
        round_set_id = round_set_id or "openreview-round-set:" + _hash(identity)[:24]
        prior = self.conn.execute(
            "SELECT owner,forum_id,revision FROM openreview_round_sets WHERE namespace=? AND round_set_id=?",
            [namespace, round_set_id],
        ).fetchone()
        if prior:
            self._authorize(namespace, prior[0], principal_id, scopes, write=True)
            if prior[1] != forum_id:
                raise OpenReviewError("identity_conflict", "round set belongs to another forum")
            if expected_revision is not None and expected_revision != prior[2]:
                raise OpenReviewError("revision_conflict", "round set changed")
        elif expected_revision is not None:
            raise OpenReviewError("revision_conflict", "new round set has no expected revision")
        revision = int(prior[2]) + 1 if prior else 1
        normalized_concerns = [self._concern(spec, notes, namespace, round_set_id, revision) for spec in concerns]
        if len({c["key"] for c in normalized_concerns}) != len(normalized_concerns):
            raise OpenReviewError("duplicate_concern", "concern keys must be unique")
        new_concerns = []
        for concern in normalized_concerns:
            prior_concern = self.conn.execute(
                "SELECT concern_revision_id,revision,payload_json FROM openreview_concern_revisions "
                "WHERE namespace=? AND concern_id=? ORDER BY revision DESC LIMIT 1",
                [namespace, concern["concern_id"]],
            ).fetchone()
            semantic_concern = {key: value for key, value in concern.items() if key != "concern_revision_id"}
            old_semantic = ({key: value for key, value in json.loads(prior_concern[2]).items()
                             if key != "concern_revision_id"} if prior_concern else None)
            if old_semantic == semantic_concern:
                concern["concern_revision_id"] = prior_concern[0]
            else:
                concern_revision = int(prior_concern[1]) + 1 if prior_concern else 1
                concern["concern_revision_id"] = "openreview-concern-revision:" + _hash(
                    [concern["concern_id"], concern_revision, semantic_concern]
                )[:24]
                new_concerns.append((concern, concern_revision))
        state = {
            "contract": SNAPSHOT_CONTRACT, "namespace": namespace, "round_set_id": round_set_id,
            "owner": principal_id, "forum_id": forum_id, "revision": revision,
            "notes": notes, "concerns": normalized_concerns,
            "round_ids": sorted({n["round_id"] for n in notes if n["round_id"]}),
            "ambiguous_membership_count": sum(n["round_id"] is None for n in notes),
            "coverage": "selected_accessible_public_notes_only",
            "paper_family": family_ref,
            "created_at_ms": self.now(),
        }
        def semantic(value):
            stable = {key: item for key, item in value.items() if key not in {"revision", "created_at_ms"}}
            stable["concerns"] = [{key: item for key, item in concern.items() if key != "concern_revision_id"}
                                  for concern in stable["concerns"]]
            return stable
        stable = semantic(state)
        content_hash = _hash(stable)
        if prior:
            current = self._state(namespace, round_set_id)
            old_stable = semantic(current)
            if _hash(old_stable) == content_hash:
                return {**current, "idempotent": True}
        self.conn.execute("BEGIN")
        try:
            if prior:
                row = self.conn.execute(
                    "UPDATE openreview_round_sets SET revision=? WHERE round_set_id=? AND revision=? RETURNING revision",
                    [revision, round_set_id, prior[2]],
                ).fetchone()
                if not row:
                    raise OpenReviewError("revision_conflict", "round set changed")
            else:
                self.conn.execute("INSERT INTO openreview_round_sets VALUES (?,?,?,?,?)",
                                  [round_set_id, namespace, principal_id, forum_id, revision])
            self.conn.execute("INSERT INTO openreview_round_set_revisions VALUES (?,?,?,?,?)",
                              [round_set_id, revision, content_hash, _json(state), state["created_at_ms"]])
            for concern, concern_revision in new_concerns:
                self.conn.execute("INSERT INTO openreview_concern_revisions VALUES (?,?,?,?,?,?)",
                                  [concern["concern_revision_id"], namespace, concern["concern_id"], concern_revision,
                                   _hash(concern), _json(concern)])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**state, "idempotent": False}

    def _full(self, namespace: str, round_set_id: str, revision: int | None,
              principal_id: str, scopes: set[str]) -> dict[str, Any]:
        state = self._state(namespace, round_set_id, revision)
        self._authorize(namespace, state["owner"], principal_id, scopes)
        for note in state["notes"]:
            self._source({key: note["citation"][key] for key in ("document_id", "revision_id")}, scopes)
        if state["paper_family"]:
            from src.domains.research.paper_families import PaperFamilyStore
            PaperFamilyStore(self.conn, initialize=False).inspect(
                namespace, state["paper_family"]["family_id"],
                revision=state["paper_family"]["revision"],
                principal_id=principal_id, scopes=scopes,
            )
        return state

    def inspect(self, namespace: str, round_set_id: str, *, principal_id: str, scopes: set[str],
                revision: int | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise OpenReviewError("invalid_page", "bounded pagination required")
        state = self._full(namespace, round_set_id, revision, principal_id, scopes)
        return {**state, "notes": state["notes"][offset:offset + limit],
                "concerns": state["concerns"][offset:offset + limit],
                "total_notes": len(state["notes"]), "total_concerns": len(state["concerns"]),
                "limit": limit, "offset": offset}

    def inspect_round(self, namespace: str, round_set_id: str, round_id: str, *, principal_id: str,
                      scopes: set[str], revision: int | None = None, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        state = self._full(namespace, round_set_id, revision, principal_id, scopes)
        if round_id not in state["round_ids"]:
            raise OpenReviewError("round_unavailable", "round is unknown or ambiguously attributed")
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise OpenReviewError("invalid_page", "bounded pagination required")
        notes = [n for n in state["notes"] if n["round_id"] == round_id]
        concerns = [c for c in state["concerns"] if c["round_id"] == round_id]
        return {"contract": ROUND_CONTRACT, "round_set_id": round_set_id,
                "revision": state["revision"], "round_id": round_id,
                "notes": notes[offset:offset + limit], "concerns": concerns[offset:offset + limit],
                "total_notes": len(notes), "total_concerns": len(concerns),
                "limit": limit, "offset": offset, "coverage": state["coverage"]}

    def assess_concern(self, namespace: str, round_set_id: str, concern_key: str, *, principal_id: str,
                       scopes: set[str], revision: int | None = None, review_task_id: str | None = None) -> dict[str, Any]:
        state = self._full(namespace, round_set_id, revision, principal_id, scopes)
        concern = next((c for c in state["concerns"] if c["key"] == concern_key), None)
        if concern is None:
            raise OpenReviewError("concern_unavailable", "concern is outside this pinned round set")
        if review_task_id is None:
            return concern
        task = ReviewInboxStore(self.conn, initialize=False).inspect(
            namespace, review_task_id, principal_id=principal_id, scopes=scopes,
        )
        if task["target"] != {"kind": "openreview_concern", "namespace": namespace, "id": concern["concern_revision_id"]}:
            raise OpenReviewError("review_mismatch", "review task targets another concern revision")
        if task["stale"] or not task["resolution"] or not task["resolution"].get("all_declared_human"):
            raise OpenReviewError("review_unavailable", "current completed independent human review required")
        label = task["resolution"]["label"]["decision"]
        return {**concern, "status": "independently_verified" if label == "resolved" else label,
                "independently_verified": label == "resolved", "review_task_id": review_task_id,
                "review_resolution": task["resolution"]}

    def concern_review_target(self, namespace: str, round_set_id: str, concern_key: str,
                              *, principal_id: str, scopes: set[str], revision: int | None = None) -> dict[str, Any]:
        """Return exact ReviewInbox target and sources for an independent task."""
        concern = self.assess_concern(namespace, round_set_id, concern_key,
                                      principal_id=principal_id, scopes=scopes, revision=revision)
        refs = []
        for key in ("review_locator", "response_locator", "manuscript_before", "manuscript_after"):
            locator = concern.get(key)
            if locator:
                ref = {name: locator[name] for name in ("document_id", "revision_id")}
                if ref not in refs:
                    refs.append(ref)
        return {"target": {"kind": "openreview_concern", "namespace": namespace,
                           "id": concern["concern_revision_id"]}, "sources": refs}

    def compare(self, namespace: str, round_set_id: str, before: Mapping[str, Any], after: Mapping[str, Any],
                *, principal_id: str, scopes: set[str], review_tasks: Mapping[str, str] | None = None) -> dict[str, Any]:
        def selector(value):
            if not isinstance(value, Mapping) or set(value) != {"revision", "round_id"} or type(value["revision"]) is not int:
                raise OpenReviewError("invalid_selector", "pin a snapshot revision and round ID")
            return self.inspect_round(namespace, round_set_id, value["round_id"],
                                      principal_id=principal_id, scopes=scopes,
                                      revision=value["revision"], limit=100)
        left, right = selector(before), selector(after)
        if before == after:
            raise OpenReviewError("invalid_selector", "comparison selectors must differ")
        a = {(n["note_id"], n["note_type"]): n for n in left["notes"]}
        b = {(n["note_id"], n["note_type"]): n for n in right["notes"]}
        note_changes = []
        for key in sorted(set(a) | set(b)):
            old, new = a.get(key), b.get(key)
            if old and new and old["citation"]["revision_id"] == new["citation"]["revision_id"]:
                continue
            note_changes.append({"note_id": key[0], "note_type": key[1],
                                 "kind": "added" if old is None else "unavailable" if new is None else "edited",
                                 "before": old["citation"] if old else None,
                                 "after": new["citation"] if new else None})
        ac = {c["key"]: c for c in left["concerns"]}
        bc = {c["key"]: c for c in right["concerns"]}
        concern_changes = []
        for key in sorted(set(ac) | set(bc)):
            old, new = ac.get(key), bc.get(key)
            if old == new:
                continue
            assessed = (self.assess_concern(namespace, round_set_id, key,
                                           revision=after["revision"], principal_id=principal_id,
                                           scopes=scopes, review_task_id=(review_tasks or {}).get(key)) if new else None)
            concern_changes.append({"key": key, "kind": "added" if old is None else "unavailable" if new is None else "changed",
                                    "before": old, "after": assessed})
        # Submission revisions commonly lack a numbered review invitation.
        # Compare them across the two pinned snapshots, independent of round
        # membership, while retaining their exact source revision citations.
        left_state = self._full(namespace, round_set_id, before["revision"], principal_id, scopes)
        right_state = self._full(namespace, round_set_id, after["revision"], principal_id, scopes)
        def submissions(state):
            values = {}
            for note in state["notes"]:
                if note["note_type"] == "submission":
                    values[note["note_id"]] = note
            return values
        submissions_before = submissions(left_state)
        submissions_after = submissions(right_state)
        manuscript_changes = []
        for note_id in sorted(set(submissions_before) | set(submissions_after)):
            old, new = submissions_before.get(note_id), submissions_after.get(note_id)
            if old and new and old["citation"]["revision_id"] == new["citation"]["revision_id"]:
                continue
            manuscript_changes.append({
                "note_id": note_id, "note_type": "submission",
                "kind": "added" if old is None else "unavailable" if new is None else "edited",
                "before": old["citation"] if old else None,
                "after": new["citation"] if new else None,
            })
        return {"contract": COMPARISON_CONTRACT, "round_set_id": round_set_id,
                "before": dict(before), "after": dict(after),
                "note_changes": note_changes, "concern_changes": concern_changes,
                "manuscript_changes": manuscript_changes,
                "manuscript_coverage": "available" if submissions_before and submissions_after else "unassessable",
                "interpretation": None, "coverage": "selected_accessible_public_notes_only"}

    def export(self, namespace: str, round_set_id: str, before: Mapping[str, Any], after: Mapping[str, Any],
               *, principal_id: str, scopes: set[str], review_tasks: Mapping[str, str] | None = None) -> dict[str, Any]:
        comparison = self.compare(namespace, round_set_id, before, after,
                                  principal_id=principal_id, scopes=scopes, review_tasks=review_tasks)
        current_round = self.inspect_round(namespace, round_set_id, after["round_id"],
                                           revision=after["revision"], principal_id=principal_id,
                                           scopes=scopes, limit=100)
        change_by_key = {item["key"]: item["kind"] for item in comparison["concern_changes"]}
        report_rows = []
        for concern in current_round["concerns"]:
            current = self.assess_concern(
                namespace, round_set_id, concern["key"], revision=after["revision"],
                principal_id=principal_id, scopes=scopes,
                review_task_id=(review_tasks or {}).get(concern["key"]),
            )
            citations = [current[name] for name in ("review_locator", "response_locator", "manuscript_before", "manuscript_after") if current.get(name)]
            report_rows.append({"concern_key": concern["key"], "change_kind": change_by_key.get(concern["key"], "unchanged"),
                                "status": current["status"],
                                "review_quote": current["review_quote"], "response_quote": current["response_quote"],
                                "manuscript_changed": current["manuscript_changed"],
                                "citations": citations, "kind": "recorded_evidence_and_review_state"})
        return {**comparison, "report_ready": report_rows,
                "limitations": ["A public note comparison is not independent verification of an author's claim."]}
