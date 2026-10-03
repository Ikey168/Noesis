"""AI models and datasets matched across sources through reviewable identity (#2776, AI06; track #2742).

Two pairings are offered, and only on identifiers a source states - never on a name:

* an **Epoch AI** notable-model row and a **Hugging Face Hub model repository**;
* a **Hub dataset repository** and an **OpenML dataset** version.

Methods, strongest first (stated identifiers before anything else; a shared name is never a match):

* ``stated-repository-id`` - one side states the other's Hub repository URL (an Epoch row's link, an OpenML
  ``original_data_url``): confidence ``high``;
* ``stated-openml-id`` - a Hub dataset's card or tags state the OpenML dataset URL: confidence ``high``;
* ``shared-arxiv-id`` and ``shared-doi`` - both sides state the same arXiv id or DOI (a paper may describe several
  models or datasets): confidence ``medium``.

Every match is ``proposed`` with its method, evidence (the identifiers and the pinned revision of each side) and
confidence, then ``accepted`` or ``rejected`` by a reviewer other than the proposer, and can be ``reverted``. Review
decisions are entity identity decisions in :class:`src.kb.entity_history.EntityHistoryStore` (``match`` /
``non-match`` / ``undo``), so the review inbox sees them. Nothing is auto-merged: records stay separate and answers put
matched records side by side. Records without an accepted match stay visible as unmatched.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.ai_models_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    AiModelsError,
    authorize,
    canonical,
    digest,
    iso,
    table_exists,
)
from src.kb.ai_models_store import AiModelsStore

CONTRACT = "noesis-ai-model-identity-v1"
PAIRS = {"epoch-hub-model": ("epoch-model", "hub-model"), "hub-dataset-openml": ("hub-dataset", "openml-dataset")}
METHODS = ("stated-repository-id", "stated-openml-id", "shared-arxiv-id", "shared-doi")
CONFIDENCE = {"stated-repository-id": "high", "stated-openml-id": "high", "shared-arxiv-id": "medium",
              "shared-doi": "medium"}
STATES = ("proposed", "accepted", "rejected", "reverted")
MATCHABLE = ("epoch-model", "hub-model", "hub-dataset", "openml-dataset")
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS ai_identity_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, pair_kind TEXT NOT NULL, left_record_id TEXT NOT NULL,
  right_record_id TEXT NOT NULL, left_revision_id TEXT NOT NULL, right_revision_id TEXT NOT NULL, method TEXT NOT NULL,
  confidence TEXT NOT NULL, evidence_json TEXT NOT NULL, evidence_hash TEXT NOT NULL, state TEXT NOT NULL,
  decision_id TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, match_id)
);
"""


def _entity(record_id: str) -> str:
    return "ent-ai-" + record_id.replace(":", "-")


def _identifiers(revision: Mapping[str, Any]) -> dict[str, set[str]]:
    stated = dict(revision["statement"].get("identifiers") or {})
    return {k: {str(v) for v in stated.get(k) or []} for k in ("arxiv", "doi", "openml_dataset", "hub")}


class AiModelsIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.store = AiModelsStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "ai_identity_matches")

    # ------------------------------------------------------------------ proposals

    def _current(self, namespace: str, record: Mapping[str, Any]) -> dict[str, Any] | None:
        published = [r for r in self.store.revision_rows(namespace, record["record_id"]) if r["state"] == "published"]
        return published[-1] if published else None

    @staticmethod
    def _methods(pair_kind: str, left: Mapping[str, Any], left_rev, right: Mapping[str, Any], right_rev
                 ) -> list[dict[str, Any]]:
        a, b = _identifiers(left_rev), _identifiers(right_rev)
        found = []
        if pair_kind == "epoch-hub-model":
            if f"model:{right['native_key']}" in a["hub"]:
                found.append({"method": "stated-repository-id", "identifier": right["native_key"],
                              "stated_by": "epoch-ai (row link)"})
        else:
            if right["native_key"] in a["openml_dataset"]:
                found.append({"method": "stated-openml-id", "identifier": right["native_key"],
                              "stated_by": "huggingface-hub (card or tags)"})
            if f"dataset:{left['native_key']}" in b["hub"]:
                found.append({"method": "stated-repository-id", "identifier": left["native_key"],
                              "stated_by": "openml (original_data_url)"})
        for shared in sorted(a["arxiv"] & b["arxiv"]):
            found.append({"method": "shared-arxiv-id", "identifier": shared, "stated_by": "both"})
        for shared in sorted(a["doi"] & b["doi"]):
            found.append({"method": "shared-doi", "identifier": shared, "stated_by": "both"})
        return sorted(found, key=lambda m: (METHODS.index(m["method"]), m["identifier"]))

    def _latest_for_pair(self, namespace: str, left_id: str, right_id: str) -> dict[str, Any] | None:
        if not self.ready():
            return None
        row = self.conn.execute("SELECT match_id FROM ai_identity_matches WHERE namespace=? AND left_record_id=? AND "
                                "right_record_id=? ORDER BY created_at_ms DESC, match_id DESC LIMIT 1",
                                [namespace, left_id, right_id]).fetchone()
        return None if row is None else self.match(namespace, row[0], scopes={"operator"})

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Propose matches on stated identifiers only; idempotent; nothing is merged or used before review."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        created, considered = [], []
        for pair_kind, (left_kind, right_kind) in PAIRS.items():
            lefts = self.store.records(namespace, record_kind=left_kind)
            rights = self.store.records(namespace, record_kind=right_kind)
            for left in lefts:
                left_rev = self._current(namespace, left)
                if left_rev is None:
                    continue
                for right in rights:
                    right_rev = self._current(namespace, right)
                    if right_rev is None:
                        continue
                    methods = self._methods(pair_kind, left, left_rev, right, right_rev)
                    if not methods:
                        continue  # a shared or similar name alone is never a match
                    evidence = {"methods": methods, "left": {"record_id": left["record_id"],
                                                             "revision_id": left_rev["revision_id"],
                                                             "source": left["source"], "label": left["label"]},
                                "right": {"record_id": right["record_id"], "revision_id": right_rev["revision_id"],
                                          "source": right["source"], "label": right["label"]},
                                "names": "context only; matching rests on stated identifiers"}
                    evidence_hash = digest([methods, left_rev["revision_id"], right_rev["revision_id"]])
                    latest = self._latest_for_pair(namespace, left["record_id"], right["record_id"])
                    if latest is not None and (latest["state"] != "reverted" or
                                               latest["evidence_hash"] == evidence_hash):
                        considered.append(latest["match_id"])
                        continue
                    match_id = "ai-match:" + digest([namespace, left["record_id"], right["record_id"],
                                                     evidence_hash])[:24]
                    now = self.now()
                    self.conn.execute(
                        "INSERT INTO ai_identity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        [namespace, match_id, pair_kind, left["record_id"], right["record_id"],
                         left_rev["revision_id"], right_rev["revision_id"], methods[0]["method"],
                         CONFIDENCE[methods[0]["method"]], canonical(evidence), evidence_hash, "proposed", None,
                         canonical([{"state": "proposed", "by": principal_id, "at": iso(now)}]), principal_id, now])
                    created.append(match_id)
                    considered.append(match_id)
        return {"created": created, "matches": [self.match(namespace, m, scopes={"operator"}) for m in considered],
                "unmatched": self.unmatched(namespace, scopes={"operator"}),
                "note": "proposals only; nothing is merged and nothing is used until a reviewer accepts it"}

    # ------------------------------------------------------------------ review

    def match(self, namespace: str, match_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT match_id, pair_kind, left_record_id, right_record_id, left_revision_id, right_revision_id, "
            "method, confidence, evidence_json, evidence_hash, state, decision_id, history_json, created_by "
            "FROM ai_identity_matches WHERE namespace=? AND match_id=?", [namespace, match_id]).fetchone() \
            if self.ready() else None
        if row is None:
            raise AiModelsError("not_found", "identity match is not visible in this namespace")
        keys = ("match_id", "pair_kind", "left_record_id", "right_record_id", "left_revision_id",
                "right_revision_id", "method", "confidence", "evidence", "evidence_hash", "state", "decision_id",
                "history", "created_by")
        view = dict(zip(keys, row))
        view["evidence"], view["history"] = json.loads(view["evidence"]), json.loads(view["history"])
        return {"contract": CONTRACT, "namespace": namespace, **view, "merged": False}

    def matches(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                record_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM ai_identity_matches WHERE namespace=? AND (? IS NULL OR state=?) AND (? IS NULL OR "
            "left_record_id=? OR right_record_id=?) ORDER BY pair_kind, left_record_id, right_record_id, "
            "created_at_ms, match_id", [namespace, state, state, record_id, record_id, record_id]).fetchall()
        return [self.match(namespace, r[0], scopes={"operator"}) for r in rows]

    def _transition(self, namespace, item, state, principal_id, reason, decision_id):
        history = item["history"] + [{"state": state, "by": principal_id, "reason": reason, "at": iso(self.now())}]
        self.conn.execute("UPDATE ai_identity_matches SET state=?, decision_id=?, history_json=? WHERE namespace=? "
                          "AND match_id=?", [state, decision_id, canonical(history), namespace, item["match_id"]])
        return self.match(namespace, item["match_id"], scopes={"operator"})

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept or reject a proposal with a reason; recorded as an entity identity decision, never a merge."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise AiModelsError("invalid_decision", "accept or reject with a reason")
        item = self.match(namespace, match_id, scopes={"operator"})
        if item["state"] != "proposed":
            raise AiModelsError("invalid_state", f"match is {item['state']}; only a proposal is reviewed")
        if item["created_by"] == principal_id:
            raise AiModelsError("self_review", "a match is reviewed by a principal other than its proposer")
        left, right = _entity(item["left_record_id"]), _entity(item["right_record_id"])
        self.history.register_entity(namespace, left, [item["left_record_id"]], principal_id=principal_id,
                                     scopes=_ENTITY_HISTORY_SCOPES)
        self.history.register_entity(namespace, right, [item["right_record_id"]], principal_id=principal_id,
                                     scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [left, right],
            {"match_id": match_id, "method": item["method"], "confidence": item["confidence"],
             "evidence": item["evidence"]["methods"], "reason": reason.strip(),
             "provenance": {"producer": "technology.ai-models",
                            "records": [item["left_record_id"], item["right_record_id"]],
                            "revisions": [item["left_revision_id"], item["right_revision_id"]]},
             "policy": {"merge": False, "note": "identity decision only; both records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"ai-models-identity:{namespace}:{match_id}:{len(item['history'])}")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip(), recorded["decision_id"])

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise AiModelsError("invalid_decision", "a revert needs a reason")
        item = self.match(namespace, match_id, scopes={"operator"})
        if item["state"] not in {"accepted", "rejected"}:
            raise AiModelsError("invalid_state", "only an accepted or rejected match can be reverted")
        undone = self.history.undo(namespace, item["decision_id"], reviewer_id=principal_id,
                                   principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, item, "reverted", principal_id, reason.strip(), undone["decision_id"])

    # ------------------------------------------------------------------ use by queries and links

    def counterparts(self, namespace: str, record_id: str) -> list[dict[str, Any]]:
        """Records an accepted match ties to ``record_id`` (side by side, never merged)."""
        out = []
        for item in self.matches(namespace, scopes={"operator"}, record_id=record_id, state="accepted"):
            other = item["right_record_id"] if item["left_record_id"] == record_id else item["left_record_id"]
            out.append({"record_id": other, "match_id": item["match_id"], "method": item["method"],
                        "confidence": item["confidence"], "reviewed": item["history"][-1],
                        "decision_id": item["decision_id"]})
        return out

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        """Matchable records without an accepted match, with any open or closed proposals; never hidden."""
        authorize(namespace, set(scopes), READ_SCOPE)
        out = []
        for record in self.store.records(namespace):
            if record["record_kind"] not in MATCHABLE:
                continue
            matches = self.matches(namespace, scopes={"operator"}, record_id=record["record_id"])
            if any(m["state"] == "accepted" for m in matches):
                continue
            out.append({"record_id": record["record_id"], "record_kind": record["record_kind"],
                        "source": record["source"], "label": record["label"], "state": "unmatched",
                        "proposals": [{"match_id": m["match_id"], "state": m["state"], "method": m["method"]}
                                      for m in matches],
                        "reason": "no accepted match; a shared name is never a match" if not matches else
                        "a proposal exists but no reviewer accepted it"})
        return out


__all__ = ["CONFIDENCE", "CONTRACT", "METHODS", "PAIRS", "STATES", "AiModelsIdentity"]
