"""Align commodities and places across agri-food sources through reviewable identity (#2213, AF07 #2353).

**Commodities.** Each publisher keeps its own code (FAOSTAT item, NASS
commodity, PSD commodity, Eurostat crop or product, portal product). Each code
list is published as an ontology module through
:class:`src.kb.ontology.OntologyAlignmentStore` (``agrifood-<scheme>``), and codes
of different publishers are connected only by **crosswalk records** with a kind
from the ontology's mapping kinds (``equivalent``, ``broader``, ``narrower``),
evidence and a review state:

* automatic proposals are ``equivalent`` candidates between two codes whose
  published labels share an exact name (the label itself, or a name the label
  states in parentheses, e.g. "Maize (corn)" states "maize" and "corn");
* ``broader`` / ``narrower`` mappings are a reviewer's explicit proposals with
  stated evidence;
* every mapping is ``proposed`` until reviewed; it can be accepted, rejected
  and reverted, and its history is kept.

Queries expand a commodity only through **accepted** mappings: equivalents
transitively, broader/narrower one hop and never chained, and each reached
code carries the mapping that connected it. Codes connected by a broader or
narrower mapping are differently defined commodities: their series are
returned beside each other with that kind and are never aggregated.

**Places.** The pack's places are registered once as ``geospatial_places``
through :class:`src.kb.geospatial.GeospatialStore` with the codes each source
uses as source identifiers (FAO area, ISO 3166-1, US state/county FIPS, PSD
country, Eurostat geo, portal member state). A place query (a code or a name
in :data:`PLACES`) resolves to the one geospatial place carrying it, recorded
as a ``geocode_resolutions`` row; no new spatial store is created.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.kb.agrifood_records import (
    CROSSWALK_CONTRACT,
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    AgrifoodError,
    authorize,
    canonical,
    digest,
    label_key,
    require,
)
from src.kb.agrifood_store import AgrifoodStore, table_exists

KINDS = ("equivalent", "broader", "narrower")
# Classification schemes other packs cite (trade flows by HS/CN/CPC heading); a reviewer may map an acquired code to
# one of them, so a trade record citing that heading reaches the commodity through the reviewed mapping only.
EXTERNAL_SCHEMES = ("hs", "cn", "cpc")
INVERSE = {"equivalent": "equivalent", "broader": "narrower", "narrower": "broader"}
OWNER = "agrifood.core"
GEO_READ, GEO_WRITE = "knowledge:geospatial:read", "knowledge:geospatial:write"
SCHEMA_READ, SCHEMA_REGISTER = "knowledge:schema:read", "knowledge:schema:register"
# The pack's bounded places with every code a source uses for them (docs/roadmaps/agrifood-source-audit.md).
PLACES: dict[str, dict[str, Any]] = {
    "us": {"name": "United States", "type": "country", "names": ["United States", "United States of America", "USA"],
           "codes": {"iso3166-1": "US", "fao-area": "231", "psd-country": "US"}},
    "fr": {"name": "France", "type": "country", "names": ["France"],
           "codes": {"iso3166-1": "FR", "fao-area": "68", "eurostat-geo": "FR", "eu-member-state": "FR"}},
    "de": {"name": "Germany", "type": "country", "names": ["Germany"],
           "codes": {"iso3166-1": "DE", "fao-area": "79", "eurostat-geo": "DE", "eu-member-state": "DE"}},
    "eu": {"name": "European Union", "type": "union", "names": ["European Union", "EU"],
           "codes": {"psd-country": "E4", "eurostat-geo": "EU27_2020"}},
    "us-ia": {"name": "Iowa", "type": "state", "names": ["Iowa"], "parent": "us", "codes": {"us-fips": "19"}},
    "us-ia-story": {"name": "Story County, Iowa", "type": "county", "names": ["Story County, Iowa", "Story County"],
                    "parent": "us-ia", "codes": {"us-fips": "19169"}},
}
_DDL = """
CREATE TABLE IF NOT EXISTS agrifood_crosswalks (
  namespace TEXT NOT NULL, crosswalk_id TEXT NOT NULL, left_scheme TEXT NOT NULL, left_code TEXT NOT NULL,
  right_scheme TEXT NOT NULL, right_code TEXT NOT NULL, kind TEXT NOT NULL, evidence_json TEXT NOT NULL,
  origin TEXT NOT NULL, state TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  history_json TEXT NOT NULL, PRIMARY KEY(namespace, crosswalk_id)
);
"""


def label_names(label: Any) -> list[str]:
    """Exact names a published label states: the label, its text before and inside parentheses."""
    text = str(label or "").strip()
    if not text:
        return []
    names = [text]
    match = re.fullmatch(r"(.+?)\s*\((.+)\)", text)
    if match:
        names += [match.group(1).strip(), *[p.strip() for p in match.group(2).split(",")]]
    return [n for n in dict.fromkeys(names) if len(n) >= 3]


def parse_code(value: Any) -> tuple[str, str] | None:
    text = str(value or "").strip()
    if ":" in text and re.fullmatch(r"[a-z0-9-]+:.+", text):
        scheme, code = text.split(":", 1)
        return scheme, code
    return None


class AgrifoodIdentity:
    def __init__(self, conn: Any, *, initialize: bool = True, now=None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = AgrifoodStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return table_exists(self.conn, "agrifood_crosswalks")

    # ------------------------------------------------------------------ code lists

    def publish_codelists(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Each publisher's acquired commodity codes as an ontology module (``agrifood-<scheme>``); idempotent."""
        from src.kb.ontology import OntologyAlignmentStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require(scopes, SCHEMA_REGISTER)
        ontology = OntologyAlignmentStore(self.conn, now=self.now)
        by_scheme: dict[str, dict[str, dict[str, Any]]] = {}
        for item in self.store.commodities(namespace):
            concept = by_scheme.setdefault(item["scheme"], {}).setdefault(item["code"], {
                "concept_id": item["code"], "labels": [], "definition": "", "broader": []})
            label = item["label"] or item["code"]
            if {"value": label, "language": "en", "kind": "preferred"} not in concept["labels"]:
                concept["labels"].append({"value": label, "language": "en", "kind": "preferred"})
            concept["definition"] = f"{item['scheme']} code {item['code']} as published by {item['provider']}"
        published = {}
        for scheme, concepts in sorted(by_scheme.items()):
            items = [concepts[k] for k in sorted(concepts)]
            module = ontology.publish(
                f"agrifood-{scheme}", "1.0.0", items, owner=OWNER,
                provenance={"kind": "imported", "source": f"{scheme} codes and labels as published"},
                idempotency_key=f"agrifood-{scheme}:{digest(items)[:16]}", principal_id=principal_id,
                scopes={SCHEMA_REGISTER, SCHEMA_READ}, compatibility_policy="none")
            published[scheme] = {"module": f"agrifood-{scheme}", "module_id": module["module_id"],
                                 "concepts": len(items)}
        return {"namespace": namespace, "codelists": published}

    # ------------------------------------------------------------------ crosswalks

    def _labels(self, namespace: str) -> dict[tuple[str, str], dict[str, Any]]:
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for item in self.store.commodities(namespace):
            entry = out.setdefault((item["scheme"], item["code"]), {"labels": set(), "providers": set()})
            if item["label"]:
                entry["labels"].add(item["label"])
            entry["providers"].add(item["provider"])
        return out

    def _offer(self, namespace, left, right, kind, evidence, *, origin, principal_id) -> dict[str, Any]:
        if kind not in KINDS:
            raise AgrifoodError("invalid_crosswalk", f"kind is one of {KINDS}")
        from src.kb.ontology import MAPPING_KINDS

        if kind not in MAPPING_KINDS:  # the ontology's own mapping vocabulary
            raise AgrifoodError("invalid_crosswalk", "kind is not an ontology mapping kind")
        crosswalk_id = "agrifood-xw:" + digest([namespace, *sorted([list(left), list(right)])])[:24]
        row = self.conn.execute("SELECT state, history_json FROM agrifood_crosswalks WHERE namespace=? AND "
                                "crosswalk_id=?", [namespace, crosswalk_id]).fetchone()
        now = self.now()
        if row is None:
            self.conn.execute(
                "INSERT INTO agrifood_crosswalks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [namespace, crosswalk_id, left[0], left[1], right[0], right[1], kind, canonical(evidence), origin,
                 "proposed", principal_id, now,
                 canonical([{"state": "proposed", "by": principal_id, "at_ms": now, "kind": kind}])])
            return {"crosswalk_id": crosswalk_id, "change": "created"}
        if origin == "manual" and row[0] in {"rejected", "reverted", "proposed"}:
            history = json.loads(row[1]) + [{"state": "proposed", "by": principal_id, "at_ms": now, "kind": kind,
                                             "previous_state": row[0]}]
            self.conn.execute(
                "UPDATE agrifood_crosswalks SET left_scheme=?, left_code=?, right_scheme=?, right_code=?, kind=?, "
                "evidence_json=?, origin=?, state='proposed', history_json=? WHERE namespace=? AND crosswalk_id=?",
                [left[0], left[1], right[0], right[1], kind, canonical(evidence), origin, canonical(history),
                 namespace, crosswalk_id])
            return {"crosswalk_id": crosswalk_id, "change": "reproposed"}
        return {"crosswalk_id": crosswalk_id, "change": None}

    def propose(self, namespace: str, *, principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """Equivalent candidates between codes of different schemes whose labels state the same exact name."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        labels = self._labels(namespace)
        names: dict[str, dict[tuple[str, str], str]] = {}
        for code, entry in labels.items():
            for label in entry["labels"]:
                for name in label_names(label):
                    names.setdefault(label_key(name), {})[code] = label
        offered = []
        keys = sorted(labels)
        for i, left in enumerate(keys):
            for right in keys[i + 1:]:
                if left[0] == right[0]:
                    continue
                shared = sorted(k for k, codes in names.items() if left in codes and right in codes)
                if not shared:
                    continue
                evidence = {"rule": "exact-published-name", "names": shared,
                            "left": {"scheme": left[0], "code": left[1], "labels": sorted(labels[left]["labels"])},
                            "right": {"scheme": right[0], "code": right[1], "labels": sorted(labels[right]["labels"])},
                            "note": "a proposal; the codes stay separate until a reviewer accepts it"}
                offered.append(self._offer(namespace, left, right, "equivalent", evidence, origin="automatic",
                                           principal_id=principal_id))
        return {"proposed": sorted(o["crosswalk_id"] for o in offered if o["change"]),
                "crosswalks": self.crosswalks(namespace, scopes=scopes)}

    def propose_manual(self, namespace: str, left: str, right: str, kind: str, evidence: str, *,
                       principal_id: str, scopes: Iterable[str]) -> dict[str, Any]:
        """A reviewer's explicit mapping: ``left`` is ``kind`` of ``right`` (e.g. wheat broader than milling wheat)."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        a, b = parse_code(left), parse_code(right)
        labels = self._labels(namespace)
        known = [x is not None and (x in labels or x[0] in EXTERNAL_SCHEMES) for x in (a, b)]
        if not all(known) or a[0] == b[0] or (a[0] in EXTERNAL_SCHEMES and b[0] in EXTERNAL_SCHEMES):
            raise AgrifoodError("not_found", "both codes must be commodity codes of different schemes (scheme:code): "
                                             f"acquired codes, or one acquired code and a {EXTERNAL_SCHEMES} heading")
        if not str(evidence or "").strip():
            raise AgrifoodError("invalid_crosswalk", "a manual mapping states its evidence")
        record = {"rule": "reviewer-stated", "stated": evidence.strip(),
                  "left": {"scheme": a[0], "code": a[1], "labels": sorted(labels.get(a, {}).get("labels", set()))},
                  "right": {"scheme": b[0], "code": b[1], "labels": sorted(labels.get(b, {}).get("labels", set()))}}
        result = self._offer(namespace, a, b, kind, record, origin="manual", principal_id=principal_id)
        return self.crosswalk(namespace, result["crosswalk_id"], scopes=scopes)

    def _row(self, namespace: str, crosswalk_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT crosswalk_id, left_scheme, left_code, right_scheme, right_code, kind, evidence_json, origin, state, "
            "created_by, created_at_ms, history_json FROM agrifood_crosswalks WHERE namespace=? AND crosswalk_id=?",
            [namespace, crosswalk_id]).fetchone() if self._ready() else None
        if row is None:
            raise AgrifoodError("not_found", "crosswalk is not visible in this namespace")
        history = json.loads(row[11])
        reviewed = [h for h in history if h["state"] in {"accepted", "rejected", "reverted"}]
        return {"contract": CROSSWALK_CONTRACT, "namespace": namespace, "crosswalk_id": row[0],
                "left": {"scheme": row[1], "code": row[2]}, "right": {"scheme": row[3], "code": row[4]},
                "kind": row[5], "evidence": json.loads(row[6]), "origin": row[7], "state": row[8],
                "created_by": row[9], "created_at_ms": int(row[10]), "history": history,
                "reviewer": reviewed[-1]["by"] if reviewed and row[8] != "proposed" else None,
                "notice": "a crosswalk joins publisher codes for queries only; records and series stay separate"}

    def crosswalk(self, namespace: str, crosswalk_id: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        return self._row(namespace, crosswalk_id)

    def crosswalks(self, namespace: str, *, scopes: Iterable[str], state: str | None = None,
                   code: str | None = None) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute("SELECT crosswalk_id FROM agrifood_crosswalks WHERE namespace=? AND "
                                 "(? IS NULL OR state=?) ORDER BY crosswalk_id", [namespace, state, state]).fetchall()
        items = [self._row(namespace, r[0]) for r in rows]
        wanted = parse_code(code) if code else None
        return [c for c in items if wanted is None or wanted in {(c["left"]["scheme"], c["left"]["code"]),
                                                                 (c["right"]["scheme"], c["right"]["code"])}]

    def _transition(self, namespace, crosswalk, state, principal_id, reason) -> dict[str, Any]:
        history = crosswalk["history"] + [{"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}]
        self.conn.execute("UPDATE agrifood_crosswalks SET state=?, history_json=? WHERE namespace=? AND crosswalk_id=?",
                          [state, canonical(history), namespace, crosswalk["crosswalk_id"]])
        return self._row(namespace, crosswalk["crosswalk_id"])

    def review(self, namespace: str, crosswalk_id: str, decision: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise AgrifoodError("invalid_decision", "accept or reject with a reason")
        crosswalk = self._row(namespace, crosswalk_id)
        if crosswalk["state"] != "proposed":
            raise AgrifoodError("invalid_state", f"crosswalk is {crosswalk['state']}; propose again to re-review")
        return self._transition(namespace, crosswalk, "accepted" if decision == "accept" else "rejected",
                                principal_id, reason.strip())

    def revert(self, namespace: str, crosswalk_id: str, reason: str, *, principal_id: str,
               scopes: Iterable[str]) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise AgrifoodError("invalid_decision", "a revert needs a reason")
        crosswalk = self._row(namespace, crosswalk_id)
        if crosswalk["state"] not in {"accepted", "rejected"}:
            raise AgrifoodError("invalid_state", "only an accepted or rejected crosswalk can be reverted")
        return self._transition(namespace, crosswalk, "reverted", principal_id, reason.strip())

    # ------------------------------------------------------------------ resolution

    def expand(self, namespace: str, scheme: str, code: str) -> list[dict[str, Any]]:
        """Codes reached from one code through accepted mappings, each with the mapping that connected it."""
        accepted = [c for c in self.crosswalks(namespace, scopes={"operator"}, state="accepted")]
        edges: dict[tuple[str, str], list[tuple[tuple[str, str], str, dict[str, Any]]]] = {}
        for c in accepted:
            a, b = (c["left"]["scheme"], c["left"]["code"]), (c["right"]["scheme"], c["right"]["code"])
            edges.setdefault(a, []).append((b, c["kind"], c))  # a is kind of b
            edges.setdefault(b, []).append((a, INVERSE[c["kind"]], c))
        start = (scheme, code)
        reached = {start: {"scheme": scheme, "code": code, "match": "query", "via": []}}
        frontier = [start]
        while frontier:  # equivalents, transitively
            node = frontier.pop()
            for other, kind, c in edges.get(node, []):
                if kind == "equivalent" and other not in reached:
                    reached[other] = {"scheme": other[0], "code": other[1], "match": "equivalent",
                                      "via": reached[node]["via"] + [_cite(c)]}
                    frontier.append(other)
        for node in list(reached):
            for other, kind, c in edges.get(node, []):
                if kind != "equivalent" and other not in reached:
                    # The query commodity is ``kind`` of the other code: the other is differently defined.
                    reached[other] = {"scheme": other[0], "code": other[1],
                                      "match": "broader" if kind == "narrower" else "narrower",
                                      "via": reached[node]["via"] + [_cite(c)],
                                      "notice": "differently defined commodity: shown beside, never aggregated"}
        return list(reached.values())

    def resolve_commodity(self, namespace: str, query: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """A ``scheme:code`` or an exact published name to acquired codes and every code accepted mappings reach."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        labels = self._labels(namespace)
        parsed = parse_code(query)
        if parsed is not None:
            entry = [parsed] if parsed in labels else []
        else:
            wanted = label_key(query)
            entry = sorted(code for code, e in labels.items()
                           if any(label_key(n) == wanted for label in e["labels"] for n in label_names(label))
                           or label_key(code[1]) == wanted)
        codes: dict[tuple[str, str], dict[str, Any]] = {}
        for scheme, code in entry:
            for item in self.expand(namespace, scheme, code):
                key = (item["scheme"], item["code"])
                if key not in codes or (codes[key]["match"] not in {"query", "equivalent"}
                                        and item["match"] in {"query", "equivalent"}):
                    codes[key] = {**item, "match": "query" if key in entry else item["match"],
                                  "labels": sorted(labels.get(key, {}).get("labels", set()))}
        pending = [c for c in self.crosswalks(namespace, scopes=scopes, state="proposed")
                   if any((c[s]["scheme"], c[s]["code"]) in codes for s in ("left", "right"))]
        return {"namespace": namespace, "query": query, "status": "resolved" if codes else "not_found",
                "entry_codes": [{"scheme": s, "code": c} for s, c in entry],
                "codes": [codes[k] for k in sorted(codes)],
                "pending_crosswalks": [p["crosswalk_id"] for p in pending],
                "matching": "exact code or exact published name; other codes only through accepted crosswalks",
                "coverage_notice": "only the bounded, acquired commodity set is searched"}

    def unmapped(self, namespace: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Codes no accepted mapping connects to another scheme, and codes with only broader/narrower matches."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        items, partial = [], []
        for (scheme, code), entry in sorted(self._labels(namespace).items()):
            reached = [r for r in self.expand(namespace, scheme, code) if r["match"] != "query"]
            if not reached:
                items.append({"scheme": scheme, "code": code, "labels": sorted(entry["labels"])})
            elif all(r["match"] in {"broader", "narrower"} for r in reached):
                partial.append({"scheme": scheme, "code": code, "labels": sorted(entry["labels"]),
                                "partial_matches": [{"scheme": r["scheme"], "code": r["code"], "match": r["match"]}
                                                    for r in reached]})
        return {"namespace": namespace, "unmapped": items, "partial": partial,
                "notice": "unmapped codes are queried alone; partial matches are differently defined commodities"}

    # ------------------------------------------------------------------ places

    def register_places(self, *, principal_id: str, scopes: Iterable[str], geo_namespace: str = "global") -> dict:
        """Register the pack's bounded places as geospatial places carrying every source code; idempotent."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        require(scopes, GEO_WRITE)
        geo = GeospatialStore(self.conn, now=self.now)
        ids: dict[str, str] = {}
        for key in sorted(PLACES, key=lambda k: k.count("-")):
            spec = PLACES[key]
            parent = spec.get("parent")
            result = geo.register_place(
                geo_namespace, spec["name"], spec["type"],
                names=[{"value": n, "language": "en", "kind": "canonical" if n == spec["name"] else "alternative"}
                       for n in spec["names"]],
                source_ids=dict(spec["codes"]), parent_ids=[ids[parent]] if parent else [], principal_id=principal_id,
                scopes={GEO_WRITE}, place_key=f"agrifood:{key}", observed_at_ms=0,
                producer={"name": "noesis-agrifood-pack", "version": "1.0.0"},
                provenance={"source": "src.kb.agrifood_identity.PLACES (codes as each source publishes them)"})
            ids[key] = result["place_id"]
        return {"geo_namespace": geo_namespace, "places": ids}

    def _places_with(self, geo_namespace: str, scheme: str, code: str) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "geospatial_place_revisions"):
            return []
        rows = self.conn.execute(
            "SELECT p.place_id, p.namespace, r.source_ids_json FROM geospatial_places p JOIN geospatial_place_current c "
            "ON c.place_id=p.place_id JOIN geospatial_place_revisions r ON r.revision_id=c.revision_id "
            "WHERE p.namespace IN (?, 'global') ORDER BY p.place_id", [geo_namespace]).fetchall()
        out = []
        for place_id, namespace, source_ids in rows:
            codes = json.loads(source_ids or "{}")
            if str(codes.get(scheme) or "").strip().upper() == code.upper():
                out.append({"place_id": place_id, "namespace": namespace, "codes": codes})
        return out

    def resolve_place(self, namespace: str, query: str, *, scopes: Iterable[str], principal_id: str = "system",
                      geo_namespace: str = "global", save: bool = True) -> dict[str, Any]:
        """A place code (``scheme:code``) or a pack place name to the one geospatial place and all its codes."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        parsed = parse_code(query)
        if parsed is None:
            wanted = label_key(query)
            named = [k for k, spec in PLACES.items() if any(label_key(n) == wanted for n in spec["names"])]
            if len(named) != 1:
                return {"query": query, "status": "unresolved" if not named else "ambiguous", "codes": [],
                        "place_id": None, "note": "name not in the pack's bounded place list; give scheme:code"}
            parsed = next(iter(PLACES[named[0]]["codes"].items()))
        scheme, code = parsed
        places = self._places_with(geo_namespace, scheme, code)
        status = "resolved" if len(places) == 1 else "ambiguous" if places else "unresolved"
        request = {"namespace": geo_namespace, "mention": f"{scheme}:{code}",
                   "candidates": sorted(p["place_id"] for p in places)}
        input_hash = digest(request)
        resolution_id = "geocode-resolution:" + input_hash[:24]
        if save and (GEO_WRITE in scopes or "operator" in scopes):
            GeospatialStore(self.conn, now=self.now).save_resolution(
                {"resolution_id": resolution_id, "namespace": geo_namespace, "mention": f"{scheme}:{code}",
                 "context": {"system": scheme, "code": code, "producer": OWNER},
                 "candidates": [{"place_id": p["place_id"], "namespace": p["namespace"], "confidence": 1.0,
                                 "reasons": [f"{scheme}:{code}"]} for p in places],
                 "status": status, "selected_place_id": places[0]["place_id"] if status == "resolved" else None,
                 "confidence": 1.0 if status == "resolved" else 0.0, "evidence": [],
                 "method": {"name": "agrifood-code", "version": "1", "system": scheme}, "input_hash": input_hash},
                principal_id=principal_id, scopes={GEO_WRITE})
        codes = ([{"scheme": s, "code": str(c)} for s, c in sorted(places[0]["codes"].items())]
                 if status == "resolved" else [{"scheme": scheme, "code": code}])
        return {"query": query, "status": status, "place_id": places[0]["place_id"] if status == "resolved" else None,
                "resolution_id": resolution_id, "codes": codes,
                "method": "exact source code through geospatial.place-resolution; never a name similarity",
                **({"note": "no geospatial place carries this code; only series published under it are reached"}
                   if status == "unresolved" else {})}

    def unmapped_places(self, namespace: str, *, scopes: Iterable[str], geo_namespace: str = "global") -> dict:
        """Place codes of acquired series that no geospatial place carries."""
        authorize(namespace, set(scopes), READ_SCOPE)
        seen = sorted({(s["place_scheme"], s["place_code"]) for s in self.store.series_list(namespace)})
        missing = [{"scheme": s, "code": c} for s, c in seen if not self._places_with(geo_namespace, s, c)]
        return {"namespace": namespace, "unmapped": missing}


def _cite(crosswalk: Mapping[str, Any]) -> dict[str, Any]:
    return {"crosswalk_id": crosswalk["crosswalk_id"], "kind": crosswalk["kind"],
            "left": crosswalk["left"], "right": crosswalk["right"], "reviewer": crosswalk["reviewer"],
            "evidence": crosswalk["evidence"]}


__all__ = ["EXTERNAL_SCHEMES", "INVERSE", "KINDS", "PLACES", "AgrifoodIdentity", "label_names", "parse_code"]
