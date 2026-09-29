"""Link procurement buyers and suppliers to corporate identity, and query award history and incumbency.

Parties stay the source strings and identifiers the notices state. Linking a
party to a ``canonical_entities`` row (:mod:`src.kb.entities`) or an LEI
record (:mod:`src.kb.lei`) is a *reviewable identity decision* recorded
through the existing identity-decision owner
(:class:`src.kb.entity_history.EntityHistoryStore`): candidates are proposed
from the evidence (a source-stated LEI, a national identifier already linked,
a canonical-alias match) but nothing is linked, and no entity is ever merged,
automatically. Every decision is auditable and reversible (``undo``); an
unmatched party stays a source string.

Award history is queryable per buyer, per supplier and per CPV branch with
sources and dates. *Incumbency* is a derived, explained fact computed from
that history; it is context, never evidence that any procedure is open.
"""

from __future__ import annotations

import json
import time

from src.kb.entity_history import EntityHistoryStore
from src.kb.funding_records import canonical, digest
from src.kb.procurement_notices import ProcurementNoticeStore
from src.kb.procurement_records import READ_SCOPE, REVIEW_SCOPE, cpv_relation, lei_of

CONTRACT = "noesis-procurement-identity-link-v1"
ENTITY_REVIEW_SCOPE = "knowledge:entity-history:review"
ENTITY_WRITE_SCOPE = "knowledge:entity-history:write"
ENTITY_EXECUTE_SCOPE = "knowledge:entity-history:execute"
_DDL = """
CREATE TABLE IF NOT EXISTS procurement_party_links(
 link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, party_key TEXT NOT NULL, party_json TEXT NOT NULL,
 entity_id TEXT NOT NULL, decision TEXT NOT NULL, decision_id TEXT NOT NULL, status TEXT NOT NULL,
 evidence_json TEXT NOT NULL, reviewer TEXT NOT NULL, created_at_ms BIGINT NOT NULL, reverted_by TEXT);
"""


class IdentityError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _normal(name):
    from src.kb.entities import normalize_surface

    return normalize_surface(name or "")


def party_key(party):
    """Role-independent identity of a source party: normalised name plus stated identifiers."""
    identifiers = sorted(f"{i['scheme']}:{i['id'].upper()}" for i in party.get("identifiers") or [])
    return "procurement-party:" + digest([_normal(party["name"]), identifiers])[:24]


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT table_name FROM information_schema.tables").fetchall()}


class ProcurementIdentityService:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn, self.now = conn, now or (lambda: int(time.time() * 1000))
        if initialize:
            conn.execute(_DDL)
        self.notices = ProcurementNoticeStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)

    @staticmethod
    def _read(namespace, scopes):
        if READ_SCOPE not in scopes or not {f"namespace:{namespace}:read", f"namespace:{namespace}:write"} & set(scopes):
            raise IdentityError("unauthorized", "procurement read and namespace access are required")

    @staticmethod
    def _review(namespace, scopes):
        if not {REVIEW_SCOPE, ENTITY_REVIEW_SCOPE, ENTITY_WRITE_SCOPE, f"namespace:{namespace}:write"} <= set(scopes):
            raise IdentityError("unauthorized", "procurement review, entity-history review/write and namespace write scopes are required")

    # ------------------------------------------------------------ parties

    def parties(self, namespace, *, scopes):
        """Every buyer and awarded supplier seen in notices, with its current link (if any)."""
        self._read(namespace, scopes)
        seen = {}
        for (record_json,) in self.conn.execute(
                "SELECT record_json FROM procurement_notice_assertions WHERE namespace=? ORDER BY observed_at_ms", [namespace]).fetchall():
            item = json.loads(record_json)
            for party in [item["buyer"]] + [s for a in item.get("awards") or [] for s in a.get("suppliers") or []]:
                key = party_key(party)
                entry = seen.setdefault(key, {"party_key": key, "party": party, "roles": set(), "notices": set()})
                entry["roles"].add(party["role"])
                entry["notices"].add(item["notice_id"])
        return [{**v, "roles": sorted(v["roles"]), "notices": sorted(v["notices"]), "link": self.link_for(namespace, v["party"])}
                for v in sorted(seen.values(), key=lambda v: v["party_key"])]

    def candidates(self, namespace, party, *, scopes):
        """Proposed identities with their evidence. Proposals are never applied automatically."""
        self._read(namespace, scopes)
        result = []
        lei = lei_of(party)
        if lei:
            record = None
            if {"lei_current"} <= _tables(self.conn):
                from src.kb.lei import LeiError, LeiStore

                for lei_namespace in (namespace, "global"):
                    try:
                        record = LeiStore(self.conn, initialize=False).entity(lei_namespace, lei, scopes=scopes)
                        break
                    except LeiError:
                        continue
            result.append({"entity_id": f"lei:{lei}", "kind": "lei", "basis": "the notice states this LEI for the party",
                           "evidence": {"lei": lei, "lei_record": {"legal_name": (record.get("legal_name") or {}).get("name"),
                                                                  "revision_id": record["revision_id"],
                                                                  "registration_status": record["registration"]["status"]}
                                        if record else "not loaded in this deployment"}})
        if {"entity_aliases", "canonical_entities"} <= _tables(self.conn):
            from src.kb.entities import resolve

            match = resolve(self.conn, party["name"])
            if match:
                result.append({"entity_id": match["canonical_id"], "kind": "canonical_entity",
                               "basis": f"canonical alias match ({match['method']}, score {match['score']})",
                               "evidence": {"preferred_name": match["preferred_name"], "entity_type": match["entity_type"]}})
        wanted = {(i["scheme"], i["id"].upper()) for i in party.get("identifiers") or [] if i["scheme"] != "other"}
        for link_party, entity_id in self.conn.execute(
                "SELECT party_json, entity_id FROM procurement_party_links WHERE namespace=? AND status='active' AND decision='match'",
                [namespace]).fetchall():
            other = json.loads(link_party)
            shared = wanted & {(i["scheme"], i["id"].upper()) for i in other.get("identifiers") or []}
            if shared and party_key(other) != party_key(party):
                result.append({"entity_id": entity_id, "kind": "linked-identifier",
                               "basis": "another party with the same identifier is already linked",
                               "evidence": {"shared_identifiers": sorted(f"{s}:{i}" for s, i in shared), "party": other["name"]}})
        return {"party_key": party_key(party), "party": party, "candidates": result,
                "policy": "proposals only; a reviewer decides each link; nothing is merged automatically"}

    def decide(self, namespace, party, entity_id, *, decision, principal_id, scopes, evidence=None, note=None):
        """Record a reviewed match / non-match decision through the identity-decision owner."""
        self._review(namespace, scopes)
        if decision not in {"match", "non-match"}:
            raise IdentityError("invalid_decision", "decision is match or non-match")
        if not isinstance(entity_id, str) or not entity_id.strip():
            raise IdentityError("invalid_decision", "entity_id is required")
        key = party_key(party)
        self.history.register_entity(namespace, key, [party["name"]], principal_id=principal_id, scopes=scopes)
        self.history.register_entity(namespace, entity_id, [], principal_id=principal_id, scopes=scopes)
        payload = {"source": "procurement", "party": party, "evidence": evidence or {}, "note": note or "",
                   "policy": {"kind": "link-only", "merge": "never automatic"}}
        made = self.history.decide(namespace, decision, [key, entity_id], payload, reviewer_id=principal_id,
                                   principal_id=principal_id, scopes=scopes, event_key=f"procurement-link:{key}:{entity_id}")
        link_id = "procurement-link:" + digest([namespace, key, entity_id])[:24]
        self.conn.execute(
            """INSERT INTO procurement_party_links VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)
               ON CONFLICT (link_id) DO UPDATE SET decision=excluded.decision, decision_id=excluded.decision_id,
               status=excluded.status, evidence_json=excluded.evidence_json, reviewer=excluded.reviewer, reverted_by=NULL""",
            [link_id, namespace, key, canonical(party), entity_id, decision, made["decision_id"], "active",
             canonical(evidence or {}), principal_id, self.now()])
        return {"contract": CONTRACT, "link_id": link_id, "party_key": key, "entity_id": entity_id, "decision": decision,
                "decision_id": made["decision_id"], "revision": made["revision"], "reversible": True,
                "merged": False}

    def revert(self, namespace, link_id, *, principal_id, scopes):
        """Undo a link decision; the undo is itself an auditable identity decision."""
        self._review(namespace, scopes)
        if ENTITY_EXECUTE_SCOPE not in scopes:
            raise IdentityError("unauthorized", "entity-history execute scope is required to undo a decision")
        row = self.conn.execute("SELECT decision_id, status FROM procurement_party_links WHERE link_id=? AND namespace=?",
                                [link_id, namespace]).fetchone()
        if not row or row[1] != "active":
            raise IdentityError("link_not_found", "no active link")
        undo = self.history.undo(namespace, row[0], reviewer_id=principal_id, principal_id=principal_id, scopes=scopes)
        self.conn.execute("UPDATE procurement_party_links SET status='reverted', reverted_by=? WHERE link_id=?", [undo["decision_id"], link_id])
        return {"link_id": link_id, "status": "reverted", "undo_decision_id": undo["decision_id"], "undoes": row[0]}

    def link_for(self, namespace, party):
        row = self.conn.execute(
            "SELECT link_id, entity_id, decision_id, reviewer FROM procurement_party_links WHERE namespace=? AND party_key=? "
            "AND status='active' AND decision='match' ORDER BY created_at_ms DESC LIMIT 1", [namespace, party_key(party)]).fetchone()
        return None if not row else {"link_id": row[0], "entity_id": row[1], "decision_id": row[2], "reviewer": row[3]}

    def audit(self, namespace, *, scopes):
        self._read(namespace, scopes)
        return [{"link_id": r[0], "party_key": r[1], "entity_id": r[2], "decision": r[3], "decision_id": r[4], "status": r[5],
                 "reviewer": r[6], "reverted_by": r[7]} for r in self.conn.execute(
            "SELECT link_id, party_key, entity_id, decision, decision_id, status, reviewer, reverted_by FROM procurement_party_links "
            "WHERE namespace=? ORDER BY created_at_ms, link_id", [namespace]).fetchall()]

    # ------------------------------------------------------------ history

    def _entity(self, namespace, party):
        link = self.link_for(namespace, party)
        return link["entity_id"] if link else None

    def award_history(self, namespace, *, scopes, buyer=None, supplier=None, buyer_entity=None, supplier_entity=None, cpv=None):
        """Award history with sources and dates; entity filters follow reviewed links only."""
        self._read(namespace, scopes)
        rows = self.notices.award_history(namespace, scopes=scopes, buyer=buyer, supplier=supplier, cpv=cpv, limit=5000)
        result = []
        for row in rows:
            buyer_link = self._entity(namespace, row["buyer"])
            supplier_links = [self._entity(namespace, s) for s in row["suppliers"]]
            if buyer_entity and buyer_link != buyer_entity:
                continue
            if supplier_entity and supplier_entity not in supplier_links:
                continue
            result.append({**row, "buyer_entity": buyer_link, "supplier_entities": supplier_links})
        return result

    def incumbency(self, namespace, *, scopes, buyer, cpv, supplier_names=(), supplier_entities=(), rows=None):
        """Derived, explained incumbency for a buyer and CPV branch.

        ``buyer`` is a party dict (from a notice) or a name. Awards count when
        the buyer matches by linked entity, name or identifier and a CPV code
        lies in the branch of ``cpv`` (either direction).
        """
        self._read(namespace, scopes)
        buyer_party = buyer if isinstance(buyer, dict) else {"name": buyer, "identifiers": []}
        buyer_entity = self._entity(namespace, buyer_party) if isinstance(buyer, dict) else None
        wanted_ids = {(i["scheme"], i["id"].upper()) for i in buyer_party.get("identifiers") or []}
        history = rows if rows is not None else self.notices.award_history(namespace, scopes=scopes, limit=5000)
        rows = []
        for row in history:
            if row["stage"] != "award" or row["status"] not in ("active", None):
                continue
            same_buyer = (_normal(row["buyer"]["name"]) == _normal(buyer_party["name"])
                          or bool(wanted_ids & {(i["scheme"], i["id"].upper()) for i in row["buyer"].get("identifiers") or []})
                          or (buyer_entity is not None and self._entity(namespace, row["buyer"]) == buyer_entity))
            related = [code for code in row["cpv"] for wanted in ([cpv] if isinstance(cpv, str) else cpv)
                       if cpv_relation(wanted, code) == "covers" or cpv_relation(code, wanted) == "covers"]
            if same_buyer and related:
                rows.append(row)
        names = {_normal(n) for n in supplier_names if n}
        incumbents = {}
        for row in rows:
            for supplier in row["suppliers"]:
                entity = self._entity(namespace, supplier)
                key = entity or party_key(supplier)
                entry = incumbents.setdefault(key, {"supplier": supplier, "entity_id": entity, "awards": [],
                                                   "is_profile_supplier": False})
                entry["awards"].append({"notice_id": row["notice_id"], "date": row["date"], "value": row["value"],
                                        "source_url": row["source_url"], "cpv": row["cpv"],
                                        "contract_period": (row.get("contract") or {}).get("period")})
                if _normal(supplier["name"]) in names or (entity and entity in set(supplier_entities)):
                    entry["is_profile_supplier"] = True
        items = sorted(incumbents.values(), key=lambda v: max((a["date"] or "") for a in v["awards"]), reverse=True)
        if items:
            latest = items[0]
            explanation = (f"{latest['supplier']['name']} holds the most recent award from {buyer_party['name']} in this CPV "
                           f"branch ({latest['awards'][0]['date']}, notice {latest['awards'][0]['notice_id']}).")
        else:
            explanation = f"No award from {buyer_party['name']} in this CPV branch is in the acquired history."
        return {"buyer": buyer_party["name"], "buyer_entity": buyer_entity, "cpv": cpv, "incumbents": items,
                "explanation": explanation, "evidence_count": len(rows),
                "semantics": "derived from acquired award history; context only, never evidence that a procedure is open, "
                             "and limited to the notices acquired"}
