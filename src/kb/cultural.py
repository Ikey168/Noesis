"""Cultural primary sources for Science/Research and Geospatial (DDB, Europeana).

Cultural objects are source records, not scholarly works and not places. This
store keeps them as their own records and connects them to the existing
stores only through evidence:

* **Objects and revisions.** One object per provider record (provider +
  native ID) with immutable revisions keyed by the native payload. Titles and
  descriptions keep their language tags; dates keep the original string next
  to a normalized year/range and the method; creators and subjects stay
  provider strings. Aggregation identity (e.g. Europeana over DDB) is kept
  separately from the contributing institution.
* **Rights.** Each record's rights statement is classified
  (:func:`rights_policy`). Missing, unrecognized or restrictive statements
  mean metadata plus links only; representations (preview, original) carry
  their own rights and are never copied into text content.
* **Places.** Provider coordinates are projected into the existing
  ``GeospatialStore`` as points with their declared (or explicitly unknown)
  precision and CRS receipt; place names resolve through the existing
  gazetteer, and ambiguous or unresolved names stay reviewable. Roles
  (institution, digitization, depicted, event) are kept distinct. Nothing is
  geocoded from free text.
* **Matches.** Explicit ``sameAs``/provider links (Europeana records shown at
  a DDB item) are recorded as identifier matches; otherwise conservative
  multi-field candidates are proposed for review. Provider records are never
  merged.
* **Research links.** A scholarly work and an object are linked only by an
  explicit citation, a provider relation or a reviewed assertion; keyword
  overlap only creates a candidate.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

READ_SCOPE = "knowledge:cultural:read"
WRITE_SCOPE = "knowledge:cultural:write"
REVIEW_SCOPE = "knowledge:cultural:review"
OBJECT_CONTRACT = "noesis-cultural-object-v1"
MATCH_CONTRACT = "noesis-cultural-object-match-v1"
LINK_CONTRACT = "noesis-cultural-research-link-v1"
ASSET_CONTRACT = "noesis-cultural-asset-v1"
DEFAULT_NAMESPACE = "global"
PLACE_ROLES = frozenset({"institution", "digitization", "depicted", "event"})
LINK_BASES = frozenset({"explicit_citation", "provider_relation", "reviewed_assertion"})
DECISIONS = frozenset({"accepted", "rejected", "deferred"})
GEOSPATIAL_SCOPES = {"knowledge:geospatial:read", "knowledge:geospatial:write"}

_DDL = """
CREATE TABLE IF NOT EXISTS cultural_objects (
  object_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, provider TEXT NOT NULL, provider_record_id TEXT NOT NULL,
  source_url TEXT NOT NULL, created_run_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS cultural_revisions (
  revision_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, object_id TEXT NOT NULL, native_sha256 TEXT NOT NULL,
  provider_revision TEXT, record_json TEXT NOT NULL, rights_json TEXT NOT NULL, document_id TEXT,
  source_id TEXT, first_run_id TEXT NOT NULL, observed_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS cultural_current (
  object_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, revision_id TEXT NOT NULL, updated_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS cultural_places (
  place_link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, object_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  role TEXT NOT NULL, name TEXT, provider_place_id TEXT, original_json TEXT, precision_basis TEXT,
  state TEXT NOT NULL, geometry_id TEXT, place_id TEXT, resolution_id TEXT, transform_json TEXT
);
CREATE TABLE IF NOT EXISTS cultural_matches (
  match_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, left_id TEXT NOT NULL, right_id TEXT NOT NULL,
  basis TEXT NOT NULL, candidate_state TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at_ms BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS cultural_match_reviews (
  match_id TEXT NOT NULL, sequence INTEGER NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
  principal_id TEXT NOT NULL, reviewed_at_ms BIGINT NOT NULL, PRIMARY KEY(match_id, sequence)
);
CREATE TABLE IF NOT EXISTS cultural_research_links (
  link_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, object_id TEXT NOT NULL, work_kind TEXT NOT NULL,
  work_id TEXT NOT NULL, state TEXT NOT NULL, basis TEXT NOT NULL, evidence TEXT NOT NULL,
  object_revision_id TEXT NOT NULL, principal_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  reviewed_by TEXT, reviewed_at_ms BIGINT
);
CREATE TABLE IF NOT EXISTS cultural_assets (
  asset_fetch_id TEXT PRIMARY KEY, namespace TEXT NOT NULL, object_id TEXT NOT NULL, revision_id TEXT NOT NULL,
  role TEXT NOT NULL, url TEXT NOT NULL, action TEXT NOT NULL, state TEXT NOT NULL, reason TEXT,
  rights_json TEXT NOT NULL, content_sha256 TEXT, media_type TEXT, bytes BIGINT, principal_id TEXT NOT NULL,
  fetched_at_ms BIGINT NOT NULL
);
"""


class CulturalError(ValueError):
    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value in (None, "") else json.loads(value)


def _authorize(namespace: str, scopes, required: str, *, write: bool) -> None:
    needed = {f"namespace:{namespace}:write"} if write else {f"namespace:{namespace}:read",
                                                             f"namespace:{namespace}:write"}
    if required not in scopes or not needed & set(scopes):
        raise CulturalError("unauthorized", f"{required} and namespace access are required")


# ------------------------------------------------------------------- rights

_PUBLIC_DOMAIN = ("creativecommons.org/publicdomain/mark/", "creativecommons.org/publicdomain/zero/",
                  "rightsstatements.org/vocab/noc-oklr/")
_OPEN = ("creativecommons.org/licenses/by/", "creativecommons.org/licenses/by-sa/")
_NONCOMMERCIAL = ("creativecommons.org/licenses/by-nc", "rightsstatements.org/vocab/noc-nc/")
_RESTRICTED = ("rightsstatements.org/vocab/inc", "creativecommons.org/licenses/by-nd",
               "rightsstatements.org/vocab/cne/", "rightsstatements.org/vocab/und/",
               "rightsstatements.org/vocab/nkc/", "rightsstatements.org/vocab/noc-cr/",
               "rightsstatements.org/vocab/noc-us/")


def rights_policy(statement: Any, *, purpose: str = "research") -> dict[str, Any]:
    """Classify one item rights statement; anything unclear is link-only."""

    text = str(statement or "").strip().casefold().replace("https://", "http://")
    if not text:
        category = "missing"
    elif any(p in text for p in _PUBLIC_DOMAIN):
        category = "public-domain"
    elif any(p in text for p in _NONCOMMERCIAL):
        category = "noncommercial"
    elif any(p in text for p in _RESTRICTED):
        category = "restricted"
    elif any(p in text for p in _OPEN):
        category = "open-attribution"
    else:
        category = "unrecognized"
    store = category in {"public-domain", "open-attribution"} or (category == "noncommercial"
                                                                  and purpose == "noncommercial-research")
    return {
        "statement": statement, "category": category,
        "allows": {"metadata": True, "store_asset": store,
                   "export_asset": category in {"public-domain", "open-attribution"}},
        "attribution_required": category in {"open-attribution", "noncommercial"},
        "default_action": "retain-permitted" if store else "link-only",
    }


class CulturalStore:
    def __init__(self, conn: Any, *, initialize: bool = True, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        from src.kb.geospatial import GeospatialStore

        self.geo = GeospatialStore(conn, initialize=initialize, now=now)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------ projection

    def observe_page(self, namespace: str, records: Sequence[Mapping[str, Any]], *, run_id: str,
                     source: Mapping[str, Any] | None, documents: Mapping[str, str] | None = None,
                     principal_id: str = "system:cultural") -> dict[str, int]:
        observed = self.now()
        counts = {"objects": 0, "revisions": 0, "places": 0, "matches": 0}
        policy = dict(dict((source or {}).get("cultural") or {}).get("places") or {})
        pending_places = []
        self.conn.execute("BEGIN")
        try:
            for item in records:
                record = dict(item.get("cultural_record") or {})
                if record.get("contract") != OBJECT_CONTRACT:
                    raise CulturalError("invalid_record", "page record is not a cultural object record")
                object_id = "cultural-object:" + _digest([namespace, record["provider"],
                                                          record["provider_record_id"]])[:24]
                created = self.conn.execute(
                    "INSERT OR IGNORE INTO cultural_objects VALUES (?,?,?,?,?,?,?) RETURNING object_id",
                    [object_id, namespace, record["provider"], record["provider_record_id"], record["source_url"],
                     run_id, observed]).fetchall()
                counts["objects"] += len(created)
                revision_id = "cultural-revision:" + _digest([object_id, record["native_sha256"]])[:24]
                rights = rights_policy(dict(record.get("rights") or {}).get("statement"))
                inserted = self.conn.execute(
                    "INSERT OR IGNORE INTO cultural_revisions VALUES (?,?,?,?,?,?,?,?,?,?,?) RETURNING revision_id",
                    [revision_id, namespace, object_id, record["native_sha256"], record.get("provider_revision"),
                     _canonical({k: v for k, v in record.items() if k != "native"}), _canonical(rights),
                     (documents or {}).get(str(item.get("id"))), (source or {}).get("source_id"), run_id,
                     observed]).fetchall()
                if not inserted:
                    continue
                counts["revisions"] += 1
                self.conn.execute(
                    "INSERT OR REPLACE INTO cultural_current VALUES (?,?,?,?)",
                    [object_id, namespace, revision_id, observed])
                for index, place in enumerate(record.get("places") or []):
                    pending_places.append((object_id, revision_id, index, dict(place)))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        for object_id, revision_id, index, place in pending_places:
            self._project_place(namespace, object_id, revision_id, index, place, policy, principal_id)
            counts["places"] += 1
        counts["matches"] = self._explicit_matches(namespace)
        return counts

    def _project_place(self, namespace, object_id, revision_id, index, place, policy, principal_id):
        from src.kb.geospatial import GeospatialError

        role = place.get("role") if place.get("role") in PLACE_ROLES else "depicted"
        link_id = "cultural-place:" + _digest([revision_id, index])[:24]
        coordinates = place.get("coordinates")
        state, geometry_id, place_id, resolution_id, transform, basis = "name-only", None, None, None, None, None
        if coordinates:
            try:
                crs = str(coordinates.get("crs") or "EPSG:4326")
                if crs == "EPSG:4326":
                    point = {"type": "Point", "coordinates": [float(coordinates["lon"]), float(coordinates["lat"])]}
                else:
                    # Projected source coordinates are x (easting) and y (northing).
                    from src.integrations.spatial import transform_geometry

                    x = float(coordinates["x"] if "x" in coordinates else coordinates["lon"])
                    y = float(coordinates["y"] if "y" in coordinates else coordinates["lat"])
                    transformed = transform_geometry({"type": "Point", "coordinates": [x, y]}, crs)
                    point = {"type": "Point", "coordinates": list(transformed["result"]["geometry"]["coordinates"])}
                    transform = {"source_crs": crs, "receipt_sha256": transformed["sha256"],
                                 "producer": transformed["producer"],
                                 "accuracy_m": transformed["result"].get("accuracy_m")}
                if not (-90 <= point["coordinates"][1] <= 90 and -180 <= point["coordinates"][0] <= 180):
                    raise ValueError("coordinates out of range")
                declared = coordinates.get("precision_m")
                precision = float(declared if declared is not None else policy.get("unspecified_precision_m", 1000))
                basis = "provider-declared" if declared is not None else "provider-unspecified; pinned default"
                stored = self.geo.store_geometry(
                    namespace, point, place_id=None, crs="EPSG:4326", precision_m=precision, simplified_from=None,
                    disputed=False, admin_hierarchy=[],
                    source={"kind": "cultural-object", "object_id": object_id, "revision_id": revision_id,
                            "role": role, "precision_basis": basis},
                    evidence=[{"object_id": object_id, "revision_id": revision_id, "original": coordinates}],
                    principal_id=principal_id, scopes=GEOSPATIAL_SCOPES)
                geometry_id, state = stored["geometry_id"], "coordinates-projected"
            except (ValueError, KeyError, TypeError, GeospatialError) as exc:
                state = "coordinates-rejected"
                transform = {"error": type(exc).__name__, "detail": str(exc)[:200]}
        if place.get("name") and state in {"name-only", "coordinates-rejected"}:
            resolution = self.geo.resolve(namespace, str(place["name"]), scopes={"knowledge:geospatial:read"})
            if resolution["status"] == "resolved":
                place_id, state = resolution["selected_place_id"], "name-resolved"
            elif resolution["status"] == "ambiguous":
                self.geo.save_resolution(resolution, principal_id=principal_id, scopes=GEOSPATIAL_SCOPES)
                resolution_id, state = resolution["resolution_id"], "needs-review"
            else:
                state = "unresolved"
        elif place.get("name") and geometry_id:
            resolution = self.geo.resolve(namespace, str(place["name"]), scopes={"knowledge:geospatial:read"})
            place_id = resolution["selected_place_id"]
        self.conn.execute(
            "INSERT OR IGNORE INTO cultural_places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [link_id, namespace, object_id, revision_id, role, place.get("name"), place.get("provider_place_id"),
             _canonical(coordinates) if coordinates else None, basis, state, geometry_id, place_id, resolution_id,
             _canonical(transform) if transform else None])

    # ---------------------------------------------------------------- reads

    def _current(self, namespace: str, object_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        row = self.conn.execute(
            "SELECT r.revision_id, r.record_json, r.rights_json FROM cultural_current c "
            "JOIN cultural_revisions r ON r.revision_id=c.revision_id WHERE c.namespace=? AND c.object_id=?",
            [namespace, object_id]).fetchone()
        if row is None:
            raise CulturalError("not_found", "cultural object is not visible in this namespace")
        return row[0], _load(row[1], {}), _load(row[2], {})

    def places(self, namespace: str, object_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT role, name, provider_place_id, original_json, precision_basis, state, geometry_id, place_id, "
            "resolution_id, transform_json FROM cultural_places WHERE namespace=? AND object_id=? "
            "AND revision_id=(SELECT revision_id FROM cultural_current WHERE object_id=?) ORDER BY place_link_id",
            [namespace, object_id, object_id]).fetchall()
        return [{"role": r[0], "name": r[1], "provider_place_id": r[2], "original": _load(r[3], None),
                 "precision_basis": r[4], "state": r[5], "geometry_id": r[6], "place_id": r[7],
                 "resolution_id": r[8], "transform": _load(r[9], None)} for r in rows]

    def object(self, namespace: str, object_id: str, *, scopes) -> dict[str, Any]:
        _authorize(namespace, scopes, READ_SCOPE, write=False)
        revision_id, record, rights = self._current(namespace, object_id)
        revisions = self.conn.execute(
            "SELECT revision_id, provider_revision, first_run_id, observed_at_ms FROM cultural_revisions "
            "WHERE object_id=? ORDER BY observed_at_ms, revision_id", [object_id]).fetchall()
        return {
            "contract": OBJECT_CONTRACT, "object_id": object_id, "revision_id": revision_id,
            "provider": record["provider"], "provider_record_id": record["provider_record_id"],
            "source_url": record["source_url"], "institution": record.get("institution"),
            "collection": record.get("collection"), "aggregation": record.get("aggregation"),
            "object_type": record.get("object_type"), "titles": record.get("titles"),
            "descriptions": record.get("descriptions"), "creators": record.get("creators"),
            "subjects": record.get("subjects"), "dates": record.get("dates"), "languages": record.get("languages"),
            "rights": rights, "representations": [
                {**rep, "rights_policy": rights_policy(rep.get("rights"))} for rep in record.get("representations") or []],
            "places": self.places(namespace, object_id),
            "revisions": [dict(zip(("revision_id", "provider_revision", "first_run_id", "observed_at_ms"), r))
                          for r in revisions],
            "matches": self._matches_for(namespace, object_id),
            "research_links": self.research_links(namespace, object_id=object_id),
            "notice": "Provider metadata; not a historical interpretation. Rights govern each representation.",
        }

    def search(self, namespace: str, *, scopes, place_id: str | None = None, geometry_ids: Sequence[str] | None = None,
               collection: str | None = None, creator: str | None = None, subject: str | None = None,
               date_from: str | None = None, date_to: str | None = None, provider: str | None = None,
               limit: int = 25) -> dict[str, Any]:
        """Bounded object search by place, collection, creator, subject and declared date range."""

        _authorize(namespace, scopes, READ_SCOPE, write=False)
        limit = max(1, min(int(limit), 100))
        rows = self.conn.execute(
            "SELECT o.object_id FROM cultural_objects o JOIN cultural_current c USING(object_id) "
            "WHERE o.namespace=? AND (? IS NULL OR o.provider=?) ORDER BY o.provider, o.provider_record_id",
            [namespace, provider, provider]).fetchall()
        results = []
        for (object_id,) in rows:
            _, record, _ = self._current(namespace, object_id)
            places = self.places(namespace, object_id)
            if place_id and not any(p["place_id"] == place_id for p in places):
                continue
            if geometry_ids is not None and not any(p["geometry_id"] in set(geometry_ids) for p in places):
                continue
            if collection and not any(collection.casefold() in str(c).casefold()
                                      for c in dict(record.get("collection") or {}).get("hierarchy") or []):
                continue
            if creator and not any(creator.casefold() in c["name"].casefold() for c in record.get("creators") or []):
                continue
            if subject and not any(subject.casefold() in s.casefold() for s in record.get("subjects") or []):
                continue
            if date_from or date_to:
                spans = [d["normalized"] for d in record.get("dates") or [] if d.get("normalized")]
                lo, hi = (date_from or "0000")[:4], (date_to or "9999")[:4]
                if not any(s["start"][:4] <= hi and s["end"][:4] >= lo for s in spans):
                    continue
            results.append(self.object(namespace, object_id, scopes=scopes))
            if len(results) > limit:
                break
        return {"namespace": namespace, "count": min(len(results), limit), "truncated": len(results) > limit,
                "objects": results[:limit],
                "coverage_notice": "Selected provider records only; undated or unplaced objects never match a "
                                   "date or place filter."}

    # --------------------------------------------------------------- matches

    def _explicit_matches(self, namespace: str) -> int:
        """Record provider sameAs links (Europeana shown at a DDB item) as identifier matches."""

        rows = self.conn.execute(
            "SELECT c.object_id, r.record_json FROM cultural_current c JOIN cultural_revisions r "
            "ON r.revision_id=c.revision_id WHERE c.namespace=?", [namespace]).fetchall()
        by_key = {}
        for object_id, record_json in rows:
            record = _load(record_json, {})
            by_key[f"{record['provider']}:{record['provider_record_id']}"] = object_id
        created = 0
        for object_id, record_json in rows:
            for target in _load(record_json, {}).get("same_as") or []:
                other = by_key.get(target)
                if other and other != object_id:
                    created += self._candidate(namespace, object_id, other, "explicit_identifier", "identifier-match",
                                               [{"kind": "same_as", "value": target}])
        return created

    def _candidate(self, namespace, left, right, basis, state, evidence) -> int:
        a, b = sorted([left, right])
        match_id = "cultural-match:" + _digest([namespace, a, b])[:24]
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO cultural_matches VALUES (?,?,?,?,?,?,?,?) RETURNING match_id",
            [match_id, namespace, a, b, basis, state, _canonical(evidence), self.now()]).fetchall()
        return len(inserted)

    def propose_matches(self, namespace: str, *, scopes) -> dict[str, Any]:
        """Conservative candidates: same normalized title, overlapping date and same institution."""

        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        rows = self.conn.execute(
            "SELECT c.object_id, r.record_json FROM cultural_current c JOIN cultural_revisions r "
            "ON r.revision_id=c.revision_id WHERE c.namespace=? ORDER BY c.object_id", [namespace]).fetchall()
        records = [(object_id, _load(record_json, {})) for object_id, record_json in rows]

        def fold(value: Any) -> str:
            return re.sub(r"\W+", " ", str(value or "")).strip().casefold()

        for i, (left, a) in enumerate(records):
            for right, b in records[i + 1:]:
                if a["provider"] == b["provider"]:
                    continue
                titles_a = {fold(t["value"]) for t in a.get("titles") or []}
                titles_b = {fold(t["value"]) for t in b.get("titles") or []}
                if not titles_a & titles_b:
                    continue
                evidence, conflicts = [{"kind": "title", "values": sorted(titles_a & titles_b)}], []
                inst_a, inst_b = fold(dict(a.get("institution") or {}).get("name")), fold(
                    dict(b.get("institution") or {}).get("name"))
                if inst_a and inst_b:
                    (evidence if inst_a == inst_b else conflicts).append({"kind": "institution", "left": inst_a,
                                                                          "right": inst_b})
                years_a = {d["normalized"]["start"][:4] for d in a.get("dates") or [] if d.get("normalized")}
                years_b = {d["normalized"]["start"][:4] for d in b.get("dates") or [] if d.get("normalized")}
                if years_a and years_b:
                    (evidence if years_a & years_b else conflicts).append({"kind": "date", "left": sorted(years_a),
                                                                           "right": sorted(years_b)})
                creators_a = {fold(c["name"]) for c in a.get("creators") or []}
                creators_b = {fold(c["name"]) for c in b.get("creators") or []}
                if creators_a and creators_b and not creators_a & creators_b:
                    conflicts.append({"kind": "creator", "left": sorted(creators_a), "right": sorted(creators_b)})
                state = "conflicting-candidate" if conflicts else "candidate"
                self._candidate(namespace, left, right, "multi-field", state, evidence + [{"conflicts": conflicts}])
        rows = self.conn.execute("SELECT match_id FROM cultural_matches WHERE namespace=? ORDER BY match_id",
                                 [namespace]).fetchall()
        return {"contract": MATCH_CONTRACT, "namespace": namespace,
                "matches": [self.match(namespace, r[0]) for r in rows]}

    def match(self, namespace: str, match_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT left_id, right_id, basis, candidate_state, evidence_json FROM cultural_matches "
            "WHERE namespace=? AND match_id=?", [namespace, match_id]).fetchone()
        if row is None:
            raise CulturalError("not_found", "match is not visible in this namespace")
        reviews = self.conn.execute(
            "SELECT sequence, decision, reason, principal_id FROM cultural_match_reviews WHERE match_id=? "
            "ORDER BY sequence", [match_id]).fetchall()
        history = [dict(zip(("sequence", "decision", "reason", "principal_id"), r)) for r in reviews]
        state = history[-1]["decision"] if history else ("accepted" if row[3] == "identifier-match" else "unreviewed")
        return {"contract": MATCH_CONTRACT, "match_id": match_id, "left_object_id": row[0], "right_object_id": row[1],
                "basis": row[2], "candidate_state": row[3], "evidence": _load(row[4], []), "review_state": state,
                "review_history": history,
                "notice": "Provider records, rights and revisions stay separate even when a match is accepted."}

    def review_match(self, namespace: str, match_id: str, decision: str, reason: str, *, scopes,
                     principal_id: str) -> dict[str, Any]:
        _authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in DECISIONS or not str(reason or "").strip():
            raise CulturalError("invalid_decision", "decide accepted, rejected or deferred with a reason")
        current = self.match(namespace, match_id)
        self.conn.execute("INSERT INTO cultural_match_reviews VALUES (?,?,?,?,?,?)",
                          [match_id, len(current["review_history"]) + 1, decision, reason.strip(), principal_id,
                           self.now()])
        return self.match(namespace, match_id)

    def _matches_for(self, namespace: str, object_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT match_id FROM cultural_matches WHERE namespace=? AND (left_id=? OR right_id=?) ORDER BY match_id",
            [namespace, object_id, object_id]).fetchall()
        return [self.match(namespace, r[0]) for r in rows]

    # -------------------------------------------------------- research links

    def link_research(self, namespace: str, object_id: str, work_kind: str, work_id: str, basis: str,
                      evidence: str, *, scopes, principal_id: str) -> dict[str, Any]:
        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if basis not in LINK_BASES:
            raise CulturalError("invalid_basis", f"basis must be one of {sorted(LINK_BASES)}; keyword overlap "
                                                 "is a candidate, not a link")
        if not str(evidence or "").strip():
            raise CulturalError("invalid_request", "cite the citation, provider relation or review")
        revision_id, _, _ = self._current(namespace, object_id)
        link_id = "cultural-link:" + _digest([namespace, object_id, work_kind, work_id])[:24]
        self.conn.execute(
            "INSERT INTO cultural_research_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (link_id) DO UPDATE "
            "SET state='linked', basis=excluded.basis, evidence=excluded.evidence, "
            "reviewed_by=excluded.principal_id, reviewed_at_ms=excluded.created_at_ms",
            [link_id, namespace, object_id, work_kind, work_id, "linked", basis, evidence.strip(), revision_id,
             principal_id, self.now(), None, None])
        return self.research_links(namespace, object_id=object_id, work_id=work_id)[0]

    def suggest_research_candidates(self, namespace: str, work_kind: str, work_id: str, text: str, *, scopes,
                                    principal_id: str, limit: int = 10) -> dict[str, Any]:
        """Keyword overlap between a work's text and object titles/subjects: candidates only."""

        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        words = {w for w in re.findall(r"\w{5,}", str(text).casefold())}
        scored = []
        for (object_id,) in self.conn.execute(
                "SELECT object_id FROM cultural_current WHERE namespace=? ORDER BY object_id", [namespace]).fetchall():
            revision_id, record, _ = self._current(namespace, object_id)
            terms = {w for t in (record.get("titles") or []) for w in re.findall(r"\w{5,}", t["value"].casefold())}
            terms |= {w for s in record.get("subjects") or [] for w in re.findall(r"\w{5,}", s.casefold())}
            overlap = sorted(words & terms)
            if overlap:
                scored.append((len(overlap), object_id, revision_id, overlap))
        scored.sort(key=lambda item: (-item[0], item[1]))
        for _, object_id, revision_id, overlap in scored[:limit]:
            link_id = "cultural-link:" + _digest([namespace, object_id, work_kind, work_id])[:24]
            self.conn.execute(
                "INSERT OR IGNORE INTO cultural_research_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [link_id, namespace, object_id, work_kind, work_id, "candidate", "keyword-overlap",
                 _canonical(overlap), revision_id, principal_id, self.now(), None, None])
        return {"work_id": work_id, "candidates": [l for l in self.research_links(namespace, work_id=work_id)
                                                   if l["state"] == "candidate"],
                "notice": "Keyword overlap is a suggestion to review, not a relationship."}

    def review_research_candidate(self, namespace: str, link_id: str, decision: str, reason: str, *, scopes,
                                  principal_id: str) -> dict[str, Any]:
        _authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accepted", "rejected"} or not str(reason or "").strip():
            raise CulturalError("invalid_decision", "accept or reject a candidate with a reason")
        row = self.conn.execute("SELECT state FROM cultural_research_links WHERE namespace=? AND link_id=?",
                                [namespace, link_id]).fetchone()
        if row is None:
            raise CulturalError("not_found", "research link is not visible in this namespace")
        state, basis = ("linked", "reviewed_assertion") if decision == "accepted" else ("rejected", "reviewed_rejection")
        self.conn.execute(
            "UPDATE cultural_research_links SET state=?, basis=?, evidence=evidence || ?, reviewed_by=?, "
            "reviewed_at_ms=? WHERE link_id=?",
            [state, basis, " | review: " + reason.strip(), principal_id, self.now(), link_id])
        return next(l for l in self.research_links(namespace) if l["link_id"] == link_id)

    def research_links(self, namespace: str, *, object_id: str | None = None,
                       work_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT link_id, object_id, work_kind, work_id, state, basis, evidence, object_revision_id, principal_id, "
            "reviewed_by FROM cultural_research_links WHERE namespace=? AND (? IS NULL OR object_id=?) "
            "AND (? IS NULL OR work_id=?) ORDER BY state, link_id",
            [namespace, object_id, object_id, work_id, work_id]).fetchall()
        return [{"contract": LINK_CONTRACT, **dict(zip(("link_id", "object_id", "work_kind", "work_id", "state", "basis",
                                                        "evidence", "object_revision_id", "principal_id",
                                                        "reviewed_by"), r))} for r in rows]

    def primary_sources_for_work(self, namespace: str, work_kind: str, work_id: str, *, scopes) -> dict[str, Any]:
        """Start from a scholarly work: explicitly linked objects and where their places lead."""

        _authorize(namespace, scopes, READ_SCOPE, write=False)
        links = [l for l in self.research_links(namespace, work_id=work_id) if l["work_kind"] == work_kind]
        linked = [l for l in links if l["state"] == "linked"]
        objects = [self.object(namespace, l["object_id"], scopes=scopes) for l in linked]
        return {"work": {"kind": work_kind, "id": work_id},
                "primary_sources": [{"link": l, "object": o} for l, o in zip(linked, objects, strict=True)],
                "candidates": [l for l in links if l["state"] == "candidate"],
                "geometry_ids": sorted({p["geometry_id"] for o in objects for p in o["places"] if p["geometry_id"]}),
                "place_ids": sorted({p["place_id"] for o in objects for p in o["places"] if p["place_id"]})}

    # ---------------------------------------------------------------- assets

    def acquire_assets(self, namespace: str, object_id: str, *, scopes, principal_id: str, action: str = "store",
                       purpose: str = "research", allowed_hosts: Sequence[str] = (), max_bytes: int = 10_000_000,
                       transport: Callable[..., Mapping[str, Any]] | None = None) -> dict[str, Any]:
        """Fetch representations only when their rights permit the action; otherwise record link-only."""

        _authorize(namespace, scopes, WRITE_SCOPE, write=True)
        from urllib.parse import urlsplit

        from src.ingestion.source_pack_runtime import HTTPSPageAdapter
        from src.ingestion.source_packs import SourcePackError, _validate_endpoint

        if action not in {"store", "export"}:
            raise CulturalError("invalid_action", "action is store or export")
        revision_id, record, _ = self._current(namespace, object_id)
        if transport is None:
            from functools import partial

            transport = partial(HTTPSPageAdapter._request, max_bytes=max_bytes)
        hosts = {h.casefold() for h in allowed_hosts}
        results = []
        for rep in record.get("representations") or []:
            policy = rights_policy(rep.get("rights"), purpose=purpose)
            allowed = policy["allows"]["store_asset" if action == "store" else "export_asset"]
            host = (urlsplit(rep["url"]).hostname or "").casefold()
            outcome: dict[str, Any]
            if not allowed:
                outcome = {"state": "link-only", "reason": f"rights {policy['category']} do not permit {action}"}
            elif host not in hosts:
                outcome = {"state": "link-only", "reason": f"host {host} is not an allowed asset host"}
            else:
                try:
                    _validate_endpoint(rep["url"], "cultural-asset")
                    response = transport(url=rep["url"], params={}, headers={}, timeout=30)
                    status = int(response.get("status", 200))
                    content = response.get("content", b"")
                    raw = content.encode() if isinstance(content, str) else bytes(content)
                    if status == 410 or status == 403:
                        outcome = {"state": "unavailable", "reason": f"http_{status} (expired or withdrawn URL)"}
                    elif status >= 400:
                        outcome = {"state": "unavailable", "reason": f"http_{status}"}
                    elif len(raw) > max_bytes:
                        outcome = {"state": "too-large", "reason": "response_too_large"}
                    else:
                        media = str(dict(response.get("headers") or {}).get("Content-Type") or "").split(";")[0] or None
                        outcome = {"state": "retained", "content_sha256": hashlib.sha256(raw).hexdigest(),
                                   "media_type": media, "bytes": len(raw)}
                except SourcePackError as exc:
                    outcome = {"state": "unavailable", "reason": exc.code}
            now = self.now()
            fetch_id = "cultural-asset:" + _digest([object_id, rep["url"], action, outcome, now])[:24]
            self.conn.execute(
                "INSERT INTO cultural_assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [fetch_id, namespace, object_id, revision_id, rep["role"], rep["url"], action, outcome["state"],
                 outcome.get("reason"), _canonical(policy), outcome.get("content_sha256"), outcome.get("media_type"),
                 outcome.get("bytes"), principal_id, now])
            results.append({"contract": ASSET_CONTRACT, "role": rep["role"], "url": rep["url"], "action": action,
                            "rights": policy, **outcome})
        return {"object_id": object_id, "revision_id": revision_id, "assets": results,
                "notice": "Metadata stays available whether or not an asset may be retained."}


class CulturalProjector:
    """Source-pack runtime projector for ``noesis-cultural-object-v1``."""

    def __init__(self, conn: Any) -> None:
        self.store = CulturalStore(conn)

    @staticmethod
    def _namespace(source: Mapping[str, Any]) -> str:
        return str(dict(source.get("cultural") or {}).get("namespace") or DEFAULT_NAMESPACE)

    def project_page(self, *, run_id, manifest, source, records, documents, page_receipt, principal_id):
        del manifest, page_receipt
        document_ids = {str(dict(d.get("metadata") or {}).get("source_pack_record_id")): str(d["document_id"])
                        for d in documents}
        return self.store.observe_page(self._namespace(source), records, run_id=run_id, source=source,
                                       documents=document_ids, principal_id=principal_id)

    def finish_source(self, *, run_id, manifest, source, status, principal_id):
        del run_id, manifest, principal_id
        return {"status": status}


def readiness(conn: Any, *, pack_id: str = "primary-scientific-evidence",
              secrets: Callable[[str], str | None] | None = None) -> dict[str, Any]:
    import os

    from src.ingestion.cultural_sources import PROVIDER_CONTRACTS

    lookup = secrets or (lambda name: os.environ.get(name))
    try:
        row = conn.execute(
            "SELECT c.enabled,v.manifest_json FROM source_pack_current c JOIN source_pack_versions v "
            "ON v.pack_id=c.pack_id AND v.version=c.version WHERE c.pack_id=?", [pack_id]).fetchone()
    except Exception:  # noqa: BLE001 - runtime tables absent until first install
        row = None
    manifest = _load(row[1], {}) if row else {}
    providers = {}
    for provider, contract in PROVIDER_CONTRACTS.items():
        sources = [s for s in manifest.get("sources") or [] if s["connector"] == provider]
        blockers = []
        if not sources:
            blockers.append({"code": "source_not_installed", "severity": "blocking"})
        elif not (row and row[0]):
            blockers.append({"code": "pack_disabled", "severity": "blocking"})
        for source in sources:
            ref = dict(source.get("auth") or {}).get("secret_ref")
            if ref and not lookup(ref):
                blockers.append({"code": "credential_missing", "secret_ref": ref, "severity": "blocking-live"})
        providers[provider] = {"sources": [s["source_id"] for s in sources],
                               "fixture": "ready" if sources and row and row[0] else "blocked",
                               "live": "ready" if sources and not blockers else "blocked",
                               "live_verification": contract["status"], "blockers": blockers}
    return {"pack_id": pack_id, "providers": providers,
            "notice": "Cultural sources extend the scientific source pack; no separate domain pack exists."}
