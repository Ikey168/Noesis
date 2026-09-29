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
REVIEW_SCOPE = "knowledge:intake:review"
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
CREATE TABLE IF NOT EXISTS intake_research_independence_reviews(
 review_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,bundle_id TEXT NOT NULL,
 bundle_revision BIGINT NOT NULL,claim_id TEXT NOT NULL,reviewer TEXT NOT NULL,
 command_key TEXT NOT NULL,request_hash TEXT NOT NULL,review_json TEXT NOT NULL,
 UNIQUE(bundle_id,bundle_revision,claim_id),
 UNIQUE(bundle_id,reviewer,command_key));
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

    def _claim_fingerprint(self, claim, cards):
        source_pins = []
        for card_id in claim["supports"]:
            card = cards[card_id]
            source_pins.append({
                "card_id": card_id,
                "source": card["source"],
                "quote_hash": _hash(card["quote"]),
            })
        claim_content = {key: claim[key] for key in (
            "id", "statement", "supports", "contradicts", "confidence",
        )}
        return _hash({"claim": claim_content, "supporting_source_pins": source_pins})

    def _review_records(self, namespace, bundle_id, revision):
        try:
            rows = self.conn.execute(
                "SELECT claim_id,review_json FROM intake_research_independence_reviews "
                "WHERE namespace=? AND bundle_id=? AND bundle_revision=?",
                [namespace, bundle_id, revision],
            ).fetchall()
        except Exception as exc:
            # Older databases do not have the review ledger yet. They remain
            # unverified until an authorized review is recorded after upgrade.
            import duckdb
            if isinstance(exc, duckdb.CatalogException):
                return {}
            raise
        return {claim_id: json.loads(review_json) for claim_id, review_json in rows}

    def _validate(self, project, document, principal_id, scopes, *, review_records=None):
        review_records = review_records or {}
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
        independence_reviews = []
        independence_blocked = False
        for claim in _list(document["claims"], "claims", limit=500):
            required_claim_fields = {"id", "statement", "supports", "contradicts", "confidence"}
            if not isinstance(claim, dict) or set(claim) not in (
                required_claim_fields,
                required_claim_fields | {"independence_review"},
            ):
                raise IntakeError("invalid_bundle", "claim needs statement, card links and confidence")
            _text(claim["id"], "claim id", limit=128)
            _text(claim["statement"], "claim statement")
            supports = _ids(claim["supports"], "supports", set(cards))
            contradicts = _ids(claim["contradicts"], "contradicts", set(cards))
            if set(supports) & set(contradicts) or not supports:
                raise IntakeError("invalid_bundle", "claim needs support distinct from contradiction")
            if claim["confidence"] not in {"low", "medium", "high"}:
                raise IntakeError("invalid_bundle", "claim confidence must be low, medium or high")
            review = claim.get("independence_review")
            if review is None:
                asserted = None
            else:
                if not isinstance(review, dict) or set(review) != {"status", "basis", "groups"}:
                    raise IntakeError("invalid_bundle", "independence assertion needs status, basis, and source groups")
                asserted = review
            if asserted is not None:
                status = asserted["status"]
                if not isinstance(status, str) or status not in {"independent", "dependent", "uncertain"}:
                    raise IntakeError("invalid_bundle", "independence review status is invalid")
                _text(asserted["basis"], "independence review basis")
                groups = _list(asserted["groups"], "independence groups", limit=100)
                if not groups:
                    raise IntakeError("invalid_bundle", "independence review needs source groups")
                group_ids = set()
                grouped_cards = []
                for group in groups:
                    if not isinstance(group, dict) or set(group) != {"group_id", "card_ids"}:
                        raise IntakeError("invalid_bundle", "source group needs an ID and supporting card IDs")
                    group_id = _text(group["group_id"], "independence group ID", limit=128)
                    if group_id in group_ids:
                        raise IntakeError("invalid_bundle", "independence group IDs must be unique")
                    group_ids.add(group_id)
                    members = _ids(group["card_ids"], "independence group card_ids", set(cards))
                    if not members or set(members) - set(supports):
                        raise IntakeError("invalid_bundle", "source groups must contain supporting cards")
                    grouped_cards.extend(members)
                if len(grouped_cards) != len(set(grouped_cards)) or set(grouped_cards) != set(supports):
                    raise IntakeError("invalid_bundle", "source groups must assign each supporting card exactly once")
                if status == "independent" and len(group_ids) < 2:
                    raise IntakeError("insufficient_independence", "independent review needs at least two declared reporting-origin groups")
            verified_review = review_records.get(claim["id"])
            fingerprint = self._claim_fingerprint(claim, cards)
            if verified_review is not None and verified_review.get("claim_hash") != fingerprint:
                verified_review = None
            verified = verified_review is not None and verified_review.get("status") == "independent"
            if claim["confidence"] == "high" and not verified:
                independence_blocked = True
            effective = verified_review if verified_review is not None else asserted
            independence_reviews.append({
                "claim_id": claim["id"],
                "status": effective["status"] if effective is not None else "unreviewed",
                "group_count": len(effective["groups"]) if effective is not None else 0,
                "distinct_host_count": len({source_hosts[c] for c in supports}),
                **({"basis": effective["basis"]} if effective is not None else {}),
                "verified": verified,
                **({
                    "review_id": verified_review["review_id"],
                    "reviewer": verified_review["reviewer"],
                    "method": verified_review["method"],
                    "reviewed_at_ms": verified_review["reviewed_at_ms"],
                    "provenance": verified_review["provenance"],
                } if verified_review is not None else {}),
            })
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
        reasons = []
        if not cards:
            reasons.append("evidence_cards_missing")
        if not document["claims"]:
            reasons.append("claims_missing")
        if not document["concepts"]:
            reasons.append("concepts_missing")
        for section in ("brief", "mental_model", "map"):
            if not document[section]["text"]:
                reasons.append(section + "_missing")
        for section in ("known", "uncertain", "unresolved"):
            if not document[section]:
                reasons.append(section + "_missing")
        if any(not review["met"] for review in reviews):
            reasons.append("definition_of_done_unmet")
        if any(status != "current" for status in source_status.values()):
            reasons.append("source_revision_superseded")
        if independence_blocked:
            reasons.append("source_independence_unverified")
        return document, {"ready": not reasons, "source_status": source_status,
                          "independent_hosts": len(set(source_hosts.values())),
                          "source_independence": independence_reviews,
                          "reasons": sorted(set(reasons))}

    def save(self, namespace, project_id, command_key, document, *,
             principal_id, scopes, expected_revision=None,
             iteration_receipt=None, _within_transaction=False):
        command_key = _text(command_key, "command_key", limit=256)
        project = self._project(namespace, project_id, principal_id, scopes, write=True)
        document, checks = self._validate(project, document, principal_id, scopes)
        bundle_id = "research-bundle:" + _hash([namespace, project_id])[:32]
        digest = _hash(document)
        if not _within_transaction:
            self.conn.execute("BEGIN")
        try:
            row = self.conn.execute(
                "SELECT revision,request_hash FROM intake_research_bundles WHERE bundle_id=?",
                [bundle_id],
            ).fetchone()
            history = []
            if row:
                previous = self._state(namespace, bundle_id)
                history = list(previous.get("iteration_history", []))
                replay = self.conn.execute(
                    "SELECT request_hash,revision FROM intake_research_bundle_commands "
                    "WHERE bundle_id=? AND command_key=?", [bundle_id, command_key],
                ).fetchone()
                if replay:
                    if replay[0] != digest:
                        raise IntakeError("idempotency_conflict", "command key identifies another bundle revision")
                    state = self.inspect(
                        namespace, bundle_id, principal_id=principal_id,
                        scopes=scopes, revision=replay[1],
                    )
                    if not _within_transaction:
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
            if iteration_receipt is not None:
                receipt = _bounded(iteration_receipt, limit=64_000)
                history.append(receipt)
                if len(history) > 500:
                    raise IntakeError("iteration_history_limit", "bundle iteration history exceeds 500 receipts")
            if history:
                state["iteration_history"] = history
            _bounded(state, limit=4_000_000)
            self.conn.execute("INSERT INTO intake_research_bundle_revisions VALUES (?,?,?,?)",
                              [bundle_id, revision, _json(state), state["updated_at_ms"]])
            self.conn.execute("INSERT INTO intake_research_bundle_commands VALUES (?,?,?,?)",
                              [bundle_id, command_key, digest, revision])
            if not _within_transaction:
                self.conn.execute("COMMIT")
            return {**state, "idempotent": False}
        except Exception:
            if not _within_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def review_independence(
        self, namespace, bundle_id, expected_revision, command_key, claim_id,
        status, basis, groups, *, principal_id, scopes,
    ):
        """Record an authorized, claim-pinned source-origin review."""
        if "operator" not in scopes and REVIEW_SCOPE not in scopes:
            raise IntakeError("unauthorized", "knowledge:intake:review is required to verify source independence")
        command_key = _text(command_key, "command_key", limit=256)
        claim_id = _text(claim_id, "claim_id", limit=128)
        basis = _text(basis, "review basis")
        method = "manual_origin_review"
        if not isinstance(status, str) or status not in {"independent", "dependent", "uncertain"}:
            raise IntakeError("invalid_independence_review", "review status must be independent, dependent, or uncertain")
        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be positive")

        self.conn.execute("BEGIN")
        try:
            current = self._state(namespace, bundle_id)
            if current["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "inspect the current bundle before reviewing it")
            project = self._project(namespace, current["project_id"], principal_id, scopes)
            claim = next((item for item in current["document"]["claims"]
                          if item["id"] == claim_id), None)
            if claim is None:
                raise IntakeError("claim_not_found", "claim is not present in this bundle revision")
            temporary = json.loads(_json(current["document"]))
            temporary_claim = next(item for item in temporary["claims"] if item["id"] == claim_id)
            temporary_claim["independence_review"] = {
                "status": status, "basis": basis, "groups": groups,
            }
            # Revalidate exact source pins and the reviewer's group assignment
            # under current source access before recording an attestation.
            _, _ = self._validate(project, temporary, principal_id, scopes)
            validated_claim = next(item for item in temporary["claims"] if item["id"] == claim_id)
            validated_cards = {card["id"]: card for card in temporary["cards"]}
            claim_hash = self._claim_fingerprint(validated_claim, validated_cards)
            request = {
                "namespace": namespace, "bundle_id": bundle_id,
                "bundle_revision": expected_revision, "claim_id": claim_id,
                "status": status, "basis": basis, "groups": validated_claim["independence_review"]["groups"],
                "method": method,
            }
            request_hash = _hash(request)
            prior = self.conn.execute(
                "SELECT request_hash,review_json FROM intake_research_independence_reviews "
                "WHERE bundle_id=? AND reviewer=? AND command_key=?",
                [bundle_id, principal_id, command_key],
            ).fetchone()
            if prior:
                if prior[0] != request_hash:
                    raise IntakeError("idempotency_conflict", "command key identifies another independence review")
                review = json.loads(prior[1])
                inspected = self.inspect(
                    namespace, bundle_id, principal_id=principal_id, scopes=scopes,
                    revision=expected_revision,
                )
                self.conn.execute("COMMIT")
                return {"review": review, "bundle": inspected, "idempotent": True}
            existing = self.conn.execute(
                "SELECT reviewer FROM intake_research_independence_reviews "
                "WHERE bundle_id=? AND bundle_revision=? AND claim_id=?",
                [bundle_id, expected_revision, claim_id],
            ).fetchone()
            if existing:
                raise IntakeError(
                    "independence_already_reviewed",
                    "this claim revision already has an authorized review; revise the bundle to reopen review",
                )
            reviewed_at_ms = self.now()
            source_pins = []
            for group in validated_claim["independence_review"]["groups"]:
                for member in group["card_ids"]:
                    card = validated_cards[member]
                    ref = card["source"]
                    source_pins.append({
                        "group_id": group["group_id"],
                        "card_id": member,
                        "namespace": ref["namespace"],
                        "source_id": ref["id"],
                        "source_version": ref["version"],
                        "quote_hash": _hash(card["quote"]),
                    })
            review = {
                "contract": "noesis-intake-research-independence-review-v1",
                "review_id": "research-independence-review:" + _hash([
                    namespace, bundle_id, expected_revision, claim_id,
                    principal_id, request_hash,
                ])[:32],
                "namespace": namespace,
                "bundle_id": bundle_id,
                "bundle_revision": expected_revision,
                "claim_id": claim_id,
                "claim_hash": claim_hash,
                "status": status,
                "basis": basis,
                "method": method,
                "groups": validated_claim["independence_review"]["groups"],
                "reviewer": principal_id,
                "reviewed_at_ms": reviewed_at_ms,
                "provenance": {
                    "bundle_revision": expected_revision,
                    "claim_hash": claim_hash,
                    "source_pins": source_pins,
                },
            }
            self.conn.execute(
                "INSERT INTO intake_research_independence_reviews VALUES (?,?,?,?,?,?,?,?,?)",
                [review["review_id"], namespace, bundle_id, expected_revision,
                 claim_id, principal_id, command_key, request_hash, _json(review)],
            )
            inspected = self.inspect(
                namespace, bundle_id, principal_id=principal_id, scopes=scopes,
                revision=expected_revision,
            )
            result = {"review": review, "bundle": inspected, "idempotent": False}
            self.conn.execute("COMMIT")
            return result
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def inspect(self, namespace, bundle_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, bundle_id)
        project = self._project(namespace, current["project_id"], principal_id, scopes)
        state = self._state(namespace, bundle_id, revision) if revision is not None else current
        review_records = self._review_records(namespace, bundle_id, state["revision"])
        document, checks = self._validate(
            project, state["document"], principal_id, scopes,
            review_records=review_records,
        )
        if state["question_revision"] != project["question_revision"]:
            checks["reasons"].append("project_question_revision_changed")
        if state["project_revision"] != project["revision"]:
            checks["reasons"].append("project_revision_changed")
        checks["reasons"] = sorted(set(checks["reasons"]))
        checks["ready"] &= not checks["reasons"]
        return {**state, "document": document, "checks": checks, "idempotent": True}

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
        try:
            reviews = self.conn.execute(
                "SELECT review_json FROM intake_research_independence_reviews "
                "WHERE namespace=? AND bundle_id=? ORDER BY bundle_revision,claim_id",
                [namespace, bundle_id],
            ).fetchall()
        except Exception as exc:
            import duckdb
            if not isinstance(exc, duckdb.CatalogException):
                raise
            reviews = []
        independence_reviews = [json.loads(row[0]) for row in reviews]
        digest_content = {"revisions": revisions, "independence_reviews": independence_reviews}
        return {"contract": "noesis-intake-research-bundle-export-v1", "bundle_id": bundle_id,
                "revisions": revisions, "independence_reviews": independence_reviews,
                "sha256": _hash(digest_content)}


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
    reviews = bundle.get("independence_reviews")
    if reviews is None:
        digest = _hash(revisions)
    elif isinstance(reviews, list):
        digest = _hash({"revisions": revisions, "independence_reviews": reviews})
    else:
        return {"valid": False, "reason": "invalid_independence_reviews"}
    return {"valid": digest == bundle.get("sha256"),
            "reason": None if digest == bundle.get("sha256") else "digest_mismatch"}
