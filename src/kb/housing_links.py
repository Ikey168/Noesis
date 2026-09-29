"""Rent-index editions and development plans linked to legal works and decisions by citation (#1912, U08).

A subject (a rent-index edition or a development plan) is *linked* only through
an explicit citation the source publishes:

* a Mietspiegel edition's declared references (the Amtsblatt notice that
  publishes it, the Berlin and federal provisions it is based on), and
* a plan layer's reference attributes (the Amtsblatt notice of an
  Aufstellungsbeschluss, the GVBl publication of a Festsetzung), each tied to
  the stage and stage date it belongs to,

whose identifier equals an identifier of exactly one work in ``legal.works``
(:class:`src.kb.legal.LegalStore`) in the reference's jurisdiction (GVBl,
Amtsblatt and juris references in Berlin, BGBl in Germany; never another
jurisdiction), or - for a printed-paper reference - a stage key of a
legislative dossier (``political.knowledge``). A reference matching nothing
stays ``unresolved`` with the publisher's string, the decision identifier and
the stage date; it is never guessed.

Shared words between a plan or edition and a work title (a name or place
similarity) are at most a *discovery candidate*, never a link; a reviewer
other than the proposer accepts or rejects it and every review can be
reverted. An explicit citation supersedes pending candidates for the same
pair. News items that mention a plan or an edition are returned as discovery
context with their source only - never stored as evidence of a stage or a
value. Nothing in a link is a summary of the law or tenancy advice: a link
shows the work, the passage locator, the decision and their sources.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Sequence
from typing import Any

from src.ingestion.housing_sources import LINK_RELATIONS, REFERENCE_SCHEMES, plan_key
from src.kb.housing import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    HousingError,
    HousingStore,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

CONTRACT = "noesis-housing-link-v1"
LEGAL_READ = "knowledge:legal:read"
DOSSIER_READ = "knowledge:political:dossier:read"
NEWS_READ = "knowledge:read"
SUBJECT_KINDS = ("rent-index-edition", "plan")
STATES = (
    "linked",
    "unresolved",
    "candidate",
    "accepted",
    "rejected",
    "reverted",
    "superseded",
)
LINKING_STATES = ("linked", "accepted")
PENDING_STATES = ("candidate",)
LEGAL_SCHEMES = ("berlin-gvbl", "berlin-amtsblatt", "juris", "de-bgbl", "celex", "eli")
NO_ADVICE = "a citation link: it shows the work, passage, decision and sources and is not tenancy or legal advice"
_DDL = """
CREATE TABLE IF NOT EXISTS housing_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_namespace TEXT, target_id TEXT, target_revision TEXT, relation TEXT NOT NULL,
  basis TEXT NOT NULL, state TEXT NOT NULL, reference_json TEXT, evidence_json TEXT NOT NULL,
  history_json TEXT NOT NULL, source_record_id TEXT, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def reference_identifier(scheme: str, value: Any) -> str:
    """The identifier as the Legal store keeps it: single spaces; CELEX upper-case."""
    text = " ".join(str(value or "").split())
    return text.upper() if scheme == "celex" else text


def _tokens(text: Any) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-zäöüß0-9-]{3,}", str(text or "").casefold())
        if w
        not in {
            "berlin",
            "fiktiv",
            "gesetz",
            "verordnung",
            "über",
            "und",
            "der",
            "die",
            "das",
            "des",
        }
    }


class HousingLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = HousingStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "housing_links")

    # ------------------------------------------------------------------ explicit references

    def references(self, namespace: str) -> list[dict[str, Any]]:
        """Every explicit reference the current edition and plan-stage records publish, with their record."""
        out = []
        for edition in self.store.editions(namespace):
            for ref in edition.get("references") or []:
                out.append(
                    {
                        "subject": {
                            "kind": "rent-index-edition",
                            "id": edition["edition_id"],
                        },
                        "reference": dict(ref),
                        "record_id": edition["record_id"],
                        "stated_in": edition["source_revision"],
                        "stage": None,
                    }
                )
        for stage in self.store.records("plan_stage", namespace):
            for ref in stage.get("references") or []:
                if ref.get("stage") and ref["stage"] != stage["stage"]:
                    # The reference belongs to another stage of the plan; it is linked from that stage's record.
                    if any(
                        s["stage"] == ref["stage"]
                        for s in self.store.records(
                            "plan_stage", namespace, plan_key=plan_key(stage["plan_id"])
                        )
                    ):
                        continue
                out.append(
                    {
                        "subject": {"kind": "plan", "id": plan_key(stage["plan_id"])},
                        "reference": dict(ref),
                        "record_id": stage["record_id"],
                        "stated_in": stage["source_revision"],
                        "stage": {
                            "stage": ref.get("stage") or stage["stage"],
                            "stage_label": stage["stage_label"],
                            "stage_date": stage["stage_date"]
                            if (ref.get("stage") or stage["stage"]) == stage["stage"]
                            else None,
                            "plan_id": stage["plan_id"],
                        },
                    }
                )
        return out

    def _insert(
        self,
        namespace,
        link_id,
        subject,
        target,
        relation,
        basis,
        state,
        reference,
        evidence,
        source_record_id,
        principal_id,
    ) -> bool:
        if self.conn.execute(
            "SELECT 1 FROM housing_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone():
            return False
        now = self.now()
        self.conn.execute(
            "INSERT INTO housing_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                namespace,
                link_id,
                subject["kind"],
                subject["id"],
                target["kind"],
                target.get("namespace"),
                target.get("id"),
                None if target.get("revision") is None else str(target["revision"]),
                relation,
                basis,
                state,
                None if reference is None else canonical(reference),
                canonical(evidence),
                canonical([{"state": state, "by": principal_id, "at_ms": now}]),
                source_record_id,
                principal_id,
                now,
            ],
        )
        return True

    def _supersede(self, namespace, subject_id, target_id, principal_id) -> None:
        for (link_id,) in self.conn.execute(
            "SELECT link_id FROM housing_links WHERE namespace=? AND subject_id=? AND target_id=? AND "
            "state IN ('candidate') ORDER BY link_id",
            [namespace, subject_id, target_id],
        ).fetchall():
            # Stronger evidence arrived: the source's own citation now links the pair.
            self._transition(
                namespace,
                self.link(namespace, link_id, scopes={"operator"}),
                "superseded",
                principal_id,
                "an explicit citation now links this pair",
            )

    def link_citations(
        self,
        namespace: str,
        *,
        legal_namespace: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Resolve each published reference to exactly one legal work in its jurisdiction, or keep it unresolved."""
        from src.kb.legal import LegalError, LegalStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, LEGAL_READ)
        linked, unresolved = [], []
        legal = (
            LegalStore(self.conn, initialize=False)
            if table_exists(self.conn, "legal_works")
            else None
        )
        for item in self.references(namespace):
            ref = item["reference"]
            scheme = ref.get("scheme")
            if scheme not in LEGAL_SCHEMES:
                continue
            identifier = reference_identifier(scheme, ref.get("identifier"))
            works: dict[str, dict[str, Any]] = {}
            statuses = []
            for jurisdiction in REFERENCE_SCHEMES[scheme]:
                if legal is None:
                    statuses.append("not_covered")
                    continue
                try:
                    found = legal.lookup(
                        legal_namespace,
                        scopes=scopes,
                        identifier=identifier,
                        jurisdiction=jurisdiction,
                    )
                except LegalError as exc:
                    raise HousingError(exc.code, str(exc)) from exc
                statuses.append(found["status"])
                for work in found["works"]:
                    works[work["work_id"]] = work
            relation = (
                ref.get("relation")
                if ref.get("relation") in LINK_RELATIONS
                else "referenced_in"
            )
            reference = {
                "scheme": scheme,
                "identifier": ref.get("identifier"),
                "relation": relation,
                "locator": ref.get("locator"),
                "attribute": ref.get("attribute"),
            }
            evidence = {
                "stated_in": item["stated_in"],
                "stage": item["stage"],
                "note": NO_ADVICE,
            }
            if len(works) == 1:
                (work,) = works.values()
                state = "linked"
                target = {
                    "kind": "legal-work",
                    "namespace": legal_namespace,
                    "id": work["work_id"],
                }
                evidence |= {
                    "work_title": work.get("title"),
                    "work_kind": work.get("work_kind"),
                    "jurisdiction": work.get("jurisdiction"),
                    "matched_identifier": identifier,
                    "matched_by": work.get("matched_by"),
                }
            else:
                state = "unresolved"
                target = {
                    "kind": "legal-work",
                    "namespace": legal_namespace,
                    "id": None,
                }
                evidence |= {
                    "lookup_status": "ambiguous"
                    if works
                    else statuses[-1]
                    if statuses
                    else "not_covered",
                    "decision_reference": identifier,
                    "note": "the cited work is not acquired (or matches several works); kept as the "
                    "publisher's string with its decision and stage date",
                }
            link_id = (
                "housing-link:"
                + digest(
                    [
                        namespace,
                        item["subject"],
                        target["id"],
                        scheme,
                        identifier,
                        relation,
                        item["record_id"],
                        state,
                    ]
                )[:24]
            )
            if self._insert(
                namespace,
                link_id,
                item["subject"],
                target,
                relation,
                "explicit_citation",
                state,
                reference,
                evidence,
                item["record_id"],
                principal_id,
            ):
                (linked if state == "linked" else unresolved).append(link_id)
            if state == "linked":
                self._supersede(
                    namespace, item["subject"]["id"], target["id"], principal_id
                )
        return {"linked": linked, "unresolved": unresolved}

    def link_dossier(
        self,
        namespace: str,
        dossier_namespace: str,
        dossier_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Link subjects to a dossier's current revision by a published printed-paper reference equal to a stage key."""
        from src.domains.political.legislative_dossiers import (
            DossierError,
            LegislativeDossierStore,
        )
        from src.ingestion.lobbying_sources import REFERENCE_SCHEMES as DOSSIER_SCHEMES
        from src.ingestion.lobbying_sources import reference_key
        from src.kb.lobbying_links import stage_keys

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, DOSSIER_READ)
        dossiers = LegislativeDossierStore(self.conn, initialize=False)
        try:
            current = dossiers._state(dossier_namespace, dossier_id)
            dossier = dossiers._full(
                dossier_namespace, dossier_id, current["revision"], principal_id, scopes
            )
        except DossierError as exc:
            raise HousingError(exc.code, str(exc)) from exc
        keys = stage_keys(dossier)
        linked = []
        for item in self.references(namespace):
            ref = item["reference"]
            scheme = ref.get("scheme")
            if (
                scheme not in DOSSIER_SCHEMES
                or DOSSIER_SCHEMES[scheme] != dossier["jurisdiction"]
            ):
                continue
            key = reference_key(scheme, ref.get("identifier"))
            for stage in keys.get(key or "", []):
                relation = (
                    ref.get("relation")
                    if ref.get("relation") in LINK_RELATIONS
                    else "referenced_in"
                )
                target = {
                    "kind": "dossier",
                    "namespace": dossier_namespace,
                    "id": dossier_id,
                    "revision": dossier["revision"],
                }
                link_id = (
                    "housing-link:"
                    + digest(
                        [
                            namespace,
                            item["subject"],
                            "dossier",
                            dossier_namespace,
                            dossier_id,
                            dossier["revision"],
                            key,
                            stage["stage_id"],
                            relation,
                            item["record_id"],
                        ]
                    )[:24]
                )
                if self._insert(
                    namespace,
                    link_id,
                    item["subject"],
                    target,
                    relation,
                    "explicit_citation",
                    "linked",
                    {"scheme": scheme, "identifier": ref.get("identifier"), "key": key},
                    {
                        "stated_in": item["stated_in"],
                        "stage": item["stage"],
                        "dossier_stage": stage,
                        "dossier_revision": dossier["revision"],
                        "note": NO_ADVICE,
                    },
                    item["record_id"],
                    principal_id,
                ):
                    linked.append(link_id)
                self._supersede(
                    namespace, item["subject"]["id"], dossier_id, principal_id
                )
        return {
            "dossier_id": dossier_id,
            "dossier_revision": dossier["revision"],
            "linked": linked,
        }

    # ------------------------------------------------------------------ discovery candidates

    def propose_candidates(
        self,
        namespace: str,
        *,
        legal_namespace: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """A plan number or edition name in a work title: a reviewable discovery candidate, never a link."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, LEGAL_READ)
        if not table_exists(self.conn, "legal_works"):
            return {"candidates": []}
        works = self.conn.execute(
            "SELECT work_id, title FROM legal_works WHERE namespace=? ORDER BY work_id",
            [legal_namespace],
        ).fetchall()
        subjects = {
            ("rent-index-edition", e["edition_id"]): (e.get("edition"), e["record_id"])
            for e in self.store.editions(namespace)
        }
        for stage in self.store.records("plan_stage", namespace):
            subjects[("plan", plan_key(stage["plan_id"]))] = (
                stage["plan_id"],
                stage["record_id"],
            )
        candidates = []
        for (kind, subject_id), (label, record_id) in sorted(subjects.items()):
            wanted = _tokens(label)
            for work_id, title in works:
                if self.conn.execute(
                    "SELECT 1 FROM housing_links WHERE namespace=? AND subject_id=? AND target_id=? AND "
                    "state IN ('linked', 'accepted', 'candidate', 'rejected')",
                    [namespace, subject_id, work_id],
                ).fetchone():
                    continue
                shared = sorted(wanted & _tokens(title))
                if kind == "plan" and str(label).casefold() not in shared:
                    continue  # a plan candidate needs its plan number in the title
                if kind == "rent-index-edition" and len(shared) < 2:
                    continue
                link_id = (
                    "housing-candidate:"
                    + digest([namespace, kind, subject_id, work_id])[:24]
                )
                if self._insert(
                    namespace,
                    link_id,
                    {"kind": kind, "id": subject_id},
                    {"kind": "legal-work", "namespace": legal_namespace, "id": work_id},
                    "referenced_in",
                    "discovery",
                    "candidate",
                    None,
                    {
                        "shared_words": shared,
                        "work_title": title,
                        "note": "a name similarity is a discovery candidate, not a link",
                    },
                    record_id,
                    principal_id,
                ):
                    candidates.append(link_id)
        return {"candidates": candidates}

    # ------------------------------------------------------------------ reviews

    def _transition(self, namespace, link, state, principal_id, reason):
        history = link["history"] + [
            {"state": state, "by": principal_id, "reason": reason, "at_ms": self.now()}
        ]
        self.conn.execute(
            "UPDATE housing_links SET state=?, history_json=? WHERE namespace=? AND link_id=?",
            [state, canonical(history), namespace, link["link_id"]],
        )
        return self.link(namespace, link["link_id"], scopes={"operator"})

    def review(self, namespace, link_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise HousingError("invalid_decision", "accept or reject with a reason")
        link = self.link(namespace, link_id, scopes={"operator"})
        if link["state"] not in PENDING_STATES:
            raise HousingError(
                "invalid_state",
                f"link is {link['state']}; only a candidate is reviewed",
            )
        if link["created_by"] == principal_id:
            raise HousingError(
                "invalid_decision",
                "a candidate is reviewed by someone other than its proposer",
            )
        return self._transition(
            namespace,
            link,
            "accepted" if decision == "accept" else "rejected",
            principal_id,
            reason.strip(),
        )

    def revert(self, namespace, link_id, reason, *, principal_id, scopes):
        """Undo a review; an explicit citation is the source's own statement and is not reverted."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise HousingError("invalid_decision", "a revert needs a reason")
        link = self.link(namespace, link_id, scopes={"operator"})
        if link["state"] not in {"accepted", "rejected"}:
            raise HousingError(
                "invalid_state",
                "only an accepted or rejected candidate can be reverted",
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
            "SELECT link_id, subject_kind, subject_id, target_kind, target_namespace, target_id, target_revision, "
            "relation, basis, state, reference_json, evidence_json, history_json, source_record_id, created_by, "
            "created_at_ms FROM housing_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            raise HousingError("not_found", "link is not visible in this namespace")
        view = dict(
            zip(
                (
                    "link_id",
                    "subject_kind",
                    "subject_id",
                    "target_kind",
                    "target_namespace",
                    "target_id",
                    "target_revision",
                    "relation",
                    "basis",
                    "state",
                ),
                row[:10],
            )
        )
        view.update(
            reference=json.loads(row[10]) if row[10] else None,
            evidence=json.loads(row[11]),
            history=json.loads(row[12]),
            source_record_id=row[13],
            created_by=row[14],
            created_at_ms=row[15],
        )
        view["counts_as_link"] = view["state"] in LINKING_STATES
        view["link_kind"] = {
            "linked": "explicit-citation",
            "accepted": "reviewed-candidate",
            "candidate": "discovery-candidate",
        }.get(view["state"], view["state"])
        view["note"] = NO_ADVICE
        return {"contract": CONTRACT, "namespace": namespace, **view}

    def links(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        subject_kind: str | None = None,
        subject_id: str | None = None,
        target_id: str | None = None,
        states: Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        wanted = set(states or STATES)
        rows = self.conn.execute(
            "SELECT link_id FROM housing_links WHERE namespace=? AND (? IS NULL OR subject_kind=?) AND "
            "(? IS NULL OR subject_id=?) AND (? IS NULL OR target_id=?) ORDER BY subject_id, created_at_ms, link_id",
            [
                namespace,
                subject_kind,
                subject_kind,
                subject_id,
                subject_id,
                target_id,
                target_id,
            ],
        ).fetchall()
        out = []
        for (link_id,) in rows:
            try:
                view = self.link(namespace, link_id, scopes={"operator"})
            except (HousingError, ValueError, TypeError) as exc:
                # One unreadable row is reported as invalid; it never hides the others.
                out.append({"link_id": link_id, "invalid": True, "reason": str(exc)})
                continue
            if view["state"] in wanted:
                out.append(view)
        return out

    def current_record_ids(self, namespace: str) -> set[str]:
        """The current edition and plan-stage records; a link stated by a superseded record is history."""
        return {r["record_id"] for r in self.store.editions(namespace)} | {
            r["record_id"] for r in self.store.records("plan_stage", namespace)
        }

    # ------------------------------------------------------------------ news context

    def news_context(
        self, terms: Sequence[str], *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """News items whose title or text names a plan or an edition: discovery context with its source only."""
        if not table_exists(self.conn, "documents"):
            return []
        out = []
        for term in sorted({t for t in terms if t and len(str(t)) >= 3}):
            rows = self.conn.execute(
                "SELECT document_id, source_type, source_id, url, content_hash, title, created_at, ingested_at "
                "FROM documents WHERE source_type IN ('news', 'blog') AND (strpos(lower(coalesce(title, '')), "
                "lower(?)) > 0 OR strpos(lower(coalesce(content, '')), lower(?)) > 0) ORDER BY document_id LIMIT ?",
                [term, term, limit],
            ).fetchall()
            for row in rows:
                out.append(
                    {
                        "mentions": term,
                        "document_id": row[0],
                        "title": row[5],
                        "source": {
                            "source_type": row[1],
                            "source_id": row[2],
                            "url": row[3],
                            "content_hash": row[4],
                            "created_at": row[6],
                            "ingested_at": row[7],
                        },
                        "role": "discovery context only; not evidence of a stage or a value",
                    }
                )
        return out
