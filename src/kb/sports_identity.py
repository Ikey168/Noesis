"""Reviewable identity for players, teams, competitions and venues across sports sources (#2141, SP06).

Every team, player and season stays the record its source published. Links
between records of different sources are *proposed* into the shared reviewable
state machine (:class:`src.kb.ownership_identity.OwnershipIdentityService`),
whose accepted, rejected and reverted decisions are entity identity decisions in
:class:`src.kb.entity_history.EntityHistoryStore`. Nothing is merged or
rewritten, and a candidate stays ``proposed`` until a reviewer decides it.

Bases (deterministic, with the evidence recorded on the candidate):

* ``cross-referenced-identifier`` - both records state the same cross id
  (a Wikidata QID, a federation id) in ``cross_ids``; accepted with that
  citation as evidence;
* ``name-jurisdiction`` - players: equal normalised names *and* equal
  published dates of birth; teams: equal normalised names in one country with
  no contradicting founding year; seasons: one competition name, area and
  season start year;
* ``similar-name`` - equal names without corroborating attributes (a player
  without a published date of birth, a team without a country) and names that
  resolve to a canonical entity; shown, never acceptable.

Two same-named players with different published dates of birth, or two clubs
with different founding years, are never proposed. Players are never offered
to ``canonical_entities``; teams and governing bodies are, as organisations.

Club renames, mergers and phoenix clubs are *identity-history events*
(:meth:`SportsIdentity.record_lineage`): a rename is an ``alias`` decision
(the same club under a new name), a merger or a phoenix club a ``non-match``
decision carrying the relation (a different club with a lineage) - explicit,
cited and reversible, never a silent merge. Venues resolve to Geospatial
places only through a saved, reviewable place resolution with the venue's
country as context (:meth:`SportsIdentity.link_venue_place`).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.sports_records import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    SportsError,
    authorize,
    canonical,
    require_scope,
    table_exists,
)
from src.kb.sports_store import SportsStore

GEO_READ = "knowledge:geospatial:read"
GEO_WRITE = "knowledge:geospatial:write"
LINEAGE_KINDS = {
    "renamed": "alias",
    "merged_into": "non-match",
    "phoenix_of": "non-match",
}
_AFFIXES = {"fc", "afc", "cf", "sc", "ac", "fk", "sv", "club", "the"}
_ENTITY_HISTORY_SCOPES = {
    "knowledge:entity-history:write",
    "knowledge:entity-history:review",
    "knowledge:entity-history:execute",
    "knowledge:entity-history:read",
}
_DDL = """
CREATE TABLE IF NOT EXISTS sports_place_links (
  namespace TEXT NOT NULL, venue_key TEXT NOT NULL, geo_namespace TEXT NOT NULL, resolution_id TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, PRIMARY KEY(namespace, venue_key, resolution_id)
);
"""


def normalize_name(value: Any, *, club: bool = False) -> str:
    words = re.sub(r"[^0-9a-zà-ɏ]+", " ", str(value or "").casefold()).split()
    if club:
        words = [w for w in words if w not in _AFFIXES] or words
    return " ".join(words)


def entity_id(key: str) -> str:
    from src.kb.ownership_store import canonical_entity_id

    return canonical_entity_id(key)


def _season_year(label: Any) -> str:
    match = re.match(r"\d{4}", str(label or ""))
    return match.group(0) if match else ""


class SportsIdentity:
    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] | None = None,
        initialize: bool = True,
    ) -> None:
        from src.kb.entity_history import EntityHistoryStore
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = SportsStore(conn, initialize=initialize, now=self.now)
        self.service = OwnershipIdentityService(
            conn, now=self.now, initialize=initialize
        )
        self.history = EntityHistoryStore(conn, now=self.now, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str) -> list[dict[str, Any]]:
        out = []
        for kind in ("team", "player", "season"):
            for key in self.store.records(namespace, kind):
                current = self.store.current(namespace, kind, key)
                body = dict(current["body"])
                if kind == "season":
                    competition = (
                        self.store.body(
                            namespace, "competition", body["competition_key"]
                        )
                        or {}
                    )
                    body = {
                        **body,
                        "name": competition.get("name"),
                        "country": competition.get("area"),
                        "governing_body": competition.get("governing_body"),
                    }
                out.append(
                    {
                        "kind": kind,
                        "record_key": key,
                        "entity_id": entity_id(key),
                        "provider": current["source"]["provider"],
                        "body": body,
                        "side": {
                            "record_key": key,
                            "revision_id": current["revision_id"],
                        },
                    }
                )
        return out

    # ------------------------------------------------------------------ proposals

    def propose(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        canonical_names: bool = True,
    ) -> dict[str, Any]:
        """Deterministic candidates across sources; idempotent, and a stronger basis upgrades a pending weaker one."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready(namespace)
        subjects = self.subjects(namespace)
        offered = []

        def offer(left, right, basis, evidence):
            if left["provider"] == right["provider"]:
                return  # a source's own distinct ids are distinct records
            offered.append(
                self.service.offer(
                    namespace,
                    left_key=left["record_key"],
                    right_key=right["record_key"],
                    left_entity=left["entity_id"],
                    right_entity=right["entity_id"],
                    basis=basis,
                    evidence=[
                        {**evidence, "left": left["side"], "right": right["side"]}
                    ],
                    principal_id=principal_id,
                    scopes=scopes,
                )
            )

        # Source-stated cross ids.
        by_cross: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for subject in subjects:
            for scheme, value in dict(subject["body"].get("cross_ids") or {}).items():
                by_cross.setdefault(
                    (subject["kind"], scheme, str(value).upper()), []
                ).append(subject)
        for (kind, scheme, value), group in sorted(by_cross.items()):
            for i, left in enumerate(group):
                for right in group[i + 1 :]:
                    offer(
                        left,
                        right,
                        "cross-referenced-identifier",
                        {
                            "kind": scheme,
                            "value": value,
                            "fields": ["cross_ids"],
                            "note": "a cross id both sources state",
                        },
                    )
        by_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for subject in subjects:
            club = subject["kind"] == "team"
            by_name.setdefault(
                (
                    subject["kind"],
                    normalize_name(subject["body"].get("name"), club=club),
                ),
                [],
            ).append(subject)
        for (kind, name), group in sorted(by_name.items()):
            if not name:
                continue
            for i, left in enumerate(group):
                for right in group[i + 1 :]:
                    a, b = left["body"], right["body"]
                    if kind == "player":
                        if a.get("birth_date") and b.get("birth_date"):
                            if a["birth_date"] != b["birth_date"]:
                                continue  # two people who share a name
                            offer(
                                left,
                                right,
                                "name-jurisdiction",
                                {
                                    "kind": "name-and-birth-date",
                                    "value": name,
                                    "birth_date": a["birth_date"],
                                    "fields": ["name", "birth_date"],
                                    "note": "equal names and published dates of birth; a reviewer decides",
                                },
                            )
                        else:
                            offer(
                                left,
                                right,
                                "similar-name",
                                {
                                    "kind": "name",
                                    "value": name,
                                    "fields": ["name"],
                                    "note": "a name without a published date of birth is never an identity",
                                },
                            )
                        continue
                    if (
                        a.get("founded")
                        and b.get("founded")
                        and a["founded"] != b["founded"]
                    ):
                        continue  # different founding years: different clubs
                    if kind == "season" and _season_year(
                        a.get("label")
                    ) != _season_year(b.get("label")):
                        continue
                    country_a, country_b = (
                        normalize_name(a.get("country")),
                        normalize_name(b.get("country")),
                    )
                    if country_a and country_a == country_b:
                        fields = ["name", "country"] + (
                            ["founded"] if a.get("founded") and b.get("founded") else []
                        )
                        offer(
                            left,
                            right,
                            "name-jurisdiction",
                            {
                                "kind": f"{kind}-name-and-country",
                                "value": name,
                                "country": a.get("country"),
                                "season": _season_year(a.get("label")) or None,
                                "fields": fields
                                + (["season"] if kind == "season" else []),
                                "note": "equal names in one country; a reviewer decides",
                            },
                        )
                    else:
                        offer(
                            left,
                            right,
                            "similar-name",
                            {
                                "kind": "name",
                                "value": name,
                                "fields": ["name"],
                                "note": "a name alone is never an identity",
                            },
                        )
        if canonical_names:
            offered += self._canonical(namespace, subjects, principal_id, scopes)
        return {
            "proposed": sorted(
                {o["candidate_id"] for o in offered if o["created"] or o.get("change")}
            ),
            "candidates": self.candidates(namespace, scopes=scopes),
        }

    def _canonical(self, namespace, subjects, principal_id, scopes):
        """Teams and governing bodies to canonical entities as organisations; players never."""
        if not table_exists(self.conn, "entity_aliases"):
            return []
        from src.kb.entities import resolve

        offered = []
        for subject in subjects:
            if subject["kind"] == "player":
                continue
            names = [subject["body"].get("name")]
            if subject["kind"] == "season":
                names = [subject["body"].get("governing_body")]
            for name in filter(None, names):
                found = resolve(self.conn, name)
                if not found or str(found.get("entity_type") or "").upper() in {
                    "PERSON",
                    "PER",
                }:
                    continue
                key = f"canonical:{found['canonical_id']}"
                offered.append(
                    self.service.offer(
                        namespace,
                        left_key=subject["record_key"],
                        right_key=key,
                        left_entity=subject["entity_id"],
                        right_entity=found["canonical_id"],
                        basis="similar-name",
                        evidence=[
                            {
                                "kind": "name",
                                "value": name,
                                "left": subject["side"],
                                "right": {
                                    "record_key": key,
                                    "canonical_id": found["canonical_id"],
                                },
                                "fields": ["name"],
                                "note": "an organisation name; a name alone is never an identity",
                            }
                        ],
                        principal_id=principal_id,
                        scopes=scopes,
                    )
                )
        return offered

    # ------------------------------------------------------------------ reviews and reads

    def _own(self, namespace: str, candidate_id: str) -> dict[str, Any]:
        candidate = self.service._row(namespace, candidate_id)
        if not (
            candidate["left_key"].startswith("sports:")
            or candidate["right_key"].startswith("sports:")
        ):
            raise SportsError(
                "not_found", "candidate is not a sports identity candidate"
            )
        return candidate

    def review(
        self, namespace, candidate_id, decision, reason, *, principal_id, scopes
    ):
        self._own(namespace, candidate_id)
        return self.view(
            self.service.review(
                namespace,
                candidate_id,
                decision,
                reason,
                principal_id=principal_id,
                scopes=scopes,
            )
        )

    def revert(self, namespace, candidate_id, reason, *, principal_id, scopes):
        self._own(namespace, candidate_id)
        return self.view(
            self.service.revert(
                namespace,
                candidate_id,
                reason,
                principal_id=principal_id,
                scopes=scopes,
            )
        )

    def candidates(
        self, namespace: str, *, scopes: Iterable[str], record_key: str | None = None
    ) -> list[dict]:
        rows = self.service.candidates(namespace, scopes=scopes, record_key=record_key)
        return [
            self.view(c)
            for c in rows
            if c["left_key"].startswith("sports:")
            or c["right_key"].startswith("sports:")
        ]

    @staticmethod
    def view(candidate: Mapping[str, Any]) -> dict[str, Any]:
        last = candidate["history"][-1]
        return {
            "candidate_id": candidate["candidate_id"],
            "state": candidate["state"],
            "review_state": {
                "accepted": "reviewed-match",
                "rejected": "reviewed-non-match",
                "reverted": "reverted",
                "proposed": "unreviewed-candidate",
            }[candidate["state"]],
            "basis": candidate["basis"],
            "confidence": candidate["confidence"],
            "records": [candidate["left_key"], candidate["right_key"]],
            "entities": [candidate["left_entity"], candidate["right_entity"]],
            "decision_id": candidate["decision_id"],
            "reviewer": last.get("by") if candidate["state"] != "proposed" else None,
            "evidence": candidate["evidence"],
            "history": candidate["history"],
            "notice": "a reviewable identity decision; sports records are never merged or edited",
        }

    def linked(self, namespace: str, record_key: str) -> list[str]:
        """The record and every sports record joined to it by accepted, unreverted decisions (one equivalence)."""
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return [record_key]
        rows = self.conn.execute(
            "SELECT left_key, right_key FROM ownership_identity_candidates WHERE namespace=? AND state='accepted' "
            "AND left_key LIKE 'sports:%' AND right_key LIKE 'sports:%' ORDER BY candidate_id",
            [namespace],
        ).fetchall()
        group, frontier = {record_key}, [record_key]
        while frontier:
            key = frontier.pop()
            for left, right in rows:
                other = right if left == key else left if right == key else None
                if other and other not in group:
                    group.add(other)
                    frontier.append(other)
        return sorted(group)

    def name_history(self, namespace: str, record_key: str) -> list[dict[str, Any]]:
        """The names one record carried, per revision; a rename within one source keeps the record's key."""
        kind = "team" if ":team:" in record_key else "player"
        names, out = set(), []
        for revision in self.store.history(namespace, kind, record_key):
            name = revision["body"].get("name")
            if name not in names:
                names.add(name)
                out.append(
                    {
                        "name": name,
                        "revision_id": revision["revision_id"],
                        "published_ms": revision["published_ms"],
                        "source": revision["source"]["provider"],
                    }
                )
        return out

    # ------------------------------------------------------------------ club lineage (identity history)

    def record_lineage(
        self,
        namespace: str,
        *,
        kind: str,
        subject_key: str,
        object_key: str,
        valid_on: str,
        source: Mapping[str, Any],
        reason: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """A cited rename, merger or phoenix club as an entity identity-history decision; never a merge of records.

        ``subject_key`` is the earlier club, ``object_key`` the later one. Reversible with ``undo``.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if kind not in LINEAGE_KINDS or subject_key == object_key:
            raise SportsError(
                "invalid_lineage",
                f"record one of {sorted(LINEAGE_KINDS)} between two clubs",
            )
        if not str(source.get("url") or "").startswith("https://") or not source.get(
            "title"
        ):
            raise SportsError(
                "invalid_lineage",
                "a lineage event cites its source (https url and title)",
            )
        if not str(reason or "").strip():
            raise SportsError("invalid_lineage", "a lineage event states a reason")
        for key in (subject_key, object_key):
            if ":team:" not in key or not self.store.current(namespace, "team", key):
                raise SportsError(
                    "not_found", f"{key} is not a team record in this namespace"
                )
        entities = [entity_id(subject_key), entity_id(object_key)]
        for key, entity in zip((subject_key, object_key), entities):
            self.history.register_entity(
                namespace,
                entity,
                [key],
                principal_id=principal_id,
                scopes=_ENTITY_HISTORY_SCOPES,
            )
        from datetime import date

        recorded = self.history.decide(
            namespace,
            LINEAGE_KINDS[kind],
            entities,
            {
                "relation": kind,
                "records": [subject_key, object_key],
                "valid_time": {"from": date.fromisoformat(valid_on[:10]).isoformat()},
                "source": dict(source),
                "reason": reason.strip(),
                "provenance": {
                    "producer": "sports.football",
                    "records": [subject_key, object_key],
                },
                "policy": {
                    "merge": False,
                    "note": "identity-history event only; records stay separate",
                },
            },
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_ENTITY_HISTORY_SCOPES,
            event_key=f"sports-lineage:{namespace}:{kind}:{subject_key}:{object_key}",
        )
        return {
            "decision_id": recorded["decision_id"],
            "decision_type": recorded["decision_type"],
            "relation": kind,
            "records": [subject_key, object_key],
            "entities": entities,
            "source": dict(source),
        }

    def undo_lineage(self, namespace, decision_id, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        return self.history.undo(
            namespace,
            decision_id,
            reviewer_id=principal_id,
            principal_id=principal_id,
            scopes=_ENTITY_HISTORY_SCOPES,
        )

    def lineage(
        self, namespace: str, record_key: str, *, scopes: Iterable[str]
    ) -> list[dict[str, Any]]:
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "entity_identity_decisions"):
            return []
        items = self.history.history(
            namespace, entity_id(record_key), scopes=_ENTITY_HISTORY_SCOPES, limit=500
        )["items"]
        undone = {
            i["payload"].get("undoes") for i in items if i["decision_type"] == "undo"
        }
        return [
            {
                "decision_id": i["decision_id"],
                "relation": i["payload"].get("relation"),
                "records": i["payload"].get("records"),
                "source": i["payload"].get("source"),
                "valid_time": i["payload"].get("valid_time"),
                "undone": i["decision_id"] in undone,
            }
            for i in items
            if i["payload"].get("relation") in LINEAGE_KINDS
        ]

    # ------------------------------------------------------------------ venues to places

    def link_venue_place(
        self, namespace, venue_key, *, geo_namespace, principal_id, scopes
    ) -> dict[str, Any]:
        """A reviewable place resolution for a venue, with its country as context; never by name alone."""
        from src.kb.geospatial import GeospatialStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, GEO_WRITE)
        venue = self.store.body(namespace, "venue", venue_key)
        if venue is None:
            raise SportsError("not_found", "venue is not visible in this namespace")
        if not venue.get("country") and not venue.get("city"):
            raise SportsError(
                "unresolvable",
                "a venue resolves with its city or country, never by name alone",
            )
        geo = GeospatialStore(self.conn, initialize=False)
        result = geo.resolve(
            geo_namespace,
            venue["name"],
            context={k: venue[k] for k in ("country", "city") if venue.get(k)}
            | {"venue_key": venue_key},
            scopes={GEO_READ},
        )
        saved = geo.save_resolution(result, principal_id=principal_id, scopes=scopes)
        self.conn.execute(
            "INSERT OR IGNORE INTO sports_place_links VALUES (?,?,?,?,?,?)",
            [
                namespace,
                venue_key,
                geo_namespace,
                saved["resolution_id"],
                principal_id,
                self.now(),
            ],
        )
        return {**self.place(namespace, venue_key, scopes=scopes), "resolution": saved}

    def place(self, namespace, venue_key, *, scopes) -> dict[str, Any]:
        """The venue's place reference: accepted by Geospatial review, or unresolved."""
        authorize(namespace, set(scopes), READ_SCOPE)
        rows = []
        if table_exists(self.conn, "sports_place_links"):
            rows = self.conn.execute(
                "SELECT l.resolution_id, l.geo_namespace FROM sports_place_links l WHERE l.namespace=? AND "
                "l.venue_key=? ORDER BY l.created_at_ms, l.resolution_id",
                [namespace, venue_key],
            ).fetchall()
        views = []
        for resolution_id, geo_namespace in rows:
            review = None
            if table_exists(self.conn, "geocode_reviews"):
                review = self.conn.execute(
                    "SELECT decision, selected_place_id, principal_id FROM geocode_reviews WHERE resolution_id=? "
                    "ORDER BY revision DESC LIMIT 1",
                    [resolution_id],
                ).fetchone()
            state = (
                "unreviewed"
                if review is None
                else {"accept": "accepted", "reject": "rejected", "defer": "deferred"}[
                    review[0]
                ]
            )
            views.append(
                {
                    "resolution_id": resolution_id,
                    "geo_namespace": geo_namespace,
                    "review_state": state,
                    "place_id": review[1] if review and review[0] == "accept" else None,
                    "reviewer": None if review is None else review[2],
                }
            )
        accepted = [v for v in views if v["place_id"]]
        return {
            "venue_key": venue_key,
            "state": "linked" if accepted else "unresolved",
            "place_id": accepted[-1]["place_id"] if accepted else None,
            "links": views,
            "note": "the Sports pack stores the place reference only; geometry stays in the Geospatial store",
        }


__all__ = [
    "LINEAGE_KINDS",
    "SportsIdentity",
    "canonical",
    "entity_id",
    "normalize_name",
]
