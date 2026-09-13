"""Versioned Deep Research synthesis grounded in pinned intake source revisions."""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from src.kb.intake_exploration import IntakeExplorationStore
from src.kb.intake_inbox import IntakeInboxStore
from src.kb.intake_modes import IntakeError, _bounded, _hash, _json, _text
from src.kb.research_projects import ResearchProjectStore

CONTRACT = "noesis-intake-research-bundle-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS intake_research_bundles(
 bundle_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
 project_id TEXT NOT NULL, revision BIGINT NOT NULL, request_hash TEXT NOT NULL,
 UNIQUE(namespace,owner,project_id));
CREATE TABLE IF NOT EXISTS intake_research_bundle_revisions(
 bundle_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
 created_at_ms BIGINT NOT NULL, PRIMARY KEY(bundle_id,revision));
CREATE TABLE IF NOT EXISTS intake_research_bundle_commands(
 bundle_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
 revision BIGINT NOT NULL, PRIMARY KEY(bundle_id,command_key));
"""


def _list(value, field, *, limit=100):
    if not isinstance(value, list) or len(value) > limit:
        raise IntakeError("invalid_bundle", f"{field} must be a bounded list")
    return value


def _ids(value, field, valid):
    result = [_text(item, field, limit=128) for item in _list(value, field)]
    if len(result) != len(set(result)) or set(result) - valid:
        raise IntakeError("invalid_bundle", f"{field} must reference distinct existing cards")
    return result


class IntakeResearchBundleStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _project(self, namespace, project_id, principal_id, scopes, *, write=False):
        projects = ResearchProjectStore(self.conn, initialize=False)
        project = projects._state(namespace, project_id)
        projects._authorize(project, principal_id, scopes, write=write)
        if write and project["status"] == "archived":
            raise IntakeError("project_archived", "archived project cannot be revised")
        return project

    def _state(self, namespace, bundle_id, revision=None):
        row = self.conn.execute(
            "SELECT r.content_json FROM intake_research_bundles b "
            "JOIN intake_research_bundle_revisions r ON r.bundle_id=b.bundle_id "
            "WHERE b.namespace=? AND b.bundle_id=? AND r.revision=coalesce(?,b.revision)",
            [namespace, bundle_id, revision],
        ).fetchone()
        if row is None:
            raise IntakeError("bundle_not_found", "research bundle revision is unavailable")
        return json.loads(row[0])

    def _source(self, ref, principal_id, scopes):
        namespace, identity, revision = ref["namespace"], ref["id"], ref["version"]
        if identity.startswith("explore:"):
            source = IntakeExplorationStore(self.conn, initialize=False).inspect_source(
                namespace, identity, version=revision,
                principal_id=principal_id, scopes=scopes,
            )
            current = IntakeExplorationStore(self.conn, initialize=False).inspect_source(
                namespace, identity, principal_id=principal_id, scopes=scopes,
            )["version"]
            url = source["url"]
        elif identity.startswith("feed:"):
            source = IntakeInboxStore(self.conn, initialize=False).inspect(
                namespace, identity, revision=revision,
                principal_id=principal_id, scopes=scopes,
            )
            current = IntakeInboxStore(self.conn, initialize=False).inspect(
                namespace, identity, principal_id=principal_id, scopes=scopes,
            )["source_version"]
            url = source["original_url"]
        else:
            raise IntakeError("invalid_bundle", "card source must be an intake source")
        return source["content"], url, current == revision

    def _validate(self, project, document, principal_id, scopes):
        if not isinstance(document, dict) or set(document) != {
            "cards", "claims", "concepts", "brief", "mental_model", "map",
            "known", "uncertain", "unresolved", "definition_of_done",
        }:
            raise IntakeError("invalid_bundle", "bundle needs cards, claims, synthesis, knowledge gaps, and review")
        document = _bounded(document, limit=512_000)
        pinned = {
            (link.get("namespace", project["namespace"]), link["id"], link.get("revision"))
            for link in project["links"] if link["kind"] == "intake_source"
        }
        cards = {}
        source_hosts = {}
        source_status = {}
        for card in _list(document["cards"], "cards", limit=500):
            if not isinstance(card, dict) or set(card) != {"id", "source", "quote", "summary"}:
                raise IntakeError("invalid_bundle", "card needs id, source span, exact quote and summary")
            identity = _text(card["id"], "card id", limit=128)
            if identity in cards:
                raise IntakeError("invalid_bundle", "card IDs must be unique")
            ref = card["source"]
            if not isinstance(ref, dict) or set(ref) != {"namespace", "id", "version", "start", "end"}:
                raise IntakeError("invalid_bundle", "card source needs exact pinned version and offsets")
            if (not isinstance(ref["namespace"], str) or not ref["namespace"]
                    or not isinstance(ref["id"], str) or not ref["id"]
                    or type(ref["version"]) is not int or ref["version"] < 1):
                raise IntakeError("invalid_bundle", "card source identity and version are invalid")
            key = (ref["namespace"], ref["id"], ref["version"])
            if key not in pinned:
                raise IntakeError("unpinned_source", "card source revision is not pinned in this project")
            content, url, current = self._source(ref, principal_id, scopes)
            start, end = ref["start"], ref["end"]
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(content):
                raise IntakeError("invalid_citation", "card offsets are outside source content")
            if content[start:end] != card["quote"]:
                raise IntakeError("invalid_citation", "card quote does not match the pinned source span")
            _text(card["summary"], "card summary")
            cards[identity] = card
            source_hosts[identity] = (urlsplit(url).hostname or url).casefold()
            source_status[identity] = "current" if current else "superseded"
        for claim in _list(document["claims"], "claims", limit=500):
            if not isinstance(claim, dict) or set(claim) != {"id", "statement", "supports", "contradicts", "confidence"}:
                raise IntakeError("invalid_bundle", "claim needs statement, card links and confidence")
            _text(claim["id"], "claim id", limit=128)
            _text(claim["statement"], "claim statement")
            supports = _ids(claim["supports"], "supports", set(cards))
            contradicts = _ids(claim["contradicts"], "contradicts", set(cards))
            if set(supports) & set(contradicts) or not supports:
                raise IntakeError("invalid_bundle", "claim needs support distinct from contradiction")
            if claim["confidence"] not in {"low", "medium", "high"}:
                raise IntakeError("invalid_bundle", "claim confidence must be low, medium or high")
            if claim["confidence"] == "high" and len({source_hosts[c] for c in supports}) < 2:
                raise IntakeError("insufficient_independence", "high-confidence claim needs independent source hosts")
        claim_ids = [claim["id"] for claim in document["claims"]]
        if len(claim_ids) != len(set(claim_ids)):
            raise IntakeError("invalid_bundle", "claim IDs must be unique")
        for concept in _list(document["concepts"], "concepts", limit=200):
            if not isinstance(concept, dict) or set(concept) != {"id", "name", "explanation", "card_ids"}:
                raise IntakeError("invalid_bundle", "concept needs name, explanation and cited cards")
            _text(concept["id"], "concept id", limit=128)
            _text(concept["name"], "concept name", limit=256)
            _text(concept["explanation"], "concept explanation")
            if not _ids(concept["card_ids"], "concept card_ids", set(cards)):
                raise IntakeError("invalid_bundle", "concept needs at least one cited card")
        if len({c["id"] for c in document["concepts"]}) != len(document["concepts"]):
            raise IntakeError("invalid_bundle", "concept IDs must be unique")
        for section in ("brief", "mental_model", "map"):
            value = document[section]
            if not isinstance(value, dict) or set(value) != {"text", "card_ids"}:
                raise IntakeError("invalid_bundle", f"{section} needs text and cited cards")
            if value["text"]:
                _text(value["text"], section, limit=50_000)
                if not _ids(value["card_ids"], f"{section} card_ids", set(cards)):
                    raise IntakeError("invalid_bundle", f"{section} needs cited cards")
            elif value["card_ids"]:
                raise IntakeError("invalid_bundle", f"{section} has citations without text")
        for section in ("known", "uncertain", "unresolved"):
            _list(document[section], section)
            for item in document[section]:
                if not isinstance(item, dict) or set(item) != {"text", "card_ids"}:
                    raise IntakeError("invalid_bundle", f"{section} item needs text and cited cards")
                _text(item["text"], section)
                _ids(item["card_ids"], f"{section} card_ids", set(cards))
                if section == "known" and not item["card_ids"]:
                    raise IntakeError("invalid_bundle", "known point needs a cited card")
        reviews = _list(document["definition_of_done"], "definition_of_done")
        if len(reviews) != len(project["success_criteria"]):
            raise IntakeError("invalid_bundle", "review must cover each project success criterion")
        for expected, review in zip(project["success_criteria"], reviews):
            if not isinstance(review, dict) or set(review) != {"criterion", "met", "rationale", "card_ids"} or review["criterion"] != expected or type(review["met"]) is not bool:
                raise IntakeError("invalid_bundle", "review criterion must match the project in order")
            _text(review["rationale"], "review rationale")
            _ids(review["card_ids"], "review card_ids", set(cards))
            if review["met"] and not review["card_ids"]:
                raise IntakeError("invalid_bundle", "met criterion needs cited cards")
        ready = bool(cards and document["claims"] and document["concepts"])
        ready &= all(document[s]["text"] for s in ("brief", "mental_model", "map"))
        ready &= bool(document["known"] and document["uncertain"] and document["unresolved"])
        ready &= all(review["met"] for review in reviews)
        ready &= all(status == "current" for status in source_status.values())
        return document, {"ready": bool(ready), "source_status": source_status,
                          "independent_hosts": len(set(source_hosts.values()))}

    def save(self, namespace, project_id, command_key, document, *,
             principal_id, scopes, expected_revision=None):
        command_key = _text(command_key, "command_key", limit=256)
        project = self._project(namespace, project_id, principal_id, scopes, write=True)
        document, checks = self._validate(project, document, principal_id, scopes)
        bundle_id = "research-bundle:" + _hash([namespace, project_id])[:32]
        digest = _hash(document)
        self.conn.execute("BEGIN")
        try:
            row = self.conn.execute(
                "SELECT revision,request_hash FROM intake_research_bundles WHERE bundle_id=?",
                [bundle_id],
            ).fetchone()
            if row:
                replay = self.conn.execute(
                    "SELECT request_hash,revision FROM intake_research_bundle_commands "
                    "WHERE bundle_id=? AND command_key=?", [bundle_id, command_key],
                ).fetchone()
                if replay:
                    if replay[0] != digest:
                        raise IntakeError("idempotency_conflict", "command key identifies another bundle revision")
                    state = self._state(namespace, bundle_id, replay[1])
                    self.conn.execute("COMMIT")
                    return {**state, "idempotent": True}
                if expected_revision != row[0]:
                    raise IntakeError("revision_conflict", "inspect current bundle revision before saving")
                revision = row[0] + 1
                self.conn.execute("UPDATE intake_research_bundles SET revision=? WHERE bundle_id=? AND revision=?", [revision, bundle_id, row[0]])
            else:
                if expected_revision is not None:
                    raise IntakeError("revision_conflict", "bundle does not exist yet")
                revision = 1
                self.conn.execute("INSERT INTO intake_research_bundles VALUES (?,?,?,?,?,?)",
                                  [bundle_id, namespace, principal_id, project_id, 1, digest])
            state = {"contract": CONTRACT, "bundle_id": bundle_id, "namespace": namespace,
                     "owner": principal_id, "project_id": project_id, "project_revision": project["revision"],
                     "question_revision": project["question_revision"], "revision": revision,
                     "document": document, "checks": checks, "updated_at_ms": self.now()}
            self.conn.execute("INSERT INTO intake_research_bundle_revisions VALUES (?,?,?,?)",
                              [bundle_id, revision, _json(state), state["updated_at_ms"]])
            self.conn.execute("INSERT INTO intake_research_bundle_commands VALUES (?,?,?,?)",
                              [bundle_id, command_key, digest, revision])
            self.conn.execute("COMMIT")
            return {**state, "idempotent": False}
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def inspect(self, namespace, bundle_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, bundle_id)
        project = self._project(namespace, current["project_id"], principal_id, scopes)
        state = self._state(namespace, bundle_id, revision) if revision is not None else current
        document, checks = self._validate(project, state["document"], principal_id, scopes)
        checks["ready"] &= (state["question_revision"] == project["question_revision"]
                            and state["project_revision"] == project["revision"])
        return {**state, "document": document, "checks": checks}

    def export(self, namespace, bundle_id, *, principal_id, scopes):
        self.inspect(namespace, bundle_id, principal_id=principal_id, scopes=scopes)
        rows = self.conn.execute("SELECT content_json FROM intake_research_bundle_revisions "
                                 "WHERE bundle_id=? ORDER BY revision", [bundle_id]).fetchall()
        revisions = [json.loads(row[0]) for row in rows]
        project = self._project(namespace, revisions[-1]["project_id"], principal_id, scopes)
        for revision in revisions:
            # Export includes historical content, so check access to each historical source.
            for card in revision["document"]["cards"]:
                ref = card["source"]
                if (ref["namespace"] not in {namespace, *project["scope"]["namespaces"]}
                        and "operator" not in scopes
                        and f"namespace:{ref['namespace']}:read" not in scopes):
                    raise IntakeError("unauthorized", "historical source access is required for export")
                self._source(ref, principal_id, scopes)
        return {"contract": "noesis-intake-research-bundle-export-v1", "bundle_id": bundle_id,
                "revisions": revisions, "sha256": _hash(revisions)}


def verify_research_bundle_export(bundle):
    if not isinstance(bundle, dict) or bundle.get("contract") != "noesis-intake-research-bundle-export-v1":
        return {"valid": False, "reason": "invalid_contract"}
    revisions = bundle.get("revisions")
    if not isinstance(revisions, list) or not revisions:
        return {"valid": False, "reason": "missing_revisions"}
    if any(not isinstance(item, dict) or item.get("contract") != CONTRACT
           or item.get("bundle_id") != bundle.get("bundle_id")
           or item.get("revision") != index
           for index, item in enumerate(revisions, 1)):
        return {"valid": False, "reason": "invalid_revision_chain"}
    return {"valid": _hash(revisions) == bundle.get("sha256"),
            "reason": None if _hash(revisions) == bundle.get("sha256") else "digest_mismatch"}
