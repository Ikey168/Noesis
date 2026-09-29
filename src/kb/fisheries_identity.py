"""Reviewable vessel identity across registers, and citation links to other packs (#2222, FI07 #2323, FI08 #2326).

**Identity (FI07).** Every provider record of a vessel - a GFW vessel, an RFMO
register entry, an RFMO or combined IUU list entry - is an identity *subject*
registered as its own ``canonical_entities`` row under an identifier-based id
(no alias rows). Subjects of different lists are connected only by recorded
matches, never merged:

* ``imo`` - both publish the same IMO number (check digit verified). Recorded
  as a match immediately, with the evidence (both revisions) and an entity
  identity decision in :class:`src.kb.entity_history.EntityHistoryStore`; it
  stays reversible;
* ``name-flag`` / ``call-sign`` / ``name-only`` - equal exact names (with the
  same flag), equal call signs, or equal names alone, where no IMO connects
  them. These are **review candidates**: nothing joins until a reviewer
  accepts. Two subjects publishing *different* valid IMO numbers are never
  proposed; the pair is reported as a conflict.

Re-flagging and renaming are read as a time-bounded identity history built from
the dated revisions of every member record (and the previous identities an IUU
list publishes). Flag states resolve to country entities
(``ent-country-<iso2>``) only from ISO codes; owners resolve to existing
``canonical_entities`` only when the published name is an organisation's -
natural persons are never resolved.

**Links (FI08).** :class:`FisheriesLinks` records explicit citations only:

* **sanctions** - a designation that states the same IMO number (read
  read-only from the Legal sanctions store), citing the listing revision; a
  name-only coincidence is reported as a candidate and never linked;
* **areas** - published FAO major area, RFMO convention area, EEZ/region codes
  and published grid cells resolve to geospatial places registered for exactly
  those codes (:meth:`FisheriesLinks.project_areas`); no geometry is guessed
  from free text;
* **other packs through the citing interface** - OSINT vessel movements
  (#2221) and Agriculture & Food Systems records join only by an explicit IMO,
  ASFIS species or FAO area code they cite. When no provider is registered the
  step reports ``provider_unavailable`` and links nothing.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.fisheries_records import (
    IDENTITY_CONTRACT,
    LINK_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    FisheriesError,
    authorize,
    call_sign_key,
    canonical,
    digest,
    flag_code,
    imo_key,
    name_key,
    require,
)
from src.kb.fisheries_store import FisheriesStore, table_exists

BASES = ("imo", "name-flag", "call-sign", "name-only")
STRENGTH = {"imo": 4, "call-sign": 3, "name-flag": 2, "name-only": 1}
SANCTIONS_READ = "knowledge:sanctions:read"
GEOSPATIAL_WRITE = "knowledge:geospatial:write"
_ENTITY_HISTORY_SCOPES = {"knowledge:entity-history:write", "knowledge:entity-history:review",
                          "knowledge:entity-history:execute", "knowledge:entity-history:read"}
_ORG_MARKERS = re.compile(
    r"\b(ltd|limited|llc|inc|corp|corporation|co|company|s\.?\s?a|s\.?\s?l|s\.?\s?a\.?\s?s|gmbh|ag|a/?s|b\.?\s?v|"
    r"n\.?\s?v|pty|plc|sarl|srl|spa|kk|pte|fishing|fisheries|pesquerias|armement|holdings|group|enterprises?|"
    r"trading|shipping|marine|seafoods?)\b\.?", re.IGNORECASE)
_DDL = """
CREATE TABLE IF NOT EXISTS fisheries_identity_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
  left_entity TEXT NOT NULL, right_entity TEXT NOT NULL, basis TEXT NOT NULL, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, decision_id TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  history_json TEXT NOT NULL, PRIMARY KEY(namespace, match_id)
);
CREATE TABLE IF NOT EXISTS fisheries_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, subject_key TEXT NOT NULL, source_revision_id TEXT,
  owner TEXT NOT NULL, target_kind TEXT NOT NULL, target_namespace TEXT NOT NULL, target_id TEXT NOT NULL,
  target_revision TEXT, basis TEXT NOT NULL, matched TEXT NOT NULL, citing_text TEXT NOT NULL,
  locator_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, link_id)
);
"""
# The generic citing interface: other packs register a reader of their records. Absent -> provider_unavailable.
CITING_PROVIDERS: dict[str, Callable[[], Sequence[Mapping[str, Any]]]] = {}
KNOWN_CITING_PROVIDERS = {
    "osint.vessel-movements": "OSINT vessel movements (#2221): observed vessel identities citing IMO numbers",
    "agrifood.food-systems": "Agriculture & Food Systems: supply and trade records citing ASFIS species or FAO areas",
}


def register_citing_provider(name: str, reader: Callable[[], Sequence[Mapping[str, Any]]] | None) -> None:
    """Install (or remove, with ``None``) a pack's record reader for citation links."""
    if reader is None:
        CITING_PROVIDERS.pop(name, None)
    else:
        CITING_PROVIDERS[name] = reader


def family(subject_key: str) -> str:
    """provider and list kind: one list's entries are never matched to each other."""
    return ":".join(subject_key.split(":", 2)[:2])


def entity_id(subject_key: str) -> str:
    return "ent-vessel-" + re.sub(r"[^a-z0-9]+", "-", subject_key.casefold()).strip("-")


def country_entity(code: Any) -> dict[str, Any]:
    """A published flag to a country entity id, from ISO codes only; names and 'Unknown' stay unresolved."""
    iso2 = flag_code(code)
    if iso2 is None:
        return {"published": code, "status": "unresolved",
                "reason": "not an ISO 3166 code; flag names are not guessed"}
    return {"published": code, "status": "resolved", "iso2": iso2, "entity_id": f"ent-country-{iso2.casefold()}"}


def organisation_name(name: Any) -> bool:
    return bool(_ORG_MARKERS.search(str(name or "")))


class FisheriesIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = FisheriesStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, initialize=initialize, now=self.now)
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def _ready(self) -> bool:
        return table_exists(self.conn, "fisheries_identity_matches")

    # ------------------------------------------------------------------ profiles

    def _profiles(self, namespace: str) -> dict[str, dict[str, Any]]:
        profiles = {s["subject_key"]: {**s, "imo": {}, "names": {}, "call_sign": {}, "flag": {}, "pairs": set()}
                    for s in self.store.subjects(namespace, kind="vessel")}
        rows = self.store.identifiers(namespace)
        by_revision: dict[str, dict[str, Any]] = {}
        for row in rows:
            profile = profiles.get(row["subject_key"])
            if profile is None or row["value_key"] is None or row["scheme"] not in {"imo", "name", "call_sign",
                                                                                     "flag"}:
                continue
            profile["names" if row["scheme"] == "name" else row["scheme"]].setdefault(row["value_key"],
                                                                                   row["revision_id"])
            by_revision.setdefault(row["revision_id"], {"subject": row["subject_key"]})[row["scheme"]] = \
                row["value_key"]
        for item in by_revision.values():
            if item.get("name"):
                profiles[item["subject"]]["pairs"].add((item["name"], item.get("flag")))
        return profiles

    def _register(self, profile: Mapping[str, Any]) -> str:
        from src.kb.entities import register_canonical_entity

        return register_canonical_entity(self.conn, entity_id(profile["subject_key"]),
                                         profile.get("name") or profile["subject_key"], "vessel")

    @staticmethod
    def _compare(left: Mapping[str, Any], right: Mapping[str, Any]) -> tuple[str | None, dict[str, Any]]:
        shared_imo = sorted(set(left["imo"]) & set(right["imo"]))
        if shared_imo:
            return "imo", {"imo": shared_imo[0], "left_revision": left["imo"][shared_imo[0]],
                           "right_revision": right["imo"][shared_imo[0]]}
        if left["imo"] and right["imo"]:
            return "conflict", {"left_imo": sorted(left["imo"]), "right_imo": sorted(right["imo"])}
        signs = sorted(set(left["call_sign"]) & set(right["call_sign"]))
        if signs:
            return "call-sign", {"call_sign": signs[0], "left_revision": left["call_sign"][signs[0]],
                                 "right_revision": right["call_sign"][signs[0]]}
        pairs = sorted((n, f) for n, f in left["pairs"] & right["pairs"] if f)
        if pairs:
            return "name-flag", {"name": pairs[0][0], "flag": pairs[0][1]}
        names = sorted(set(left["names"]) & set(right["names"]) - {"not stated"})
        if names:
            return "name-only", {"name": names[0], "left_revision": left["names"][names[0]],
                                 "right_revision": right["names"][names[0]]}
        return None, {}

    # ------------------------------------------------------------------ proposals

    def _offer(self, namespace, left, right, basis, evidence, *, principal_id) -> dict[str, Any]:
        a, b = sorted((left["subject_key"], right["subject_key"]))
        match_id = "fisheries-idm:" + digest([namespace, a, b])[:24]
        row = self.conn.execute("SELECT state, basis FROM fisheries_identity_matches WHERE namespace=? AND "
                                "match_id=?", [namespace, match_id]).fetchone()
        if row is not None and not (row[0] == "proposed" and STRENGTH[basis] > STRENGTH[row[1]]):
            return {"match_id": match_id, "change": None}
        entities = {left["subject_key"]: self._register(left), right["subject_key"]: self._register(right)}
        now = self.now()
        if row is None:
            self.conn.execute(
                "INSERT INTO fisheries_identity_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, match_id, a, b, entities[a], entities[b], basis, canonical(evidence), "proposed", None,
                 principal_id, now, canonical([{"state": "proposed", "by": principal_id, "at_ms": now,
                                                "basis": basis}])])
        else:
            history = json.loads(self.conn.execute(
                "SELECT history_json FROM fisheries_identity_matches WHERE namespace=? AND match_id=?",
                [namespace, match_id]).fetchone()[0])
            history.append({"state": "proposed", "by": principal_id, "at_ms": now, "change": "upgraded",
                            "previous_basis": row[1]})
            self.conn.execute("UPDATE fisheries_identity_matches SET basis=?, evidence_json=?, history_json=? "
                              "WHERE namespace=? AND match_id=?",
                              [basis, canonical(evidence), canonical(history), namespace, match_id])
        return {"match_id": match_id, "change": "created" if row is None else "upgraded"}

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """IMO matches are recorded with evidence; name, flag and call-sign coincidences become review candidates."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        profiles = self._profiles(namespace)
        keys = sorted(profiles)
        offered, conflicts = [], []
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                if family(a) == family(b):
                    continue
                left, right = profiles[a], profiles[b]
                basis, detail = self._compare(left, right)
                if basis is None:
                    continue
                if basis == "conflict":
                    if (left["names"].keys() & right["names"].keys()) or (left["call_sign"].keys()
                                                                        & right["call_sign"].keys()):
                        conflicts.append({"subjects": [a, b], **detail,
                                          "reason": "equal names or call signs but different IMO numbers; never "
                                                    "proposed"})
                    continue
                evidence = {**detail, "method": basis,
                            "left": {"subject_key": a, "names": sorted(left["names"])},
                            "right": {"subject_key": b, "names": sorted(right["names"])},
                            "note": "records stay separate; a match only connects them"}
                result = self._offer(namespace, left, right, basis, evidence, principal_id=principal_id)
                if basis == "imo" and result["change"]:
                    # An IMO match is recorded with its evidence as an identity decision (still reversible).
                    self._decide(namespace, result["match_id"], "accept", "same IMO number (check digit verified)",
                                 principal_id=principal_id)
                offered.append(result)
        return {"recorded_or_proposed": sorted(o["match_id"] for o in offered if o["change"]),
                "conflicts": conflicts, "matches": self.matches(namespace, scopes=scopes)}

    # ------------------------------------------------------------------ reviews

    def _row(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT match_id, left_key, right_key, left_entity, right_entity, basis, evidence_json, state, decision_id, "
            "created_by, created_at_ms, history_json FROM fisheries_identity_matches WHERE namespace=? AND "
            "match_id=?", [namespace, match_id]).fetchone() if self._ready() else None
        if row is None:
            raise FisheriesError("not_found", "identity match is not visible in this namespace")
        history = json.loads(row[11])
        reviewed = [h for h in history if h["state"] in {"accepted", "rejected", "reverted"}]
        return {"contract": IDENTITY_CONTRACT, "namespace": namespace,
                **dict(zip(("match_id", "left_key", "right_key", "left_entity", "right_entity", "basis"), row[:6])),
                "evidence": json.loads(row[6]), "state": row[7], "decision_id": row[8], "created_by": row[9],
                "created_at_ms": row[10], "history": history,
                "review_required": row[5] != "imo",
                "reviewer": reviewed[-1]["by"] if reviewed and row[7] != "proposed" else None,
                "notice": "a match connects records; it never merges them"}

    def matches(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                subject_key: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT match_id FROM fisheries_identity_matches WHERE namespace=? AND (? IS NULL OR state=?) AND "
            "(? IS NULL OR left_key=? OR right_key=?) ORDER BY match_id",
            [namespace, state, state, subject_key, subject_key, subject_key]).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def _decide(self, namespace, match_id, decision, reason, *, principal_id):
        match = self._row(namespace, match_id)
        for side in ("left", "right"):
            self.history.register_entity(namespace, match[f"{side}_entity"], [match[f"{side}_key"]],
                                         principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        recorded = self.history.decide(
            namespace, "match" if decision == "accept" else "non-match", [match["left_entity"], match["right_entity"]],
            {"match_id": match_id, "basis": match["basis"], "evidence": match["evidence"], "reason": reason,
             "provenance": {"producer": "fisheries.core", "records": [match["left_key"], match["right_key"]]},
             "policy": {"merge": False, "note": "identity decision only; register and list records stay separate"}},
            reviewer_id=principal_id, principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"fisheries-identity:{namespace}:{match_id}:{len(match['history'])}")
        return self._transition(namespace, match, "accepted" if decision == "accept" else "rejected",
                                recorded["decision_id"], principal_id, reason)

    def _transition(self, namespace, match, state, decision_id, principal_id, reason):
        history = match["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now(),
                                        "decision_id": decision_id}]
        self.conn.execute("UPDATE fisheries_identity_matches SET state=?, decision_id=?, history_json=? "
                          "WHERE namespace=? AND match_id=?",
                          [state, decision_id, canonical(history), namespace, match["match_id"]])
        return self._row(namespace, match["match_id"])

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise FisheriesError("invalid_decision", "accept or reject with a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in {"proposed", "reverted"}:
            raise FisheriesError("invalid_state", f"match is {match['state']}; revert it before re-reviewing")
        return self._decide(namespace, match_id, decision, reason.strip(), principal_id=principal_id)

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise FisheriesError("invalid_decision", "a revert needs a reason")
        match = self._row(namespace, match_id)
        if match["state"] not in {"accepted", "rejected"}:
            raise FisheriesError("invalid_state", "only an accepted or rejected match can be reverted")
        undo = self.history.undo(namespace, match["decision_id"], reviewer_id=principal_id,
                                 principal_id=principal_id, scopes=_ENTITY_HISTORY_SCOPES)
        return self._transition(namespace, match, "reverted", undo["decision_id"], principal_id, reason.strip())

    # ------------------------------------------------------------------ resolution

    def clusters(self, namespace: str) -> dict[str, str]:
        parent: dict[str, str] = {}

        def find(key: str) -> str:
            parent.setdefault(key, key)
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        for subject in self.store.subjects(namespace, kind="vessel"):
            find(subject["subject_key"])
        if self._ready():
            for left, right in self.conn.execute("SELECT left_key, right_key FROM fisheries_identity_matches WHERE "
                                                 "namespace=? AND state='accepted'", [namespace]).fetchall():
                a, b = find(left), find(right)
                if a != b:
                    parent[max(a, b)] = min(a, b)
        return {key: find(key) for key in list(parent)}

    def members(self, namespace: str, subject_key: str) -> list[str]:
        clusters = self.clusters(namespace)
        root = clusters.get(subject_key, subject_key)
        return sorted(k for k, v in clusters.items() if v == root) or [subject_key]

    def find(self, namespace: str, query: str) -> tuple[str, list[str]]:
        """(interpreted_as, subject keys) for an IMO, GFW vessel id, register number, list entry, call sign or key."""
        text = str(query or "").strip()
        if not text:
            raise FisheriesError("invalid_request", "give an IMO number, a register number, a GFW vessel id or a "
                                                    "subject key")
        known = {s["subject_key"] for s in self.store.subjects(namespace, kind="vessel")}
        if text in known:
            return "subject_key", [text]
        if imo_key(text):
            return "imo", self.store.find_subjects(namespace, "imo", imo_key(text))
        for scheme in ("gfw_vessel_id", "register_number", "list_entry"):
            found = self.store.find_subjects(namespace, scheme, text)
            if found:
                return scheme, found
        found = self.store.find_subjects(namespace, "call_sign", call_sign_key(text) or "")
        if found:
            return "call_sign", found
        return "name", self.store.find_subjects(namespace, "name", name_key(text))

    def identity_history(self, namespace: str, members: Sequence[str], *, as_of: str | None = None,
                         cutoff_seq: int | None = None) -> dict[str, Any]:
        """Time-bounded names, flags and call signs across the member records; each point cites its revision."""
        points, previous = [], []
        for record in self.store.records(namespace, subject_keys=members):
            if record["record_type"] not in {"vessel", "authorisation", "listing"}:
                continue
            for revision in self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff_seq):
                published = revision["statement"]["as_published"]
                when = (published.get("transmission_from") or published.get("valid_from")
                        or revision["snapshot_date"] or revision["effective_from"])
                if as_of and when and when > as_of:
                    continue
                points.append({"date": when, "until": published.get("transmission_to") or published.get("valid_to"),
                               "name": published.get("vessel_name") or published.get("shipname"),
                               "flag": published.get("flag"),
                               "call_sign": published.get("call_sign") or published.get("callsign"),
                               "imo": published.get("imo"), "subject_key": record["subject_key"],
                               "record_type": record["record_type"], "revision_id": revision["revision_id"],
                               "date_basis": "transmission period" if published.get("transmission_from")
                               else "authorisation period" if published.get("valid_from")
                               else "snapshot date" if revision["snapshot_date"] else "effective date"})
                ids = published.get("previous_identities") or {}
                if ids.get("names") or ids.get("flags"):
                    previous.append({"names": ids.get("names") or [], "flags": ids.get("flags") or [],
                                     "subject_key": record["subject_key"], "revision_id": revision["revision_id"],
                                     "note": "previous identities as published by the list (undated)"})
        points.sort(key=lambda p: (p["date"] or "0000", p["subject_key"]))
        segments: list[dict[str, Any]] = []
        for point in points:
            key = (name_key(point["name"]), flag_code(point["flag"]) or point["flag"])
            if segments and segments[-1]["key"] == key:
                segments[-1]["to"] = point["until"] or point["date"]
                segments[-1]["citations"].append({k: point[k] for k in ("subject_key", "revision_id", "date",
                                                                       "date_basis")})
                continue
            change = []
            if segments:
                if segments[-1]["key"][0] != key[0]:
                    change.append("renamed")
                if segments[-1]["key"][1] != key[1]:
                    change.append("re-flagged")
            segments.append({"key": key, "name": point["name"], "flag": point["flag"],
                             "flag_entity": country_entity(point["flag"]), "call_sign": point["call_sign"],
                             "from": point["date"], "to": point["until"] or point["date"], "change": change,
                             "citations": [{k: point[k] for k in ("subject_key", "revision_id", "date",
                                                                  "date_basis")}]})
        for segment in segments:
            segment.pop("key")
        return {"segments": segments, "previous_identities": previous,
                "notice": "an identity history reads dated revisions; records are never merged or rewritten"}

    def resolve_parties(self, namespace: str, members: Sequence[str]) -> dict[str, Any]:
        """Flag states to country entities (ISO codes only) and organisation owners to canonical entities."""
        from src.kb.entities import register_canonical_entity

        flags, owners = {}, {}
        for record in self.store.records(namespace, subject_keys=members):
            revisions = self.store.revisions(namespace, record["record_id"])
            if not revisions:
                continue
            published = revisions[-1]["statement"]["as_published"]
            if published.get("flag") and published["flag"] not in flags:
                resolved = country_entity(published["flag"])
                if resolved["status"] == "resolved":
                    register_canonical_entity(self.conn, resolved["entity_id"], resolved["iso2"], "country")
                flags[published["flag"]] = resolved
            owner = published.get("owner_or_operator")
            if owner and owner not in owners:
                owners[owner] = self._owner(owner, revisions[-1]["revision_id"])
        return {"flags": list(flags.values()), "owners": list(owners.values())}

    def _owner(self, name: str, revision_id: str) -> dict[str, Any]:
        if not organisation_name(name):
            return {"published": name, "status": "not_resolved", "revision_id": revision_id,
                    "reason": "the name carries no organisation form and may be a natural person; natural persons "
                              "are never resolved"}
        found = None
        if table_exists(self.conn, "entity_aliases"):
            from src.kb.entities import resolve

            found = resolve(self.conn, name)
        if found is None:
            return {"published": name, "status": "no_canonical_entity", "revision_id": revision_id,
                    "reason": "no canonical entity has this exact name; none is created"}
        return {"published": name, "status": "candidate", "entity_id": found["canonical_id"],
                "revision_id": revision_id, "basis": "exact normalised name of an organisation",
                "note": "a candidate to review; not an ownership claim"}

    def vessel(self, namespace: str, subject_key: str, *, scopes: Iterable[str],
               as_of: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        members = self.members(namespace, subject_key)
        matches = [m for m in self.matches(namespace, scopes=scopes)
                   if m["left_key"] in members and m["right_key"] in members and m["state"] == "accepted"]
        pending = [m for m in self.matches(namespace, scopes=scopes, state="proposed")
                   if m["left_key"] in members or m["right_key"] in members]
        return {"vessel_id": entity_id(min(members)), "members": members,
                "matches": [{"match_id": m["match_id"], "records": [m["left_key"], m["right_key"]],
                             "basis": m["basis"], "evidence": m["evidence"], "decision_id": m["decision_id"],
                             "reviewer": m["reviewer"]} for m in matches],
                "pending_candidates": [{"match_id": m["match_id"], "records": [m["left_key"], m["right_key"]],
                                        "basis": m["basis"]} for m in pending],
                "history": self.identity_history(namespace, members, as_of=as_of),
                "parties": self.resolve_parties(namespace, members)}


class FisheriesLinks:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = FisheriesStore(conn, initialize=initialize, now=self.now)
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    def _insert(self, namespace, subject_key, source_revision_id, owner, target_kind, target_namespace, target_id,
                target_revision, basis, matched, citing_text, locator, principal_id) -> dict[str, Any] | None:
        link_id = "fisheries-link:" + digest([namespace, subject_key, source_revision_id, owner, target_id,
                                              target_revision, basis, matched])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO fisheries_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING link_id",
            [namespace, link_id, subject_key, source_revision_id, owner, target_kind, target_namespace, target_id,
             target_revision, basis, matched, str(citing_text), canonical(locator), principal_id,
             self.now()]).fetchall()
        return {"link_id": link_id, "subject_key": subject_key, "owner": owner, "target_id": target_id,
                "basis": basis, "matched": matched} if inserted else None

    def _latest(self, namespace: str, record: Mapping[str, Any]) -> dict[str, Any]:
        return self.store.revisions(namespace, record["record_id"])[-1]

    # ------------------------------------------------------------------ sanctions

    def link_sanctions(self, namespace: str, *, scopes: Iterable[str], principal_id: str,
                       sanctions_namespace: str | None = None) -> dict[str, Any]:
        """A sanctions designation that states the vessel's IMO number; name coincidences are candidates only."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require(scopes, SANCTIONS_READ)
        self.store.require_ready()
        target_ns = sanctions_namespace or namespace
        if not table_exists(self.conn, "sanctions_aliases"):
            return {"namespace": namespace, "status": "provider_unavailable", "linked": [], "name_only": [],
                    "note": "the Legal sanctions store is not present in this deployment"}
        created, name_only = [], []
        vessels: dict[str, dict[str, Any]] = {}
        for row in self.store.identifiers(namespace):
            if row["scheme"] in {"imo", "name"} and row["value_key"]:
                vessels.setdefault(row["subject_key"], {"imo": {}, "name": {}})[row["scheme"]][row["value_key"]] = \
                    row["revision_id"]
        for subject, ids in sorted(vessels.items()):
            imo_hits = set()
            for imo, revision_id in sorted(ids["imo"].items()):
                rows = self.conn.execute(
                    "SELECT a.designation_id, a.revision_id, a.list_id, a.value FROM sanctions_aliases a WHERE "
                    "a.namespace=? AND a.alias_kind='imo' AND a.normalized IN (?, ?) ORDER BY a.designation_id, "
                    "a.revision_id", [target_ns, imo, f"IMO{imo}"]).fetchall()
                for designation_id, sanctions_revision, list_id, value in rows:
                    imo_hits.add(designation_id)
                    link = self._insert(namespace, subject, revision_id, "sanctions", "sanctions-designation",
                                        target_ns, designation_id, sanctions_revision, "stated-imo", f"imo:{imo}",
                                        f"{list_id} designation states IMO {value}",
                                        {"list_id": list_id, "alias_kind": "imo", "stated": value}, principal_id)
                    created += [link] if link else []
            for name, revision_id in sorted(ids["name"].items()):
                rows = self.conn.execute(
                    "SELECT DISTINCT designation_id, list_id FROM sanctions_aliases WHERE namespace=? AND "
                    "alias_kind IN ('name','transliteration') AND normalized=?",
                    [target_ns, re.sub(r"[^A-Z0-9]", "", name.upper())]).fetchall()
                for designation_id, list_id in rows:
                    if designation_id not in imo_hits:
                        name_only.append({"subject_key": subject, "designation_id": designation_id,
                                          "list_id": list_id, "name": name, "revision_id": revision_id,
                                          "status": "candidate only: a shared name is never a link"})
        return {"namespace": namespace, "status": "linked", "linked": created, "name_only": name_only,
                "policy": "an explicit IMO number stated by the designation; never a name alone"}

    # ------------------------------------------------------------------ areas

    def _area_codes(self, namespace: str) -> list[dict[str, Any]]:
        found: dict[tuple, dict[str, Any]] = {}
        for record in self.store.records(namespace):
            if record["record_type"] not in {"effort_aggregate", "catch_observation", "authorisation"}:
                continue
            revision = self._latest(namespace, record)
            published = revision["statement"]["as_published"]
            area = published.get("area") or published.get("convention_area")
            if area and area.get("code"):
                found.setdefault(("area", area["scheme"], area["code"]), {
                    "kind": "area", "scheme": area["scheme"], "code": area["code"], "name": area.get("name"),
                    "records": []})["records"].append((record["subject_key"], revision["revision_id"]))
            grid = published.get("grid")
            if grid and grid.get("lat") is not None:
                code = f"{grid['resolution']}:{grid['lat']},{grid['lon']}"
                found.setdefault(("grid", "gfw-grid", code), {
                    "kind": "grid", "scheme": "gfw-grid", "code": code, "grid": grid, "records": []})[
                    "records"].append((record["subject_key"], revision["revision_id"]))
        return [found[k] for k in sorted(found)]

    @staticmethod
    def place_key(scheme: str, code: str) -> str:
        return f"fisheries-area:{scheme}:{code}"

    def project_areas(self, namespace: str, *, scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Register each published area code and grid cell as a geospatial place keyed by exactly that code."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require(scopes, GEOSPATIAL_WRITE)
        geo = GeospatialStore(self.conn, now=self.now)
        geo_scopes = scopes | {GEOSPATIAL_WRITE} if "operator" in scopes else scopes
        places = []
        for area in self._area_codes(namespace):
            geometry = None
            if area["kind"] == "grid":
                grid, half = area["grid"], area["grid"]["resolution_deg"] / 2
                lat, lon = float(grid["lat"]), float(grid["lon"])
                geometry = {"type": "Polygon", "coordinates": [[[lon - half, lat - half], [lon + half, lat - half],
                                                                [lon + half, lat + half], [lon - half, lat + half],
                                                                [lon - half, lat - half]]]}
            name = area.get("name") or f"{area['scheme']} {area['code']}"
            result = geo.register_place(
                namespace, name, {"fao-major-area": "fao-major-fishing-area", "rfmo-convention-area":
                                  "rfmo-convention-area", "eez": "eez", "gfw-grid": "grid-cell"}.get(
                    area["scheme"], "fishing-area"),
                names=[{"value": name, "language": "und", "kind": "canonical"}],
                source_ids={area["scheme"]: area["code"]}, parent_ids=[], principal_id=principal_id,
                scopes=geo_scopes, place_key=self.place_key(area["scheme"], area["code"]), geometry=geometry,
                observed_at_ms=0, producer={"name": "fisheries.core", "version": "1.0.0"},
                policy={"geometry": "published grid cell bounds only; area codes carry no guessed geometry"},
                provenance={"published_code": area["code"], "scheme": area["scheme"]})
            places.append({"scheme": area["scheme"], "code": area["code"], "place_id": result["place_id"]})
        return {"namespace": namespace, "places": places}

    def link_areas(self, namespace: str, *, scopes: Iterable[str], principal_id: str) -> dict[str, Any]:
        """Link records to the places registered for their published codes (read-only on geospatial)."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        if not table_exists(self.conn, "geospatial_places"):
            return {"namespace": namespace, "status": "provider_unavailable", "linked": [], "unresolved": []}
        created, unresolved = [], []
        for area in self._area_codes(namespace):
            row = self.conn.execute("SELECT p.place_id, c.revision_id FROM geospatial_places p JOIN "
                                    "geospatial_place_current c USING(place_id) WHERE p.namespace=? AND "
                                    "p.place_key=?", [namespace, self.place_key(area["scheme"],
                                                                                 area["code"])]).fetchone()
            if row is None:
                unresolved.append({"scheme": area["scheme"], "code": area["code"],
                                   "reason": "no place is registered for this published code; run project_areas"})
                continue
            for subject_key, revision_id in area["records"]:
                link = self._insert(namespace, subject_key, revision_id, "geospatial", "geospatial-place", namespace,
                                    row[0], row[1], "published-grid-cell" if area["kind"] == "grid"
                                    else "published-area-code", f"{area['scheme']}:{area['code']}",
                                    f"{area['scheme']} {area['code']} as published",
                                    {"scheme": area["scheme"], "code": area["code"]}, principal_id)
                created += [link] if link else []
        return {"namespace": namespace, "status": "linked", "linked": created, "unresolved": unresolved,
                "policy": "published area codes and grid cells only; no geometry guessed from free text"}

    # ------------------------------------------------------------------ citing interface

    def link_cited(self, namespace: str, provider: str, *, scopes: Iterable[str], principal_id: str,
                   reader: Callable[[], Sequence[Mapping[str, Any]]] | None = None) -> dict[str, Any]:
        """Records of another pack that cite an IMO, ASFIS species or FAO area code; unavailable when absent."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        reader = reader or CITING_PROVIDERS.get(provider)
        if reader is None:
            return {"namespace": namespace, "provider": provider, "status": "provider_unavailable", "linked": [],
                    "records_read": 0, "note": KNOWN_CITING_PROVIDERS.get(provider, "no such provider is composed")}
        self.store.require_ready()
        by_imo: dict[str, list[tuple[str, str]]] = {}
        for row in self.store.identifiers(namespace):
            if row["scheme"] == "imo" and row["value_key"]:
                by_imo.setdefault(row["value_key"], []).append((row["subject_key"], row["revision_id"]))
        by_area: dict[str, set[str]] = {}
        by_species: dict[str, set[str]] = {}
        for record in self.store.records(namespace, record_type="catch_observation"):
            published = self._latest(namespace, record)["statement"]["as_published"]
            by_area.setdefault(published["area"]["code"], set()).add(record["subject_key"])
            by_species.setdefault(published["species"], set()).add(record["subject_key"])
        records = list(reader())
        created = []
        for item in records:
            identifiers = dict(item.get("identifiers") or {})
            target = (str(item.get("record_id")), item.get("revision_id"), str(item.get("kind") or provider))
            text = str(item.get("title") or item.get("text") or target[0])
            for value in identifiers.get("imo") or []:
                for subject, revision_id in sorted(set(by_imo.get(imo_key(value) or "", []))):
                    link = self._insert(namespace, subject, revision_id, provider, target[2],
                                        str(item.get("namespace") or namespace), target[0], target[1], "cited-imo",
                                        f"imo:{imo_key(value)}", text, {"field": "identifiers.imo"}, principal_id)
                    created += [link] if link else []
            for field, index, basis in (("fao_area", by_area, "cited-fao-area"),
                                        ("asfis", by_species, "cited-asfis-species")):
                for value in identifiers.get(field) or []:
                    for subject in sorted(index.get(str(value).strip().upper() if field == "asfis"
                                                    else str(value).strip(), set())):
                        link = self._insert(namespace, subject, None, provider, target[2],
                                            str(item.get("namespace") or namespace), target[0], target[1], basis,
                                            f"{field}:{value}", text, {"field": f"identifiers.{field}"},
                                            principal_id)
                        created += [link] if link else []
        return {"namespace": namespace, "provider": provider, "status": "linked", "linked": created,
                "records_read": len(records),
                "policy": "an explicit IMO, ASFIS species or FAO area code the record cites; never a name"}

    # ------------------------------------------------------------------ reads

    def links(self, namespace: str, subject_keys: Iterable[str], *, scopes: Iterable[str],
              owner: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        keys = sorted(set(subject_keys))
        if not keys or not table_exists(self.conn, "fisheries_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, subject_key, source_revision_id, owner, target_kind, target_namespace, target_id, "
            "target_revision, basis, matched, citing_text, locator_json, created_by, created_at_ms FROM fisheries_links "
            "WHERE namespace=? AND (? IS NULL OR owner=?) AND subject_key IN (" + ",".join("?" * len(keys)) + ") "
            "ORDER BY owner, target_id, link_id", [namespace, owner, owner, *keys]).fetchall()
        return [{"contract": LINK_CONTRACT, **dict(zip(
            ("link_id", "subject_key", "source_revision_id", "owner", "target_kind", "target_namespace", "target_id",
             "target_revision", "basis", "matched", "citing_text"), r[:11])), "locator": json.loads(r[11]),
            "created_by": r[12], "created_at_ms": int(r[13])} for r in rows]

    def generation(self, namespace: str) -> int:
        if not table_exists(self.conn, "fisheries_links"):
            return 0
        return int(self.conn.execute("SELECT count(*) FROM fisheries_links WHERE namespace=?",
                                     [namespace]).fetchone()[0])


__all__ = ["BASES", "CITING_PROVIDERS", "FisheriesIdentity", "FisheriesLinks", "KNOWN_CITING_PROVIDERS",
           "country_entity", "entity_id", "family", "organisation_name", "register_citing_provider"]
