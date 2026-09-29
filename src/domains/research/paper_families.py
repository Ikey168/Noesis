"""Reviewable scholarly version families over immutable document revisions."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import time
from typing import Any

from src.kb.review_inbox import ReviewInboxStore

CONTRACT = "noesis-paper-family-v1"
READ_SCOPE = "knowledge:paper-family:read"
WRITE_SCOPE = "knowledge:paper-family:write"
REVIEW_SCOPE = "knowledge:paper-family:review"
RELATIONS = {"is-version-of", "has-version", "is-preprint-of", "has-preprint",
             "is-supplement-to", "has-supplement", "is-dataset-for", "has-dataset",
             "is-correction-of", "related"}
STAGES = {"preprint", "accepted-manuscript", "version-of-record", "dataset", "supplement", "other"}
NOTICE_TYPES = {"correction", "retraction", "withdrawal", "expression_of_concern"}

_DDL = """
CREATE TABLE IF NOT EXISTS paper_families(
 family_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,
 revision BIGINT NOT NULL,created_at_ms BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS paper_family_revisions(
 family_id TEXT NOT NULL,revision BIGINT NOT NULL,content_hash TEXT NOT NULL,
 state_json TEXT NOT NULL,created_at_ms BIGINT NOT NULL,
 PRIMARY KEY(family_id,revision));
CREATE TABLE IF NOT EXISTS paper_family_commands(
 family_id TEXT NOT NULL,command_key TEXT NOT NULL,request_hash TEXT NOT NULL,
 revision BIGINT NOT NULL,PRIMARY KEY(family_id,command_key));
"""


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _text(value, name, limit=1000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise PaperFamilyError("invalid_input", f"{name} must be bounded nonempty text")
    return value.strip()


def _identifier(kind, value):
    kind = _text(kind, "identifier type", 30).lower()
    value = _text(value, "identifier", 500).strip()
    if kind == "doi":
        value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value, flags=re.I).lower()
        if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
            raise PaperFamilyError("invalid_identifier", "invalid DOI")
    elif kind == "arxiv":
        value = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv:)", "", value, flags=re.I).lower()
        if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z.-]+/\d{7})(?:v\d+)?", value):
            raise PaperFamilyError("invalid_identifier", "invalid arXiv identifier")
    elif kind == "provider":
        if len(value) > 500:
            raise PaperFamilyError("invalid_identifier", "provider identifier is too long")
    else:
        raise PaperFamilyError("invalid_identifier", "identifier type must be DOI, arXiv or provider")
    return {"kind": kind, "value": value}


class PaperFamilyError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class PaperFamilyStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(namespace, owner, principal_id, scopes, *, write=False, review=False):
        needed = REVIEW_SCOPE if review else WRITE_SCOPE if write else READ_SCOPE
        ns = f"namespace:{namespace}:{'write' if write or review else 'read'}"
        if not principal_id or ("operator" not in scopes and
            (needed not in scopes or ns not in scopes or (owner != principal_id and not review))):
            raise PaperFamilyError("unauthorized", "current family and namespace access required")

    def _source(self, ref, scopes):
        if not isinstance(ref, dict) or set(ref) != {"document_id", "revision_id"}:
            raise PaperFamilyError("invalid_source", "exact document and revision required")
        try:
            fingerprint = ReviewInboxStore(self.conn, initialize=False)._sources([ref], scopes)[0]
        except Exception as exc:
            raise PaperFamilyError(getattr(exc, "code", "source_unavailable"),
                                   "paper source revision is unavailable or unreadable") from exc
        row = self.conn.execute(
            "SELECT payload_json FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
            [ref["document_id"], ref["revision_id"]],
        ).fetchone()
        payload = json.loads(row[0]) if row else {}
        if payload.get("_payload_reclaimed"):
            raise PaperFamilyError("source_unavailable", "paper source payload was reclaimed")
        return fingerprint, payload

    def _state(self, namespace, family_id, *, revision=None):
        row = self.conn.execute(
            "SELECT f.owner,r.state_json FROM paper_families f JOIN paper_family_revisions r ON f.family_id=r.family_id "
            "WHERE f.namespace=? AND f.family_id=? AND r.revision=coalesce(?,f.revision)",
            [namespace, family_id, revision],
        ).fetchone()
        if not row:
            raise PaperFamilyError("family_unavailable", "paper family revision is unavailable")
        return row[0], json.loads(row[1])

    def _member(self, spec, scopes):
        if not isinstance(spec, dict) or set(spec) != {"document_id", "revision_id", "stage", "identifiers"}:
            raise PaperFamilyError("invalid_member", "member needs exact source, stage and identifiers")
        if spec["stage"] not in STAGES:
            raise PaperFamilyError("invalid_member", "unsupported publication stage")
        if not isinstance(spec["identifiers"], list) or not 1 <= len(spec["identifiers"]) <= 20:
            raise PaperFamilyError("invalid_member", "one to 20 scholarly identifiers required")
        ref = {key: spec[key] for key in ("document_id", "revision_id")}
        fingerprint, payload = self._source(ref, scopes)
        if any(not isinstance(value, dict) or set(value) != {"kind", "value"} for value in spec["identifiers"]):
            raise PaperFamilyError("invalid_identifier", "identifier entries need kind and value")
        identifiers = [_identifier(value["kind"], value["value"]) for value in spec["identifiers"]]
        identifiers = sorted({_json(value): value for value in identifiers}.values(), key=_json)
        representation = payload.get("content_representation") or (payload.get("metadata") or {}).get("content_representation")
        if representation in {"full-text", "open-full-text", "plain-text-full-text"}:
            availability = "full-text"
        elif representation in {"plain-text-abstract", "abstract-only"} or payload.get("abstract"):
            availability = "abstract-only"
        elif payload.get("content"):
            availability = "representation-unknown"
        else:
            availability = "metadata-only"
        return {
            "member_id": "paper-member:" + _hash(ref)[:24], "source": ref,
            "stage": spec["stage"], "identifiers": identifiers,
            "availability": availability, "content_hash": fingerprint["content_hash"],
            "title": str(payload.get("title") or "")[:1000],
            "authors": list(payload.get("authors") or [])[:100] if isinstance(payload.get("authors"), list) else [],
            "status": "active", "added_at_ms": self.now(),
        }

    def _provider_relation(self, source, target, relation_type, provenance, scopes):
        _, payload = self._source(source["source"], scopes)
        raw = (payload.get("metadata") or {}).get("related_resources_json", "[]")
        try:
            links = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError as exc:
            raise PaperFamilyError("invalid_provenance", "stored provider relationships are malformed") from exc
        evidence = provenance.get("relation")
        matched = (isinstance(links, list) and isinstance(evidence, dict) and evidence in links and
                   _relation_name(evidence.get("predicate")) == relation_type and
                   str(evidence.get("target_identifier", "")).lower() in
                   {v["value"] for v in target["identifiers"]})
        if not matched:
            raise PaperFamilyError("invalid_provenance", "provider relation is not in captured source revision")
        return True

    def _commit(self, family_id, state, command_key, request_hash, expected_revision):
        if state["revision"] != expected_revision:
            raise PaperFamilyError("revision_conflict", "paper family changed; inspect current revision")
        next_state = copy.deepcopy(state)
        next_state["revision"] += 1
        next_state["predecessor_hash"] = state["content_hash"]
        next_state.pop("content_hash", None)
        next_state["content_hash"] = _hash(next_state)
        self.conn.execute("BEGIN")
        try:
            current = self.conn.execute("SELECT revision FROM paper_families WHERE family_id=?", [family_id]).fetchone()
            if not current or int(current[0]) != expected_revision:
                raise PaperFamilyError("revision_conflict", "paper family changed; inspect current revision")
            self.conn.execute("INSERT INTO paper_family_revisions VALUES (?,?,?,?,?)",
                              [family_id, next_state["revision"], next_state["content_hash"], _json(next_state), self.now()])
            self.conn.execute("UPDATE paper_families SET revision=? WHERE family_id=?", [next_state["revision"], family_id])
            self.conn.execute("INSERT INTO paper_family_commands VALUES (?,?,?,?)",
                              [family_id, command_key, request_hash, next_state["revision"]])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return next_state

    def _change(self, namespace, family_id, command_key, request, expected_revision,
                principal_id, scopes, mutation, *, review=False):
        _text(command_key, "command key", 200)
        owner, state = self._state(namespace, family_id)
        self._authorize(namespace, owner, principal_id, scopes, write=not review, review=review)
        for member in state["members"]:
            self._source(member["source"], scopes)
        request_hash = _hash(request)
        if len(_json(request).encode()) > 64_000:
            raise PaperFamilyError("input_limit", "family command exceeds 64 KiB")
        prior = self.conn.execute("SELECT request_hash,revision FROM paper_family_commands WHERE family_id=? AND command_key=?",
                                  [family_id, command_key]).fetchone()
        if prior:
            if prior[0] != request_hash:
                raise PaperFamilyError("command_conflict", "command key has different content")
            previous = self._state(namespace, family_id, revision=int(prior[1]))[1]
            for member in previous["members"]:
                self._source(member["source"], scopes)
            return {**previous, "idempotent": True}
        if expected_revision != state["revision"]:
            raise PaperFamilyError("revision_conflict", "paper family changed; inspect current revision")
        revised = copy.deepcopy(state)
        mutation(revised)
        if (len(revised["members"]) > 100 or len(revised["relations"]) > 100 or
            len(revised["notices"]) > 100 or len(revised["selections"]) > 100 or
            len(_json(revised).encode()) > 2_000_000):
            raise PaperFamilyError("family_limit", "family exceeds retained revision bounds")
        return {**self._commit(family_id, revised, command_key, request_hash, expected_revision), "idempotent": False}

    def create(self, namespace, family_key, root_member, *, principal_id, scopes):
        _text(namespace, "namespace", 128)
        _text(family_key, "family key", 200)
        self._authorize(namespace, principal_id, principal_id, scopes, write=True)
        family_id = "paper-family:" + _hash([namespace, principal_id, family_key])[:24]
        row = self.conn.execute("SELECT family_id FROM paper_families WHERE family_id=?", [family_id]).fetchone()
        member = self._member(root_member, scopes)
        if row:
            old = self._state(namespace, family_id, revision=1)[1]
            if old["members"][0]["member_id"] != member["member_id"]:
                raise PaperFamilyError("family_conflict", "family key has a different root member")
            return {**self.inspect(namespace, family_id, principal_id=principal_id, scopes=scopes), "idempotent": True}
        state = {"contract": CONTRACT, "namespace": namespace, "family_id": family_id,
                 "owner": principal_id, "revision": 1, "predecessor_hash": None,
                 "members": [member], "relations": [], "notices": [], "selections": [],
                 "created_at_ms": self.now()}
        state["content_hash"] = _hash(state)
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO paper_families VALUES (?,?,?,?,?)",
                              [family_id, namespace, principal_id, 1, self.now()])
            self.conn.execute("INSERT INTO paper_family_revisions VALUES (?,?,?,?,?)",
                              [family_id, 1, state["content_hash"], _json(state), self.now()])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**state, "idempotent": False}

    def inspect(self, namespace, family_id, *, principal_id, scopes, revision=None, limit=50, offset=0):
        owner, state = self._state(namespace, family_id, revision=revision)
        self._authorize(namespace, owner, principal_id, scopes)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise PaperFamilyError("invalid_page", "bounded family pagination required")
        for member in state["members"]:
            self._source(member["source"], scopes)
        return {**state, "members": state["members"][offset:offset + limit],
                "member_total": len(state["members"]), "offset": offset,
                "next_offset": offset + limit if offset + limit < len(state["members"]) else None}

    def add_member(self, namespace, family_id, command_key, member_spec, *, source_member_id,
                   relation_type, provenance, expected_revision, principal_id, scopes):
        if relation_type not in RELATIONS or not isinstance(provenance, dict) or provenance.get("kind") not in {"provider", "inference"}:
            raise PaperFamilyError("invalid_relation", "typed relation and provider or inference provenance required")
        request = ["add_member", member_spec, source_member_id, relation_type, provenance]
        def mutate(state):
            source = next((m for m in state["members"] if m["member_id"] == source_member_id and m["status"] == "active"), None)
            if source is None:
                raise PaperFamilyError("member_unavailable", "active source member required")
            member = self._member(member_spec, scopes)
            if any(m["member_id"] == member["member_id"] for m in state["members"]):
                raise PaperFamilyError("duplicate_member", "exact source revision is already in family")
            identifiers = {(v["kind"], v["value"]) for v in member["identifiers"]}
            collision = any(identifiers & {(v["kind"], v["value"]) for v in old["identifiers"]}
                            and old["source"]["document_id"] != member["source"]["document_id"]
                            for old in state["members"] if old["status"] == "active")
            provider_match = False
            if provenance["kind"] == "provider":
                provider_match = self._provider_relation(source, member, relation_type, provenance, scopes)
            accepted = provider_match and not collision
            member["status"] = "active" if accepted else "candidate"
            state["members"].append(member)
            state["relations"].append({"relation_id": "paper-relation:" + _hash([source_member_id, member["member_id"], relation_type])[:24],
                                       "source_member_id": source_member_id, "target_member_id": member["member_id"],
                                       "type": relation_type, "provenance": provenance,
                                       "status": "accepted_provider" if accepted else "candidate",
                                       "collision": collision, "created_at_ms": self.now()})
        return self._change(namespace, family_id, command_key, request, expected_revision, principal_id, scopes, mutate)

    def correct_relation(self, namespace, family_id, command_key, relation_id, relation_type, provenance, rationale,
                         *, expected_revision, principal_id, scopes):
        if relation_type not in RELATIONS or not isinstance(provenance, dict) or provenance.get("kind") not in {"provider", "inference"}:
            raise PaperFamilyError("invalid_relation", "typed corrected relation and provenance required")
        _text(rationale, "correction rationale", 2000)
        def mutate(state):
            old = next((r for r in state["relations"] if r["relation_id"] == relation_id and r["status"] not in {"superseded", "rejected"}), None)
            if old is None:
                raise PaperFamilyError("relation_unavailable", "current relation required")
            source = next(m for m in state["members"] if m["member_id"] == old["source_member_id"])
            target = next(m for m in state["members"] if m["member_id"] == old["target_member_id"])
            accepted = provenance["kind"] == "provider" and self._provider_relation(source, target, relation_type, provenance, scopes)
            accepted = accepted and not old["collision"]
            old["status"] = "superseded"
            replacement = {"relation_id": "paper-relation:" + _hash([relation_id, command_key, relation_type])[:24],
                           "source_member_id": source["member_id"], "target_member_id": target["member_id"],
                           "type": relation_type, "provenance": provenance,
                           "status": "accepted_provider" if accepted else "candidate",
                           "collision": old["collision"], "supersedes_relation_id": relation_id,
                           "correction_rationale": rationale, "created_at_ms": self.now()}
            state["relations"].append(replacement)
            target["status"] = "active" if accepted else "candidate"
        return self._change(namespace, family_id, command_key,
                            ["correct_relation", relation_id, relation_type, provenance, rationale],
                            expected_revision, principal_id, scopes, mutate)

    def review_relation(self, namespace, family_id, command_key, relation_id, decision, rationale,
                        *, expected_revision, principal_id, scopes):
        if decision not in {"accept", "reject"}:
            raise PaperFamilyError("invalid_review", "accept or reject required")
        _text(rationale, "review rationale", 2000)
        request = ["review_relation", relation_id, decision, rationale]
        def mutate(state):
            relation = next((r for r in state["relations"] if r["relation_id"] == relation_id), None)
            if not relation or relation["status"] != "candidate":
                raise PaperFamilyError("relation_unavailable", "candidate relation required")
            if principal_id == state["owner"]:
                raise PaperFamilyError("independent_review_required", "candidate review needs a different reviewer")
            relation["status"] = "accepted_review" if decision == "accept" else "rejected"
            relation["review"] = {"principal_id": principal_id, "decision": decision,
                                  "rationale": rationale, "annotation_origin": "human",
                                  "reviewed_at_ms": self.now()}
            target = next(m for m in state["members"] if m["member_id"] == relation["target_member_id"])
            target["status"] = "active" if decision == "accept" else "rejected"
        return self._change(namespace, family_id, command_key, request, expected_revision, principal_id, scopes, mutate, review=True)

    def remove_member(self, namespace, family_id, command_key, member_id, rationale, *, expected_revision, principal_id, scopes):
        _text(rationale, "split rationale", 2000)
        def mutate(state):
            member = next((m for m in state["members"] if m["member_id"] == member_id), None)
            if not member or member["status"] not in {"active", "candidate"}:
                raise PaperFamilyError("member_unavailable", "current family member required")
            member["status"] = "removed"
            member["removal"] = {"reason": rationale, "principal_id": principal_id, "at_ms": self.now()}
            for relation in state["relations"]:
                if member_id in {relation["source_member_id"], relation["target_member_id"]} and relation["status"] not in {"rejected", "superseded"}:
                    relation["status"] = "superseded"
        return self._change(namespace, family_id, command_key, ["remove_member", member_id, rationale],
                            expected_revision, principal_id, scopes, mutate)

    def attach_notice(self, namespace, family_id, command_key, notice_id, *, expected_revision, principal_id, scopes):
        _text(notice_id, "notice id", 200)
        def mutate(state):
            try:
                row = self.conn.execute("SELECT document_id,notice_document_id,notice_json FROM crossref_notices WHERE notice_id=?", [notice_id]).fetchone()
            except Exception as exc:
                raise PaperFamilyError("notice_unavailable", "retained notice is unavailable") from exc
            if not row:
                raise PaperFamilyError("notice_unavailable", "retained notice is unavailable")
            if row[1] and "operator" not in scopes and f"document:{row[1]}:read" not in scopes:
                raise PaperFamilyError("unauthorized", "current notice source access required")
            notice = json.loads(row[2])
            if notice.get("notice_type") not in NOTICE_TYPES:
                raise PaperFamilyError("notice_unresolved", "notice type is unsupported")
            target_doi = notice.get("target_doi")
            targets = [m for m in state["members"] if m["status"] == "active" and
                       any(v == {"kind": "doi", "value": target_doi} for v in m["identifiers"])]
            if row[0]:
                targets = [m for m in targets if m["source"]["document_id"] == row[0]]
            target = targets[0]["member_id"] if len(targets) == 1 and notice.get("status") == "supported" else None
            entry = {"notice_id": notice_id, "notice_type": notice["notice_type"],
                     "target_member_id": target, "status": "targeted" if target else "unresolved",
                     "target_doi": target_doi, "notice_document_id": row[1],
                     "notice_source": {"snapshot": notice.get("snapshot"),
                                       "record_id": notice.get("record_id"),
                                       "notice_doi": notice.get("notice_doi"),
                                       "target_before_revision": notice.get("target_before_revision")},
                     "attached_at_ms": self.now()}
            if any(v["notice_id"] == notice_id for v in state["notices"]):
                raise PaperFamilyError("duplicate_notice", "notice already attached")
            state["notices"].append(entry)
        return self._change(namespace, family_id, command_key, ["attach_notice", notice_id],
                            expected_revision, principal_id, scopes, mutate)

    def select_citation(self, namespace, family_id, command_key, member_id, *, expected_revision,
                        principal_id, scopes, locator=None):
        locator = dict(locator or {})
        if set(locator) - {"page", "section", "url", "snapshot_id"} or len(_json(locator).encode()) > 2000:
            raise PaperFamilyError("invalid_locator", "bounded citation locator required")
        def mutate(state):
            member = next((m for m in state["members"] if m["member_id"] == member_id and m["status"] == "active"), None)
            if member is None:
                raise PaperFamilyError("member_unavailable", "active family member required")
            self._source(member["source"], scopes)
            selection = {"selection_id": "paper-citation:" + _hash([family_id, command_key])[:24],
                         "member_id": member_id, "source": member["source"],
                         "identifiers": member["identifiers"], "title": member["title"],
                         "authors": member["authors"], "availability": member["availability"],
                         "locator": locator, "selected_by": principal_id, "selected_at_ms": self.now(),
                         "notice_ids": [n["notice_id"] for n in state["notices"] if n["target_member_id"] == member_id]}
            state["selections"].append(selection)
        return self._change(namespace, family_id, command_key, ["select_citation", member_id, locator],
                            expected_revision, principal_id, scopes, mutate)

    def review_notice_target(self, namespace, family_id, command_key, notice_id, member_id, rationale,
                             *, expected_revision, principal_id, scopes):
        _text(rationale, "notice targeting rationale", 2000)
        def mutate(state):
            notice = next((n for n in state["notices"] if n["notice_id"] == notice_id), None)
            member = next((m for m in state["members"] if m["member_id"] == member_id and m["status"] == "active"), None)
            if notice is None or member is None or notice["target_member_id"] is not None:
                raise PaperFamilyError("notice_unresolved", "unresolved notice and active member required")
            if principal_id == state["owner"]:
                raise PaperFamilyError("independent_review_required", "target review needs a different reviewer")
            if notice["target_doi"] and not any(v == {"kind": "doi", "value": notice["target_doi"]} for v in member["identifiers"]):
                raise PaperFamilyError("target_mismatch", "review target DOI differs from member")
            self._source(member["source"], scopes)
            notice["target_member_id"] = member_id
            notice["status"] = "targeted_review"
            notice["target_review"] = {"principal_id": principal_id, "rationale": rationale,
                                       "annotation_origin": "human", "reviewed_at_ms": self.now()}
        return self._change(namespace, family_id, command_key,
                            ["review_notice_target", notice_id, member_id, rationale],
                            expected_revision, principal_id, scopes, mutate, review=True)

    def compare(self, namespace, family_id, member_ids, *, principal_id, scopes):
        state = self.inspect(namespace, family_id, principal_id=principal_id, scopes=scopes, limit=100)
        if not isinstance(member_ids, list) or not 2 <= len(member_ids) <= 10 or len(set(member_ids)) != len(member_ids):
            raise PaperFamilyError("invalid_members", "two to ten distinct members required")
        members = [next((m for m in state["members"] if m["member_id"] == mid), None) for mid in member_ids]
        if any(m is None for m in members):
            raise PaperFamilyError("member_unavailable", "member is unavailable")
        return {"contract": "noesis-paper-family-comparison-v1", "family_id": family_id,
                "revision": state["revision"], "members": members,
                "relations": [r for r in state["relations"] if r["source_member_id"] in member_ids and r["target_member_id"] in member_ids],
                "notices": [n for n in state["notices"] if n["target_member_id"] in member_ids]}

    def export(self, namespace, family_id, *, principal_id, scopes):
        state = self.inspect(namespace, family_id, principal_id=principal_id, scopes=scopes, limit=100)
        bibliography = [{"id": s["selection_id"],
                         "text": "; ".join(filter(None, [", ".join(str(a) for a in s["authors"]), s["title"],
                                                   next((v["value"] for v in s["identifiers"] if v["kind"] == "doi"), "")])),
                         "source": s["source"], "locator": s["locator"], "notice_ids": s["notice_ids"]}
                        for s in state["selections"]]
        statuses = {m["member_id"]: [n["notice_type"] for n in state["notices"] if n["target_member_id"] == m["member_id"]]
                    for m in state["members"]}
        return {"contract": "noesis-paper-family-export-v1", "family_id": family_id,
                "revision": state["revision"], "content_hash": state["content_hash"],
                "bibliography": bibliography, "member_lifecycle": statuses,
                "unresolved_notices": [n for n in state["notices"] if n["target_member_id"] is None],
                "members": state["members"], "relations": state["relations"],
                "replay_hash": _hash([state["content_hash"], bibliography, statuses])}


def _relation_name(value):
    word = re.sub(r"(?<!^)(?=[A-Z])", "-", str(value or "")).replace("_", "-").lower()
    return word
