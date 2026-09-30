"""Objects, launch providers and sites through source-stated identifiers and reviewable matches (#2149, AS07).

Two kinds of connection, never mixed:

* **Source-stated links** are deterministic and computed at read time from the
  records in view (so they follow the as-of cutoff): MPC identifications and
  small-body designations (packed and unpacked forms normalise through
  :func:`src.kb.astronomy_records.designation_key`), JPL objects by the
  designations SBDB states (never by SPK-ID), exoplanet cross-identifiers the
  archive states (``toi`` on a planet row, ``kepler_name`` on a KOI), and
  COSPAR/NORAD pairs GCAT and SATCAT state identically. Noesis creates no
  identification.
* **Reviewable candidates** (``astronomy_identity_candidates``) for everything
  else: a TOI and a planet that only share a host identifier, a COSPAR/NORAD
  pair the two catalogues state differently, an organisation whose name equals
  a ``canonical_entities`` organisation, and similarly named organisations.
  A candidate stays ``proposed`` until a reviewer accepts or rejects it with a
  reason; the decision is an entity identity decision in
  :class:`src.kb.entity_history.EntityHistoryStore` (``match``/``non-match``),
  and a revert appends an ``undo``. ``similar-name`` candidates are shown and
  can never be accepted. A rejected or reverted candidate is proposed again
  only with new evidence, and nothing reactivates an earlier decision.

Launch sites resolve to Geospatial places reviewably: a saved
:meth:`src.kb.geospatial.GeospatialStore.resolve` result with nothing
selected, which the Geospatial owner's review accepts, rejects or defers.

The one equivalence (:func:`designation_group`, :func:`exoplanet_group`,
:func:`orbital_group`) is used by lookups, queries and monitors alike.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.astronomy_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    AstronomyError,
    authorize,
    canonical,
    designation_key,
    digest,
    object_name_key,
    require_scope,
)
from src.kb.astronomy_store import AstronomyStore

CONTRACT = "noesis-astronomy-identity-candidate-v1"
BASES = {
    "shared-host-identifier": 0.5,
    "catalogue-conflict": 0.5,
    "exact-name": 0.6,
    "similar-name": 0.1,
    # Space-object registration (#2224, SO07): identifiers two sources state differently, and objects a
    # registration names without a designator (src.kb.astronomy_registration_identity).
    "registration-identifier-conflict": 0.5,
    "registration-name-only": 0.3,
}
NEVER_ACCEPTED = frozenset({"similar-name"})
PREFIX = "astronomy:"
GEO_READ, GEO_WRITE, GEO_REVIEW = (
    "knowledge:geospatial:read",
    "knowledge:geospatial:write",
    "knowledge:geospatial:review",
)
_ENTITY_HISTORY_SCOPES = {
    "knowledge:entity-history:write",
    "knowledge:entity-history:review",
    "knowledge:entity-history:execute",
    "knowledge:entity-history:read",
}
_LEGAL_FORMS = {
    "inc",
    "corp",
    "corporation",
    "ltd",
    "llc",
    "plc",
    "co",
    "company",
    "gmbh",
    "ag",
    "sa",
    "sas",
    "srl",
    "bv",
    "nv",
    "limited",
    "the",
}
_DDL = """
CREATE TABLE IF NOT EXISTS astronomy_identity_candidates (
  namespace TEXT NOT NULL, candidate_id TEXT NOT NULL, left_key TEXT NOT NULL, right_key TEXT NOT NULL,
  basis TEXT NOT NULL, confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL, state TEXT NOT NULL,
  decision_id TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL,
  PRIMARY KEY(namespace, candidate_id)
);
CREATE TABLE IF NOT EXISTS astronomy_site_resolutions (
  namespace TEXT NOT NULL, site_code TEXT NOT NULL, geo_namespace TEXT NOT NULL, resolution_id TEXT NOT NULL,
  record_id TEXT NOT NULL, revision_id TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, site_code, resolution_id)
);
"""


def organisation_name_key(name: Any) -> str:
    """Organisation names compared without punctuation, case or trailing legal forms."""
    tokens = re.sub(
        r"[^0-9a-z ]+", " ", str(name or "").casefold().replace(".", "")
    ).split()
    while tokens and tokens[-1] in _LEGAL_FORMS:
        tokens.pop()
    return " ".join(tokens)


def _similar(a: str, b: str) -> bool:
    """Names sharing their first two words (a weak signal only)."""
    ta, tb = a.split(), b.split()
    return a != b and len(ta) >= 2 and len(tb) >= 2 and ta[:2] == tb[:2]


# ------------------------------------------------------------------ source-stated equivalences


class _Union:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, key: str) -> str:
        self.parent.setdefault(key, key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def group(self, key: str) -> set[str]:
        root = self.find(key)
        return {k for k in list(self.parent) if self.find(k) == root}


def designation_group(
    records: Iterable[Mapping[str, Any]], designation: Any
) -> set[str]:
    """Every designation the sources link with ``designation`` (unpacked keys), including itself."""
    start = designation_key(designation)
    if not start:
        return set()
    union = _Union()
    union.find(start)
    for record in records:
        kind = record["kind"]
        linked: list[Any] = []
        if kind == "identification":
            linked = [
                record["designation"],
                record["identified_with"],
                record.get("permanent"),
            ]
        elif kind == "small_body":
            linked = [
                record["primary_designation"],
                record.get("number") and str(record["number"]),
                record.get("name"),
                *(record.get("stated_designations") or []),
            ]
        elif kind == "designation":
            linked = [record["designation"], record.get("object_designation")]
        keys = [designation_key(v) for v in linked if v]
        keys = [k for k in keys if k]
        for other in keys[1:]:
            union.join(keys[0], other)
    return union.group(start)


# Planet-level cross-identifiers (host-level ones such as TIC, Gaia or Kepler IDs never link two planets).
_PLANET_SCHEMES = {"toi": "toi", "pl_name": "planet", "koi": "koi"}


def _planet_keys(record: Mapping[str, Any]) -> list[str]:
    kind = record["kind"]
    if kind == "exoplanet":
        keys = ["planet:" + object_name_key(record["name"])]
    elif kind == "exoplanet_status_assertion":
        prefix = {"pl_name": "planet", "toi": "toi", "koi": "koi"}[
            record["object_scheme"]
        ]
        keys = [f"{prefix}:{object_name_key(record['object_name'])}"]
    else:
        return []
    for scheme, value in (record.get("identifiers") or {}).items():
        if scheme in _PLANET_SCHEMES:
            keys.append(f"{_PLANET_SCHEMES[scheme]}:{object_name_key(value)}")
    return keys


def exoplanet_start_keys(name: Any) -> list[str]:
    folded = object_name_key(name)
    return [f"planet:{folded}", f"toi:{folded.removeprefix('toi')}", f"koi:{folded}"]


def exoplanet_group(
    records: Iterable[Mapping[str, Any]],
    name: Any,
    accepted: Iterable[tuple[str, str]] = (),
) -> set[str]:
    """Planet, TOI and KOI keys linked with ``name`` by archive-stated cross-identifiers or accepted reviews."""
    union = _Union()
    known: set[str] = set()
    for record in records:
        keys = _planet_keys(record)
        known.update(keys)
        for other in keys[1:]:
            union.join(keys[0], other)
    for left, right in accepted:
        a, b = left.removeprefix(PREFIX), right.removeprefix(PREFIX)
        if a.split(":")[0] in {"planet", "toi", "koi"} and b.split(":")[0] in {
            "planet",
            "toi",
            "koi",
        }:
            union.join(a, b)
    found: set[str] = set()
    for key in exoplanet_start_keys(name):
        if key in known:
            found |= union.group(key)
    return found


def orbital_group(
    records: Iterable[Mapping[str, Any]], identifier: Any
) -> dict[str, Any]:
    """Records naming a COSPAR, NORAD or GCAT identifier, joined through pairs each catalogue states.

    Returns ``{"keys", "conflicts"}``: a conflict is a NORAD number two catalogues pair with different COSPAR
    designators (or the reverse); both stay visible and the pairing goes to review.
    """
    from src.kb.astronomy_records import normalize_cospar, normalize_norad

    text = str(identifier or "").strip()
    start = []
    if normalize_cospar(text):
        start.append("cospar:" + normalize_cospar(text))
    if normalize_norad(text):
        start.append("norad:" + normalize_norad(text))
    if re.fullmatch(r"[A-Z]\d+", text.upper()):
        start.append("jcat:" + text.upper())
    records = [r for r in records if r["kind"] == "orbital_object"]
    union = _Union()
    for key in start:
        union.find(key)
    pairs: dict[str, set[tuple[str, str]]] = {}
    for record in records:
        keys = [
            f"{p}:{record[p]}" for p in ("cospar", "norad", "jcat") if record.get(p)
        ]
        for other in keys[1:]:
            union.join(keys[0], other)
        if record.get("norad") and record.get("cospar"):
            pairs.setdefault("norad:" + record["norad"], set()).add(
                (record["source"]["provider"], record["cospar"])
            )
            pairs.setdefault("cospar:" + record["cospar"], set()).add(
                (record["source"]["provider"], record["norad"])
            )
    keys: set[str] = set()
    for key in start:
        keys |= union.group(key)
    conflicts = [
        {"key": k, "stated": sorted(v)}
        for k, v in sorted(pairs.items())
        if k in keys and len({x for _, x in v}) > 1
    ]
    return {"keys": keys, "conflicts": conflicts}


# ------------------------------------------------------------------ reviewable candidates


class AstronomyIdentity:
    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] | None = None,
        initialize: bool = True,
    ) -> None:
        from src.kb.entity_history import EntityHistoryStore

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = AstronomyStore(conn, initialize=initialize, now=self.now)
        self.initialize = initialize
        self.history = EntityHistoryStore(conn, now=self.now, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name='astronomy_identity_candidates'"
            ).fetchone()
        )

    def _current(self, namespace: str) -> list[dict[str, Any]]:
        return self.store.visible(namespace)["records"]

    def offer(
        self,
        namespace: str,
        *,
        left_key: str,
        right_key: str,
        basis: str,
        evidence: Mapping[str, Any],
        principal_id: str,
    ) -> dict[str, Any]:
        """Add or refresh one candidate: new evidence re-proposes a rejected or reverted one; accepted stays."""
        if basis not in BASES or left_key == right_key:
            raise AstronomyError(
                "invalid_candidate", "a candidate needs two keys and a known basis"
            )
        a, b = sorted((left_key, right_key))
        candidate_id = "astro-idc:" + digest([namespace, a, b])[:24]
        row = self.conn.execute(
            "SELECT state, basis, evidence_json, history_json FROM astronomy_identity_candidates "
            "WHERE namespace=? AND candidate_id=?",
            [namespace, candidate_id],
        ).fetchone()
        now = self.now()
        if row is None:
            self.conn.execute(
                "INSERT INTO astronomy_identity_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    candidate_id,
                    a,
                    b,
                    basis,
                    BASES[basis],
                    canonical(dict(evidence)),
                    "proposed",
                    None,
                    principal_id,
                    now,
                    canonical(
                        [{"state": "proposed", "by": principal_id, "at_ms": now}]
                    ),
                ],
            )
            return {"candidate_id": candidate_id, "change": "created"}
        state, old_basis, old_evidence, history = (
            row[0],
            row[1],
            json.loads(row[2]),
            json.loads(row[3]),
        )
        stronger = BASES[basis] > BASES[old_basis]
        new = digest(dict(evidence)) != digest(old_evidence) or basis != old_basis
        if state == "proposed" and stronger:
            change = "upgraded"
        elif state in {"rejected", "reverted"} and new:
            change = "reproposed"
        else:
            return {"candidate_id": candidate_id, "change": None}
        history.append(
            {
                "state": "proposed",
                "by": principal_id,
                "at_ms": now,
                "change": change,
                "previous_state": state,
                "previous_basis": old_basis,
                "previous_evidence": old_evidence,
            }
        )
        self.conn.execute(
            "UPDATE astronomy_identity_candidates SET state='proposed', decision_id=NULL, basis=?, confidence=?, "
            "evidence_json=?, history_json=? WHERE namespace=? AND candidate_id=?",
            [
                basis,
                BASES[basis],
                canonical(dict(evidence)),
                canonical(history),
                namespace,
                candidate_id,
            ],
        )
        return {"candidate_id": candidate_id, "change": change}

    def propose(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        geo_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Offer every reviewable candidate the current records support; idempotent. Nothing is accepted here."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready()
        records = self._current(namespace)
        offered: list[dict[str, Any]] = []
        body = [v["record"] for v in records]
        # Exoplanets: a TOI whose TIC is a host's TIC, with no planet row stating that TOI.
        stated_tois = {
            object_name_key(r["identifiers"]["toi"])
            for r in body
            if r["kind"] == "exoplanet" and (r.get("identifiers") or {}).get("toi")
        }
        planets_by_tic: dict[str, set[str]] = {}
        for r in body:
            if r["kind"] == "exoplanet" and (r.get("identifiers") or {}).get("tic"):
                planets_by_tic.setdefault(
                    object_name_key(r["identifiers"]["tic"]), set()
                ).add(r["name"])
        for r in body:
            if r["kind"] != "exoplanet_status_assertion" or r["object_scheme"] != "toi":
                continue
            toi = object_name_key(r["object_name"])
            tic = object_name_key((r.get("identifiers") or {}).get("tic"))
            if toi in stated_tois or not tic:
                continue
            for planet in sorted(planets_by_tic.get(tic, ())):
                offered.append(
                    self.offer(
                        namespace,
                        left_key=f"{PREFIX}toi:{toi}",
                        right_key=f"{PREFIX}planet:{object_name_key(planet)}",
                        basis="shared-host-identifier",
                        evidence={
                            "tic": r["identifiers"]["tic"],
                            "toi": r["object_name"],
                            "planet": planet,
                            "note": "a shared host identifier is not an identification; a reviewer decides",
                        },
                        principal_id=principal_id,
                    )
                )
        # Orbital objects: catalogues pairing one identifier with different others.
        objects = [
            r
            for r in body
            if r["kind"] == "orbital_object" and r.get("norad") and r.get("cospar")
        ]
        for i, left in enumerate(objects):
            for right in objects[i + 1 :]:
                if left["source"]["provider"] == right["source"]["provider"]:
                    continue
                if (left["norad"] == right["norad"]) != (
                    left["cospar"] == right["cospar"]
                ):
                    offered.append(
                        self.offer(
                            namespace,
                            left_key=f"{PREFIX}object:{left['source']['provider']}:{left['source']['source_record_id']}",
                            right_key=f"{PREFIX}object:{right['source']['provider']}:"
                            f"{right['source']['source_record_id']}",
                            basis="catalogue-conflict",
                            evidence={
                                "stated": sorted(
                                    [
                                        [
                                            left["source"]["provider"],
                                            left["norad"],
                                            left["cospar"],
                                        ],
                                        [
                                            right["source"]["provider"],
                                            right["norad"],
                                            right["cospar"],
                                        ],
                                    ]
                                ),
                                "note": "the catalogues pair these identifiers differently; both stay as stated",
                            },
                            principal_id=principal_id,
                        )
                    )
        # Organisations: equal names with canonical organisations; similar names among themselves.
        orgs = [r for r in body if r["kind"] == "space_organisation"]
        canonical_orgs = []
        if self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='canonical_entities'"
        ).fetchone():
            canonical_orgs = self.conn.execute(
                "SELECT canonical_id, preferred_name FROM canonical_entities WHERE lower(coalesce(entity_type, '')) "
                "IN ('organization', 'organisation', 'org') ORDER BY canonical_id"
            ).fetchall()
        for org in orgs:
            name = organisation_name_key(org["name"])
            for canonical_id, preferred in canonical_orgs:
                other = organisation_name_key(preferred)
                basis = (
                    "exact-name"
                    if other == name
                    else "similar-name"
                    if _similar(name, other)
                    else None
                )
                if basis:
                    offered.append(
                        self.offer(
                            namespace,
                            left_key=f"{PREFIX}org:{org['code'].upper()}",
                            right_key=canonical_id,
                            basis=basis,
                            evidence={
                                "names": [org["name"], preferred],
                                "gcat_code": org["code"],
                                "state_code": org.get("state_code") or "not stated",
                            },
                            principal_id=principal_id,
                        )
                    )
        for i, left in enumerate(orgs):
            for right in orgs[i + 1 :]:
                a, b = (
                    organisation_name_key(left["name"]),
                    organisation_name_key(right["name"]),
                )
                if _similar(a, b):
                    offered.append(
                        self.offer(
                            namespace,
                            left_key=f"{PREFIX}org:{left['code'].upper()}",
                            right_key=f"{PREFIX}org:{right['code'].upper()}",
                            basis="similar-name",
                            evidence={
                                "names": [left["name"], right["name"]],
                                "note": "similar names are never an identity",
                            },
                            principal_id=principal_id,
                        )
                    )
        sites = []
        if geo_namespace:
            require_scope(scopes, GEO_READ)
            require_scope(scopes, GEO_WRITE)
            sites = self._resolve_sites(namespace, records, geo_namespace, principal_id)
        return {
            "proposed": sorted({o["candidate_id"] for o in offered if o["change"]}),
            "candidates": self.candidates(namespace, scopes=scopes),
            "sites": sites,
            "notice": "candidates stay proposed until reviewed; source-stated identifiers link without review",
        }

    def _resolve_sites(
        self, namespace, records, geo_namespace, principal_id
    ) -> list[dict[str, Any]]:
        from src.kb.geospatial import GeospatialStore

        geo = GeospatialStore(self.conn, now=self.now)
        used = {
            r["record"].get("site_code") for r in records if r["kind"] == "launch"
        } - {None}
        out = []
        for view in records:
            org = view["record"]
            if view["kind"] != "space_organisation" or org["code"] not in used:
                continue
            hint = (
                [float(org["longitude"]), float(org["latitude"])]
                if org.get("latitude") and org.get("longitude")
                else None
            )
            result = geo.resolve(
                geo_namespace,
                org["name"],
                scopes={GEO_READ},
                context={"producer": "astronomy.launches", "gcat_code": org["code"]},
                coordinate_hint=hint,
            )
            # A reviewable resolution: nothing is selected until the Geospatial owner's review accepts a place.
            result = {
                **result,
                "status": "ambiguous" if result["candidates"] else "unresolved",
                "selected_place_id": None,
            }
            saved = geo.save_resolution(
                result, principal_id=principal_id, scopes={GEO_WRITE}
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO astronomy_site_resolutions VALUES (?,?,?,?,?,?,?)",
                [
                    namespace,
                    org["code"],
                    geo_namespace,
                    saved["resolution_id"],
                    view["record_id"],
                    view["revision_id"],
                    self.now(),
                ],
            )
            out.append(
                {
                    "site_code": org["code"],
                    "resolution_id": saved["resolution_id"],
                    "candidates": [c["place_id"] for c in result["candidates"]],
                    "status": result["status"],
                }
            )
        return out

    def review_site(
        self,
        namespace: str,
        site_code: str,
        decision: str,
        *,
        selected_place_id: str | None,
        reason: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Review a launch site's saved resolution through the Geospatial owner (accept, reject or defer)."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        require_scope(scopes, GEO_REVIEW)
        row = self._site_row(namespace, site_code)
        if row is None:
            raise AstronomyError(
                "not_found", "the site has no saved resolution in this namespace"
            )
        return GeospatialStore(self.conn, now=self.now).review(
            row[0],
            row[1],
            decision,
            selected_place_id=selected_place_id,
            reason=reason,
            principal_id=principal_id,
            scopes={GEO_REVIEW},
        )

    def _site_row(self, namespace: str, site_code: str):
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE "
            "table_name='astronomy_site_resolutions'"
        ).fetchone():
            return None
        return self.conn.execute(
            "SELECT geo_namespace, resolution_id FROM astronomy_site_resolutions WHERE namespace=? AND site_code=? "
            "ORDER BY created_at_ms DESC, resolution_id LIMIT 1",
            [namespace, site_code],
        ).fetchone()

    def site(self, namespace: str, site_code: str) -> dict[str, Any]:
        """A launch site's place: resolved only through an accepted Geospatial review, else visibly unresolved."""
        row = self._site_row(namespace, site_code) if site_code else None
        if row is None:
            return {
                "site_code": site_code,
                "state": "unresolved",
                "reason": "no saved place resolution",
            }
        review = None
        if self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='geocode_reviews'"
        ).fetchone():
            review = self.conn.execute(
                "SELECT decision, selected_place_id, review_id FROM geocode_reviews WHERE resolution_id=? "
                "ORDER BY revision DESC LIMIT 1",
                [row[1]],
            ).fetchone()
        if review and review[0] == "accept":
            return {
                "site_code": site_code,
                "state": "resolved",
                "place_id": review[1],
                "geo_namespace": row[0],
                "resolution_id": row[1],
                "review_id": review[2],
            }
        return {
            "site_code": site_code,
            "state": "rejected" if review and review[0] == "reject" else "proposed",
            "geo_namespace": row[0],
            "resolution_id": row[1],
        }

    # -------------------------------------------------------------- reviews

    def _row(self, namespace: str, candidate_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT candidate_id, left_key, right_key, basis, confidence, evidence_json, state, decision_id, "
            "created_by, created_at_ms, history_json FROM astronomy_identity_candidates WHERE namespace=? "
            "AND candidate_id=?",
            [namespace, candidate_id],
        ).fetchone()
        if row is None:
            raise AstronomyError(
                "not_found", "identity candidate is not visible in this namespace"
            )
        return {
            "contract": CONTRACT,
            "namespace": namespace,
            **dict(
                zip(
                    ("candidate_id", "left_key", "right_key", "basis", "confidence"),
                    row[:5],
                )
            ),
            "evidence": json.loads(row[5]),
            "state": row[6],
            "decision_id": row[7],
            "created_by": row[8],
            "created_at_ms": int(row[9]),
            "history": json.loads(row[10]),
            "notice": "a reviewable proposal; records are never merged or edited",
        }

    def candidates(
        self, namespace: str, *, scopes: Iterable[str], state: str | None = None
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT candidate_id FROM astronomy_identity_candidates WHERE namespace=? "
            "AND (? IS NULL OR state=?) ORDER BY candidate_id",
            [namespace, state, state],
        )
        return [self._row(namespace, r[0]) for r in rows.fetchall()]

    def _entity(self, key: str) -> str:
        if key.startswith("ent-"):
            return key
        return "ent-astronomy-" + digest(key)[:20]

    def _transition(
        self, namespace, candidate, state, decision_id, principal_id, reason
    ) -> dict[str, Any]:
        history = candidate["history"] + [
            {
                "state": state,
                "by": principal_id,
                "reason": reason,
                "at_ms": self.now(),
                "decision_id": decision_id,
            }
        ]
        self.conn.execute(
            "UPDATE astronomy_identity_candidates SET state=?, decision_id=?, history_json=? "
            "WHERE namespace=? AND candidate_id=?",
            [
                state,
                decision_id,
                canonical(history),
                namespace,
                candidate["candidate_id"],
            ],
        )
        return self._row(namespace, candidate["candidate_id"])

    def review(
        self,
        namespace: str,
        candidate_id: str,
        decision: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Accept (``match``) or reject (``non-match``) as an entity identity decision; never by the proposer alone."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise AstronomyError("invalid_decision", "accept or reject with a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] != "proposed":
            raise AstronomyError(
                "invalid_state",
                f"candidate is {candidate['state']}; propose again to re-review",
            )
        if decision == "accept" and candidate["basis"] in NEVER_ACCEPTED:
            raise AstronomyError(
                "insufficient_evidence",
                "a similar name alone never produces an accepted match",
            )
        entities = [
            self._entity(candidate["left_key"]),
            self._entity(candidate["right_key"]),
        ]
        for entity, key in zip(
            entities, (candidate["left_key"], candidate["right_key"])
        ):
            self.history.register_entity(
                namespace,
                entity,
                [key],
                principal_id=principal_id,
                scopes=_ENTITY_HISTORY_SCOPES,
            )
        recorded = self.history.decide(
            namespace,
            "match" if decision == "accept" else "non-match",
            entities,
            {
                "candidate_id": candidate_id,
                "basis": candidate["basis"],
                "evidence": candidate["evidence"],
                "reason": reason.strip(),
                "provenance": {
                    "producer": "astronomy",
                    "records": [candidate["left_key"], candidate["right_key"]],
                },
                "policy": {
                    "merge": False,
                    "note": "identity decision only; records stay separate",
                },
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"astronomy-identity:{namespace}:{candidate_id}:{len(candidate['history'])}",
        )
        return self._transition(
            namespace,
            candidate,
            "accepted" if decision == "accept" else "rejected",
            recorded["decision_id"],
            principal_id,
            reason.strip(),
        )

    def revert(
        self,
        namespace: str,
        candidate_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise AstronomyError("invalid_decision", "a revert needs a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] not in {"accepted", "rejected"}:
            raise AstronomyError(
                "invalid_state",
                "only an accepted or rejected candidate can be reverted",
            )
        undo = self.history.undo(
            namespace,
            candidate["decision_id"],
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_ENTITY_HISTORY_SCOPES,
        )
        return self._transition(
            namespace,
            candidate,
            "reverted",
            undo["decision_id"],
            principal_id,
            reason.strip(),
        )

    def accepted_pairs(self, namespace: str) -> list[tuple[str, str]]:
        if not self._ready():
            return []
        return [
            (r[0], r[1])
            for r in self.conn.execute(
                "SELECT left_key, right_key FROM astronomy_identity_candidates WHERE namespace=? AND state='accepted' "
                "ORDER BY candidate_id",
                [namespace],
            ).fetchall()
        ]

    def organisation(self, namespace: str, code: str) -> dict[str, Any]:
        """A GCAT code's canonical organisation through an accepted candidate, else the source string."""
        key = f"{PREFIX}org:{code.upper()}"
        targets = sorted(
            {
                right if left == key else left
                for left, right in self.accepted_pairs(namespace)
                if key in (left, right)
            }
            - {key}
        )
        canonical_ids = [t for t in targets if t.startswith("ent-")]
        return {
            "code": code,
            "canonical_entity": canonical_ids[0] if len(canonical_ids) == 1 else None,
            "state": "linked" if len(canonical_ids) == 1 else "source string",
        }


def records_of(views: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(v["record"]) for v in views]
