"""Owner-scoped, versioned retrieval practice with attempt-before-reveal history.

Answers and assessments are authored or reported by the caller. Neither a model
answer nor a self-rating is independent evidence of mastery.
"""

from __future__ import annotations

import json
import time
from itertools import pairwise
from typing import Any

from src.kb.intake_modes import (
    IntakeError,
    IntakeStore,
    _bounded,
    _hash,
    _json,
    _reference,
    _text,
)

CONTRACT = "noesis-intake-practice-v1"
EXPORT_CONTRACT = "noesis-intake-practice-export-v1"
INTERVALS = (1, 3, 7, 14, 30, 60, 120)
_DDL = """
CREATE TABLE IF NOT EXISTS intake_practice_packs (
  pack_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
  request_hash TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_practice_pack_revisions (
  pack_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
  PRIMARY KEY(pack_id,revision)
);
CREATE TABLE IF NOT EXISTS intake_practice_pack_edits (
  pack_id TEXT NOT NULL, edit_key TEXT NOT NULL, request_hash TEXT NOT NULL,
  revision BIGINT NOT NULL, PRIMARY KEY(pack_id,edit_key)
);
CREATE TABLE IF NOT EXISTS intake_practice_progress (
  pack_id TEXT NOT NULL, card_id TEXT NOT NULL, pack_revision BIGINT NOT NULL,
  due_at_ms BIGINT NOT NULL, stage BIGINT NOT NULL, unassisted_passes BIGINT NOT NULL,
  last_assessed_ms BIGINT, PRIMARY KEY(pack_id,card_id)
);
CREATE TABLE IF NOT EXISTS intake_practice_reviews (
  review_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, owner TEXT NOT NULL,
  request_hash TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS intake_practice_review_revisions (
  review_id TEXT NOT NULL, revision BIGINT NOT NULL, content_json TEXT NOT NULL,
  PRIMARY KEY(review_id,revision)
);
CREATE TABLE IF NOT EXISTS intake_practice_review_commands (
  review_id TEXT NOT NULL, command_key TEXT NOT NULL, request_hash TEXT NOT NULL,
  revision BIGINT NOT NULL, PRIMARY KEY(review_id,command_key)
);
CREATE INDEX IF NOT EXISTS idx_intake_practice_due
  ON intake_practice_packs(namespace,owner);
"""


def _cards(values: Any, namespace: str, scopes: set[str]) -> list[dict]:
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        raise IntakeError("invalid_practice", "pack needs 1–100 cards")
    result = []
    for index, raw in enumerate(values):
        if not isinstance(raw, dict) or set(raw) != {
            "kind", "prompt", "answer", "mastery_criterion", "references"
        }:
            raise IntakeError("invalid_practice", "card fields are incomplete")
        if raw["kind"] not in {"recall", "procedure", "explanation"}:
            raise IntakeError("invalid_practice", "unsupported practice kind")
        references = raw["references"]
        if not isinstance(references, list) or not 1 <= len(references) <= 20:
            raise IntakeError("invalid_practice", "card needs 1–20 source or concept references")
        result.append({
            "id": f"card-{index + 1}", "kind": raw["kind"],
            "prompt": _text(raw["prompt"], "prompt", limit=2000),
            "answer": _text(raw["answer"], "answer", limit=5000),
            "mastery_criterion": _text(raw["mastery_criterion"], "criterion", limit=1000),
            "references": [_reference(ref, namespace, scopes) for ref in references],
            "answer_status": "author_supplied_unverified",
        })
    return result


def _intervals(values: Any) -> list[int]:
    if not isinstance(values, list) or not 1 <= len(values) <= 20 or any(
        type(day) is not int or not 1 <= day <= 3650 for day in values
    ) or any(left >= right for left, right in pairwise(values)):
        raise IntakeError("invalid_schedule", "interval days must increase from 1 to 3650")
    return values


def verify_practice_export(bundle: Any) -> dict:
    """Check integrity and revision order; this does not authenticate the exporter."""
    if not isinstance(bundle, dict):
        return {"valid": False, "reason": "bundle must be an object"}
    if bundle.get("contract") != EXPORT_CONTRACT:
        return {"valid": False, "reason": "unsupported export contract"}
    supplied = bundle.get("sha256")
    payload = {key: value for key, value in bundle.items()
               if key not in {"sha256", "exported_at_ms"}}
    try:
        _bounded(bundle, limit=20_000_000)
        actual = _hash(payload)
    except (IntakeError, TypeError, ValueError):
        return {"valid": False, "reason": "invalid export data"}
    if not isinstance(supplied, str) or actual != supplied:
        return {"valid": False, "reason": "digest mismatch"}
    revisions = payload.get("pack_revisions")
    if not isinstance(revisions, list) or not revisions or [
        item.get("revision") for item in revisions if isinstance(item, dict)
    ] != list(range(1, len(revisions) + 1)) or payload.get("current_revision") != len(revisions):
        return {"valid": False, "reason": "pack revision chain is incomplete"}
    reviews = payload.get("reviews")
    if not isinstance(reviews, list) or any(
        not isinstance(entry, dict) or not isinstance(entry.get("revisions"), list)
        or [item.get("revision") for item in entry["revisions"]
            if isinstance(item, dict)] != list(range(1, len(entry["revisions"]) + 1))
        for entry in reviews
    ):
        return {"valid": False, "reason": "review revision chain is incomplete"}
    return {"valid": True, "pack_revision_count": len(revisions),
            "review_count": len(reviews)}


class IntakePracticeStore:
    def __init__(self, conn: Any, *, initialize=True, now=None):
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)

    def _pack(self, namespace: str, pack_id: str, revision: int | None = None) -> dict:
        row = self.conn.execute(
            "SELECT r.content_json FROM intake_practice_packs p "
            "JOIN intake_practice_pack_revisions r ON p.pack_id=r.pack_id "
            "WHERE p.namespace=? AND p.pack_id=? AND r.revision=coalesce(?,p.revision)",
            [namespace, pack_id, revision],
        ).fetchone()
        if row is None:
            raise IntakeError("pack_not_found", "practice pack revision is unavailable")
        return json.loads(row[0])

    def _review(self, namespace: str, review_id: str, revision: int | None = None) -> dict:
        row = self.conn.execute(
            "SELECT r.content_json FROM intake_practice_reviews p "
            "JOIN intake_practice_review_revisions r ON p.review_id=r.review_id "
            "WHERE p.namespace=? AND p.review_id=? AND r.revision=coalesce(?,p.revision)",
            [namespace, review_id, revision],
        ).fetchone()
        if row is None:
            raise IntakeError("review_not_found", "practice review revision is unavailable")
        return json.loads(row[0])

    @staticmethod
    def _access(value: dict, principal_id: str, scopes: set[str], *, write=False) -> None:
        IntakeStore._authorize(value, principal_id, scopes, write=write)
        for card in value.get("cards", []):
            for ref in card["references"]:
                _reference(ref, value["namespace"], scopes)

    def create_pack(
        self, namespace: str, request_key: str, title: str, cards: list[dict], *,
        principal_id: str, scopes: set[str], interval_days: list[int] | None = None,
    ) -> dict:
        request_key = _text(request_key, "request_key", limit=256)
        pack_id = "practice-pack:" + _hash([namespace, principal_id, request_key])[:32]
        content = {
            "contract": CONTRACT, "pack_id": pack_id, "namespace": namespace,
            "owner": principal_id, "revision": 1,
            "title": _text(title, "title", limit=1000),
            "cards": _cards(cards, namespace, scopes),
            "interval_days": _intervals(
                list(INTERVALS) if interval_days is None else interval_days
            ),
            "created_at_ms": self.now(),
        }
        self._access(content, principal_id, scopes, write=True)
        _bounded(content)
        digest = _hash({key: value for key, value in content.items() if key != "created_at_ms"})
        prior = self.conn.execute(
            "SELECT request_hash FROM intake_practice_packs WHERE pack_id=?", [pack_id]
        ).fetchone()
        if prior:
            if prior[0] != digest:
                raise IntakeError("idempotency_conflict", "request_key identifies another pack")
            current = self._pack(namespace, pack_id)
            self._access(current, principal_id, scopes, write=True)
            return {**current, "idempotent": True}
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO intake_practice_packs VALUES (?,?,?,?,?,?)", [
                pack_id, namespace, principal_id, digest, 1, _json(content),
            ])
            self.conn.execute("INSERT INTO intake_practice_pack_revisions VALUES (?,?,?)", [
                pack_id, 1, _json(content),
            ])
            for card in content["cards"]:
                self.conn.execute("INSERT INTO intake_practice_progress VALUES (?,?,?,?,?,?,?)", [
                    pack_id, card["id"], 1, self.now(), 0, 0, None,
                ])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**content, "idempotent": False}

    def inspect_pack(
        self, namespace: str, pack_id: str, *, principal_id: str,
        scopes: set[str], revision: int | None = None,
    ) -> dict:
        current = self._pack(namespace, pack_id)
        self._access(current, principal_id, scopes)
        value = current if revision is None else self._pack(namespace, pack_id, revision)
        self._access(value, principal_id, scopes)
        return value

    def export_pack(
        self, namespace: str, pack_id: str, *, principal_id: str, scopes: set[str],
    ) -> dict:
        current = self.inspect_pack(namespace, pack_id,
                                    principal_id=principal_id, scopes=scopes)
        revisions = [json.loads(row[0]) for row in self.conn.execute(
            "SELECT content_json FROM intake_practice_pack_revisions WHERE pack_id=? "
            "ORDER BY revision", [pack_id],
        ).fetchall()]
        for revision in revisions:
            self._access(revision, principal_id, scopes)
        review_ids = [row[0] for row in self.conn.execute(
            "SELECT review_id FROM intake_practice_reviews WHERE namespace=? AND owner=? "
            "AND json_extract_string(content_json,'$.pack_id')=? ORDER BY review_id",
            [namespace, principal_id, pack_id],
        ).fetchall()]
        if len(review_ids) > 10_000:
            raise IntakeError("export_too_large", "practice export exceeds 10,000 reviews")
        reviews = []
        for review_id in review_ids:
            values = [json.loads(row[0]) for row in self.conn.execute(
                "SELECT content_json FROM intake_practice_review_revisions "
                "WHERE review_id=? ORDER BY revision", [review_id],
            ).fetchall()]
            for value in values:
                self._access(value, principal_id, scopes)
            reviews.append({"review_id": review_id, "revisions": values})
        progress = [{"card_id": row[0], "pack_revision": row[1],
                     "due_at_ms": row[2], "stage": row[3],
                     "self_reported_unassisted_passes": row[4],
                     "last_assessed_ms": row[5]}
                    for row in self.conn.execute(
                        "SELECT card_id,pack_revision,due_at_ms,stage,unassisted_passes,"
                        "last_assessed_ms FROM intake_practice_progress "
                        "WHERE pack_id=? ORDER BY card_id", [pack_id],
                    ).fetchall()]
        payload = {"contract": EXPORT_CONTRACT, "namespace": namespace,
                   "owner": principal_id, "pack_id": pack_id,
                   "current_revision": current["revision"],
                   "pack_revisions": revisions, "reviews": reviews,
                   "progress": progress}
        _bounded(payload, limit=20_000_000)
        return {**payload, "sha256": _hash(payload), "exported_at_ms": self.now()}

    def revise_pack(
        self, namespace: str, pack_id: str, edit_key: str, expected_revision: int,
        title: str, cards: list[dict], interval_days: list[int], *,
        principal_id: str, scopes: set[str],
    ) -> dict:
        edit_key = _text(edit_key, "edit_key", limit=256)
        patch = {"title": _text(title, "title", limit=1000),
                 "cards": _cards(cards, namespace, scopes),
                 "interval_days": _intervals(interval_days)}
        digest = _hash(patch)
        self.conn.execute("BEGIN")
        try:
            current = self._pack(namespace, pack_id)
            self._access(current, principal_id, scopes, write=True)
            replay = self.conn.execute(
                "SELECT request_hash,revision FROM intake_practice_pack_edits "
                "WHERE pack_id=? AND edit_key=?", [pack_id, edit_key]
            ).fetchone()
            if replay:
                if replay[0] != digest:
                    raise IntakeError("idempotency_conflict", "edit_key identifies another revision")
                value = self._pack(namespace, pack_id, int(replay[1]))
                self._access(value, principal_id, scopes, write=True)
                self.conn.execute("COMMIT")
                return {**value, "idempotent": True}
            if current["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "pack changed; inspect before editing")
            value = {**current, **patch, "revision": expected_revision + 1,
                     "updated_at_ms": self.now()}
            _bounded(value)
            changed = self.conn.execute(
                "UPDATE intake_practice_packs SET revision=?,content_json=? "
                "WHERE pack_id=? AND revision=? RETURNING revision",
                [value["revision"], _json(value), pack_id, expected_revision],
            ).fetchone()
            if not changed:
                raise IntakeError("revision_conflict", "concurrent pack edit")
            self.conn.execute("INSERT INTO intake_practice_pack_revisions VALUES (?,?,?)", [
                pack_id, value["revision"], _json(value),
            ])
            self.conn.execute("INSERT INTO intake_practice_pack_edits VALUES (?,?,?,?)", [
                pack_id, edit_key, digest, value["revision"],
            ])
            self.conn.execute("DELETE FROM intake_practice_progress WHERE pack_id=?", [pack_id])
            for card in value["cards"]:
                self.conn.execute("INSERT INTO intake_practice_progress VALUES (?,?,?,?,?,?,?)", [
                    pack_id, card["id"], value["revision"], self.now(), 0, 0, None,
                ])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**value, "idempotent": False}

    def due(self, namespace: str, *, principal_id: str, scopes: set[str], limit=50) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise IntakeError("invalid_limit", "limit must be 1–100")
        now = self.now()
        rows = self.conn.execute(
            "SELECT p.pack_id,g.card_id,g.due_at_ms,g.stage,g.unassisted_passes "
            "FROM intake_practice_packs p JOIN intake_practice_progress g ON p.pack_id=g.pack_id "
            "WHERE p.namespace=? AND p.owner=? AND g.due_at_ms<=? "
            "ORDER BY g.due_at_ms,p.pack_id,g.card_id LIMIT ?",
            [namespace, principal_id, now, limit],
        ).fetchall()
        result = []
        for pack_id, card_id, due_at, stage, passes in rows:
            pack = self.inspect_pack(namespace, pack_id, principal_id=principal_id, scopes=scopes)
            card = next(card for card in pack["cards"] if card["id"] == card_id)
            result.append({"pack_id": pack_id, "pack_revision": pack["revision"],
                           "card_id": card_id, "kind": card["kind"],
                           "prompt": card["prompt"], "references": card["references"],
                           "due_at_ms": due_at, "overdue_ms": max(0, now - due_at),
                           "overdue": now > due_at,
                           "stage": stage, "self_reported_unassisted_passes": passes})
        return {"cards": result, "as_of_ms": now}

    def _public_review(
        self, review: dict, *, principal_id: str, scopes: set[str], write=False,
    ) -> dict:
        pack = self._pack(review["namespace"], review["pack_id"],
                          review["pack_revision"])
        self._access(pack, principal_id, scopes, write=write)
        card = next(card for card in pack["cards"] if card["id"] == review["card_id"])
        result = {**review, "prompt": card["prompt"], "kind": card["kind"],
                  "references": card["references"],
                  "mastery_criterion": card["mastery_criterion"]}
        if review["status"] in {"revealed", "assessed"}:
            result["answer"] = card["answer"]
            result["answer_status"] = card["answer_status"]
        return result

    def start_review(
        self, namespace: str, pack_id: str, card_id: str, request_key: str, *,
        principal_id: str, scopes: set[str],
    ) -> dict:
        request_key = _text(request_key, "request_key", limit=256)
        review_id = "practice-review:" + _hash([namespace, principal_id, request_key])[:32]
        digest = _hash([pack_id, card_id])
        prior = self.conn.execute(
            "SELECT request_hash FROM intake_practice_reviews WHERE review_id=?", [review_id]
        ).fetchone()
        if prior:
            if prior[0] != digest:
                raise IntakeError("idempotency_conflict", "request_key identifies another review")
            value = self._review(namespace, review_id)
            self._access(value, principal_id, scopes, write=True)
            return {**self._public_review(value, principal_id=principal_id,
                                          scopes=scopes, write=True), "idempotent": True}
        pack = self._pack(namespace, pack_id)
        self._access(pack, principal_id, scopes, write=True)
        if card_id not in {card["id"] for card in pack["cards"]}:
            raise IntakeError("card_not_found", "card is not in the current pack")
        review = {"contract": CONTRACT, "review_id": review_id,
                  "namespace": namespace, "owner": principal_id,
                  "pack_id": pack_id, "pack_revision": pack["revision"],
                  "card_id": card_id, "revision": 1, "status": "active",
                  "attempt": None, "assistance": None, "assessment": None,
                  "created_at_ms": self.now()}
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("INSERT INTO intake_practice_reviews VALUES (?,?,?,?,?,?)", [
                review_id, namespace, principal_id, digest, 1, _json(review),
            ])
            self.conn.execute("INSERT INTO intake_practice_review_revisions VALUES (?,?,?)", [
                review_id, 1, _json(review),
            ])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**self._public_review(review, principal_id=principal_id,
                                      scopes=scopes, write=True),
                "idempotent": False}

    def inspect_review(
        self, namespace: str, review_id: str, *, principal_id: str,
        scopes: set[str], revision: int | None = None,
    ) -> dict:
        current = self._review(namespace, review_id)
        self._access(current, principal_id, scopes)
        value = current if revision is None else self._review(namespace, review_id, revision)
        return self._public_review(value, principal_id=principal_id, scopes=scopes)

    def command_review(
        self, namespace: str, review_id: str, command_key: str,
        expected_revision: int, action: str, payload: dict | None, *,
        principal_id: str, scopes: set[str],
    ) -> dict:
        command_key = _text(command_key, "command_key", limit=256)
        if type(expected_revision) is not int or expected_revision < 1:
            raise IntakeError("invalid_revision", "expected_revision must be positive")
        if action not in {"attempt", "reveal", "assess"}:
            raise IntakeError("invalid_action", "unsupported review command")
        payload = _bounded(payload or {}, limit=20_000)
        if not isinstance(payload, dict):
            raise IntakeError("invalid_input", "payload must be an object")
        digest = _hash([action, payload])
        self.conn.execute("BEGIN")
        try:
            review = self._review(namespace, review_id)
            self._access(review, principal_id, scopes, write=True)
            replay = self.conn.execute(
                "SELECT request_hash,revision FROM intake_practice_review_commands "
                "WHERE review_id=? AND command_key=?", [review_id, command_key]
            ).fetchone()
            if replay:
                if replay[0] != digest:
                    raise IntakeError("idempotency_conflict", "command_key identifies another action")
                prior = self._review(namespace, review_id, int(replay[1]))
                self.conn.execute("COMMIT")
                return {**self._public_review(prior, principal_id=principal_id,
                                              scopes=scopes, write=True), "idempotent": True}
            if review["revision"] != expected_revision:
                raise IntakeError("revision_conflict", "review changed; inspect before retry")
            if action == "attempt":
                if review["status"] != "active" or set(payload) != {"answer", "assisted"}:
                    raise IntakeError("invalid_status", "record one answer before reveal")
                if type(payload["assisted"]) is not bool:
                    raise IntakeError("invalid_input", "assisted must be boolean")
                review["attempt"] = _text(payload["answer"], "attempt", limit=5000)
                review["assistance"] = "reported_assisted" if payload["assisted"] else "reported_unaided"
                review["status"] = "attempted"
            elif action == "reveal":
                if review["status"] != "attempted" or payload:
                    raise IntakeError("invalid_status", "answer follows a recorded attempt")
                review["status"] = "revealed"
            else:
                if review["status"] != "revealed" or set(payload) != {"passed", "notes"}:
                    raise IntakeError("invalid_status", "assess after answer reveal")
                if type(payload["passed"]) is not bool:
                    raise IntakeError("invalid_input", "passed must be boolean")
                review["assessment"] = {"reported_passed": payload["passed"],
                                        "notes": _text(payload["notes"], "notes", limit=2000),
                                        "at_ms": self.now()}
                review["status"] = "assessed"
                progress = self.conn.execute(
                    "SELECT pack_revision,stage,unassisted_passes FROM intake_practice_progress "
                    "WHERE pack_id=? AND card_id=?", [review["pack_id"], review["card_id"]]
                ).fetchone()
                if progress and progress[0] == review["pack_revision"]:
                    success = payload["passed"] and review["assistance"] == "reported_unaided"
                    pack = self._pack(namespace, review["pack_id"], review["pack_revision"])
                    stage = min(int(progress[1]) + 1, len(pack["interval_days"])) if success else 0
                    passes = int(progress[2]) + 1 if success else int(progress[2])
                    days = pack["interval_days"][stage - 1] if stage else 1
                    self.conn.execute(
                        "UPDATE intake_practice_progress SET due_at_ms=?,stage=?,"
                        "unassisted_passes=?,last_assessed_ms=? WHERE pack_id=? AND card_id=?", [
                            self.now() + days * 86_400_000, stage, passes, self.now(),
                            review["pack_id"], review["card_id"],
                        ])
            review["revision"] += 1
            review["updated_at_ms"] = self.now()
            changed = self.conn.execute(
                "UPDATE intake_practice_reviews SET revision=?,content_json=? "
                "WHERE review_id=? AND revision=? RETURNING revision", [
                    review["revision"], _json(review), review_id, expected_revision,
                ]
            ).fetchone()
            if not changed:
                raise IntakeError("revision_conflict", "concurrent review update")
            self.conn.execute("INSERT INTO intake_practice_review_revisions VALUES (?,?,?)", [
                review_id, review["revision"], _json(review),
            ])
            self.conn.execute("INSERT INTO intake_practice_review_commands VALUES (?,?,?,?)", [
                review_id, command_key, digest, review["revision"],
            ])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return {**self._public_review(review, principal_id=principal_id,
                                      scopes=scopes, write=True),
                "idempotent": False}
