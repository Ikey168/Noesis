"""Reviewable identity for food records (#2216, FC06).

Food records from Open Food Facts, FoodData Central and composition tables are
connected to each other and to Products identities (``src.kb.products``) only
through *proposed* match candidates that a reviewer accepts, rejects or defers:

* **GTIN** - UPC-A, EAN-8, EAN-13 and GTIN-14 normalised to one 14-digit key
  with a valid check digit (:func:`src.kb.food_composition.gtin_key`), so
  ``071000000208`` (FDC Branded) and ``0071000000208`` (Open Food Facts) meet.
  Candidates run between food products of different providers or records and
  between a food product and a Products model whose variant publishes the GTIN.
  The same GTIN under different brands is a ``conflict``: surfaced, never
  resolved silently, and never acceptable.
* **generic foods** - composition-table foods and FDC Foundation / SR Legacy /
  Survey foods are proposed only from their published names (same name, or a
  name-token overlap), with the tokens used as evidence. Nothing is accepted
  automatically.

Reviews follow the Products match review flow (:meth:`ProductStore.review_match`
semantics): decisions ``accepted`` / ``rejected`` / ``deferred`` with a reason,
append-only with reviewer and time, a later review reversing an earlier one.
Brands and manufacturers stay source strings (canonical entities are consulted
only as corroboration); no new entity store is created and nothing is merged.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from src.kb.food_composition import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    FoodCompositionError,
    FoodCompositionStore,
    _load,
    authorize,
    canonical,
    digest,
    gtin_key,
    table_exists,
)

MATCH_CONTRACT = "noesis-food-match-v1"
MATCH_METHOD = "gtin-brand-or-published-name-v1"
MATCH_DECISIONS = frozenset({"accepted", "rejected", "deferred"})
CANDIDATE_STATES = ("proposed", "weak", "conflict")
_LEGAL_FORMS = {"inc", "llc", "ltd", "gmbh", "sa", "sas", "bv", "ag", "co", "corp", "company", "foods", "plc"}

_DDL = """
CREATE TABLE IF NOT EXISTS food_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, left_id TEXT NOT NULL, right_kind TEXT NOT NULL,
  right_id TEXT NOT NULL, method TEXT NOT NULL, basis TEXT NOT NULL, candidate_state TEXT NOT NULL,
  evidence_json TEXT NOT NULL, reasons_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  updated_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, match_id)
);
CREATE TABLE IF NOT EXISTS food_match_reviews (
  namespace TEXT NOT NULL, review_id TEXT NOT NULL, match_id TEXT NOT NULL, sequence INTEGER NOT NULL,
  decision TEXT NOT NULL, reason TEXT NOT NULL, candidate_state TEXT NOT NULL, principal_id TEXT NOT NULL,
  reviewed_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, review_id)
);
"""


def brand_tokens(value: Any) -> frozenset[str]:
    tokens = [t for t in re.split(r"[^0-9a-z]+", str(value or "").casefold()) if t]
    return frozenset(t for t in tokens if t not in _LEGAL_FORMS)


def brands_agree(left: Iterable[Any], right: Iterable[Any]) -> bool | None:
    """True when a brand on one side names a brand on the other (legal forms ignored); None when one side has none."""
    a = [brand_tokens(b) for b in left if brand_tokens(b)]
    b = [brand_tokens(x) for x in right if brand_tokens(x)]
    if not a or not b:
        return None
    return any(x <= y or y <= x for x in a for y in b)


def name_tokens(value: Any) -> frozenset[str]:
    tokens = [t for t in re.split(r"[^0-9a-z]+", str(value or "").casefold()) if len(t) > 1]
    return frozenset(t[:-1] if len(t) > 3 and t.endswith("s") else t for t in tokens)


class FoodIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.store = FoodCompositionStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ inputs

    def _foods(self, namespace: str) -> list[dict[str, Any]]:
        foods = []
        for item in self.store.items(namespace):
            revision = self.store.current_revision(namespace, item["food_id"])
            if revision is None:
                continue
            statement = self.store.statement(namespace, revision["revision_id"])
            names = statement["names"]
            foods.append({**item, "revision_id": revision["revision_id"], "brands": list(names.get("brands") or []),
                          "name": names.get("product_name") or names.get("description"),
                          "data_type": statement["identifiers"].get("data_type"),
                          "gtin_value": (statement["identifiers"].get("gtin") or {}).get("value")})
        return foods

    def _product_models(self, namespace: str) -> list[dict[str, Any]]:
        """Products models with the valid GTINs their variants publish (the Products identity store is read only)."""
        if not table_exists(self.conn, "product_identities"):
            return []
        models: dict[str, dict[str, Any]] = {}
        for model_id, brand, designation in self.conn.execute(
                "SELECT identity_id, brand, designation FROM product_identities WHERE namespace=? AND level='model'",
                [namespace]).fetchall():
            models[model_id] = {"model_id": model_id, "brand": brand, "designation": designation, "gtins": {}}
        for parent, identifiers in self.conn.execute(
                "SELECT parent_id, identifiers_json FROM product_identities WHERE namespace=? AND level='variant'",
                [namespace]).fetchall():
            if parent not in models:
                continue
            for item in _load(identifiers, {}).get("gtin") or []:
                key = gtin_key(item.get("value"))
                if key:
                    models[parent]["gtins"][key] = item.get("value")
        return [m for m in models.values() if m["gtins"]]

    # ------------------------------------------------------------------ proposals

    def propose(self, namespace: str, *, scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Deterministic candidates (GTIN, published names); none is accepted without a review."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        del principal_id
        self.store.require_ready()
        foods = self._foods(namespace)
        produced: list[dict[str, Any]] = []
        products = [f for f in foods if f["gtin_key"]]
        for i, left in enumerate(products):
            for right in products[i + 1:]:
                if left["gtin_key"] != right["gtin_key"]:
                    continue
                produced.append(self._gtin_candidate(namespace, left, "food", right["food_id"], right["brands"],
                                                     right["gtin_value"], right["provider"]))
        for food in products:
            for model in self._product_models(namespace):
                if food["gtin_key"] in model["gtins"]:
                    produced.append(self._gtin_candidate(namespace, food, "product-model", model["model_id"],
                                                         [model["brand"]], model["gtins"][food["gtin_key"]],
                                                         "products"))
        generic = [f for f in foods if f["food_kind"] == "generic-food"]
        for i, left in enumerate(generic):
            for right in generic[i + 1:]:
                if left["provider"] == right["provider"] and left["provider"] != "fooddata-central":
                    continue
                if left["provider"] == right["provider"] and left["data_type"] == right["data_type"]:
                    continue
                a, b = name_tokens(left["name"]), name_tokens(right["name"])
                if not a or not b:
                    continue
                overlap = len(a & b) / len(a | b)
                if overlap < 0.5:
                    continue
                basis = "same-published-name" if a == b else "name-token-overlap"
                evidence = [{"kind": "published-name", "left": left["name"], "right": right["name"],
                             "shared_tokens": sorted(a & b), "overlap": round(overlap, 2)}]
                produced.append(self._upsert(namespace, left["food_id"], "food", right["food_id"], basis,
                                             "proposed" if basis == "same-published-name" else "weak", evidence,
                                             ["generic foods are matched only by reviewers; names are evidence, "
                                              "not identity"]))
        return {"contract": MATCH_CONTRACT, "namespace": namespace, "method": MATCH_METHOD, "candidates": produced,
                "conflicts": [c for c in produced if c["candidate_state"] == "conflict"],
                "policy": "candidates only; a reviewer accepts each; conflicts are never resolved silently"}

    def _gtin_candidate(self, namespace, left, right_kind, right_id, right_brands, right_gtin, right_provider):
        agree = brands_agree(left["brands"], right_brands)
        evidence = [{"kind": "gtin", "left": left["gtin_value"], "right": right_gtin, "gtin_key": left["gtin_key"]},
                    {"kind": "brand", "left": left["brands"], "right": list(right_brands)}]
        reasons = []
        if agree is False:
            reasons.append(f"the same GTIN is published under different brands: {left['brands']} ({left['provider']}) "
                           f"vs {list(right_brands)} ({right_provider})")
        elif agree is None:
            reasons.append("one side states no brand; the GTIN alone is the evidence")
        state = "conflict" if agree is False else "proposed"
        return self._upsert(namespace, left["food_id"], right_kind, right_id, "gtin", state, evidence, reasons)

    def _upsert(self, namespace, left_id, right_kind, right_id, basis, state, evidence, reasons) -> dict[str, Any]:
        match_id = "food-match:" + digest([namespace, left_id, right_kind, right_id])[:24]
        now = self.now()
        existing = self.conn.execute(
            "SELECT candidate_state, basis, evidence_json, reasons_json FROM food_matches WHERE namespace=? AND "
            "match_id=?", [namespace, match_id]).fetchone()
        if existing is None:
            self.conn.execute("INSERT INTO food_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", [
                namespace, match_id, left_id, right_kind, right_id, MATCH_METHOD, basis, state, canonical(evidence),
                canonical(reasons), now, now])
            self.store.bump(namespace)
        elif (existing[0], existing[1], _load(existing[2], []), _load(existing[3], [])) != (state, basis, evidence,
                                                                                               reasons):
            self.conn.execute(
                "UPDATE food_matches SET candidate_state=?, basis=?, evidence_json=?, reasons_json=?, updated_at_ms=? "
                "WHERE namespace=? AND match_id=?",
                [state, basis, canonical(evidence), canonical(reasons), now, namespace, match_id])
            self.store.bump(namespace)
        return self.match(namespace, match_id)

    # ------------------------------------------------------------------ reads and reviews

    def match(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT left_id, right_kind, right_id, method, basis, candidate_state, evidence_json, reasons_json "
            "FROM food_matches WHERE namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        if row is None:
            raise FoodCompositionError("not_found", "food match candidate is not visible in this namespace")
        history = [dict(zip(("sequence", "decision", "reason", "candidate_state", "principal_id", "reviewed_at_ms"), r))
                   for r in self.conn.execute(
                       "SELECT sequence, decision, reason, candidate_state, principal_id, reviewed_at_ms FROM "
                       "food_match_reviews WHERE namespace=? AND match_id=? ORDER BY sequence",
                       [namespace, match_id]).fetchall()]
        review_state = history[-1]["decision"] if history else "unreviewed"
        return {"contract": MATCH_CONTRACT, "match_id": match_id, "left_food_id": row[0], "right_kind": row[1],
                "right_id": row[2], "method": row[3], "basis": row[4], "candidate_state": row[5],
                "evidence": _load(row[6], []), "reasons": _load(row[7], []), "review_state": review_state,
                "review_history": history,
                "accepted": review_state == "accepted" and row[5] != "conflict"}

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, scopes: Iterable[str],
               principal_id: str) -> dict[str, Any]:
        """Append a review (reviewer and time recorded); conflicts cannot be accepted."""
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in MATCH_DECISIONS:
            raise FoodCompositionError("invalid_decision", "decision must be accepted, rejected or deferred")
        if not str(reason or "").strip():
            raise FoodCompositionError("invalid_decision", "a review reason is required")
        current = self.match(namespace, match_id)
        if decision == "accepted" and current["candidate_state"] == "conflict":
            raise FoodCompositionError("conflicting_identifiers",
                                       "the same GTIN names different brands; resolve the conflict at the source",
                                       reasons=current["reasons"])
        sequence = len(current["review_history"]) + 1
        self.conn.execute("INSERT INTO food_match_reviews VALUES (?,?,?,?,?,?,?,?,?)", [
            namespace, "food-match-review:" + digest([namespace, match_id, sequence])[:24], match_id, sequence,
            decision, reason.strip(), current["candidate_state"], principal_id, self.now()])
        self.store.bump(namespace)
        return self.match(namespace, match_id)

    def _match_ids(self, namespace: str, food_id: str) -> list[str]:
        if not table_exists(self.conn, "food_matches"):
            return []
        return [r[0] for r in self.conn.execute(
            "SELECT match_id FROM food_matches WHERE namespace=? AND (left_id=? OR (right_kind='food' AND right_id=?)) "
            "ORDER BY match_id", [namespace, food_id, food_id]).fetchall()]

    def matches_for(self, namespace: str, food_id: str) -> list[dict[str, Any]]:
        return [self.match(namespace, m) for m in self._match_ids(namespace, food_id)]

    def conflicts(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        authorize(namespace, scopes, READ_SCOPE)
        if not table_exists(self.conn, "food_matches"):
            return []
        return [self.match(namespace, r[0]) for r in self.conn.execute(
            "SELECT match_id FROM food_matches WHERE namespace=? AND candidate_state='conflict' ORDER BY match_id",
            [namespace]).fetchall()]

    def accepted(self, namespace: str, food_id: str) -> dict[str, list[str]]:
        """Food ids and Products model ids accepted as the same food (one hop, no transitive closure)."""
        foods, models = [], []
        for match in self.matches_for(namespace, food_id):
            if not match["accepted"]:
                continue
            if match["right_kind"] == "product-model":
                models.append(match["right_id"])
            else:
                foods.append(match["right_id"] if match["left_food_id"] == food_id else match["left_food_id"])
        return {"foods": sorted(set(foods)), "product_models": sorted(set(models))}

    def resolve_gtin(self, namespace: str, gtin: str) -> dict[str, Any]:
        """Every food record publishing a GTIN (by normalised key), with the candidates and conflicts around them."""
        key = gtin_key(gtin)
        if key is None:
            return {"gtin": gtin, "gtin_key": None, "status": "invalid_gtin", "foods": [], "matches": []}
        foods = self.store.items(namespace, gtin=gtin)
        matches = {m["match_id"]: m for f in foods for m in self.matches_for(namespace, f["food_id"])}
        return {"gtin": gtin, "gtin_key": key, "status": "matched" if foods else "unmatched_gtin", "foods": foods,
                "matches": [matches[k] for k in sorted(matches)],
                "conflicts": [m for m in matches.values() if m["candidate_state"] == "conflict"]}


__all__ = ["MATCH_CONTRACT", "FoodIdentity", "brand_tokens", "brands_agree", "name_tokens"]
