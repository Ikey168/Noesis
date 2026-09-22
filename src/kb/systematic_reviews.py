"""Protocol amendments, publication-preserving candidates, and independent screening."""

import csv
import io
import json
import time
from datetime import date

from src.kb.research_projects import _hash, _json, _strings

READ_SCOPE = "knowledge:reviews:read"
WRITE_SCOPE = "knowledge:reviews:write"
_DDL = """
CREATE TABLE IF NOT EXISTS systematic_review_protocols(
 protocol_id TEXT PRIMARY KEY,namespace TEXT NOT NULL,owner TEXT NOT NULL,request_hash TEXT NOT NULL,revision BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS systematic_review_protocol_revisions(
 protocol_id TEXT NOT NULL,revision BIGINT NOT NULL,content_json TEXT NOT NULL,PRIMARY KEY(protocol_id,revision));
CREATE TABLE IF NOT EXISTS systematic_review_candidates(
 candidate_id TEXT PRIMARY KEY,protocol_id TEXT NOT NULL,content_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS systematic_review_screening(
 candidate_id TEXT NOT NULL,stage TEXT NOT NULL,reviewer TEXT NOT NULL,revision BIGINT NOT NULL,
 content_json TEXT NOT NULL,PRIMARY KEY(candidate_id,stage,reviewer,revision));
CREATE TABLE IF NOT EXISTS systematic_review_adjudications(
 candidate_id TEXT NOT NULL,stage TEXT NOT NULL,screening_hash TEXT NOT NULL,content_json TEXT NOT NULL,
 PRIMARY KEY(candidate_id,stage,screening_hash));
CREATE TABLE IF NOT EXISTS systematic_review_fields(
 field_id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL,content_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS systematic_review_field_reviews(
 field_id TEXT NOT NULL,reviewer TEXT NOT NULL,revision BIGINT NOT NULL,content_json TEXT NOT NULL,
 PRIMARY KEY(field_id,reviewer,revision));
"""


class ReviewError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 100000:
        raise ReviewError("invalid_review", "bounded nonempty text is required")
    return value


def _protocol(value):
    required = {"question", "inclusion", "exclusion", "databases", "search_expressions", "date_from", "date_to", "reviewers", "fields"}
    if not isinstance(value, dict) or set(value) != required:
        raise ReviewError("invalid_protocol", "protocol requires question, eligibility criteria, search plan, date range, reviewers, and fields")
    _text(value["question"])
    for key in ("inclusion", "exclusion", "databases", "search_expressions", "reviewers", "fields"):
        _strings(value[key], key, required=True)
    if len(set(value["reviewers"])) < 2:
        raise ReviewError("invalid_protocol", "at least two distinct screening reviewers are required")
    try:
        if date.fromisoformat(value["date_from"]) > date.fromisoformat(value["date_to"]):
            raise ValueError
    except (TypeError, ValueError):
        raise ReviewError("invalid_protocol", "search dates require an ordered ISO date range") from None
    if len(_json(value).encode()) > 4 * 1024 * 1024:
        raise ReviewError("invalid_protocol", "protocol exceeds 4 MiB")
    return json.loads(_json(value))


class SystematicReviewStore:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    @staticmethod
    def _authorize(state, principal_id, scopes, *, write=False, owner_only=False):
        if principal_id and "operator" in scopes:
            return
        allowed = {state["owner"]} if owner_only else {state["owner"], *state["content"]["reviewers"]}
        if not principal_id or principal_id not in allowed or (WRITE_SCOPE if write else READ_SCOPE) not in scopes:
            raise ReviewError("unauthorized", "current review scope and protocol participation are required")
        ns = state["namespace"]
        if f"namespace:{ns}:write" not in scopes and (write or f"namespace:{ns}:read" not in scopes):
            raise ReviewError("unauthorized", "current protocol namespace access is required")

    def _state(self, namespace, protocol_id, revision=None):
        row = self.conn.execute("""SELECT r.content_json FROM systematic_review_protocols p JOIN systematic_review_protocol_revisions r
            ON r.protocol_id=p.protocol_id WHERE p.namespace=? AND p.protocol_id=? AND r.revision=coalesce(?,p.revision)""", [namespace, protocol_id, revision]).fetchone()
        if not row:
            raise ReviewError("protocol_unavailable", "protocol revision is unavailable")
        return json.loads(row[0])

    def inspect(self, namespace, protocol_id, *, principal_id, scopes, revision=None):
        current = self._state(namespace, protocol_id)
        self._authorize(current, principal_id, scopes)
        state = self._state(namespace, protocol_id, revision) if revision is not None else current
        self._authorize(state, principal_id, scopes)
        return state

    def _abort(self, exc):
        import duckdb
        self.conn.execute("ROLLBACK")
        if isinstance(exc, (duckdb.TransactionException, duckdb.ConstraintException)):
            raise ReviewError("revision_conflict", "concurrent review update; inspect and retry") from exc
        raise exc

    def create(self, namespace, request_key, content, *, principal_id, scopes):
        state = {"contract": "noesis-review-protocol-v1", "protocol_id": "protocol:" + _hash([_text(namespace), principal_id, _text(request_key)])[:32],
                 "namespace": namespace, "owner": principal_id, "revision": 1, "content": _protocol(content), "amendment": None}
        self._authorize(state, principal_id, scopes, write=True, owner_only=True)
        digest = _hash(state)
        prior = self.conn.execute("SELECT request_hash FROM systematic_review_protocols WHERE protocol_id=?", [state["protocol_id"]]).fetchone()
        if prior:
            if prior[0] != digest:
                raise ReviewError("idempotency_conflict", "protocol key already identifies a different request")
            current = self._state(namespace, state["protocol_id"])
            self._authorize(current, principal_id, scopes, write=True, owner_only=True)
            return {**current, "idempotent": True}
        state["recorded_at_ms"] = self.now()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO systematic_review_protocols VALUES (?,?,?,?,1)", [state["protocol_id"], namespace, principal_id, digest])
            self.conn.execute("INSERT INTO systematic_review_protocol_revisions VALUES (?,1,?)", [state["protocol_id"], _json(state)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return state

    def amend(self, namespace, protocol_id, expected_revision, content, rationale, *, principal_id, scopes):
        content, rationale = _protocol(content), _text(rationale)
        self.conn.execute("BEGIN")
        try:
            state = self._state(namespace, protocol_id)
            self._authorize(state, principal_id, scopes, write=True, owner_only=True)
            row = self.conn.execute("UPDATE systematic_review_protocols SET revision=revision+1 WHERE protocol_id=? AND revision=? RETURNING revision", [protocol_id, expected_revision]).fetchone()
            if not row:
                raise ReviewError("revision_conflict", "protocol changed; inspect and retry")
            state.update(content=content, revision=int(row[0]), amendment=rationale, recorded_at_ms=self.now())
            self.conn.execute("INSERT INTO systematic_review_protocol_revisions VALUES (?,?,?)", [protocol_id, state["revision"], _json(state)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return state

    @staticmethod
    def _source_access(candidate, scopes):
        ns = candidate["source_namespace"]
        if "operator" not in scopes and f"namespace:{ns}:read" not in scopes and f"namespace:{ns}:write" not in scopes:
            raise ReviewError("unauthorized", "current candidate source namespace access is required")

    @staticmethod
    def _document_access(candidate, scopes):
        if "operator" not in scopes and f"document:{candidate['publication_id']}:read" not in scopes:
            raise ReviewError("unauthorized", "explicit document read access is required for shared-corpus evidence")

    def add_candidate(self, namespace, protocol_id, protocol_revision, *, publication_id, source_revision,
                      source_namespace, search_run_id, study_id, title, abstract, full_text_available, principal_id, scopes):
        protocol = self.inspect(namespace, protocol_id, revision=protocol_revision, principal_id=principal_id, scopes=scopes)
        self._authorize(protocol, principal_id, scopes, write=True)
        if type(full_text_available) is not bool or not isinstance(abstract, str) or len(abstract) > 100000:
            raise ReviewError("invalid_candidate", "candidate requires bounded abstract and explicit full-text availability")
        candidate = {"protocol_id": protocol_id, "protocol_revision": protocol_revision,
                     "publication_id": _text(publication_id), "source_revision": _text(source_revision),
                     "source_namespace": _text(source_namespace), "search_run_id": _text(search_run_id),
                     "study_id": _text(study_id), "title": _text(title), "abstract": abstract,
                     "full_text_available": full_text_available}
        self._source_access(candidate, scopes)
        candidate["candidate_id"] = "candidate:" + _hash(candidate)[:32]
        # Stable search/protocol/publication identities preserve repeated discovery
        # and related publications without content-based identity collapse.
        self.conn.execute("INSERT OR IGNORE INTO systematic_review_candidates VALUES (?,?,?)", [candidate["candidate_id"], protocol_id, _json(candidate)])
        return candidate

    def _candidate(self, namespace, candidate_id, principal_id, scopes):
        row = self.conn.execute("SELECT content_json FROM systematic_review_candidates WHERE candidate_id=?", [candidate_id]).fetchone()
        if not row:
            raise ReviewError("candidate_unavailable", "candidate is unavailable")
        candidate = json.loads(row[0])
        protocol = self.inspect(namespace, candidate["protocol_id"], revision=candidate["protocol_revision"], principal_id=principal_id, scopes=scopes)
        self._source_access(candidate, scopes)
        return candidate, protocol

    def _screening(self, candidate_id, stage):
        rows = self.conn.execute("""SELECT content_json FROM systematic_review_screening WHERE candidate_id=? AND stage=?
            QUALIFY row_number() OVER (PARTITION BY reviewer ORDER BY revision DESC)=1 ORDER BY reviewer""", [candidate_id, stage]).fetchall()
        return [json.loads(row[0]) for row in rows]

    def inspect_candidate(self, namespace, candidate_id, *, principal_id, scopes):
        candidate, protocol = self._candidate(namespace, candidate_id, principal_id, scopes)
        coordinator = principal_id == protocol["owner"] or "operator" in scopes
        screening = {}
        for stage in ("title_abstract", "full_text"):
            if coordinator:
                screening[stage] = self._result(candidate, protocol, stage)
            else:
                own = [row for row in self._screening(candidate_id, stage) if row["reviewer"] == principal_id]
                screening[stage] = {"own_decisions": own, "other_reviews_hidden": True}
        return {**candidate, "protocol": protocol, "screening": screening}

    def list_candidates(self, namespace, protocol_id, *, principal_id, scopes, limit=50, offset=0):
        self.inspect(namespace, protocol_id, principal_id=principal_id, scopes=scopes)
        if type(limit) is not int or type(offset) is not int or not 1 <= limit <= 100 or offset < 0:
            raise ReviewError("invalid_limit", "candidate pages require limit 1..100 and nonnegative offset")
        rows = self.conn.execute("SELECT candidate_id FROM systematic_review_candidates WHERE protocol_id=? ORDER BY candidate_id LIMIT ? OFFSET ?", [protocol_id, limit, offset]).fetchall()
        candidates = []
        for (candidate_id,) in rows:
            try:
                candidates.append(self.inspect_candidate(namespace, candidate_id, principal_id=principal_id, scopes=scopes))
            except ReviewError as exc:
                if exc.code != "unauthorized":
                    raise
        return {"candidates": candidates, "next_offset": offset + limit if len(rows) == limit else None}

    def _result(self, candidate, protocol, stage):
        decisions = self._screening(candidate["candidate_id"], stage)
        digest = _hash(decisions)
        adjudication = self.conn.execute("SELECT content_json FROM systematic_review_adjudications WHERE candidate_id=? AND stage=? AND screening_hash=?", [candidate["candidate_id"], stage, digest]).fetchone()
        status = "pending"
        if adjudication:
            status = json.loads(adjudication[0])["decision"]
        elif {v["reviewer"] for v in decisions} >= set(protocol["content"]["reviewers"]):
            choices = {v["decision"] for v in decisions}
            status = next(iter(choices)) if len(choices) == 1 else "disputed"
        return {"status": status, "screening_hash": digest, "decisions": decisions,
                "adjudication": json.loads(adjudication[0]) if adjudication else None}

    def screen(self, namespace, candidate_id, stage, expected_revision, decision, reason, *, principal_id, scopes):
        if stage not in {"title_abstract", "full_text"} or decision not in {"include", "exclude", "pending"}:
            raise ReviewError("invalid_screening", "unsupported screening stage or decision")
        _text(reason)
        self.conn.execute("BEGIN")
        try:
            candidate, protocol = self._candidate(namespace, candidate_id, principal_id, scopes)
            self._authorize(protocol, principal_id, scopes, write=True)
            if principal_id not in protocol["content"]["reviewers"]:
                raise ReviewError("unauthorized", "screening must identify a registered independent reviewer")
            if stage == "full_text":
                if self._result(candidate, protocol, "title_abstract")["status"] != "include":
                    raise ReviewError("screening_pending", "title/abstract screening must first resolve to include")
                if not candidate["full_text_available"] and decision != "pending":
                    raise ReviewError("full_text_unavailable", "missing full text stays pending; it cannot be included or excluded")
            previous = self.conn.execute("SELECT coalesce(max(revision),0) FROM systematic_review_screening WHERE candidate_id=? AND stage=? AND reviewer=?", [candidate_id, stage, principal_id]).fetchone()[0]
            if previous != expected_revision:
                raise ReviewError("revision_conflict", "reviewer's screening changed; inspect and retry")
            result = {"candidate_id": candidate_id, "protocol_revision": candidate["protocol_revision"], "stage": stage,
                      "reviewer": principal_id, "revision": previous + 1, "decision": decision, "reason": reason, "recorded_at_ms": self.now()}
            self.conn.execute("INSERT INTO systematic_review_screening VALUES (?,?,?,?,?)", [candidate_id, stage, principal_id, previous + 1, _json(result)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return result

    def adjudicate(self, namespace, candidate_id, stage, screening_hash, decision, reason, *, principal_id, scopes):
        if stage not in {"title_abstract", "full_text"} or decision not in {"include", "exclude", "pending"}:
            raise ReviewError("invalid_screening", "unsupported adjudication stage or decision")
        _text(reason)
        self.conn.execute("BEGIN")
        try:
            candidate, protocol = self._candidate(namespace, candidate_id, principal_id, scopes)
            self._authorize(protocol, principal_id, scopes, write=True, owner_only=True)
            result = self._result(candidate, protocol, stage)
            if result["screening_hash"] != screening_hash:
                raise ReviewError("revision_conflict", "screening changed before adjudication")
            if result["status"] != "disputed":
                raise ReviewError("no_disagreement", "adjudication requires an unresolved disagreement")
            if stage == "full_text" and not candidate["full_text_available"] and decision != "pending":
                raise ReviewError("full_text_unavailable", "missing full text stays pending")
            result = {"decision": decision, "reason": reason, "reviewer": principal_id, "recorded_at_ms": self.now(), "screening_hash": screening_hash}
            self.conn.execute("INSERT INTO systematic_review_adjudications VALUES (?,?,?,?)", [candidate_id, stage, screening_hash, _json(result)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return result

    def export(self, namespace, protocol_id, *, principal_id, scopes, limit=10000):
        protocol = self.inspect(namespace, protocol_id, principal_id=principal_id, scopes=scopes)
        self._authorize(protocol, principal_id, scopes, owner_only=True)
        if type(limit) is not int or not 1 <= limit <= 10000:
            raise ReviewError("invalid_limit", "export limit must be one to 10000")
        rows = self.conn.execute("SELECT content_json FROM systematic_review_candidates WHERE protocol_id=? ORDER BY candidate_id LIMIT ?", [protocol_id, limit + 1]).fetchall()
        if len(rows) > limit:
            raise ReviewError("export_overflow", "candidate count exceeds the explicit export budget")
        candidates, counts = [], {}
        for (encoded,) in rows:
            candidate = json.loads(encoded)
            self._source_access(candidate, scopes)
            original = self.inspect(namespace, protocol_id, revision=candidate["protocol_revision"], principal_id=principal_id, scopes=scopes)
            stages = {stage: self._result(candidate, original, stage) for stage in ("title_abstract", "full_text")}
            status = stages["title_abstract"]["status"]
            if status == "include":
                status = stages["full_text"]["status"] if candidate["full_text_available"] else "full_text_unavailable"
            counts[status] = counts.get(status, 0) + 1
            fields = []
            for field_id, encoded_field in self.conn.execute("SELECT field_id,content_json FROM systematic_review_fields WHERE candidate_id=? ORDER BY field_id", [candidate["candidate_id"]]).fetchall():
                self._document_access(candidate, scopes)
                reviews = [json.loads(row[0]) for row in self.conn.execute("""SELECT content_json FROM systematic_review_field_reviews
                    WHERE field_id=? QUALIFY row_number() OVER (PARTITION BY reviewer ORDER BY revision DESC)=1 ORDER BY reviewer""", [field_id]).fetchall()]
                choices = {row["decision"] for row in reviews}
                fields.append({**json.loads(encoded_field), "reviews": reviews,
                    "review_state": "pending" if not choices else next(iter(choices)) if len(choices) == 1 else "disputed"})
            candidates.append({**candidate, "screening": stages, "status": status, "fields": fields})
        amendments = [json.loads(row[0]) for row in self.conn.execute("SELECT content_json FROM systematic_review_protocol_revisions WHERE protocol_id=? ORDER BY revision", [protocol_id]).fetchall()]
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=["candidate_id", "title", "abstract"])
        writer.writeheader()
        for candidate in candidates:
            writer.writerow({key: candidate[key] for key in writer.fieldnames})
        return {"contract": "noesis-systematic-review-export-v1", "protocol": protocol, "amendments": amendments,
                "candidates": candidates, "candidate_count": len(candidates), "counts": counts,
                "publication_count": len({(v["source_namespace"], v["publication_id"]) for v in candidates}),
                "study_count": len({v["study_id"] for v in candidates}), "asreview_unlabeled_csv": stream.getvalue(),
                "prisma_reporting_map": {"4": "protocol.content.question", "5": "protocol.content.inclusion/exclusion; candidates.study_id",
                    "6": "protocol.content.databases; candidates.search_run_id (actual search dates need source receipts)",
                    "7": "protocol.content.search_expressions", "8": "protocol.content.reviewers; candidates.screening",
                    "9": "candidates.fields.producer/reviews", "10a/10b": "protocol.content.fields",
                    "16a": "candidate_count/publication_count/study_count/counts (distinct denominators)",
                    "16b": "candidates.screening.decisions.reason", "17": "candidates.fields", "24b/24c": "protocol; amendments"},
                "limitations": ["Candidate counts include repeated search discoveries and source revisions",
                    "Full-text availability and search-run references are supplied provenance, not independently verified",
                    "ASReview export supports ordering only; model suggestions never become screening decisions",
                    "Screening labels are recorded reviewer decisions, not a validated human evaluation dataset",
                    "PRISMA mapping identifies reporting inputs, not methodological compliance; risk-of-bias and synthesis remain separate"]}

    def extract_field(self, namespace, candidate_id, field_name, value, start, end, *, principal_id, scopes):
        candidate, protocol = self._candidate(namespace, candidate_id, principal_id, scopes)
        self._authorize(protocol, principal_id, scopes, write=True)
        # A caller-supplied source namespace must not grant access to arbitrary
        # documents in the shared corpus. Require the independently granted ID.
        self._document_access(candidate, scopes)
        if field_name not in protocol["content"]["fields"]:
            raise ReviewError("invalid_field", "field is not defined by the candidate's protocol revision")
        _text(value)
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            raise ReviewError("invalid_locator", "field evidence requires an exact nonempty character span")
        exists = self.conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name='document_revision_records'").fetchone()
        source = self.conn.execute("SELECT payload_json FROM document_revision_records WHERE document_id=? AND revision_id=? AND committed_watermark IS NOT NULL",
            [candidate["publication_id"], candidate["source_revision"]]).fetchone() if exists else None
        if not source:
            raise ReviewError("source_unavailable", "pinned committed source revision is unavailable")
        text = json.loads(source[0]).get("content")
        if not isinstance(text, str) or end > len(text):
            raise ReviewError("source_unavailable", "full text or requested exact span is unavailable")
        result = {"candidate_id": candidate_id, "study_id": candidate["study_id"], "protocol_revision": candidate["protocol_revision"],
                  "field": field_name, "value": value, "quote": text[start:end],
                  "locator": {"document_id": candidate["publication_id"], "revision_id": candidate["source_revision"],
                              "coordinate_field": "content", "start": start, "end": end}, "producer": principal_id}
        result["field_id"] = "review-field:" + _hash(result)[:32]
        self.conn.execute("INSERT OR IGNORE INTO systematic_review_fields VALUES (?,?,?)", [result["field_id"], candidate_id, _json(result)])
        return {**result, "review_state": "pending"}

    def review_field(self, namespace, field_id, expected_revision, decision, reason, *, principal_id, scopes):
        if decision not in {"accepted", "rejected"}:
            raise ReviewError("invalid_decision", "field review must accept or reject the proposed value")
        _text(reason)
        self.conn.execute("BEGIN")
        try:
            row = self.conn.execute("SELECT content_json FROM systematic_review_fields WHERE field_id=?", [field_id]).fetchone()
            if not row:
                raise ReviewError("field_unavailable", "field extraction is unavailable")
            field = json.loads(row[0])
            candidate, protocol = self._candidate(namespace, field["candidate_id"], principal_id, scopes)
            self._document_access(candidate, scopes)
            self._authorize(protocol, principal_id, scopes, write=True)
            if principal_id == field["producer"] or principal_id not in protocol["content"]["reviewers"]:
                raise ReviewError("independent_review_required", "another registered reviewer must evaluate the field")
            previous = self.conn.execute("SELECT coalesce(max(revision),0) FROM systematic_review_field_reviews WHERE field_id=? AND reviewer=?", [field_id, principal_id]).fetchone()[0]
            if previous != expected_revision:
                raise ReviewError("revision_conflict", "field review changed; inspect and retry")
            result = {"field_id": field_id, "reviewer": principal_id, "decision": decision, "reason": reason,
                      "revision": previous + 1, "recorded_at_ms": self.now()}
            self.conn.execute("INSERT INTO systematic_review_field_reviews VALUES (?,?,?,?)", [field_id, principal_id, previous + 1, _json(result)])
            self.conn.execute("COMMIT")
        except Exception as exc:
            self._abort(exc)
        return result
