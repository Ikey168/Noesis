"""Port records keyed by UN/LOCODE, reviewable port identity and citation links (#2229, SL02/SL03, SL07, SL08).

**Port records.** Each UN/LOCODE release is a version: an entry is stored as published (code, name, name without
diacritics, subdivision, function, status, date, IATA code, coordinates, change indicator, remarks) with the release
version; a changed entry adds a revision, an entry published with change indicator ``X`` is a revision in state
``marked-for-removal`` and a code of a covered country that a later release no longer lists becomes a ``removed``
revision - nothing is deleted. :meth:`LogisticsPorts.project_places` registers the current ports as Geospatial
places (:class:`src.kb.geospatial.GeospatialStore`) carrying the UN/LOCODE, with a point geometry only when the
release publishes coordinates.

**Identity (SL07).** Source port codes (UNCTAD port identifiers, Eurostat ``rep_mar``/``par_mar`` codes) are
matched to UN/LOCODE port records:

* ``embedded-unlocode`` - the source row itself publishes the UN/LOCODE: exact, accepted;
* ``published-crosswalk`` - a published code list or crosswalk recorded with its citation
  (:meth:`LogisticsPorts.import_crosswalk`): exact, accepted;
* ``name`` - the source label equals a port's published name: a candidate, ``proposed`` until a reviewer accepts
  or rejects it. Coordinates are not published by the selected sources; a coordinate candidate would be proposed
  the same way.

Rejected and reverted candidates are never used, a source code without a match stays queryable by its own code
(``unmatched``), and a UN/LOCODE change or removal records a re-match (``logistics_rematches``) instead of silently
re-pointing a match. The state machine follows the reviewable identity of :mod:`src.kb.trade_identity`
(proposed, accepted or rejected, reverted); the platform's review inbox targets are unchanged.

**Links (SL08).** A logistics series is linked to Economics trade-flow series (:mod:`src.kb.trade_flows`) and to
other Economics series only through a shared published identifier (the same M49 or Eurostat GEO code, or a port's
UN/LOCODE country code equal to a Comext reporter code) or an explicit citation; never through a name. Each link
records its basis and both series; units and frequencies are listed side by side (the Economics comparability check
for Economics series) and never combined. Without trade records the join reports ``none_on_record``.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.logistics_records import (
    ECONOMIC_DOMAIN,
    LINK_CONTRACT,
    MATCH_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    LogisticsError,
    authorize,
    canonical,
    digest,
    iso_from_ms,
    load,
    require_scope,
    table_exists,
)

GEO_READ, GEO_WRITE = "knowledge:geospatial:read", "knowledge:geospatial:write"
TRADE_READ = "knowledge:trade:read"
MATCH_STATES = ("proposed", "accepted", "rejected", "reverted", "superseded", "target-removed")
EXACT_BASES = ("embedded-unlocode", "published-crosswalk")
CANDIDATE_BASES = ("name", "coordinates")
LINK_BASES = ("shared-m49-code", "shared-eurostat-geo-code", "unlocode-country-code", "shared-country-code",
              "explicit-citation")
# Codes that differ between ISO 3166-1 alpha-2 (UN/LOCODE) and Eurostat GEO: never joined by equality.
DIVERGENT_COUNTRY_CODES = frozenset({"GR", "EL", "GB", "UK"})
POLICY = ("links join records by a shared published identifier or an explicit citation only; values of different "
          "units or frequencies are listed side by side and never combined, and no ratio is derived")
_DDL = """
CREATE TABLE IF NOT EXISTS logistics_ports (
  namespace TEXT NOT NULL, unlocode TEXT NOT NULL, revision INTEGER NOT NULL, release_id TEXT NOT NULL,
  release_version TEXT, published_on TEXT, country TEXT NOT NULL, state TEXT NOT NULL, change_indicator TEXT,
  content_hash TEXT NOT NULL, record_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, unlocode, revision)
);
CREATE TABLE IF NOT EXISTS logistics_crosswalks (
  namespace TEXT NOT NULL, crosswalk_id TEXT NOT NULL, publisher TEXT NOT NULL, source_scheme TEXT NOT NULL,
  citation_json TEXT NOT NULL, rows_json TEXT NOT NULL, recorded_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, crosswalk_id)
);
CREATE TABLE IF NOT EXISTS logistics_port_matches (
  namespace TEXT NOT NULL, match_id TEXT NOT NULL, source_scheme TEXT NOT NULL, source_code TEXT NOT NULL,
  source_label TEXT, unlocode TEXT NOT NULL, port_revision INTEGER NOT NULL, basis TEXT NOT NULL, exact BOOLEAN NOT NULL,
  state TEXT NOT NULL, evidence_json TEXT NOT NULL, place_id TEXT, decided_by TEXT, reason TEXT,
  created_at_ms BIGINT NOT NULL, updated_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, match_id)
);
CREATE TABLE IF NOT EXISTS logistics_rematches (
  namespace TEXT NOT NULL, rematch_id TEXT NOT NULL, match_id TEXT NOT NULL, unlocode TEXT NOT NULL,
  from_revision INTEGER NOT NULL, to_revision INTEGER NOT NULL, trigger TEXT NOT NULL, outcome TEXT NOT NULL,
  release_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, rematch_id)
);
CREATE TABLE IF NOT EXISTS logistics_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, series_id TEXT NOT NULL, target_kind TEXT NOT NULL,
  target_namespace TEXT NOT NULL, target_id TEXT NOT NULL, basis TEXT NOT NULL, shared_json TEXT,
  citation_json TEXT, side_by_side_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""
TABLES = ("logistics_ports", "logistics_crosswalks", "logistics_port_matches", "logistics_rematches",
          "logistics_links")
_PORT_FIELDS = ("unlocode", "country", "location", "name", "name_wo_diacritics", "subdivision", "function",
                "status", "date", "iata", "coordinates", "remarks")


def fold(value: Any) -> str:
    raw = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(c for c in raw if not unicodedata.combining(c)).casefold().split())


class LogisticsPorts:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        if initialize:
            for sql in _DDL.split(";"):
                if sql.strip():
                    conn.execute(sql)

    # ------------------------------------------------------------------ port records (SL03)

    def _latest(self, namespace: str, unlocode: str) -> dict[str, Any] | None:
        rows = self.port_history(namespace, unlocode)
        return rows[-1] if rows else None

    def _add_revision(self, namespace, unlocode, country, release_id, header, state, change, record) -> int:
        latest = self._latest(namespace, unlocode)
        revision = 1 if latest is None else latest["revision"] + 1
        self.conn.execute(
            "INSERT INTO logistics_ports VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [namespace, unlocode, revision, release_id, header.get("release_version"), header.get("published_on"),
             country, state, change, digest([state, {k: record.get(k) for k in _PORT_FIELDS}]), canonical(record),
             self.now()])
        return revision

    def apply_ports(self, namespace: str, release_id: str, header: Mapping[str, Any],
                    items: Sequence[Mapping[str, Any]]) -> dict[str, int]:
        """Apply one UN/LOCODE release (inside the caller's transaction); returns counts per outcome."""
        counts = {"created": 0, "revised": 0, "unchanged": 0, "removed": 0}
        countries = {str(c).upper() for c in dict(header.get("structure") or {}).get("countries") or []}
        listed = set()
        for item in items:
            if item.get("kind") != "port" or not item.get("unlocode"):
                raise LogisticsError("invalid_release", "a UN/LOCODE release carries port entries")
            code = str(item["unlocode"])
            listed.add(code)
            state = "marked-for-removal" if item.get("change_indicator") == "X" else "active"
            record = {k: item.get(k) for k in (*_PORT_FIELDS, "change_indicator", "change_label", "row")}
            latest = self._latest(namespace, code)
            content = digest([state, {k: record.get(k) for k in _PORT_FIELDS}])
            if latest is not None and latest["content_hash"] == content:
                counts["unchanged"] += 1
                continue
            revision = self._add_revision(namespace, code, item["country"], release_id, header, state,
                                          item.get("change_indicator"), record)
            if latest is None:
                counts["created"] += 1
            else:
                counts["revised"] += 1
                self._rematch(namespace, code, latest["revision"], revision, state, release_id)
        for code, country in self.conn.execute(
                "SELECT DISTINCT unlocode, country FROM logistics_ports WHERE namespace=?", [namespace]).fetchall():
            if country not in countries or code in listed:
                continue
            latest = self._latest(namespace, code)
            if latest["state"] == "removed":
                continue
            record = {**latest["record"], "removed_in_release": header.get("release_version")}
            revision = self._add_revision(namespace, code, country, release_id, header, "removed", None, record)
            counts["removed"] += 1
            self._rematch(namespace, code, latest["revision"], revision, "removed", release_id)
        return counts

    def _rematch(self, namespace, unlocode, from_revision, to_revision, state, release_id) -> None:
        """A changed or removed code re-checks every live match on it and records the outcome; never re-points."""
        port = self.port_history(namespace, unlocode)[-1]
        for match in self.matches(namespace, unlocode=unlocode):
            if match["state"] not in ("accepted", "proposed"):
                continue
            if state == "removed":
                outcome, new_state = "target removed; the source code is unmatched until a new match is reviewed", \
                    "target-removed"
            elif match["basis"] == "name" and fold(match["source_label"]) not in {
                    fold(port["record"].get("name")), fold(port["record"].get("name_wo_diacritics"))}:
                outcome, new_state = "the published name no longer equals the source label; candidate superseded", \
                    "superseded"
            elif state == "marked-for-removal":
                outcome, new_state = "target marked for removal in this release; match kept and flagged", \
                    match["state"]
            else:
                outcome, new_state = f"basis {match['basis']} re-checked against revision {to_revision}; kept", \
                    match["state"]
            rematch_id = "logistics-rematch:" + digest([namespace, match["match_id"], to_revision])[:24]
            self.conn.execute(
                "INSERT OR IGNORE INTO logistics_rematches VALUES (?,?,?,?,?,?,?,?,?,?)",
                [namespace, rematch_id, match["match_id"], unlocode, from_revision, to_revision,
                 "removed" if state == "removed" else "changed", outcome, release_id, self.now()])
            self.conn.execute(
                "UPDATE logistics_port_matches SET state=?, port_revision=?, updated_at_ms=? WHERE namespace=? AND "
                "match_id=?", [new_state, to_revision, self.now(), namespace, match["match_id"]])

    def port_history(self, namespace: str, unlocode: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "logistics_ports"):
            return []
        rows = self.conn.execute(
            "SELECT revision, release_id, release_version, published_on, country, state, change_indicator, "
            "content_hash, record_json FROM logistics_ports WHERE namespace=? AND unlocode=? ORDER BY revision",
            [namespace, str(unlocode).replace(" ", "").upper()]).fetchall()
        return [{"unlocode": str(unlocode).replace(" ", "").upper(), "revision": int(r[0]), "release_id": r[1],
                 "release_version": r[2], "published_on": r[3], "country": r[4], "state": r[5],
                 "change_indicator": r[6], "content_hash": r[7], "record": load(r[8], {})} for r in rows]

    def port(self, namespace: str, unlocode: str, *, as_of_day: str | None = None) -> dict[str, Any] | None:
        """The revision of a port in force by a day (release publication date), with its full history."""
        history = self.port_history(namespace, unlocode)
        if as_of_day is not None:
            history = [h for h in history if (h["published_on"] or "") <= as_of_day]
        if not history:
            return None
        current = history[-1]
        return {**current, "revisions": len(self.port_history(namespace, unlocode)),
                "history": [{k: h[k] for k in ("revision", "release_version", "state", "change_indicator")}
                            for h in self.port_history(namespace, unlocode)]}

    def ports(self, namespace: str, *, country: str | None = None, include_removed: bool = False) -> list[dict]:
        if not table_exists(self.conn, "logistics_ports"):
            return []
        codes = [r[0] for r in self.conn.execute(
            "SELECT DISTINCT unlocode FROM logistics_ports WHERE namespace=? AND (? IS NULL OR country=?) "
            "ORDER BY unlocode", [namespace, country, country]).fetchall()]
        out = []
        for code in codes:
            latest = self._latest(namespace, code)
            if latest["state"] == "removed" and not include_removed:
                continue
            out.append(latest)
        return out

    def project_places(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                       geo_namespace: str = "global") -> dict[str, Any]:
        """Register current ports as Geospatial places carrying the UN/LOCODE; a point only when published."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_WRITE)
        geo = GeospatialStore(self.conn, now=self.now)
        places, without = {}, []
        for port in self.ports(namespace):
            record = port["record"]
            coordinates = record.get("coordinates") or {}
            geometry = ({"type": "Point", "coordinates": [coordinates["lon"], coordinates["lat"]]}
                        if coordinates.get("parsed") else None)
            if geometry is None:
                without.append(port["unlocode"])
            names = [record.get("name"), record.get("name_wo_diacritics")]
            result = geo.register_place(
                geo_namespace, str(record.get("name") or port["unlocode"]), "port",
                names=[{"value": n, "language": "und", "kind": "canonical" if i == 0 else "alternative"}
                       for i, n in enumerate(dict.fromkeys(n for n in names if n))],
                source_ids={"unlocode": port["unlocode"]}, parent_ids=[], principal_id=principal_id,
                scopes={GEO_WRITE}, place_key=f"unlocode:{port['unlocode']}", geometry=geometry,
                observed_at_ms=0, producer={"name": "noesis-economics-logistics", "version": "1.0.0"},
                provenance={"source": "UN/LOCODE", "release_version": port["release_version"],
                            "revision": port["revision"],
                            "coordinates": "as published" if geometry else "not published; no geometry"})
            places[port["unlocode"]] = result["place_id"]
        return {"geo_namespace": geo_namespace, "places": places, "without_published_coordinates": without}

    def place_id(self, unlocode: str, geo_namespace: str = "global") -> str | None:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return None
        rows = self.conn.execute(
            "SELECT p.place_id, r.source_ids_json FROM geospatial_places p JOIN geospatial_place_current c "
            "ON c.place_id=p.place_id JOIN geospatial_place_revisions r ON r.revision_id=c.revision_id "
            "WHERE p.namespace IN (?, 'global')", [geo_namespace]).fetchall()
        hits = [r[0] for r in rows if str(load(r[1], {}).get("unlocode") or "") == unlocode]
        return hits[0] if len(hits) == 1 else None

    # ------------------------------------------------------------------ identity (SL07)

    def import_crosswalk(self, namespace: str, crosswalk: Mapping[str, Any], *, principal_id: str,
                         scopes: Iterable[str]) -> dict[str, Any]:
        """Record a published code list or crosswalk (source port code -> UN/LOCODE) with its citation."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        body = dict(crosswalk)
        citation = dict(body.get("citation") or {})
        rows = [dict(r) for r in body.get("rows") or []]
        if not body.get("publisher") or not body.get("source_scheme") or not citation.get("url") or not rows:
            raise LogisticsError("invalid_crosswalk", "a crosswalk names its publisher, source scheme, citation URL "
                                 "and rows")
        if not all(r.get("source_code") and r.get("unlocode") for r in rows):
            raise LogisticsError("invalid_crosswalk", "each crosswalk row pairs a source code with a UN/LOCODE")
        crosswalk_id = "logistics-crosswalk:" + digest([namespace, body["publisher"], body["source_scheme"],
                                                        citation, rows])[:24]
        self.conn.execute(
            "INSERT OR IGNORE INTO logistics_crosswalks VALUES (?,?,?,?,?,?,?,?)",
            [namespace, crosswalk_id, body["publisher"], body["source_scheme"], canonical(citation),
             canonical(rows), principal_id, self.now()])
        return {"crosswalk_id": crosswalk_id, "rows": len(rows), "citation": citation}

    def _crosswalk_rows(self, namespace: str, scheme: str, code: str) -> list[dict[str, Any]]:
        out = []
        for crosswalk_id, publisher, source_scheme, citation, rows in self.conn.execute(
                "SELECT crosswalk_id, publisher, source_scheme, citation_json, rows_json FROM logistics_crosswalks "
                "WHERE namespace=? ORDER BY created_at_ms, crosswalk_id", [namespace]).fetchall():
            if source_scheme != scheme:
                continue
            for row in load(rows, []):
                if str(row["source_code"]) == code:
                    out.append({"crosswalk_id": crosswalk_id, "publisher": publisher, "citation": load(citation, {}),
                                "unlocode": str(row["unlocode"]).replace(" ", "").upper()})
        return out

    def source_ports(self, namespace: str) -> list[dict[str, Any]]:
        """Every source port code the acquired series publish (reporting and partner ends), with labels."""
        if not table_exists(self.conn, "logistics_series"):
            return []
        rows = self.conn.execute(
            "SELECT geo_scheme, geo_code, geo_label, geo_unlocode, provider FROM logistics_series WHERE namespace=? "
            "AND geo_kind IN ('port', 'route') UNION SELECT partner_scheme, partner_code, partner_label, "
            "partner_unlocode, provider FROM logistics_series WHERE namespace=? AND geo_kind='route'",
            [namespace, namespace]).fetchall()
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        for scheme, code, label, unlocode, provider in rows:
            if scheme == "unlocode":
                continue
            entry = seen.setdefault((scheme, code), {"scheme": scheme, "code": code, "labels": set(),
                                                    "embedded_unlocode": None, "providers": set()})
            if label:
                entry["labels"].add(label)
            if unlocode:
                entry["embedded_unlocode"] = unlocode
            entry["providers"].add(provider)
        return [{**v, "labels": sorted(v["labels"]), "providers": sorted(v["providers"])}
                for _, v in sorted(seen.items())]

    def _insert_match(self, namespace, scheme, code, label, port, basis, evidence, principal_id) -> tuple[str, bool]:
        exact = basis in EXACT_BASES
        match_id = "logistics-match:" + digest([namespace, scheme, code, port["unlocode"], basis])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO logistics_port_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) RETURNING match_id",
            [namespace, match_id, scheme, code, label, port["unlocode"], port["revision"], basis, exact,
             "accepted" if exact else "proposed", canonical(evidence), self.place_id(port["unlocode"]),
             principal_id if exact else None,
             "exact: " + basis if exact else None, self.now(), self.now()]).fetchall()
        return match_id, bool(inserted)

    def propose_matches(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Match every source port code: exact from an embedded UN/LOCODE or a published crosswalk, name candidates
        for review otherwise; codes with neither stay unmatched and queryable by their own code."""
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        active = [p for p in self.ports(namespace) if p["state"] != "removed"]
        by_code = {p["unlocode"]: p for p in active}
        exact, candidates, unmatched, created = [], [], [], 0
        for source in self.source_ports(namespace):
            scheme, code = source["scheme"], source["code"]
            label = source["labels"][0] if source["labels"] else None
            hits = []
            embedded = source["embedded_unlocode"]
            if embedded and embedded in by_code:
                hits.append((by_code[embedded], "embedded-unlocode",
                             {"published_unlocode": embedded, "providers": source["providers"]}))
            for row in self._crosswalk_rows(namespace, scheme, code):
                if row["unlocode"] in by_code:
                    hits.append((by_code[row["unlocode"]], "published-crosswalk",
                                 {"crosswalk_id": row["crosswalk_id"], "publisher": row["publisher"],
                                  "citation": row["citation"]}))
            if hits:
                for port, basis, evidence in hits:
                    match_id, new = self._insert_match(namespace, scheme, code, label, port, basis, evidence,
                                                       principal_id)
                    created += new
                    exact.append(match_id)
                continue
            wanted = {fold(label) for label in source["labels"]}
            named = [p for p in active if wanted & {fold(p["record"].get("name")),
                                                    fold(p["record"].get("name_wo_diacritics"))}]
            if named:
                for port in named:
                    match_id, new = self._insert_match(
                        namespace, scheme, code, label, port, "name",
                        {"source_labels": source["labels"], "published_name": port["record"].get("name"),
                         "note": "a label equal to a published name is a candidate only; review required"},
                        principal_id)
                    created += new
                    candidates.append(match_id)
                continue
            unmatched.append({"scheme": scheme, "code": code, "labels": source["labels"],
                              "note": "no published UN/LOCODE, crosswalk row or equal name; queryable by its own code"})
        return {"namespace": namespace, "exact": exact, "candidates": candidates, "unmatched": unmatched,
                "created": created}

    def _match(self, namespace: str, match_id: str) -> dict[str, Any]:
        found = [m for m in self.matches(namespace) if m["match_id"] == match_id]
        if not found:
            raise LogisticsError("not_found", "match is not visible in this namespace")
        return found[0]

    def review(self, namespace: str, match_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        match = self._match(namespace, match_id)
        if decision not in ("accept", "reject") or not str(reason or "").strip():
            raise LogisticsError("invalid_review", "decision is accept or reject, with a reason")
        if match["state"] != "proposed":
            raise LogisticsError("invalid_transition", f"a {match['state']} match cannot be reviewed")
        state = "accepted" if decision == "accept" else "rejected"
        self.conn.execute(
            "UPDATE logistics_port_matches SET state=?, decided_by=?, reason=?, updated_at_ms=? WHERE namespace=? AND "
            "match_id=?", [state, principal_id, reason, self.now(), namespace, match_id])
        return self._match(namespace, match_id)

    def revert(self, namespace: str, match_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        match = self._match(namespace, match_id)
        if match["state"] != "accepted" or not str(reason or "").strip():
            raise LogisticsError("invalid_transition", "only an accepted match is reverted, with a reason")
        self.conn.execute(
            "UPDATE logistics_port_matches SET state='reverted', decided_by=?, reason=?, updated_at_ms=? WHERE "
            "namespace=? AND match_id=?", [principal_id, reason, self.now(), namespace, match_id])
        return self._match(namespace, match_id)

    def matches(self, namespace: str, *, unlocode: str | None = None, source: tuple[str, str] | None = None,
                state: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "logistics_port_matches"):
            return []
        rows = self.conn.execute(
            "SELECT match_id, source_scheme, source_code, source_label, unlocode, port_revision, basis, exact, state, "
            "evidence_json, place_id, decided_by, reason, updated_at_ms FROM logistics_port_matches WHERE namespace=? "
            "AND (? IS NULL OR unlocode=?) AND (? IS NULL OR state=?) ORDER BY source_scheme, source_code, unlocode, "
            "basis", [namespace, unlocode, unlocode, state, state]).fetchall()
        out = []
        for r in rows:
            if source is not None and (r[1], r[2]) != tuple(source):
                continue
            out.append({"contract": MATCH_CONTRACT, "match_id": r[0], "source": {"scheme": r[1], "code": r[2]},
                        "source_label": r[3], "unlocode": r[4], "port_revision": int(r[5]), "basis": r[6],
                        "exact": bool(r[7]), "state": r[8], "evidence": load(r[9], {}), "place_id": r[10],
                        "decided_by": r[11], "reason": r[12], "updated_at": iso_from_ms(r[13]),
                        "used_in_answers": r[8] == "accepted",
                        "rematches": self.rematches(namespace, r[0])})
        return out

    def rematches(self, namespace: str, match_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT rematch_id, unlocode, from_revision, to_revision, trigger, outcome, release_id FROM "
            "logistics_rematches WHERE namespace=? AND match_id=? ORDER BY to_revision", [namespace, match_id]
        ).fetchall()
        return [{"rematch_id": r[0], "unlocode": r[1], "from_revision": int(r[2]), "to_revision": int(r[3]),
                 "trigger": r[4], "outcome": r[5], "release_id": r[6]} for r in rows]

    def resolve_source(self, namespace: str, scheme: str, code: str) -> dict[str, Any]:
        """The accepted UN/LOCODE of a source port code, or ``unmatched`` (never a rejected or pending candidate)."""
        matches = self.matches(namespace, source=(scheme, code))
        accepted = [m for m in matches if m["state"] == "accepted"]
        if len({m["unlocode"] for m in accepted}) == 1:
            return {"status": "matched", "unlocode": accepted[0]["unlocode"], "basis": accepted[0]["basis"],
                    "exact": accepted[0]["exact"], "match_id": accepted[0]["match_id"]}
        return {"status": "ambiguous" if accepted else "unmatched", "unlocode": None,
                "candidates": [{"unlocode": m["unlocode"], "basis": m["basis"], "state": m["state"]}
                               for m in matches]}

    def source_codes(self, namespace: str, unlocode: str) -> list[dict[str, Any]]:
        """Source port codes accepted for a UN/LOCODE, each with its match basis."""
        return [{"scheme": m["source"]["scheme"], "code": m["source"]["code"], "basis": m["basis"],
                 "exact": m["exact"], "match_id": m["match_id"]}
                for m in self.matches(namespace, unlocode=unlocode, state="accepted")]

    # ------------------------------------------------------------------ links (SL08)

    def _record_link(self, namespace, series, target_kind, target_namespace, target_id, basis, shared, citation,
                     side_by_side, principal_id) -> tuple[str, bool]:
        link_id = "logistics-link:" + digest([namespace, series["series_id"], target_kind, target_namespace,
                                              target_id, basis])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO logistics_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?) RETURNING link_id",
            [namespace, link_id, series["series_id"], target_kind, target_namespace, target_id, basis,
             canonical(shared) if shared else None, canonical(citation) if citation else None,
             canonical(side_by_side), principal_id, self.now()]).fetchall()
        return link_id, bool(inserted)

    @staticmethod
    def _side_by_side(series: Mapping[str, Any], other_unit: Any, other_frequency: Any) -> dict[str, Any]:
        return {
            "logistics": {"unit": series["unit"], "frequency": series["frequency"]},
            "other": {"unit": other_unit, "frequency": other_frequency},
            "same_unit_and_frequency": canonical(series["unit"]) == canonical(other_unit)
            and series["frequency"] == other_frequency,
            "combined": False,
            "note": "listed side by side; never combined, and no ratio is derived",
        }

    def _trade_keys(self, namespace: str, series: Mapping[str, Any]) -> list[tuple[str, str, str]]:
        """(trade reporter scheme, code, basis) pairs a logistics series shares with trade records."""
        geography = series["geography"]
        if geography["kind"] == "country":
            if geography["scheme"] == "m49":
                return [("m49", geography["code"], "shared-m49-code")]
            if geography["scheme"] in ("eurostat-geo", "iso2") and geography["code"] not in DIVERGENT_COUNTRY_CODES:
                return [("eurostat-geo", geography["code"], "shared-eurostat-geo-code")]
            return []
        if geography["kind"] == "port":
            unlocode = geography.get("unlocode")
            if not unlocode:
                resolved = self.resolve_source(namespace, geography["scheme"], geography["code"])
                unlocode = resolved["unlocode"]
            if unlocode and unlocode[:2] not in DIVERGENT_COUNTRY_CODES:
                return [("eurostat-geo", unlocode[:2], "unlocode-country-code")]
        return []

    def link_trade_flows(self, namespace: str, *, principal_id: str, scopes: Iterable[str],
                         trade_namespace: str | None = None, series_ids: Sequence[str] | None = None) -> dict:
        """Join logistics series to trade-flow series that publish the same reporter code; none on record when the
        trade store holds no records."""
        from src.kb.logistics_series import LogisticsStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, TRADE_READ)
        trade_namespace = trade_namespace or namespace
        if not table_exists(self.conn, "trade_series"):
            return {"namespace": namespace, "status": "none_on_record", "linked": [], "unlinked": [],
                    "note": "no trade-flow records are held; nothing to join", "policy": POLICY}
        from src.kb.trade_flows import TradeFlowStore

        trade = TradeFlowStore(self.conn, initialize=False)
        store = LogisticsStore(self.conn, initialize=False)
        linked, unlinked = [], []
        for series in store.find_series(namespace):
            if series_ids is not None and series["series_id"] not in series_ids:
                continue
            found = []
            for scheme, code, basis in self._trade_keys(namespace, series):
                for flow in trade.find_series(trade_namespace, reporter_codes=[code]):
                    if flow["reporter"].get("scheme") != scheme:
                        continue
                    link_id, _ = self._record_link(
                        namespace, series, "trade-series", trade_namespace, flow["series_id"], basis,
                        {"scheme": scheme, "code": code, "trade_side": "reporter"}, None,
                        self._side_by_side(series, flow["unit"], flow["frequency"]), principal_id)
                    found.append(link_id)
            (linked if found else unlinked).append({"series_id": series["series_id"], "links": found})
        return {"namespace": namespace, "status": "linked" if any(i["links"] for i in linked) else "none_on_record",
                "linked": linked, "unlinked": unlinked, "policy": POLICY}

    def link_economic_series(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict:
        """Join country-level logistics series to other Economics series of the same published country code,
        with the Economics comparability check listing why they are not combined."""
        from src.domains.economic.model import assess_comparability
        from src.ingestion.connectors.dataset.normalize import normalize_geography
        from src.kb.logistics_series import LogisticsStore

        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not table_exists(self.conn, "economic_series_map"):
            return {"namespace": namespace, "status": "none_on_record", "linked": []}
        store = LogisticsStore(self.conn, initialize=False)
        own = {s["series_id"] for s in store.find_series(namespace)}
        others = self.conn.execute(
            "SELECT m.series_id, i.geography FROM economic_series_map m JOIN economic_indicators i ON "
            "i.domain=m.domain AND i.indicator_id=m.indicator_id WHERE m.domain=? ORDER BY m.series_id",
            [ECONOMIC_DOMAIN]).fetchall()
        linked = []
        for series in store.find_series(namespace, geo_kind="country"):
            geography = series["geography"]
            if geography["scheme"] not in ("eurostat-geo", "iso2") or geography["code"] in DIVERGENT_COUNTRY_CODES:
                continue
            wanted = normalize_geography(geography["code"])
            for other_id, other_geo in others:
                if other_id in own or other_geo != wanted:
                    continue
                check = assess_comparability(self.conn, series["series_id"], other_id, domain=ECONOMIC_DOMAIN,
                                             comparison_mode="cross_section")
                side = {**self._side_by_side(series, check["series"][1]["unit"], check["series"][1]["frequency"]),
                        "comparability": {"comparable": check["comparable"], "blockers": check["blockers"],
                                          "qualifications": check["qualifications"]}}
                link_id, _ = self._record_link(namespace, series, "economic-series", ECONOMIC_DOMAIN, other_id,
                                               "shared-country-code", {"scheme": "iso2", "code": wanted}, None,
                                               side, principal_id)
                linked.append(link_id)
        return {"namespace": namespace, "status": "linked" if linked else "none_on_record", "linked": linked,
                "policy": POLICY}

    def link_by_citation(self, namespace: str, series_id: str, target_kind: str, target_id: str,
                         citation: Mapping[str, Any], *, principal_id: str, scopes: Iterable[str],
                         target_namespace: str | None = None) -> dict[str, Any]:
        """An explicit citation (quoted text and locator) joining a logistics series to a trade or Economics
        series; the target must exist."""
        from src.kb.logistics_series import LogisticsStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        citation = dict(citation or {})
        if not str(citation.get("text") or "").strip() or not citation.get("locator"):
            raise LogisticsError("invalid_citation", "an explicit citation quotes its text and gives a locator")
        series = LogisticsStore(self.conn, initialize=False).series(namespace, series_id)
        target_namespace = target_namespace or (namespace if target_kind == "trade-series" else ECONOMIC_DOMAIN)
        if target_kind == "trade-series":
            require_scope(scopes, TRADE_READ)
            if not table_exists(self.conn, "trade_series"):
                raise LogisticsError("target_not_found", "no trade-flow records are held")
            from src.kb.trade_flows import TradeError, TradeFlowStore

            try:
                target = TradeFlowStore(self.conn, initialize=False).series(target_namespace, target_id)
            except TradeError as exc:
                raise LogisticsError("target_not_found", str(exc)) from exc
            unit, frequency = target["unit"], target["frequency"]
        elif target_kind == "economic-series":
            row = self.conn.execute(
                "SELECT i.unit, i.frequency FROM economic_series_map m JOIN economic_indicators i ON "
                "i.domain=m.domain AND i.indicator_id=m.indicator_id WHERE m.domain=? AND m.series_id=?",
                [ECONOMIC_DOMAIN, target_id]).fetchone() if table_exists(self.conn, "economic_series_map") else None
            if row is None:
                raise LogisticsError("target_not_found", "the Economics series is not held")
            unit, frequency = row
        else:
            raise LogisticsError("invalid_request", "target_kind is trade-series or economic-series")
        link_id, created = self._record_link(namespace, series, target_kind, target_namespace, target_id,
                                             "explicit-citation", None, citation,
                                             self._side_by_side(series, unit, frequency), principal_id)
        return {"link_id": link_id, "created": created, "basis": "explicit-citation", "citation": citation}

    def links(self, namespace: str, *, series_id: str | None = None,
              target_kind: str | None = None) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "logistics_links"):
            return []
        rows = self.conn.execute(
            "SELECT link_id, series_id, target_kind, target_namespace, target_id, basis, shared_json, citation_json, "
            "side_by_side_json, created_by, created_at_ms FROM logistics_links WHERE namespace=? AND "
            "(? IS NULL OR series_id=?) AND (? IS NULL OR target_kind=?) ORDER BY series_id, target_kind, target_id",
            [namespace, series_id, series_id, target_kind, target_kind]).fetchall()
        return [{"contract": LINK_CONTRACT, "link_id": r[0], "series_id": r[1],
                 "target": {"kind": r[2], "namespace": r[3], "id": r[4]}, "basis": r[5], "shared": load(r[6]),
                 "citation": load(r[7]), "side_by_side": load(r[8], {}), "created_by": r[9],
                 "created_at": iso_from_ms(r[10])} for r in rows]

    def trade_join(self, namespace: str, series_id: str) -> dict[str, Any]:
        """The trade-flow links of one series; ``none_on_record`` when there are none (or no trade records)."""
        links = self.links(namespace, series_id=series_id, target_kind="trade-series")
        return {"series_id": series_id, "status": "linked" if links else "none_on_record", "links": links}


def read_ports(conn: Any, namespace: str, scopes) -> LogisticsPorts:
    authorize(namespace, scopes, READ_SCOPE)
    return LogisticsPorts(conn, initialize=False)


__all__ = ["DIVERGENT_COUNTRY_CODES", "EXACT_BASES", "LINK_BASES", "MATCH_STATES", "POLICY", "TABLES",
           "LogisticsPorts", "fold", "read_ports"]
