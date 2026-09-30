"""IPEDS and ETER institutions matched to ROR through reviewable identity (#2227, ED08 #2414).

ROR (:mod:`src.ingestion.ror`) is the identity backbone. ROR records are acquired through
:class:`~src.ingestion.ror.RORClient` (``record`` for an identifier, ``search`` for name candidates) and normalised by
it; each distinct normalised record is kept as a snapshot (``edu_ror_snapshots``) so a later status change, merger or
split is visible against the snapshot a match was made on. Every institution a source publishes (IPEDS UNITID, ETER
ID) is offered to the held ROR records:

* ``source-stated-ror`` - the source record itself publishes the ROR id: **exact**;
* ``ror-external-id`` - an identifier the source publishes (Wikidata, ISNI, GRID, FundRef) appears among the ROR
  record's published ``external_ids`` of the same type: **exact** (several ROR records carrying it are candidates);
* ``name-location`` - a ROR name equal to the source's institution name in the same country (the city is shown as
  evidence): a **candidate** that a reviewer must accept or reject.

Exact matches are usable at once; candidates are not used to answer ROR-keyed queries until accepted, and rejected
or reverted matches never are. Unmatched institutions stay queryable by their source id. Review decisions are entity
identity decisions in :class:`src.kb.entity_history.EntityHistoryStore`, so the review inbox
(:mod:`src.kb.review_targets`, kind ``entity``) sees them. A ROR record that becomes inactive or withdrawn, or that
states a successor or predecessor (a merger or split), is **reported** with the matches it concerns; no match is ever
re-pointed to the successor. No new entity store is created.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from src.kb.education_statistics import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    EducationError,
    EducationStatisticsStore,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    table_exists,
)

CONTRACT = "noesis-education-identity-v1"
BASES = ("source-stated-ror", "ror-external-id", "name-location")
EXACT_BASES = ("source-stated-ror", "ror-external-id")
CONFIRMED = ("exact", "accepted")
EXTERNAL_TYPES = ("wikidata", "isni", "grid", "fundref")
# Country codes as some sources publish them against ISO 3166-1 alpha-2 (ROR locations).
COUNTRY_ALIASES = {"EL": "GR", "UK": "GB"}
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_DDL = """
CREATE TABLE IF NOT EXISTS edu_ror_snapshots (
  namespace TEXT NOT NULL, ror_id TEXT NOT NULL, sha256 TEXT NOT NULL, status TEXT NOT NULL,
  record_json TEXT NOT NULL, observed_at_ms BIGINT NOT NULL, sequence INTEGER NOT NULL,
  PRIMARY KEY(namespace, ror_id, sha256)
);
CREATE TABLE IF NOT EXISTS edu_identity_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, subject_scheme TEXT NOT NULL, subject_code TEXT NOT NULL,
  ror_id TEXT, basis TEXT, evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT,
  history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, match_id)
);
"""


def ror_url(value: Any) -> str:
    """A ROR id as its canonical URL (``https://ror.org/0abcdef12``); refused when it is not a ROR id."""
    from src.ingestion.ror import RORClient
    from src.integrations.common import IntegrationError

    try:
        return "https://ror.org/" + RORClient.identifier(str(value or "").strip())
    except IntegrationError as exc:
        raise EducationError("invalid_request", "not a ROR identifier") from exc


def _country(value: Any) -> str | None:
    code = str(value or "").strip().upper()
    return COUNTRY_ALIASES.get(code, code) or None


def _entity(kind: str, key: str) -> str:
    return f"ent-edu-{kind}-" + re.sub(r"[^a-z0-9]+", "-", key.casefold()).strip("-")


class EducationIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.store = EducationStatisticsStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "edu_identity_matches")

    # ------------------------------------------------------------------ ROR snapshots

    def record_ror(self, namespace: str, records: Sequence[Mapping[str, Any]], *, principal_id: str,
                   scopes: Iterable[str]) -> dict[str, Any]:
        """Keep each ROR record (native v2 or already normalised by RORClient) as a snapshot; a changed record is a
        new snapshot, and a status or relationship change is reported."""
        from src.ingestion.ror import RORClient

        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        del principal_id
        client = RORClient(transport=lambda **_: {"status": 599})  # normalisation only; never contacts ROR here
        recorded, changes = [], []
        for record in records:
            record = dict(record)
            normal = dict(record) if "ror" in record and "native_record" in record else client.normalize(record)
            ror_id, sha = normal["ror"], normal["sha256"]
            previous = self.ror_record(namespace, ror_id)
            exists = self.conn.execute(
                "SELECT 1 FROM edu_ror_snapshots WHERE namespace=? AND ror_id=? AND sha256=?", [namespace, ror_id, sha]
            ).fetchone()
            if exists:
                continue
            sequence = 1 + int(self.conn.execute(
                "SELECT count(*) FROM edu_ror_snapshots WHERE namespace=? AND ror_id=?", [namespace, ror_id]
            ).fetchone()[0])
            self.conn.execute(
                "INSERT INTO edu_ror_snapshots VALUES (?,?,?,?,?,?,?)",
                [namespace, ror_id, sha, normal["status"], canonical(normal), self.now(), sequence],
            )
            recorded.append(ror_id)
            if previous is not None:
                change = self._difference(previous, normal)
                if change:
                    changes.append({"ror_id": ror_id, **change})
        return {"recorded": recorded, "changes": changes}

    def fetch_ror(self, namespace: str, identifiers: Iterable[str], *, client: Any, principal_id: str,
                  scopes: Iterable[str]) -> dict[str, Any]:
        """Fetch ROR records by identifier through the ROR client (bounded by the identifiers given)."""
        records = [client.record(ror_url(i)) for i in list(identifiers)[:50]]
        return self.record_ror(namespace, records, principal_id=principal_id, scopes=scopes)

    @staticmethod
    def _difference(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any] | None:
        out = {}
        if before["status"] != after["status"]:
            out["status"] = {"before": before["status"], "after": after["status"]}
        rel_before = {(r["type"], r["id"]) for r in before.get("relationships") or []}
        rel_after = {(r["type"], r["id"]) for r in after.get("relationships") or []}
        if rel_before != rel_after:
            out["relationships"] = {"added": sorted(rel_after - rel_before), "removed": sorted(rel_before - rel_after)}
        return out or None

    def ror_record(self, namespace: str, ror_id: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "edu_ror_snapshots"):
            return None
        row = self.conn.execute(
            "SELECT record_json FROM edu_ror_snapshots WHERE namespace=? AND ror_id=? ORDER BY sequence DESC LIMIT 1",
            [namespace, ror_id],
        ).fetchone()
        return None if row is None else json.loads(row[0])

    def ror_snapshots(self, namespace: str, ror_id: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "edu_ror_snapshots"):
            return []
        return [
            {"ror_id": ror_id, "sha256": r[0], "status": r[1], "observed_at": iso_from_ms(r[2]), "sequence": r[3]}
            for r in self.conn.execute(
                "SELECT sha256, status, observed_at_ms, sequence FROM edu_ror_snapshots WHERE namespace=? AND "
                "ror_id=? ORDER BY sequence", [namespace, ror_id]).fetchall()
        ]

    def _ror_records(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "edu_ror_snapshots"):
            return []
        ids = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT ror_id FROM edu_ror_snapshots WHERE namespace=? ORDER BY ror_id", [namespace]).fetchall()]
        return [self.ror_record(namespace, i) for i in ids]

    def ror_changes(self, namespace: str, ror_id: str) -> list[dict[str, Any]]:
        """Reported identity events for a ROR record: inactive or withdrawn status, successors and predecessors
        (mergers and splits) and changes between snapshots. Nothing is re-pointed."""
        record = self.ror_record(namespace, ror_id)
        if record is None:
            return []
        out = []
        if record["status"] != "active":
            out.append({"kind": "status", "status": record["status"],
                        "note": "the ROR record is not active; matches stay on this record and are not re-pointed"})
        for relation in record.get("relationships") or []:
            if relation["type"] in {"successor", "predecessor"}:
                out.append({"kind": relation["type"], "related_ror_id": relation["id"],
                            "label": relation.get("label"),
                            "note": "a merger or split stated by ROR is reported; nothing is re-pointed"})
        snapshots = self.ror_snapshots(namespace, ror_id)
        for before, after in zip(snapshots, snapshots[1:]):
            if before["status"] != after["status"]:
                out.append({"kind": "status_changed", "before": before["status"], "after": after["status"],
                            "observed_at": after["observed_at"]})
        return out

    # ------------------------------------------------------------------ proposals

    @staticmethod
    def _names(record: Mapping[str, Any]) -> set[str]:
        return {str(n["value"]).casefold() for n in record.get("names") or [] if n.get("value")}

    @staticmethod
    def _locations(record: Mapping[str, Any]) -> list[dict[str, Any]]:
        out = []
        for location in record.get("locations") or []:
            details = dict(location.get("geonames_details") or {})
            out.append({"country": _country(details.get("country_code")), "city": details.get("name")})
        return out

    @staticmethod
    def _external(record: Mapping[str, Any]) -> set[tuple[str, str]]:
        native = dict(record.get("native_record") or {})
        found = set()
        for external in native.get("external_ids") or []:
            for value in external.get("all") or []:
                found.add((str(external.get("type")).casefold(), str(value).strip().casefold()))
        return found

    def _evaluate(self, subject: Mapping[str, Any], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        stated = {str(k).casefold(): str(v) for k, v in dict(subject.get("stated_ids") or {}).items()}
        if stated.get("ror"):
            try:
                ror = ror_url(stated["ror"])
            except EducationError:
                ror = None
            if ror:
                held = next((r for r in records if r["ror"] == ror), None)
                return [{"ror_id": ror, "basis": "source-stated-ror", "state": "exact",
                         "evidence": {"stated_by": subject["scheme"], "value": stated["ror"],
                                      "ror_record_held": held is not None,
                                      "ror_record_sha256": held["sha256"] if held else None}}]
        published = {(k, v.strip().casefold()) for k, v in stated.items() if k in EXTERNAL_TYPES}
        by_external = [(r, sorted(published & self._external(r))) for r in records]
        by_external = [(r, shared) for r, shared in by_external if shared]
        if by_external:
            state = "exact" if len(by_external) == 1 else "candidate"
            return [{"ror_id": r["ror"], "basis": "ror-external-id", "state": state,
                     "evidence": {"shared_identifiers": [{"type": t, "value": v} for t, v in shared],
                                  "ror_record_sha256": r["sha256"],
                                  **({"conflict": "several ROR records publish this identifier"}
                                     if state == "candidate" else {})}}
                    for r, shared in by_external]
        label = str(subject.get("label") or "").casefold()
        country = _country(subject.get("country"))
        out = []
        if label:
            for record in records:
                locations = self._locations(record)
                if label in self._names(record) and country in {loc["country"] for loc in locations}:
                    city = str(subject.get("city") or "").casefold()
                    out.append({"ror_id": record["ror"], "basis": "name-location", "state": "candidate",
                                "evidence": {"name": subject.get("label"), "country": country,
                                             "source_city": subject.get("city"),
                                             "ror_cities": [loc["city"] for loc in locations],
                                             "same_city": bool(city) and city in {str(loc["city"]).casefold()
                                                                                  for loc in locations},
                                             "ror_record_sha256": record["sha256"],
                                             "note": "equal name and country only; a reviewer decides"}})
        return out

    def _matches_for(self, namespace: str, scheme: str, code: str) -> list[dict[str, Any]]:
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM edu_identity_matches WHERE namespace=? AND subject_scheme=? AND subject_code=? "
            "ORDER BY created_at_ms, match_id", [namespace, scheme, str(code)]).fetchall()
        return [self.match(namespace, r[0], scopes={"operator"}) for r in rows]

    def _insert(self, namespace, subject, ror_id, basis, evidence, state, principal_id):
        match_id = "edu-identity:" + digest([namespace, subject["scheme"], subject["code"], ror_id, basis])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO edu_identity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, match_id, subject["scheme"], str(subject["code"]), ror_id, basis, canonical(evidence), state,
             None, canonical([{"state": state, "by": principal_id, "at_ms": now}]), principal_id, now],
        )
        return match_id

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str], ror_client: Any = None
                ) -> dict[str, Any]:
        """Offer every institution to the held ROR records (and, with a ROR client, to its name search);
        idempotent. Exact identifier matches are usable at once; name/location matches wait for review."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.conn.execute(_DDL)
        created, out = [], []
        for subject in self.store.institutions(namespace):
            records = self._ror_records(namespace)
            proposals = self._evaluate(subject, records)
            if not proposals and ror_client is not None and subject.get("label"):
                found = ror_client.search(str(subject["label"]))
                self.record_ror(namespace, found["candidates"], principal_id=principal_id, scopes={"operator"})
                proposals = self._evaluate(subject, self._ror_records(namespace))
            existing = self._matches_for(namespace, subject["scheme"], subject["code"])
            known = {(m["ror_id"], m["basis"]) for m in existing}
            for proposal in proposals:
                if (proposal["ror_id"], proposal["basis"]) in known:
                    continue
                created.append(self._insert(namespace, subject, proposal["ror_id"], proposal["basis"],
                                            proposal["evidence"], proposal["state"], principal_id))
            unmatched = [m for m in existing if m["state"] == "unmatched"]
            if proposals:
                for item in unmatched:
                    self._transition(namespace, item, "superseded", None, principal_id, "a match was proposed")
            elif not unmatched and not [m for m in existing if m["state"] != "superseded"]:
                created.append(self._insert(namespace, subject, None, None,
                                            {"reason": "no ROR record carries a published identifier, name and "
                                             "country of this institution", "label": subject.get("label")},
                                            "unmatched", principal_id))
            out += [m["match_id"] for m in self._matches_for(namespace, subject["scheme"], subject["code"])]
        return {"created": created, "matches": [self.match(namespace, m, scopes={"operator"}) for m in out]}

    # ------------------------------------------------------------------ review

    def match(self, namespace: str, match_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT match_id, subject_scheme, subject_code, ror_id, basis, evidence_json, state, decision_id, "
            "history_json, created_by, created_at_ms FROM edu_identity_matches WHERE namespace=? AND match_id=?",
            [namespace, match_id],
        ).fetchone()
        if row is None:
            raise EducationError("not_found", "identity match is not visible in this namespace")
        return {
            "contract": CONTRACT, "namespace": namespace, "match_id": row[0],
            "subject": {"scheme": row[1], "code": row[2]}, "ror_id": row[3], "basis": row[4],
            "exact": row[4] in EXACT_BASES and row[6] == "exact", "evidence": json.loads(row[5]), "state": row[6],
            "decision_id": row[7], "history": json.loads(row[8]), "created_by": row[9], "created_at_ms": row[10],
            "usable_for_ror_queries": row[6] in CONFIRMED,
        }

    def matches(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                ror_id: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM edu_identity_matches WHERE namespace=? AND (? IS NULL OR state=?) AND "
            "(? IS NULL OR ror_id=?) ORDER BY subject_scheme, subject_code, created_at_ms, match_id",
            [namespace, state, state, ror_id, ror_id]).fetchall()
        return [self.match(namespace, r[0], scopes={"operator"}) for r in rows]

    def _transition(self, namespace, item, state, decision_id, principal_id, reason):
        history = item["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                      "decision_id": decision_id}]
        self.conn.execute(
            "UPDATE edu_identity_matches SET state=?, decision_id=coalesce(?, decision_id), history_json=? WHERE "
            "namespace=? AND match_id=?", [state, decision_id, canonical(history), namespace, item["match_id"]])
        return self.match(namespace, item["match_id"], scopes={"operator"})

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Accept or reject a candidate (or a reverted match) with a reason, as an entity identity decision."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise EducationError("invalid_decision", "accept or reject with a reason")
        item = self.match(namespace, match_id, scopes={"operator"})
        if item["state"] not in {"candidate", "reverted"}:
            raise EducationError("invalid_state", f"match is {item['state']}; only a candidate is reviewed")
        subject_key = f"{item['subject']['scheme']}:{item['subject']['code']}"
        left, right = _entity("inst", subject_key), _entity("ror", item["ror_id"].rsplit("/", 1)[-1])
        self.history.register_entity(namespace, left, [subject_key], principal_id=principal_id,
                                     scopes=_ENTITY_HISTORY_SCOPES)
        self.history.register_entity(namespace, right, [item["ror_id"]], principal_id=principal_id,
                                     scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [left, right],
            {"match_id": match_id, "basis": item["basis"], "evidence": item["evidence"], "reason": reason.strip(),
             "provenance": {"producer": "science.education-statistics", "records": [subject_key, item["ror_id"]]},
             "policy": {"merge": False, "note": "identity decision only; the source institution stays separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"education-identity:{namespace}:{match_id}:{len(item['history'])}")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason.strip())

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        """Withdraw a reviewed or exact match; it is no longer used until it is reviewed again."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise EducationError("invalid_decision", "a revert needs a reason")
        item = self.match(namespace, match_id, scopes={"operator"})
        if item["state"] not in {"accepted", "rejected", "exact"}:
            raise EducationError("invalid_state", "only an exact, accepted or rejected match can be reverted")
        decision_id = None
        if item["decision_id"]:
            decision_id = self.history.undo(namespace, item["decision_id"], reviewer_id=principal_id,
                                            principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)["decision_id"]
        return self._transition(namespace, item, "reverted", decision_id, principal_id, reason.strip())

    # ------------------------------------------------------------------ use by queries

    def institutions_for_ror(self, namespace: str, ror: str) -> dict[str, Any]:
        """The source institutions a confirmed (exact or accepted) match ties to a ROR id; unreviewed, rejected and
        reverted matches are listed as not used."""
        ror = ror_url(ror)
        matches = self.matches(namespace, scopes={"operator"}, ror_id=ror) if self._ready() else []
        used = [m for m in matches if m["state"] in CONFIRMED]
        return {
            "ror_id": ror,
            "subjects": [m["subject"] for m in used],
            "basis": [{k: m[k] for k in ("match_id", "subject", "basis", "state", "exact")} for m in used],
            "not_used": [{k: m[k] for k in ("match_id", "subject", "basis", "state")} for m in matches
                         if m["state"] not in CONFIRMED],
            "ror_record": self.ror_record(namespace, ror),
            "ror_changes": self.ror_changes(namespace, ror),
        }

    def ror_for(self, namespace: str, scheme: str, code: str) -> dict[str, Any]:
        """The confirmed ROR id of a source institution, or why there is none."""
        matches = self._matches_for(namespace, scheme, code)
        confirmed = [m for m in matches if m["state"] in CONFIRMED]
        rors = sorted({m["ror_id"] for m in confirmed})
        if len(rors) == 1:
            return {"ror_id": rors[0], "status": "confirmed",
                    "basis": [{k: m[k] for k in ("match_id", "basis", "state", "exact")} for m in confirmed],
                    "ror_changes": self.ror_changes(namespace, rors[0])}
        if len(rors) > 1:
            return {"ror_id": None, "status": "conflict", "candidates": rors,
                    "note": "several confirmed ROR ids; none is used until a reviewer reverts all but one"}
        pending = [m["ror_id"] for m in matches if m["state"] == "candidate"]
        return {"ror_id": None, "status": "candidates_pending" if pending else "unmatched", "candidates": pending}


__all__ = ["BASES", "CONFIRMED", "EducationIdentity", "ror_url"]
