"""Facility operators to corporate identity, and facility evidence for policy obligations (E10).

Operators are kept exactly as the source published them. Matching to
``canonical_entities`` (``src/kb/entities.py``) and LEI records
(``src/kb/lei.py``) produces **candidate identity decisions** only; a
different principal with ``knowledge:environment:review`` accepts or rejects
each one with a reason. Unmatched operators stay source strings.

Policy obligations are read from the policy monitor's assertion store
(``versioned_assertions`` via ``src/kb/assertions.py``). Attaching evidence
records which facility release (series + pinned vintage + period) was
shown next to which obligation and its source document. The pack never
compares the two, never produces a compliance determination and never
asserts a threshold of its own.
"""

from __future__ import annotations

import json
import time

from src.kb import environment_records as er
from src.kb.environment_records import READ_SCOPE, REVIEW_SCOPE, WRITE_SCOPE, canonical, digest
from src.kb.environment_store import EnvironmentStore, EnvironmentStoreError, authorize

LINK_CONTRACT = "noesis-environment-operator-link-v1"
EVIDENCE_CONTRACT = "noesis-environment-obligation-evidence-v1"
_DDL = """
CREATE TABLE IF NOT EXISTS environment_operator_links(
 link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, facility_record TEXT NOT NULL, operator_name TEXT NOT NULL,
 target_kind TEXT NOT NULL, target_id TEXT NOT NULL, basis TEXT NOT NULL, evidence_json TEXT NOT NULL,
 state TEXT NOT NULL, proposed_by TEXT NOT NULL, reviewed_by TEXT, reason TEXT, created_at_ms BIGINT NOT NULL,
 reviewed_at_ms BIGINT);
CREATE TABLE IF NOT EXISTS environment_obligation_evidence(
 evidence_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, subject_id TEXT NOT NULL, assertion_key TEXT NOT NULL,
 obligation_json TEXT NOT NULL, facility_record TEXT NOT NULL, series_record TEXT NOT NULL, vintage_id TEXT NOT NULL,
 period_start TEXT NOT NULL, evidence_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL);
"""


def _table(conn, name):
    return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_name=?", [name]).fetchone())


def operator_view(conn, namespace, facility_record, operator):
    """The operator as published plus accepted links and open candidates."""

    links = []
    if _table(conn, "environment_operator_links"):
        links = [{"link_id": r[0], "target_kind": r[1], "target_id": r[2], "basis": r[3], "state": r[4]}
                 for r in conn.execute("SELECT link_id, target_kind, target_id, basis, state FROM environment_operator_links "
                                       "WHERE namespace=? AND facility_record=? ORDER BY link_id",
                                       [namespace, facility_record]).fetchall()]
    accepted = [link for link in links if link["state"] == "accepted"]
    return {"source_name": (operator or {}).get("name"), "identifiers": (operator or {}).get("identifiers") or {},
            "links": accepted, "candidates": [link for link in links if link["state"] == "candidate"],
            "state": "linked" if accepted else "unmatched (source string)"}


class EnvironmentIdentity:
    def __init__(self, conn, *, initialize=True, now=None):
        self.conn = conn
        self.store = EnvironmentStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ operators

    def _lei_candidates(self, namespace, name, identifiers):
        from src.kb.entities import normalize_surface

        if not _table(self.conn, "lei_current"):
            return []
        wanted = normalize_surface(name)
        number = "".join(ch for ch in str(identifiers.get("COMPANY_REGISTRATION_NUMBER") or "") if ch.isalnum()).upper()
        result = []
        for lei, attributes in self.conn.execute(
                "SELECT c.lei, r.attributes_json FROM lei_current c JOIN lei_revisions r USING(revision_id) "
                "WHERE c.namespace IN (?, 'global') ORDER BY c.lei", [namespace]).fetchall():
            entity = dict(json.loads(attributes).get("entity") or {})
            legal = dict(entity.get("legalName") or {}).get("name") or ""
            registered = "".join(ch for ch in str(entity.get("registeredAs") or "") if ch.isalnum()).upper()
            if number and registered and number == registered:
                result.append((lei, "registration-number match (operator record vs LEI registeredAs)",
                               {"registered_as": entity.get("registeredAs"), "legal_name": legal}))
            elif legal and normalize_surface(legal) == wanted:
                result.append((lei, "normalized legal-name match", {"legal_name": legal}))
        return result

    def _entity_candidates(self, name):
        if not _table(self.conn, "entity_aliases"):
            return []
        from src.kb.entities import resolve

        match = resolve(self.conn, name)
        if match is None:
            return []
        return [(match["canonical_id"], f"entity alias match ({match['method']})",
                 {"preferred_name": match["preferred_name"], "score": match["score"]})]

    def propose_operator_links(self, namespace, *, principal_id, scopes):
        """Create candidate identity decisions for every facility operator; nothing is accepted."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        created, unmatched = [], []
        for record in self.store.records(namespace, scopes=scopes, record_type="facility"):
            operator = record["content"]["operator"]
            name = operator.get("name")
            if not name:
                unmatched.append({"record_id": record["record_id"], "reason": "operator not published"})
                continue
            candidates = [("lei", *c) for c in self._lei_candidates(namespace, name, operator.get("identifiers") or {})]
            candidates += [("canonical-entity", *c) for c in self._entity_candidates(name)]
            if not candidates:
                unmatched.append({"record_id": record["record_id"], "operator": name,
                                  "reason": "no candidate; operator stays a source string"})
            for kind, target, basis, evidence in candidates:
                link_id = "env-operator-link:" + digest([namespace, record["record_id"], kind, target])[:24]
                inserted = self.conn.execute(
                    "INSERT INTO environment_operator_links VALUES (?,?,?,?,?,?,?,?,'candidate',?,NULL,NULL,?,NULL) "
                    "ON CONFLICT DO NOTHING RETURNING link_id",
                    [link_id, namespace, record["record_id"], name, kind, target, basis,
                     canonical({**evidence, "operator_as_published": name, "facility_revision": record["revision_id"]}),
                     principal_id, self.now()]).fetchall()
                if inserted:
                    created.append(link_id)
        return {"contract": LINK_CONTRACT, "created": created, "candidates": self.links(namespace, scopes=scopes),
                "unmatched": unmatched, "policy": "candidates only; another principal reviews each decision"}

    def review(self, namespace, link_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accepted", "rejected"} or not str(reason or "").strip():
            raise EnvironmentStoreError("invalid_decision", "accept or reject with a reason")
        row = self.conn.execute("SELECT proposed_by, state FROM environment_operator_links WHERE namespace=? AND link_id=?",
                                [namespace, link_id]).fetchone()
        if row is None:
            raise EnvironmentStoreError("not_found", "identity decision is not visible in this namespace")
        if row[0] == principal_id:
            raise EnvironmentStoreError("self_review", "the proposer cannot review their own identity decision")
        if row[1] != "candidate":
            raise EnvironmentStoreError("already_reviewed", "identity decision was already reviewed")
        self.conn.execute("UPDATE environment_operator_links SET state=?, reviewed_by=?, reason=?, reviewed_at_ms=? "
                          "WHERE link_id=?", [decision, principal_id, reason.strip(), self.now(), link_id])
        return next(link for link in self.links(namespace, scopes=scopes) if link["link_id"] == link_id)

    def links(self, namespace, *, scopes, facility_record=None):
        authorize(namespace, scopes, READ_SCOPE)
        rows = self.conn.execute(
            "SELECT link_id, facility_record, operator_name, target_kind, target_id, basis, evidence_json, state, proposed_by, "
            "reviewed_by, reason FROM environment_operator_links WHERE namespace=? AND (? IS NULL OR facility_record=?) "
            "ORDER BY link_id", [namespace, facility_record, facility_record]).fetchall()
        keys = ("link_id", "facility_record", "operator_name", "target_kind", "target_id", "basis")
        return [{"contract": LINK_CONTRACT, **dict(zip(keys, r[:6])), "evidence": json.loads(r[6]), "state": r[7],
                 "proposed_by": r[8], "reviewed_by": r[9], "reason": r[10]} for r in rows]

    # ------------------------------------------------------------ obligations

    def obligations(self, subject_id):
        """Public obligations the policy monitor recorded for a subject (latest effective per key)."""

        from src.kb.assertions import ensure_assertion_schema

        ensure_assertion_schema(self.conn)
        rows = self.conn.execute(
            "SELECT assertion_key, assertion_value_json, effective_at_ms, document_id, record_kind FROM versioned_assertions "
            "WHERE subject_id=? AND visibility='public' QUALIFY row_number() OVER (PARTITION BY assertion_key "
            "ORDER BY effective_at_ms DESC, document_id DESC)=1 ORDER BY assertion_key", [subject_id]).fetchall()
        return [{"subject_id": subject_id, "assertion_key": r[0], "value": json.loads(r[1]), "effective_at_ms": int(r[2]),
                 "document_id": r[3], "record_kind": r[4], "owner": "src/policy_monitor (versioned_assertions)"}
                for r in rows]

    def attach_evidence(self, namespace, *, subject_id, assertion_key, facility_record, series_record, period_start,
                        principal_id, scopes):
        """Show a facility release beside a policy obligation, citing both; no determination is produced."""

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        obligation = next((o for o in self.obligations(subject_id) if o["assertion_key"] == assertion_key), None)
        if obligation is None:
            raise EnvironmentStoreError("not_found", "the policy monitor holds no public obligation with that key")
        facility = self.store.record(namespace, facility_record, scopes=scopes)
        series = self.store.series(namespace, series_record, scopes=scopes)
        ref = series["location"].get("ref") if series["location"] else None
        if ref != f"{facility['provider']}:{facility['native_id']}":
            raise EnvironmentStoreError("invalid_evidence", "the series is not a release series of that facility")
        vintage = series["vintage"]
        value = next((v for v in series["values"] if v["start"] == period_start), None)
        if vintage is None or value is None:
            raise EnvironmentStoreError("not_found", "no value for that period in the current vintage")
        target_unit = obligation["value"].get("unit") if isinstance(obligation["value"], dict) else None
        converted = None
        if target_unit and value["value"] is not None and er.unit_expression(target_unit):
            converted = er.normalise(value["value"], value["unit"], target=target_unit)
        evidence = {"facility": {"record_id": facility_record, "revision_id": facility["revision_id"],
                                 "title": facility["content"]["title"], "source_url": facility["content"]["source_url"],
                                 "operator": operator_view(self.conn, namespace, facility_record,
                                                           facility["content"]["operator"]),
                                 "permits": facility["content"].get("permits")},
                    "release": {"series_record": series_record, "indicator": series["indicator"], "kind": series["kind"],
                                "period_start": period_start, "value": value["value"], "unit": value["unit"],
                                "status": value["status"], "flags": value["flags"],
                                "converted_to_obligation_unit": converted},
                    "vintage": {k: vintage[k] for k in ("vintage_id", "release_at_ms", "release_at_basis",
                                                        "retrieved_at_ms", "status")}}
        evidence_id = "env-obligation-evidence:" + digest([namespace, subject_id, assertion_key, series_record,
                                                           vintage["vintage_id"], period_start])[:24]
        self.conn.execute("INSERT INTO environment_obligation_evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                          [evidence_id, namespace, subject_id, assertion_key, canonical(obligation), facility_record,
                           series_record, vintage["vintage_id"], period_start, canonical(evidence), principal_id,
                           int(time.time() * 1000)])
        return self.evidence(namespace, evidence_id, scopes=scopes)

    def evidence(self, namespace, evidence_id, *, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        row = self.conn.execute("SELECT subject_id, assertion_key, obligation_json, vintage_id, evidence_json, created_by "
                                "FROM environment_obligation_evidence WHERE namespace=? AND evidence_id=?",
                                [namespace, evidence_id]).fetchone()
        if row is None:
            raise EnvironmentStoreError("not_found", "obligation evidence is not visible")
        newer = [r[0] for r in self.conn.execute(
            "SELECT v2.vintage_id FROM environment_vintages v1 JOIN environment_vintages v2 ON v2.record_id=v1.record_id "
            "AND v2.sequence>v1.sequence WHERE v1.vintage_id=? ORDER BY v2.sequence", [row[3]]).fetchall()]
        return {"contract": EVIDENCE_CONTRACT, "evidence_id": evidence_id, "subject_id": row[0], "assertion_key": row[1],
                "obligation": json.loads(row[2]), "evidence": json.loads(row[4]), "created_by": row[5],
                "stale": bool(newer), "newer_vintages": newer,
                "determination": None,
                "notice": ("The obligation and the facility evidence are shown side by side with their sources. No "
                           "compliance determination is produced; the policy monitor decides nothing new.")}

    def evidence_for_subject(self, subject_id, *, namespace, scopes):
        authorize(namespace, scopes, READ_SCOPE)
        ids = [r[0] for r in self.conn.execute(
            "SELECT evidence_id FROM environment_obligation_evidence WHERE namespace=? AND subject_id=? ORDER BY evidence_id",
            [namespace, subject_id]).fetchall()]
        return [self.evidence(namespace, evidence_id, scopes=scopes) for evidence_id in ids]
