"""Links from declared interests and meetings to legislative dossiers (#1911, T07).

A declaration is *linked* to a dossier in :mod:`src.domains.political.legislative_dossiers`
only when

* a register field names an identifier the dossier's committed document
  revisions carry - an EU procedure reference, CELEX or ELI identifier for an
  EU dossier, or a Bundestag printed-paper (Drucksache) number for a German
  dossier's proposal or amendment stage (``explicit-field``); the issuing
  jurisdiction must match, and matching is on canonical keys, never on text
  similarity; or
* a reviewer accepted a candidate (``reviewed-assertion``), recording reviewer,
  reason, evidence and time; it can be reverted.

Free-text subjects that only share words with a dossier title are discovery
*candidates*, never links. Every link cites both the register revision and the
dossier revision it was made against; a later dossier revision gets its own
links and earlier ones stay attached to the revision they cited - nothing is
re-pointed. Dossier stage semantics are untouched.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.lobbying_sources import REFERENCE_SCHEMES, reference_key
from src.kb.lobbying import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    LobbyingError,
    LobbyingStore,
    authorize,
    canonical,
    digest,
)

CONTRACT = "noesis-lobbying-dossier-link-v1"
STATES = ("linked", "candidate", "accepted", "rejected", "reverted", "superseded")
LINKING_STATES = ("linked", "accepted")
# Stages whose official identifier can be a printed-paper (Drucksache) number; plenary protocols share the
# number shape and are never matched.
DRUCKSACHE_STAGES = frozenset({"proposal", "amendment"})
_STOPWORDS = frozenset(
    {
        "about",
        "amending",
        "amendment",
        "council",
        "decision",
        "directive",
        "draft",
        "entwurf",
        "eines",
        "european",
        "gesetz",
        "gesetzes",
        "gesetzentwurf",
        "parliament",
        "proposal",
        "regarding",
        "regulation",
        "their",
        "there",
        "these",
        "views",
        "which",
        "with",
        "exchange",
        "meeting",
    }
)
_DDL = """
CREATE TABLE IF NOT EXISTS lobbying_dossier_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, dossier_namespace TEXT NOT NULL, dossier_id TEXT NOT NULL,
  dossier_revision BIGINT NOT NULL, entry_id TEXT NOT NULL, revision_id TEXT NOT NULL, interest_key TEXT NOT NULL,
  basis TEXT NOT NULL, state TEXT NOT NULL, reference_json TEXT, stage_json TEXT, evidence_json TEXT NOT NULL,
  history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def _tokens(text: Any) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-zäöüß]{5,}", str(text or "").casefold())
        if w not in _STOPWORDS
    }


def stage_keys(dossier: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Canonical reference keys the dossier's stages carry, with the stage and citation each came from."""
    keys: dict[str, list[dict[str, Any]]] = {}
    jurisdiction = dossier["jurisdiction"]
    for stage in dossier["stages"]:
        values = list(stage.get("procedure_identifiers") or [])
        values += [
            stage.get("normalized_document_id"),
            stage.get("normalized_instrument_id"),
        ]
        for value in [v for v in values if v]:
            local = str(value).rsplit(":", 1)[-1]
            for scheme, scope in REFERENCE_SCHEMES.items():
                if scope != jurisdiction:
                    continue
                if (
                    scheme == "de-drucksache"
                    and stage.get("stage") not in DRUCKSACHE_STAGES
                ):
                    continue
                key = reference_key(scheme, local)
                if key:
                    keys.setdefault(key, []).append(
                        {
                            "stage_id": stage["stage_id"],
                            "stage": stage["stage"],
                            "identifier": value,
                            "citation": stage["citation"],
                        }
                    )
    return keys


class LobbyingDossierLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = LobbyingStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='lobbying_dossier_links'"
            ).fetchone()
        )

    def _dossier(
        self, dossier_namespace, dossier_id, principal_id, scopes, revision=None
    ):
        from src.domains.political.legislative_dossiers import (
            DossierError,
            LegislativeDossierStore,
        )

        dossiers = LegislativeDossierStore(self.conn, initialize=False)
        try:
            current = dossiers._state(dossier_namespace, dossier_id)
            return dossiers._full(
                dossier_namespace,
                dossier_id,
                revision or current["revision"],
                principal_id,
                set(scopes),
            )
        except DossierError as exc:
            raise LobbyingError(exc.code, str(exc)) from exc

    def _revisions(self, namespace: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT revision_id FROM lobbying_revisions WHERE namespace=? AND statement_json IS NOT NULL "
            "ORDER BY entry_id, revision_no",
            [namespace],
        ).fetchall()
        return [self.store.revision(namespace, r[0]) for r in rows]

    def link_dossier(
        self,
        namespace: str,
        dossier_namespace: str,
        dossier_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Link declarations to the dossier's current revision by explicit fields; propose word-overlap candidates.

        Idempotent. Links made against earlier dossier revisions are kept as they were.
        """
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        dossier = self._dossier(dossier_namespace, dossier_id, principal_id, scopes)
        keys = stage_keys(dossier)
        titles = {stage["stage_id"]: stage.get("title") for stage in dossier["stages"]}
        linked, proposed = [], []
        latest_by_entry: dict[str, str] = {}
        for entry_id in {
            r[0]
            for r in self.conn.execute(
                "SELECT DISTINCT entry_id FROM lobbying_revisions WHERE namespace=?",
                [namespace],
            ).fetchall()
        }:
            current = self.store.in_force(namespace, entry_id)
            if current is not None and current["statement"] is not None:
                latest_by_entry[entry_id] = current["revision_id"]
        explicit_interests, explicit_entries = set(), set()
        for revision in self._revisions(namespace):
            for interest in self.store.interests(revision):
                for ref in interest["references"]:
                    if (
                        ref.get("jurisdiction") != dossier["jurisdiction"]
                        or ref.get("key") not in keys
                    ):
                        continue
                    for stage in keys[ref["key"]]:
                        link_id = (
                            "lobbying-link:"
                            + digest(
                                [
                                    namespace,
                                    dossier_namespace,
                                    dossier_id,
                                    dossier["revision"],
                                    interest["interest_key"],
                                    ref["key"],
                                    stage["stage_id"],
                                ]
                            )[:24]
                        )
                        created = self._insert(
                            namespace,
                            link_id,
                            dossier_namespace,
                            dossier_id,
                            dossier["revision"],
                            revision,
                            interest["interest_key"],
                            "explicit-field",
                            "linked",
                            ref,
                            stage,
                            {
                                "register_field": ref.get("source_field"),
                                "extracted_from_text": ref.get("extracted_from_text"),
                                "dossier_identifier": stage["identifier"],
                            },
                            principal_id,
                        )
                        explicit_interests.add(interest["interest_key"])
                        if (
                            latest_by_entry.get(revision["entry_id"])
                            == revision["revision_id"]
                        ):
                            explicit_entries.add(revision["entry_id"])
                        if created:
                            linked.append(link_id)
        for (link_id,) in self.conn.execute(
            "SELECT link_id FROM lobbying_dossier_links WHERE namespace=? AND dossier_namespace=? AND dossier_id=? "
            "AND state='candidate' ORDER BY link_id",
            [namespace, dossier_namespace, dossier_id],
        ).fetchall():
            pending = self.link(namespace, link_id, scopes=scopes)
            if (
                pending["interest_key"] in explicit_interests
                or pending["entry_id"] in explicit_entries
            ):
                # Stronger evidence arrived: a register field (of this revision, or of the entry's revision now in
                # force) names the dossier, so the pending candidate is superseded by the explicit link rather
                # than left for review.
                self._transition(
                    namespace,
                    pending,
                    "superseded",
                    principal_id,
                    "an explicit register field now links this declaration",
                )
        for revision in self._revisions(namespace):
            if latest_by_entry.get(revision["entry_id"]) != revision["revision_id"]:
                continue  # discovery runs on the statement in force; earlier revisions keep their own links
            for interest in self.store.interests(revision):
                if (
                    interest["interest_key"] in explicit_interests
                    or revision["entry_id"] in explicit_entries
                ):
                    continue  # already linked by a register field; words add nothing
                words = _tokens(interest["text"])
                for stage_id, title in titles.items():
                    shared = sorted(words & _tokens(title))
                    if len(shared) < 2:
                        continue
                    link_id = (
                        "lobbying-candidate:"
                        + digest(
                            [
                                namespace,
                                dossier_namespace,
                                dossier_id,
                                interest["interest_key"],
                                stage_id,
                            ]
                        )[:24]
                    )
                    stage = next(
                        s for s in dossier["stages"] if s["stage_id"] == stage_id
                    )
                    created = self._insert(
                        namespace,
                        link_id,
                        dossier_namespace,
                        dossier_id,
                        dossier["revision"],
                        revision,
                        interest["interest_key"],
                        "discovery",
                        "candidate",
                        None,
                        {
                            "stage_id": stage_id,
                            "stage": stage["stage"],
                            "identifier": None,
                            "citation": stage["citation"],
                        },
                        {
                            "shared_words": shared,
                            "subject": interest["text"],
                            "dossier_title": title,
                            "note": "shared words are a discovery candidate, not a link",
                        },
                        principal_id,
                    )
                    if created:
                        proposed.append(link_id)
        return {
            "dossier_id": dossier_id,
            "dossier_revision": dossier["revision"],
            "linked": linked,
            "candidates": proposed,
            "links": self.links(
                namespace, dossier_namespace, dossier_id, scopes=scopes
            ),
        }

    def _insert(
        self,
        namespace,
        link_id,
        dossier_namespace,
        dossier_id,
        dossier_revision,
        revision,
        interest_key,
        basis,
        state,
        ref,
        stage,
        evidence,
        principal_id,
    ) -> bool:
        if self.conn.execute(
            "SELECT 1 FROM lobbying_dossier_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone():
            return False
        now = self.now()
        self.conn.execute(
            "INSERT INTO lobbying_dossier_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                link_id,
                dossier_namespace,
                dossier_id,
                int(dossier_revision),
                revision["entry_id"],
                revision["revision_id"],
                interest_key,
                basis,
                state,
                None if ref is None else canonical(ref),
                canonical(stage),
                canonical(evidence),
                canonical([{"state": state, "by": principal_id, "at_ms": now}]),
                principal_id,
                now,
            ],
        )
        return True

    def propose(
        self,
        namespace: str,
        revision_id: str,
        interest_key: str,
        dossier_namespace: str,
        dossier_id: str,
        *,
        evidence: Mapping[str, Any],
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """A reviewer's candidate assertion that a declaration concerns a dossier (reviewed before it links)."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if not str(evidence.get("reason") or "").strip():
            raise LobbyingError(
                "invalid_request", "a proposed assertion states its evidence (reason)"
            )
        revision = self.store.revision(namespace, revision_id)
        if interest_key not in {
            i["interest_key"] for i in self.store.interests(revision)
        }:
            raise LobbyingError(
                "not_found", "the register revision has no such declaration"
            )
        dossier = self._dossier(dossier_namespace, dossier_id, principal_id, scopes)
        link_id = (
            "lobbying-candidate:"
            + digest(
                [namespace, dossier_namespace, dossier_id, interest_key, "reviewer"]
            )[:24]
        )
        self._insert(
            namespace,
            link_id,
            dossier_namespace,
            dossier_id,
            dossier["revision"],
            revision,
            interest_key,
            "reviewer-proposed",
            "candidate",
            None,
            {"stage_id": None, "stage": None, "identifier": None, "citation": None},
            dict(evidence),
            principal_id,
        )
        return self.link(namespace, link_id, scopes=scopes)

    def _transition(self, namespace, link, state, principal_id, reason, extra=None):
        history = link["history"] + [
            {
                "state": state,
                "by": principal_id,
                "reason": reason,
                "at_ms": self.now(),
                **(extra or {}),
            }
        ]
        self.conn.execute(
            "UPDATE lobbying_dossier_links SET state=?, history_json=? WHERE namespace=? AND link_id=?",
            [state, canonical(history), namespace, link["link_id"]],
        )
        return self.link(namespace, link["link_id"], scopes={"operator"})

    def review(
        self,
        namespace: str,
        link_id: str,
        decision: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Accept (a reviewed assertion that links) or reject a candidate, recording reviewer, evidence and time."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise LobbyingError("invalid_decision", "accept or reject with a reason")
        link = self.link(namespace, link_id, scopes=scopes)
        if link["state"] != "candidate":
            raise LobbyingError(
                "invalid_state",
                f"link is {link['state']}; only a candidate is reviewed",
            )
        return self._transition(
            namespace,
            link,
            "accepted" if decision == "accept" else "rejected",
            principal_id,
            reason.strip(),
            {"evidence": dict(evidence or {})},
        )

    def revert(
        self,
        namespace: str,
        link_id: str,
        reason: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Undo a reviewed decision; the declaration is no longer linked. Explicit register fields are not decisions."""
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise LobbyingError("invalid_decision", "a revert needs a reason")
        link = self.link(namespace, link_id, scopes=scopes)
        if link["state"] not in {"accepted", "rejected"}:
            raise LobbyingError(
                "invalid_state",
                "only an accepted or rejected assertion can be reverted; an "
                "explicit register field is the register's own statement",
            )
        return self._transition(
            namespace, link, "reverted", principal_id, reason.strip()
        )

    # ------------------------------------------------------------------ reads

    def link(
        self, namespace: str, link_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        authorize(namespace, set(scopes), READ_SCOPE)
        row = self.conn.execute(
            "SELECT link_id, dossier_namespace, dossier_id, dossier_revision, entry_id, revision_id, interest_key, "
            "basis, state, reference_json, stage_json, evidence_json, history_json, created_by, created_at_ms "
            "FROM lobbying_dossier_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            raise LobbyingError(
                "not_found", "dossier link is not visible in this namespace"
            )
        view = dict(
            zip(
                (
                    "link_id",
                    "dossier_namespace",
                    "dossier_id",
                    "dossier_revision",
                    "entry_id",
                    "revision_id",
                    "interest_key",
                    "basis",
                    "state",
                ),
                row[:9],
            )
        )
        view.update(
            reference=json.loads(row[9]) if row[9] else None,
            stage=json.loads(row[10]),
            evidence=json.loads(row[11]),
            history=json.loads(row[12]),
            created_by=row[13],
            created_at_ms=row[14],
        )
        view["link_kind"] = (
            "explicit-field"
            if view["basis"] == "explicit-field"
            else "reviewed-assertion"
            if view["state"] == "accepted"
            else "unreviewed-candidate"
            if view["state"] == "candidate"
            else view["state"]
        )
        last = view["history"][-1]
        view["reviewer"] = (
            last.get("by")
            if view["state"] in {"accepted", "rejected", "reverted"}
            else None
        )
        view["reviewed_at_ms"] = last.get("at_ms") if view["reviewer"] else None
        return {"contract": CONTRACT, "namespace": namespace, **view}

    def links(
        self,
        namespace: str,
        dossier_namespace: str,
        dossier_id: str,
        *,
        scopes: Iterable[str],
        states: Iterable[str] | None = None,
        dossier_revision: int | None = None,
    ) -> list[dict[str, Any]]:
        """Links and candidates for a dossier; with ``dossier_revision``, those made against it or earlier ones."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        wanted = set(states or STATES)
        rows = self.conn.execute(
            "SELECT link_id FROM lobbying_dossier_links WHERE namespace=? AND dossier_namespace=? AND dossier_id=? "
            "AND (? IS NULL OR dossier_revision<=?) ORDER BY dossier_revision, entry_id, revision_id, link_id",
            [
                namespace,
                dossier_namespace,
                dossier_id,
                dossier_revision,
                dossier_revision,
            ],
        ).fetchall()
        return [
            v
            for v in (self.link(namespace, r[0], scopes=scopes) for r in rows)
            if v["state"] in wanted
        ]

    def links_for_revision(
        self, namespace: str, revision_id: str, *, scopes: Iterable[str]
    ) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        rows = self.conn.execute(
            "SELECT link_id FROM lobbying_dossier_links WHERE namespace=? AND revision_id=? "
            "ORDER BY link_id",
            [namespace, revision_id],
        ).fetchall()
        return [self.link(namespace, r[0], scopes=scopes) for r in rows]

    def dossier_entries(
        self,
        namespace: str,
        dossier_namespace: str,
        dossier_id: str,
        revision: int,
        *,
        scopes: Iterable[str],
    ) -> list[dict[str, Any]]:
        """Declared-interest and meeting entries for a dossier timeline (linked or reviewed; each with its sources)."""
        entries = []
        for link in self.links(
            namespace,
            dossier_namespace,
            dossier_id,
            scopes=scopes,
            states=LINKING_STATES,
            dossier_revision=revision,
        ):
            register_revision = self.store.revision(namespace, link["revision_id"])
            statement = register_revision["statement"] or {}
            meeting = statement.get("entry_kind") == "meeting"
            entries.append(
                {
                    "event_kind": "meeting" if meeting else "declared_interest",
                    "date": statement.get("date")
                    if meeting
                    else register_revision["effective_on"],
                    "register": register_revision["register"],
                    "native_id": register_revision["native_id"],
                    "declarant": statement.get("name")
                    or [o.get("name") for o in statement.get("organisations") or []],
                    "official": statement.get("official") if meeting else None,
                    "link_id": link["link_id"],
                    "link_kind": link["link_kind"],
                    "reference": link["reference"],
                    "cited_dossier_revision": link["dossier_revision"],
                    "stage_id": link["stage"].get("stage_id"),
                    "register_revision_id": register_revision["revision_id"],
                    "source_revision": register_revision["source_revision"],
                    "stage_semantics": "unchanged; a declaration is not a legislative stage",
                }
            )
        entries.sort(
            key=lambda e: (e["date"] or "", e["register"], e["native_id"], e["link_id"])
        )
        return entries
