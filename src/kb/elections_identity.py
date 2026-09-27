"""Reviewable party, candidate and constituency identity across elections and sources (#1908, L06).

Every party list, candidate and constituency stays the record its source
published: one party in two elections is two candidate-or-list records, and a
poll publisher's party column is a third. Links between them are *proposed*
into the existing reviewable state machine
(:class:`src.kb.ownership_identity.OwnershipIdentityService`), whose accepted
and reverted decisions are entity identity decisions in
:class:`src.kb.entity_history.EntityHistoryStore` (``match``, ``non-match``,
``undo``) - the same decisions ``record_entity_identity_decision`` and
``undo_entity_identity_change`` expose. Nothing is merged or rewritten, and a
name without an accepted decision stays the source string.

Bases, comparing only within one jurisdiction (a German and a British "Example
Party" are never proposed):

* ``name-jurisdiction`` - equal normalized labels in one country (across
  elections, or a poll option and a result list of the same election), or a
  label that the Political pack's scoped alias table
  (:func:`src.domains.political.model.resolve_alias`) resolves to exactly one
  party in that jurisdiction;
* ``unqualified-identifier`` - one constituency number in two boundary
  vintages (a reviewer must establish that the area is the same);
* ``similar-name`` - a label that resolves to a canonical entity alias; shown,
  never acceptable.

Successor parties, mergers and renamings, and constituency redistricting, are
*assertions*: dated, cited, side by side with conflicting assertions, and
retractable. None of them transfers votes or makes figures comparable.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.ingestion.election_sources import slug
from src.kb.elections import (
    CERTIFIED_CLASS,
    READ_SCOPE,
    WRITE_SCOPE,
    ElectionError,
    ElectionStore,
    _day,
    _load,
    authorize,
    canonical,
    digest,
    entity_id,
    normalize_name,
    table_exists,
)

ASSERTION_KINDS = (
    "party_successor",
    "party_merger",
    "party_renamed",
    "constituency_successor",
)
_DDL = """
CREATE TABLE IF NOT EXISTS election_assertions (
  namespace TEXT NOT NULL, assertion_id TEXT NOT NULL, revision_no INTEGER NOT NULL, kind TEXT NOT NULL,
  subject_key TEXT NOT NULL, object_key TEXT NOT NULL, valid_on TEXT NOT NULL, source_json TEXT NOT NULL,
  reason TEXT, state TEXT NOT NULL, recorded_by TEXT NOT NULL, recorded_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, assertion_id, revision_no)
);
"""


def _country(jurisdiction: Any) -> str:
    return str(jurisdiction or "").split("-")[0].upper()


def poll_record_key(series_id: str) -> str:
    return f"elections:poll-series:{series_id}"


class ElectionIdentity:
    def __init__(
        self,
        conn: Any,
        *,
        now: Callable[[], int] | None = None,
        initialize: bool = True,
    ) -> None:
        from src.kb.ownership_identity import OwnershipIdentityService

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = ElectionStore(conn, initialize=initialize, now=self.now)
        self.service = OwnershipIdentityService(
            conn, now=self.now, initialize=initialize
        )
        if initialize:
            conn.execute(_DDL)

    # ------------------------------------------------------------------ subjects

    def subjects(self, namespace: str) -> list[dict[str, Any]]:
        """Party lists, candidates, poll options and constituencies as their sources state them."""
        subjects = []
        for record in self.store.candidates(namespace):
            election = self.store.election(namespace, record["election_id"])
            subjects.append(
                {
                    "kind": "list" if record["kind"] == "list" else "candidate",
                    "record_key": record["record_key"],
                    "entity_id": entity_id(record["record_key"]),
                    "label": record["label"],
                    "party": record["party"],
                    "election_id": record["election_id"],
                    "election_date": election["election_date"],
                    "jurisdiction": election["jurisdiction"],
                    "side": {
                        "record_key": record["record_key"],
                        "first_release_id": record["first_release_id"],
                    },
                }
            )
        if table_exists(self.conn, "election_poll_readings"):
            rows = self.conn.execute(
                "SELECT DISTINCT series_id, publisher, election_id, option FROM election_poll_readings "
                "WHERE namespace=? ORDER BY series_id",
                [namespace],
            ).fetchall()
            for series_id, publisher, election_id, option in rows:
                try:
                    election = self.store.election(namespace, election_id)
                except ElectionError:
                    election = {"election_date": None, "jurisdiction": None}
                key = poll_record_key(series_id)
                subjects.append(
                    {
                        "kind": "poll-option",
                        "record_key": key,
                        "entity_id": entity_id(key),
                        "label": option,
                        "party": option,
                        "election_id": election_id,
                        "election_date": election["election_date"],
                        "jurisdiction": election["jurisdiction"]
                        or election_id.split(":")[0].split("-")[0].upper(),
                        "side": {
                            "record_key": key,
                            "series_id": series_id,
                            "publisher": publisher,
                        },
                    }
                )
        for record in self.store.constituencies(namespace):
            subjects.append(
                {
                    "kind": "constituency",
                    "record_key": record["record_key"],
                    "entity_id": entity_id(record["record_key"]),
                    "label": record["name"],
                    "party": None,
                    "scheme": record["scheme"],
                    "native_id": record["native_id"],
                    "boundary_vintage": record["boundary_vintage"],
                    "jurisdiction": record["jurisdiction"],
                    "side": {
                        "record_key": record["record_key"],
                        "constituency_id": record["constituency_id"],
                    },
                }
            )
        return subjects

    # ------------------------------------------------------------------ proposals

    def propose(
        self,
        namespace: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        political_jurisdictions: Mapping[str, str] | None = None,
        canonical_names: bool = True,
    ) -> dict[str, Any]:
        """Propose candidates between election records, to Political pack parties and to canonical entities.

        Idempotent; a stronger basis upgrades a pending weaker candidate and new evidence re-proposes a rejected or
        reverted one (shared state-machine rules). Records are never edited.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        subjects = self.subjects(namespace)
        offered = []
        parties = [s for s in subjects if s["kind"] in {"list", "poll-option"}]
        by_label: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for subject in parties:
            by_label.setdefault(
                (_country(subject["jurisdiction"]), normalize_name(subject["label"])),
                [],
            ).append(subject)
        for (country, label), holders in sorted(by_label.items()):
            if not label or not country:
                continue
            for i, left in enumerate(holders):
                for right in holders[i + 1 :]:
                    if (
                        left["election_id"] == right["election_id"]
                        and left["kind"] == right["kind"]
                    ):
                        continue
                    offered.append(
                        self._offer(
                            namespace,
                            left,
                            right["record_key"],
                            right["entity_id"],
                            "name-jurisdiction",
                            {
                                "kind": "label",
                                "value": left["label"],
                                "country": country,
                                "left": left["side"],
                                "right": right["side"],
                                "fields": ["label", "jurisdiction"],
                                "note": "equal labels in one country are a weak signal; a reviewer decides",
                            },
                            principal_id,
                            scopes,
                        )
                    )
        units: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for subject in subjects:
            if subject["kind"] == "constituency":
                units.setdefault((subject["scheme"], subject["native_id"]), []).append(
                    subject
                )
        for (scheme, native), holders in sorted(units.items()):
            for i, left in enumerate(holders):
                for right in holders[i + 1 :]:
                    offered.append(
                        self._offer(
                            namespace,
                            left,
                            right["record_key"],
                            right["entity_id"],
                            "unqualified-identifier",
                            {
                                "kind": scheme,
                                "value": native,
                                "left": {
                                    **left["side"],
                                    "boundary_vintage": left["boundary_vintage"],
                                },
                                "right": {
                                    **right["side"],
                                    "boundary_vintage": right["boundary_vintage"],
                                },
                                "fields": ["scheme", "native_id"],
                                "note": "one number in two boundary vintages; the area may differ",
                            },
                            principal_id,
                            scopes,
                        )
                    )
        offered += self._political(
            namespace, parties, political_jurisdictions, principal_id, scopes
        )
        if canonical_names:
            offered += self._canonical(namespace, parties, principal_id, scopes)
        return {
            "proposed": sorted(
                {o["candidate_id"] for o in offered if o["created"] or o.get("change")}
            ),
            "candidates": self.candidates(namespace, scopes=scopes),
        }

    def _offer(
        self,
        namespace,
        subject,
        right_key,
        right_entity,
        basis,
        evidence,
        principal_id,
        scopes,
    ):
        return self.service.offer(
            namespace,
            left_key=subject["record_key"],
            right_key=right_key,
            left_entity=subject["entity_id"],
            right_entity=right_entity,
            basis=basis,
            evidence=[evidence],
            principal_id=principal_id,
            scopes=scopes,
        )

    def _political(self, namespace, parties, jurisdictions, principal_id, scopes):
        """A label the Political pack's scoped alias table resolves to exactly one party in its jurisdiction."""
        if not jurisdictions or not table_exists(self.conn, "political_aliases"):
            return []
        from src.domains.political.model import resolve_alias

        offered = []
        for subject in parties:
            scope = jurisdictions.get(_country(subject["jurisdiction"]))
            if not scope:
                continue  # never resolved without the issuing jurisdiction
            found = resolve_alias(
                self.conn, subject["label"], object_type="party", jurisdiction_id=scope
            )
            if found["status"] != "resolved":
                continue
            party = found["object"]
            key = f"political:{party['object_id']}"
            offered.append(
                self._offer(
                    namespace,
                    subject,
                    key,
                    entity_id(key),
                    "name-jurisdiction",
                    {
                        "kind": "political-alias",
                        "value": subject["label"],
                        "left": subject["side"],
                        "right": {
                            "record_key": key,
                            "object_id": party["object_id"],
                            "jurisdiction_id": scope,
                        },
                        "fields": ["label", "jurisdiction"],
                        "note": "a scoped alias of one Political pack party; a reviewer decides",
                    },
                    principal_id,
                    scopes,
                )
            )
        return offered

    def _canonical(self, namespace, parties, principal_id, scopes):
        if not table_exists(self.conn, "entity_aliases"):
            return []
        from src.kb.entities import resolve

        offered = []
        for subject in parties:
            found = resolve(self.conn, subject["label"])
            if not found:
                continue
            key = f"canonical:{found['canonical_id']}"
            offered.append(
                self._offer(
                    namespace,
                    subject,
                    key,
                    found["canonical_id"],
                    "similar-name",
                    {
                        "kind": "name",
                        "value": subject["label"],
                        "left": subject["side"],
                        "right": {
                            "record_key": key,
                            "canonical_id": found["canonical_id"],
                        },
                        "fields": ["label"],
                        "note": "a name alone is never an identity",
                    },
                    principal_id,
                    scopes,
                )
            )
        return offered

    # ------------------------------------------------------------------ reads

    def candidates(
        self, namespace: str, *, scopes: Iterable[str], record_key: str | None = None
    ) -> list[dict[str, Any]]:
        rows = [
            c
            for c in self.service.candidates(
                namespace, scopes=scopes, record_key=record_key
            )
            if c["left_key"].startswith("elections:")
            or c["right_key"].startswith("elections:")
        ]
        return [self.view(c) for c in rows]

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
            "reason": last.get("reason"),
            "evidence": candidate["evidence"],
            "history": candidate["history"],
            "notice": "a reviewable identity decision; election records are never merged or edited",
        }

    def linked(self, namespace: str, record_key: str) -> list[str]:
        """The record and every election record joined to it by accepted, unreverted decisions."""
        if not table_exists(self.conn, "ownership_identity_candidates"):
            return [record_key]
        rows = self.conn.execute(
            "SELECT left_key, right_key FROM ownership_identity_candidates WHERE namespace=? AND state='accepted' "
            "AND left_key LIKE 'elections:%' AND right_key LIKE 'elections:%' ORDER BY candidate_id",
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

    def identity(
        self, namespace: str, record_key: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Accepted links and open candidates for one record; without an accepted link it is the source string."""
        try:
            views = self.candidates(namespace, scopes=scopes, record_key=record_key)
        except Exception as exc:  # noqa: BLE001 - identity access is optional for an answer
            return {
                "state": "unmatched",
                "links": [],
                "candidates": [],
                "unavailable": [getattr(exc, "code", "x")],
            }
        links = [v for v in views if v["state"] == "accepted"]
        return {
            "state": "matched" if links else "unmatched",
            "links": links,
            "candidates": [v for v in views if v["state"] == "proposed"],
        }

    def party_results(
        self,
        namespace: str,
        record_key: str,
        *,
        scopes: Iterable[str],
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Results of a party list across the elections its accepted identity decisions join, each figure cited.

        Figures stay per election and contest as published; nothing is summed or made comparable.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        rows = []
        for key in self.linked(namespace, record_key):
            if ":party:" not in key:
                continue
            election_id = key.removeprefix("elections:").rsplit(":party:", 1)[0]
            party_slug = key.rsplit(":party:", 1)[1]
            for contest in self.store.contests(namespace, election_id=election_id):
                vintage = self.store.in_force(namespace, contest["contest_id"], as_of)
                if vintage is None:
                    continue
                for entry in vintage["figures"].get("entries") or []:
                    if slug(entry.get("party") or "") != party_slug:
                        continue
                    rows.append(
                        {
                            "record_key": key,
                            "election_id": election_id,
                            "contest_id": contest["contest_id"],
                            "unit": {
                                "scheme": contest["unit_scheme"],
                                "native_id": contest["unit_native_id"],
                            },
                            "ballot": contest["ballot"],
                            "entry": entry["key"],
                            "label_as_published": entry["name"],
                            "votes": entry.get("votes"),
                            "share_published": entry.get("share_published"),
                            "vintage_kind": vintage["kind"],
                            "certified": vintage["kind"] in CERTIFIED_CLASS,
                            "vintage_id": vintage["vintage_id"],
                            "source_revision": vintage["source_revision"],
                        }
                    )
        return {
            "record_key": record_key,
            "joined_records": self.linked(namespace, record_key),
            "identity": self.identity(namespace, record_key, scopes=scopes),
            "results": sorted(
                rows,
                key=lambda r: (
                    r["election_id"],
                    r["unit"]["scheme"],
                    r["unit"]["native_id"],
                    r["ballot"],
                    r["entry"],
                ),
            ),
            "lineage": self.lineage(namespace, record_key, scopes=scopes),
        }

    # ------------------------------------------------------------------ assertions

    def _known(self, namespace: str, key: str) -> bool:
        if key.startswith("elections:constituency:"):
            return any(
                c["record_key"] == key for c in self.store.constituencies(namespace)
            )
        if key.startswith("elections:poll-series:"):
            return True
        return bool(
            self.conn.execute(
                "SELECT 1 FROM election_candidates WHERE namespace=? AND record_key=?",
                [namespace, key],
            ).fetchone()
        ) or key.startswith("elections:label:")

    def assert_relation(
        self,
        namespace: str,
        *,
        kind: str,
        subject_key: str,
        object_key: str,
        valid_on: str,
        source: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Record a dated, cited assertion (successor party, merger, renaming, redistricting); never a merge.

        ``subject_key`` precedes ``object_key`` (predecessor -> successor). A party named by no election record yet
        can be addressed as ``elections:label:<country>:<label>``. A constituency successor cites the official
        redistricting source (``source.official = true``) and never transfers votes.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if kind not in ASSERTION_KINDS or subject_key == object_key:
            raise ElectionError(
                "invalid_assertion",
                f"assert one of {ASSERTION_KINDS} between two records",
            )
        source = json.loads(canonical(dict(source)))
        if not str(source.get("url") or "").startswith("https://") or not source.get(
            "title"
        ):
            raise ElectionError(
                "invalid_assertion",
                "an assertion cites its source (https url and title)",
            )
        constituency = kind == "constituency_successor"
        for key in (subject_key, object_key):
            if constituency != key.startswith("elections:constituency:"):
                raise ElectionError(
                    "invalid_assertion",
                    "constituency assertions link constituencies only",
                )
            if not self._known(namespace, key):
                raise ElectionError(
                    "not_found", f"{key} is not an election record in this namespace"
                )
        if constituency and source.get("official") is not True:
            raise ElectionError(
                "invalid_assertion",
                "a boundary change cites the official redistricting source (official=true)",
            )
        return self._append(
            namespace,
            kind,
            subject_key,
            object_key,
            _day(valid_on),
            source,
            reason,
            "asserted",
            principal_id,
        )

    def retract(
        self,
        namespace: str,
        assertion_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Retract an assertion (a new revision); answers return to what they were without it."""
        authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
        current = self._current(namespace, assertion_id)
        if current is None or current["state"] != "asserted":
            raise ElectionError(
                "invalid_state", "only an asserted assertion can be retracted"
            )
        if not str(reason or "").strip():
            raise ElectionError("invalid_assertion", "a retraction needs a reason")
        return self._append(
            namespace,
            current["kind"],
            current["subject_key"],
            current["object_key"],
            current["valid_on"],
            current["source"],
            reason,
            "retracted",
            principal_id,
        )

    def _append(
        self,
        namespace,
        kind,
        subject_key,
        object_key,
        valid_on,
        source,
        reason,
        state,
        principal_id,
    ):
        assertion_id = (
            "election-assertion:"
            + digest([namespace, kind, subject_key, object_key, valid_on, source])[:24]
        )
        current = self._current(namespace, assertion_id)
        if current is not None and current["state"] == state:
            return {**current, "idempotent": True}
        number = 1 if current is None else current["revision_no"] + 1
        self.conn.execute(
            "INSERT INTO election_assertions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                assertion_id,
                number,
                kind,
                subject_key,
                object_key,
                valid_on,
                canonical(source),
                reason,
                state,
                principal_id,
                self.now(),
            ],
        )
        return self._current(namespace, assertion_id)

    def _current(self, namespace: str, assertion_id: str) -> dict[str, Any] | None:
        if not table_exists(self.conn, "election_assertions"):
            return None
        row = self.conn.execute(
            "SELECT assertion_id, revision_no, kind, subject_key, object_key, valid_on, source_json, reason, state, "
            "recorded_by, recorded_at_ms FROM election_assertions WHERE namespace=? AND assertion_id=? "
            "ORDER BY revision_no DESC LIMIT 1",
            [namespace, assertion_id],
        ).fetchone()
        if row is None:
            return None
        view = dict(
            zip(
                (
                    "assertion_id",
                    "revision_no",
                    "kind",
                    "subject_key",
                    "object_key",
                    "valid_on",
                    "source",
                    "reason",
                    "state",
                    "recorded_by",
                    "recorded_at_ms",
                ),
                row,
            )
        )
        view["source"] = _load(view["source"], {})
        return view

    def lineage(
        self, namespace: str, record_key: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Active assertions about a record (and its accepted identity group), conflicting ones side by side."""
        authorize(namespace, set(scopes), READ_SCOPE)
        if not table_exists(self.conn, "election_assertions"):
            return {"assertions": [], "conflicts": []}
        keys = set(self.linked(namespace, record_key))
        ids = [
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT assertion_id FROM election_assertions WHERE namespace=? AND "
                "(list_contains(?, subject_key) OR list_contains(?, object_key)) ORDER BY assertion_id",
                [namespace, sorted(keys), sorted(keys)],
            ).fetchall()
        ]
        active = [
            a
            for a in (self._current(namespace, i) for i in ids)
            if a and a["state"] == "asserted"
        ]
        active.sort(key=lambda a: (a["valid_on"], a["assertion_id"]))
        conflicts = []
        by_subject: dict[tuple[str, str], set[str]] = {}
        for item in active:
            by_subject.setdefault((item["kind"], item["subject_key"]), set()).add(
                item["object_key"]
            )
        for (kind, subject), objects in sorted(by_subject.items()):
            if len(objects) > 1:
                conflicts.append(
                    {
                        "kind": kind,
                        "subject_key": subject,
                        "asserted_successors": sorted(objects),
                        "note": "sources disagree; every assertion is kept with its own source",
                    }
                )
        return {
            "assertions": active,
            "conflicts": conflicts,
            "note": "dated, cited assertions only; no votes are transferred and no figures made comparable",
        }


__all__ = ["ASSERTION_KINDS", "ElectionIdentity", "poll_record_key"]
