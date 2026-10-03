"""Countries and NUTS regions across NUTS versions through reviewable identity (#2739, TO05).

**Place keys.** Every area a tourism series states is a place key ``(eurostat-geo, NUTS version, code)``: the same
code under NUTS 2021 and NUTS 2024 is two keys, never one. Each key is offered to the Geospatial places the platform
already holds (:class:`src.kb.geospatial.GeospatialStore`; no new place store), matching by the published code only,
code before name:

* ``published-code-and-version`` - a place whose source identifiers carry the code under the version's key
  (``nuts-2021``: ``DE30``);
* ``published-code`` - a place whose source identifiers carry the code under ``nuts`` (or ``eurostat-geo``) and state no
  other NUTS version (``nuts-version``);
* ``iso-alpha2-equivalent`` - a two-letter NUTS 0 code read as ISO 3166-1 alpha-2 (``EL`` is ``GR``).

A key whose candidates name more than one place is ``ambiguous`` (a reviewer chooses one of the cited candidates); a
key with none stays ``unmatched`` and visible. Names are never a match.

**NUTS versions.** A key under one NUTS version reaches a key under another only through Eurostat's published NUTS
correspondence, imported by an operator with its citation (URL, publisher, publication date and file digest). Each
row becomes a reviewable *correspondence link* (relation as published: ``unchanged``, ``recoded``, ``split``,
``merged`` or ``boundary_change``); series keep their own key and nothing is merged or re-coded.

Every assertion is ``proposed`` (or ``ambiguous`` / ``unmatched``), then ``accepted`` or ``rejected`` by a reviewer -
reviewer, reason and time are recorded - and can be ``reverted``; nothing is auto-merged and queries use accepted
assertions only. The assertion history follows :mod:`src.kb.entity_history`'s proposed / reviewed / reverted states.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.tourism_sources import NUTS_VERSIONS
from src.kb.tourism_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    TourismError,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)
from src.kb.tourism_store import TourismStore

CONTRACT = "noesis-tourism-identity-v1"
GEO_READ = "knowledge:geospatial:read"
KINDS = ("area", "nuts_correspondence")
CORRESPONDENCE_RELATIONS = ("unchanged", "recoded", "split", "merged", "boundary_change")
STATES = ("proposed", "ambiguous", "unmatched", "accepted", "rejected", "reverted")
EUROSTAT_ISO2 = {"EL": "GR", "UK": "GB"}
_DDL = """
CREATE TABLE IF NOT EXISTS tourism_identity_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, kind TEXT NOT NULL, subject_key TEXT NOT NULL,
  subject_json TEXT NOT NULL, target_json TEXT, method TEXT, relation TEXT, evidence_json TEXT NOT NULL,
  state TEXT NOT NULL, reason TEXT, history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id)
);
CREATE TABLE IF NOT EXISTS tourism_nuts_correspondences (
  namespace TEXT NOT NULL, correspondence_id TEXT NOT NULL, label TEXT NOT NULL, source_version TEXT NOT NULL,
  target_version TEXT NOT NULL, citation_json TEXT NOT NULL, rows_json TEXT NOT NULL, content_hash TEXT NOT NULL,
  recorded_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, correspondence_id)
);
"""


def area_key(value: Mapping[str, Any]) -> dict[str, str]:
    """The place key of an area: scheme, NUTS version and code (the version is part of the key)."""
    value = dict(value or {})
    if value.get("scheme") != "eurostat-geo" or not value.get("code") or str(value.get("nuts_version")) not in \
            NUTS_VERSIONS:
        raise TourismError("invalid_request", "an area states scheme eurostat-geo, its NUTS version and its code")
    return {"scheme": "eurostat-geo", "nuts_version": str(value["nuts_version"]), "code": str(value["code"])}


class TourismIdentity:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.store = TourismStore(conn, initialize=initialize, now=now)
        self.now = self.store.now
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "tourism_identity_assertions")

    # ------------------------------------------------------------------ subjects

    def stated_areas(self, namespace: str) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for series in self.store.find_series(namespace):
            key = area_key(series["area"])
            entry = found.setdefault(canonical(key), {**key, "labels": set(), "series": 0})
            entry["series"] += 1
            if series["area"].get("label"):
                entry["labels"].add(series["area"]["label"])
        return [{**v, "labels": sorted(v["labels"])} for _, v in sorted(found.items())]

    def _places(self, geo_namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, r.revision_id, r.canonical_name, r.place_type, r.source_ids_json FROM "
            "geospatial_places p JOIN geospatial_place_current c ON c.place_id=p.place_id JOIN "
            "geospatial_place_revisions r ON r.revision_id=c.revision_id WHERE p.namespace IN (?, 'global') "
            "ORDER BY p.place_id", [geo_namespace]).fetchall()
        return [{"place_id": r[0], "revision_id": r[1], "name": r[2], "place_type": r[3],
                 "source_ids": {str(k): str(v) for k, v in json.loads(r[4] or "{}").items()}} for r in rows]

    @staticmethod
    def _candidates(key: Mapping[str, str], places: list[dict[str, Any]]) -> list[dict[str, Any]]:
        version, code = key["nuts_version"], key["code"]
        out = []
        for place in places:
            ids = place["source_ids"]
            stated_version = ids.get("nuts-version")
            match = None
            if ids.get(f"nuts-{version}") == code:
                match = ("published-code-and-version", f"nuts-{version}", code,
                         f"the place carries the code under NUTS {version}")
            elif any(ids.get(k) == code for k in ("nuts", "eurostat-geo")) and stated_version in (None, version):
                found = "nuts" if ids.get("nuts") == code else "eurostat-geo"
                match = ("published-code", found, code,
                         "the place carries the published code" + (f" and states NUTS {version}" if stated_version
                                                                   else "; it states no NUTS version"))
            elif len(code) == 2 and ids.get("iso3166-1-alpha2") == EUROSTAT_ISO2.get(code, code):
                match = ("iso-alpha2-equivalent", "iso3166-1-alpha2", EUROSTAT_ISO2.get(code, code),
                         "NUTS 0 country code read as ISO 3166-1 alpha-2 (EL=GR, UK=GB)")
            if match is None:
                continue
            method, id_key, value, rule = match
            out.append({"place_id": place["place_id"], "place_revision_id": place["revision_id"],
                        "place_name": place["name"], "place_type": place["place_type"], "method": method,
                        "evidence": {"source_id_key": id_key, "value": value, "rule": rule}})
        return out

    def _latest(self, namespace: str, kind: str, subject_key: str) -> dict[str, Any] | None:
        if not self.ready():
            return None
        row = self.conn.execute(
            "SELECT assertion_id FROM tourism_identity_assertions WHERE namespace=? AND kind=? AND subject_key=? "
            "ORDER BY created_at_ms DESC, assertion_id DESC LIMIT 1", [namespace, kind, subject_key]).fetchone()
        return None if row is None else self.assertion(namespace, row[0], scopes={"operator"})

    def _record(self, namespace, kind, subject, target, method, relation, evidence, state, reason, principal_id):
        subject_key = canonical(subject)
        latest = self._latest(namespace, kind, subject_key)
        if latest is not None and latest["target"] == target and latest["method"] == method and \
                latest["state"] in {state, "reverted"}:
            return latest["assertion_id"], False
        number = 1 + int(self.conn.execute(
            "SELECT count(*) FROM tourism_identity_assertions WHERE namespace=? AND kind=? AND subject_key=?",
            [namespace, kind, subject_key]).fetchone()[0])
        assertion_id = "to-identity:" + digest([namespace, kind, subject, number, target, method, state])[:24]
        now = self.now()
        self.conn.execute(
            "INSERT INTO tourism_identity_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, assertion_id, kind, subject_key, canonical(subject),
             None if target is None else canonical(target), method, relation, canonical(evidence), state, reason,
             canonical([{"state": state, "by": principal_id, "at_ms": now, "reason": reason}]), principal_id, now])
        return assertion_id, True

    # ------------------------------------------------------------------ places

    def propose_places(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                       geo_namespace: str = "global") -> dict[str, Any]:
        """Offer every stated place key to Geospatial places by the published code; idempotent; nothing merged."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_READ)
        places = self._places(geo_namespace)
        created, out = [], []
        for area in self.stated_areas(namespace):
            subject = area_key(area)
            latest = self._latest(namespace, "area", canonical(subject))
            if latest and latest["state"] in {"accepted", "rejected"}:
                out.append(latest["assertion_id"])
                continue
            candidates = self._candidates(subject, places)
            evidence = {"labels": area["labels"], "candidates": candidates,
                        "note": "names are context only; matching rests on published codes"}
            distinct = {c["place_id"] for c in candidates}
            if len(distinct) == 1:
                chosen = candidates[0]
                target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
                method, state, reason = chosen["method"], "proposed", None
            elif distinct:
                target, method, state = None, None, "ambiguous"
                reason = "more than one place carries this code; a reviewer chooses one of the cited candidates"
            else:
                target, method, state, reason = None, None, "unmatched", "no place carries this code and version"
            assertion_id, new = self._record(namespace, "area", subject, target, method,
                                             "exact" if target else None, evidence, state, reason, principal_id)
            if new:
                created.append(assertion_id)
            out.append(assertion_id)
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out]}

    # ------------------------------------------------------------------ NUTS correspondence

    def import_correspondence(self, namespace: str, table: Mapping[str, Any], *, principal_id: str,
                              scopes: Iterable[str]) -> dict[str, Any]:
        """Record Eurostat's published NUTS correspondence with its citation; rows keep their relation as published."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        table = dict(table)
        citation = dict(table.get("citation") or {})
        if not all(citation.get(k) for k in ("url", "publisher", "published_on", "file_sha256")):
            raise TourismError("invalid_table", "a correspondence cites its URL, publisher, publication date and "
                                                "digest")
        source, target = str(table.get("source_version")), str(table.get("target_version"))
        if source not in NUTS_VERSIONS or target not in NUTS_VERSIONS or source == target:
            raise TourismError("invalid_table", f"a correspondence links two NUTS versions of {NUTS_VERSIONS}")
        rows = []
        for row in table.get("rows") or []:
            row = dict(row)
            if not row.get("source_code") or not row.get("target_code") or \
                    row.get("relation") not in CORRESPONDENCE_RELATIONS:
                raise TourismError("invalid_table", f"each row names both codes and a relation in "
                                                    f"{CORRESPONDENCE_RELATIONS}")
            rows.append({k: str(row[k]) for k in ("source_code", "target_code", "relation")})
        if not rows or not str(table.get("label") or "").strip():
            raise TourismError("invalid_table", "a correspondence has a label and at least one row")
        rows.sort(key=lambda r: (r["source_code"], r["target_code"]))
        content_hash = digest([source, target, rows, citation])
        correspondence_id = "to-nuts:" + content_hash[:24]
        if not self.conn.execute("SELECT 1 FROM tourism_nuts_correspondences WHERE namespace=? AND "
                                 "correspondence_id=?", [namespace, correspondence_id]).fetchone():
            self.conn.execute("INSERT INTO tourism_nuts_correspondences VALUES (?,?,?,?,?,?,?,?,?,?)",
                              [namespace, correspondence_id, str(table["label"]).strip(), source, target,
                               canonical(citation), canonical(rows), content_hash, principal_id, self.now()])
        return next(c for c in self.correspondences(namespace) if c["correspondence_id"] == correspondence_id)

    def correspondences(self, namespace: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "tourism_nuts_correspondences"):
            return []
        return [{"correspondence_id": r[0], "label": r[1], "source_version": r[2], "target_version": r[3],
                 "citation": json.loads(r[4]), "rows": json.loads(r[5]), "recorded_by": r[6]}
                for r in self.conn.execute(
                    "SELECT correspondence_id, label, source_version, target_version, citation_json, rows_json, "
                    "recorded_by FROM tourism_nuts_correspondences WHERE namespace=? ORDER BY label, "
                    "correspondence_id", [namespace]).fetchall()]

    def propose_correspondence_links(self, namespace: str, *, principal_id: str,
                                     scopes: Iterable[str]) -> dict[str, Any]:
        """Candidate links between a stated place key and its counterpart under another NUTS version, one per
        published correspondence row; reviewable, never a merge."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        stated = {canonical(area_key(a)) for a in self.stated_areas(namespace)}
        created, out, linked = [], [], set()
        for table in self.correspondences(namespace):
            for row in table["rows"]:
                left = {"scheme": "eurostat-geo", "nuts_version": table["source_version"], "code": row["source_code"]}
                right = {"scheme": "eurostat-geo", "nuts_version": table["target_version"],
                         "code": row["target_code"]}
                if canonical(left) not in stated and canonical(right) not in stated:
                    continue
                subject = {"pair": [left, right]}
                linked |= {canonical(left), canonical(right)}
                latest = self._latest(namespace, "nuts_correspondence", canonical(subject))
                if latest and latest["state"] in {"accepted", "rejected"}:
                    out.append(latest["assertion_id"])
                    continue
                evidence = {"correspondence_id": table["correspondence_id"], "label": table["label"],
                            "citation": table["citation"], "row": row,
                            "stated": {"from": canonical(left) in stated, "to": canonical(right) in stated}}
                assertion_id, new = self._record(namespace, "nuts_correspondence", subject,
                                                 {"pair": subject["pair"], "merge": False},
                                                 "published-nuts-correspondence", row["relation"], evidence,
                                                 "proposed", None, principal_id)
                if new:
                    created.append(assertion_id)
                out.append(assertion_id)
        unlinked = [a for a in self.stated_areas(namespace) if canonical(area_key(a)) not in linked]
        return {"created": created, "assertions": [self.assertion(namespace, a, scopes={"operator"}) for a in out],
                "unlinked_place_keys": [area_key(a) for a in unlinked],
                "note": "candidate links only; every series keeps its own NUTS version and code, nothing is merged"}

    # ------------------------------------------------------------------ review

    def assertion(self, namespace: str, assertion_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT assertion_id, kind, subject_json, target_json, method, relation, evidence_json, state, reason, "
            "history_json, created_by, created_at_ms FROM tourism_identity_assertions WHERE namespace=? AND "
            "assertion_id=?", [namespace, assertion_id]).fetchone()
        if row is None:
            raise TourismError("not_found", "identity assertion is not visible in this namespace")
        return {"contract": CONTRACT, "namespace": namespace, "assertion_id": row[0], "kind": row[1],
                "subject": json.loads(row[2]), "target": None if row[3] is None else json.loads(row[3]),
                "method": row[4], "relation": row[5], "evidence": json.loads(row[6]), "state": row[7],
                "reason": row[8], "history": json.loads(row[9]), "created_by": row[10], "created_at_ms": row[11]}

    def assertions(self, namespace: str, *, scopes: Iterable[str], kind: str | None = None,
                   state: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT assertion_id FROM tourism_identity_assertions WHERE namespace=? AND (? IS NULL OR kind=?) "
            "ORDER BY kind, subject_key, created_at_ms, assertion_id", [namespace, kind, kind]).fetchall()
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for (assertion_id,) in rows:
            item = self.assertion(namespace, assertion_id, scopes={"operator"})
            latest[(item["kind"], canonical(item["subject"]))] = item
        return [a for a in latest.values() if state is None or a["state"] == state]

    def _transition(self, namespace, item, state, principal_id, reason, target=None):
        history = item["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}]
        if target is not None:
            self.conn.execute(
                "UPDATE tourism_identity_assertions SET state=?, history_json=?, target_json=?, method=?, "
                "relation=? WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), canonical(target["target"]), target["method"], "exact", namespace,
                 item["assertion_id"]])
        else:
            self.conn.execute(
                "UPDATE tourism_identity_assertions SET state=?, history_json=? WHERE namespace=? AND assertion_id=?",
                [state, canonical(history), namespace, item["assertion_id"]])
        return self.assertion(namespace, item["assertion_id"], scopes={"operator"})

    def review(self, namespace, assertion_id, decision, reason, *, principal_id, scopes, place_id=None):
        """Accept or reject a proposal; an ambiguous place is accepted by choosing one of the cited candidates."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise TourismError("invalid_decision", "accept or reject with a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] == "ambiguous":
            if decision == "reject":
                return self._transition(namespace, item, "rejected", principal_id, reason.strip())
            chosen = next((c for c in item["evidence"]["candidates"] if c["place_id"] == place_id), None)
            if chosen is None:
                raise TourismError("invalid_decision", "choose one of the cited candidate places")
            target = {k: chosen[k] for k in ("place_id", "place_revision_id", "place_name", "place_type")}
            return self._transition(namespace, item, "accepted", principal_id, reason.strip(),
                                    target={"target": target, "method": chosen["method"]})
        if item["state"] != "proposed":
            raise TourismError("invalid_state", f"assertion is {item['state']}; only a proposal is reviewed")
        return self._transition(namespace, item, "accepted" if decision == "accept" else "rejected", principal_id,
                                reason.strip())

    def revert(self, namespace, assertion_id, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise TourismError("invalid_decision", "a revert needs a reason")
        item = self.assertion(namespace, assertion_id, scopes={"operator"})
        if item["state"] not in {"accepted", "rejected"}:
            raise TourismError("invalid_state", "only an accepted or rejected assertion can be reverted")
        return self._transition(namespace, item, "reverted", principal_id, reason.strip())

    # ------------------------------------------------------------------ use by queries and links

    def area_keys_for_place(self, namespace: str, place_id: str) -> list[dict[str, Any]]:
        """The place keys accepted as this place, each with the assertion it rests on."""
        return [{**a["subject"], "assertion_id": a["assertion_id"], "method": a["method"],
                 "reviewed": a["history"][-1]}
                for a in self.assertions(namespace, scopes={"operator"}, kind="area", state="accepted")
                if a["target"]["place_id"] == place_id]

    def place_for_area(self, namespace: str, area: Mapping[str, Any]) -> dict[str, Any] | None:
        """The accepted place of a place key (``None`` unless a reviewer accepted one)."""
        latest = self._latest(namespace, "area", canonical(area_key(area)))
        if latest is None or latest["state"] != "accepted":
            return None
        return {**latest["target"], "assertion_id": latest["assertion_id"]}

    def corresponding_keys(self, namespace: str, area: Mapping[str, Any], *,
                           state: str = "accepted") -> list[dict[str, Any]]:
        """Place keys under other NUTS versions linked to ``area`` by correspondence assertions in ``state``."""
        wanted = area_key(area)
        out = []
        for a in self.assertions(namespace, scopes={"operator"}, kind="nuts_correspondence", state=state):
            pair = a["subject"]["pair"]
            if wanted in pair:
                other = pair[1] if pair[0] == wanted else pair[0]
                out.append({**other, "relation": a["relation"], "method": a["method"],
                            "assertion_id": a["assertion_id"], "state": a["state"], "reviewed": a["history"][-1],
                            "citation": a["evidence"]["citation"]})
        return out

    def unmatched(self, namespace: str, *, scopes: Iterable[str]) -> list[dict[str, Any]]:
        return [a for a in self.assertions(namespace, scopes=scopes) if a["state"] in {"unmatched", "ambiguous",
                                                                                      "proposed", "rejected",
                                                                                      "reverted"}]


__all__ = ["CONTRACT", "CORRESPONDENCE_RELATIONS", "KINDS", "TourismIdentity", "area_key"]
