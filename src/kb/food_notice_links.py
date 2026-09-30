"""Link food products to RASFF (and other) notices held by Products safety, by citation (#2216, FC07).

No notice is acquired here: notices, their revisions and their reviewed
product matches are read from :mod:`src.kb.product_safety` (RASFF acquisition
belongs to #1916). A food product is linked to a notice revision only when

* the revision's identification cites the product's **GTIN** (one 14-digit key
  on both sides; a brand the notice states beside it must not contradict the
  product's brand, else the link is ``contradicted``), or
* it cites the product's **brand and exact designation** (brand ignoring case
  and legal forms, designation ignoring case and separators only), or
* a **reviewed notice match** exists: the food product is accepted as a
  Products model (FC06) and that model's notice match was accepted in Products
  safety (``attached``).

Batch and best-before strings the notice states are kept as quoted evidence,
never used alone. Hazard, category or ingredient similarity never creates a
link. Every link row records the notice revision it was derived from and is
never deleted: a later notice revision is evaluated again and gets its own row
(``cited`` or ``not_cited_in_revision``). A food product without a link has
*no notice on record*, which is not a statement that it is safe; allergen
declarations and a notice's stated hazard are shown side by side, both quoted,
with no causal or compliance relationship asserted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from src.kb.food_composition import (
    READ_SCOPE,
    WRITE_SCOPE,
    FoodCompositionStore,
    _load,
    authorize,
    canonical,
    digest,
    gtin_key,
    table_exists,
)
from src.kb.food_identity import FoodIdentity, brands_agree

LINK_CONTRACT = "noesis-food-notice-link-v1"
NO_NOTICE = "no notice on record"
SIDE_BY_SIDE_NOTE = (
    "The label's allergen declarations and the notice's stated hazard are both quoted as published; Noesis asserts "
    "no causal, compliance or safety relationship between them."
)
_DDL = """
CREATE TABLE IF NOT EXISTS food_notice_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, food_id TEXT NOT NULL, food_revision_id TEXT NOT NULL,
  notice_id TEXT NOT NULL, notice_revision_id TEXT NOT NULL, notice_revision_seq BIGINT NOT NULL,
  basis TEXT NOT NULL, state TEXT NOT NULL, evidence_json TEXT NOT NULL, principal_id TEXT NOT NULL,
  linked_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""


def _designation(value: Any) -> str:
    from src.kb.products import designation_key

    return designation_key(value)


class FoodNoticeLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.store = FoodCompositionStore(conn, initialize=initialize, now=now)
        self.identity = FoodIdentity(conn, initialize=initialize, now=self.store.now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def _safety(self):
        from src.kb.product_safety import ProductSafetyStore

        return ProductSafetyStore(self.conn, initialize=False)

    def _groups(self, namespace: str, revision_id: str) -> list[dict[str, dict[str, str] | list[str]]]:
        """Identification groups of one notice revision; notice-level identifiers (group -1) join every group."""
        groups: dict[int, dict[str, Any]] = {}
        for group, kind, value in self.conn.execute(
                "SELECT group_index, kind, value FROM product_safety_identifications WHERE namespace=? AND "
                "revision_id=? ORDER BY group_index, identification_id", [namespace, revision_id]).fetchall():
            target = groups.setdefault(group, {"gtin": {}, "brand": [], "designation": {}, "batch": []})
            if kind == "gtin" and gtin_key(value):
                target["gtin"][gtin_key(value)] = value
            elif kind == "brand":
                target["brand"].append(value)
            elif kind in {"model", "name"}:
                target["designation"][_designation(value)] = value
            elif kind in {"batch", "best_before"}:
                target["batch"].append({"kind": kind, "value": value})
        shared = groups.pop(-1, None)
        if shared:
            if not groups:
                groups[0] = {"gtin": {}, "brand": [], "designation": {}, "batch": []}
            for group in groups.values():
                group["gtin"] = {**shared["gtin"], **group["gtin"]}
                group["brand"] = shared["brand"] + group["brand"]
                group["designation"] = {**shared["designation"], **group["designation"]}
                group["batch"] = shared["batch"] + group["batch"]
        return [groups[k] for k in sorted(groups)]

    @staticmethod
    def _cites(food: Mapping[str, Any], group: Mapping[str, Any]) -> tuple[str, str, list[dict[str, Any]]] | None:
        """(basis, state, evidence) when one identification group cites the food product, else None."""
        evidence = [{"kind": b["kind"], "notice": b["value"], "note": "quoted; never used alone"}
                    for b in group["batch"]]
        agree = brands_agree(food["brands"], group["brand"])
        if food["gtin_key"] and food["gtin_key"] in group["gtin"]:
            evidence = [{"kind": "gtin", "notice": group["gtin"][food["gtin_key"]], "product": food["gtin_value"]},
                        *([{"kind": "brand", "notice": group["brand"], "product": food["brands"]}]
                          if group["brand"] else []), *evidence]
            return "gtin", "contradicted" if agree is False else "cited", evidence
        key = _designation(food["name"]) if food["name"] else ""
        if agree and key and key in group["designation"]:
            return "brand+designation", "cited", [
                {"kind": "brand", "notice": group["brand"], "product": food["brands"]},
                {"kind": "designation", "notice": group["designation"][key], "product": food["name"]}, *evidence]
        return None

    def link(self, namespace: str, *, scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Evaluate every notice revision against every food product; append-only and idempotent."""
        from src.kb.product_safety import ProductSafetyError

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        if not table_exists(self.conn, "product_safety_revisions"):
            return {"namespace": namespace, "status": "provider_absent", "linked": [],
                    "note": "no Products safety notices are held; nothing is linked"}
        safety = self._safety()
        foods = [f for f in self.identity._foods(namespace) if f["food_kind"] == "food-product"]
        created: list[dict[str, Any]] = []
        notices = [r[0] for r in self.conn.execute(
            "SELECT notice_id FROM product_safety_notices WHERE namespace=? ORDER BY notice_id", [namespace]).fetchall()]
        for food in foods:
            for notice_id in notices:
                cited_before = False
                for revision in safety.revisions(namespace, notice_id):
                    hits = [h for g in self._groups(namespace, revision["revision_id"]) if (h := self._cites(food, g))]
                    if hits:
                        basis, state, evidence = sorted(hits, key=lambda h: h[1] != "cited")[0]
                        cited_before = True
                    elif cited_before:
                        basis, state, evidence = "re-evaluation", "not_cited_in_revision", []
                    else:
                        continue
                    created += self._insert(namespace, food, notice_id, revision, basis, state, evidence,
                                            principal_id)
            for model_id in self.identity.accepted(namespace, food["food_id"])["product_models"]:
                try:
                    matches = safety.matches_for_models(namespace, [model_id])
                except ProductSafetyError:
                    matches = []
                for match in matches:
                    if not match["attached"]:
                        continue
                    revision = next(r for r in safety.revisions(namespace, match["notice_id"])
                                    if r["revision_id"] == match["revision_id"])
                    created += self._insert(namespace, food, match["notice_id"], revision, "reviewed-notice-match",
                                            "cited", [{"kind": "reviewed-notice-match", "match_id": match["match_id"],
                                                       "model_id": model_id,
                                                       "decision": match["review_history"][-1]}], principal_id)
        return {"contract": LINK_CONTRACT, "namespace": namespace, "linked": created,
                "policy": "notice identification (GTIN, brand + exact designation) or a reviewed notice match only; "
                          "never hazard, category or ingredient similarity; each link cites its notice revision"}

    def _insert(self, namespace, food, notice_id, revision, basis, state, evidence, principal_id) -> list[dict]:
        link_id = "food-notice-link:" + digest([namespace, food["food_id"], revision["revision_id"], basis])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO food_notice_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING link_id",
            [namespace, link_id, food["food_id"], food["revision_id"], notice_id, revision["revision_id"],
             int(revision["seq"]), basis, state, canonical(evidence), principal_id, self.now()]).fetchall()
        if not inserted:
            return []
        self.store.bump(namespace)
        return [{"link_id": link_id, "food_id": food["food_id"], "notice_id": notice_id,
                 "notice_revision_id": revision["revision_id"], "basis": basis, "state": state}]

    # ------------------------------------------------------------------ reads

    def rows(self, namespace: str, food_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "food_notice_links"):
            return []
        keys = ("link_id", "food_revision_id", "notice_id", "notice_revision_id", "notice_revision_seq", "basis",
                "state", "evidence", "principal_id", "linked_at_ms")
        found = [dict(zip(keys, r)) for r in self.conn.execute(
            "SELECT link_id, food_revision_id, notice_id, notice_revision_id, notice_revision_seq, basis, state, "
            "evidence_json, principal_id, linked_at_ms FROM food_notice_links WHERE namespace=? AND food_id=? "
            "ORDER BY notice_id, notice_revision_seq, basis", [namespace, food_id]).fetchall()]
        for item in found:
            item["evidence"] = _load(item["evidence"], [])
        return found

    def notices_for(self, namespace: str, food_id: str, *, scopes: Iterable[str],
                    as_of: date | None = None, allergens: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Linked notices as of a date (the notice revision current then must itself cite the food)."""
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.rows(namespace, food_id)
        if not rows:
            return {"status": NO_NOTICE, "notices": [], "note": "no notice on record is not a statement of safety"}
        safety = self._safety()
        by_notice: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_notice.setdefault(row["notice_id"], []).append(row)
        notices, later = [], []
        for notice_id, history in sorted(by_notice.items()):
            chosen, _ = safety.revision_as_of(namespace, notice_id, as_of)
            if chosen is None:
                later.append({"notice_id": notice_id, "note": "first published after as_of"})
                continue
            eligible = [r for r in history if r["notice_revision_seq"] <= chosen["seq"]]
            current = eligible[-1] if eligible else None
            if current is None or current["state"] != "cited":
                if current is not None:
                    later.append({"notice_id": notice_id, "state": current["state"],
                                  "notice_revision_id": current["notice_revision_id"]})
                continue
            head = safety._notice_row(namespace, notice_id)
            parts = safety.parts(namespace, chosen["revision_id"])
            notices.append({
                **head, "link": {k: current[k] for k in ("link_id", "basis", "state", "evidence",
                                                         "notice_revision_id")},
                "notice_revision": {k: chosen[k] for k in ("revision_id", "revision_no", "revision_date",
                                                           "authority_declared")},
                "link_history": [{k: r[k] for k in ("link_id", "notice_revision_id", "basis", "state")}
                                 for r in history],
                "side_by_side": {
                    "label_allergens_quoted": [{"relation": a["relation"], "value": a["value"]}
                                               for a in (allergens or [])],
                    "notice_hazards_quoted": [{"hazard_type": x["hazard_type"], "description": x["description"]}
                                              for x in parts["hazards"]],
                    "note": SIDE_BY_SIDE_NOTE,
                },
            })
        return {"status": "notices on record" if notices else NO_NOTICE, "notices": notices,
                "not_current": later,
                **({} if notices else {"note": "no notice on record is not a statement of safety"})}


__all__ = ["LINK_CONTRACT", "NO_NOTICE", "SIDE_BY_SIDE_NOTE", "FoodNoticeLinks"]
