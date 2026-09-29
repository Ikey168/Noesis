"""Demographic series linked to legal acts, court decisions and legislative dossiers by citation (#1914, M08).

A series is *linked* only through an explicit citation:

* a **publisher reference** - a regulation, directive, act or decision the
  publisher names in the dataset metadata or the statistical release, declared
  per document from that page - whose identifier (CELEX, ELI, ECLI, BGBl)
  equals an identifier of exactly one work in ``legal.works``
  (:class:`src.kb.legal.LegalStore`, never another jurisdiction: an ECLI is
  looked up in its issuing country), or whose printed-paper number or EU
  procedure reference equals a stage key of a legislative dossier
  (:mod:`src.domains.political.legislative_dossiers`, the normalisation the
  lobbying feature uses; jurisdictions must match);
* a **reviewed assertion** - a user asserts a link with a passage locator or
  dossier revision, and it counts only once a reviewer accepts it.

Shared words between a definition and an act title are at most a *discovery
candidate*, marked as such, until a reviewer accepts it. Every decision is
reversible; an explicit publisher reference supersedes pending candidates and
assertions for the same pair. A link has a typed relation (``defined_by``,
``reported_under``, ``referenced_in``) and never asserts that a series
measures an act's effect, a policy effect or a cause of migration.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.demographic_sources import LINK_RELATIONS
from src.kb.demographics import (
    READ_SCOPE,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    DemographicError,
    DemographicStore,
    authorize,
    canonical,
    digest,
    require_scope,
    table_exists,
)

CONTRACT = "noesis-demographic-link-v1"
LEGAL_READ = "knowledge:legal:read"
DOSSIER_READ = "knowledge:political:dossier:read"
STATES = (
    "linked",
    "unresolved",
    "asserted",
    "candidate",
    "accepted",
    "rejected",
    "reverted",
    "superseded",
)
LINKING_STATES = ("linked", "accepted")
PENDING_STATES = ("asserted", "candidate")
ACT_SCHEMES = {"celex": ("EU",), "eli": ("EU",), "de-bgbl": ("DE",)}
DOSSIER_SCHEMES = ("de-drucksache", "eu-procedure", "celex", "eli")
NO_CAUSAL_CLAIM = (
    "a citation link: it does not assert that the series measures the act's effect, a policy effect or a cause "
    "of migration"
)
_STOPWORDS = frozenset(
    {
        "regulation",
        "directive",
        "council",
        "parliament",
        "european",
        "gesetz",
        "about",
        "their",
    }
)
_DDL = """
CREATE TABLE IF NOT EXISTS demographic_links (
  namespace TEXT NOT NULL, link_id TEXT NOT NULL, subject_kind TEXT NOT NULL, subject_id TEXT NOT NULL,
  target_kind TEXT NOT NULL, target_namespace TEXT, target_id TEXT, target_revision TEXT, relation TEXT NOT NULL,
  basis TEXT NOT NULL, state TEXT NOT NULL, reference_json TEXT, evidence_json TEXT NOT NULL,
  history_json TEXT NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL,
  PRIMARY KEY(namespace, link_id)
);
"""


def act_identifier(scheme: str, value: Any) -> str:
    """The identifier as the Legal store keeps it: CELEX and ECLI upper-case, gazette text with single spaces."""
    text = " ".join(str(value or "").split())
    return text.upper() if scheme in {"celex", "ecli"} else text


def ecli_jurisdictions(identifier: str) -> tuple[str, ...]:
    """The issuing country of an ECLI (ECLI:<country>:<court>:<year>:<number>); DE includes the Länder."""
    parts = identifier.split(":")
    if len(parts) < 5 or parts[0] != "ECLI":
        return ()
    return ("DE", "DE-BE") if parts[1] == "DE" else (parts[1],)


def _tokens(text: Any) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-zäöüß]{5,}", str(text or "").casefold())
        if w not in _STOPWORDS
    }


class DemographicLinks:
    def __init__(self, conn: Any, *, now=None, initialize: bool = True) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = DemographicStore(conn, initialize=initialize, now=self.now)
        if initialize:
            conn.execute(_DDL)

    def ready(self) -> bool:
        return table_exists(self.conn, "demographic_links")

    # ------------------------------------------------------------------ helpers

    def _references(self, namespace: str) -> list[dict[str, Any]]:
        """Every publisher reference with the series whose vintage the stating release carries."""
        out = []
        for release in self.store.releases(namespace):
            refs = list(release["document"].get("references") or [])
            if not refs:
                continue
            series_ids = [
                m["series_id"]
                for m in self.store.release_series(namespace, release["release_id"])
            ]
            for ref in refs:
                for series_id in series_ids:
                    out.append(
                        {
                            "reference": dict(ref),
                            "series_id": series_id,
                            "stated_in": self.store.source_revision(
                                namespace, release["release_id"]
                            ),
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
        principal_id,
    ):
        if self.conn.execute(
            "SELECT 1 FROM demographic_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone():
            return False
        now = self.now()
        self.conn.execute(
            "INSERT INTO demographic_links VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                principal_id,
                now,
            ],
        )
        return True

    def _supersede(self, namespace, subject_id, target_kind, target_id, principal_id):
        for (link_id,) in self.conn.execute(
            "SELECT link_id FROM demographic_links WHERE namespace=? AND subject_id=? AND target_kind=? AND "
            "target_id=? AND state IN ('asserted', 'candidate') ORDER BY link_id",
            [namespace, subject_id, target_kind, target_id],
        ).fetchall():
            # Stronger evidence arrived: the publisher's own reference now links the pair.
            self._transition(
                namespace,
                self.link(namespace, link_id, scopes={"operator"}),
                "superseded",
                principal_id,
                "a publisher reference now links this pair",
            )

    # ------------------------------------------------------------------ publisher references

    def link_references(
        self,
        namespace: str,
        *,
        legal_namespace: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Resolve stated act and decision references to Legal works by exact identifier in the issuing jurisdiction."""
        from src.kb.legal import LegalError, LegalStore

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, LEGAL_READ)
        linked, unresolved = [], []
        if not table_exists(self.conn, "legal_works"):
            return {"linked": linked, "unresolved": unresolved}
        legal = LegalStore(self.conn, initialize=False)
        for item in self._references(namespace):
            ref = item["reference"]
            scheme = ref.get("scheme")
            if scheme not in ACT_SCHEMES and scheme != "ecli":
                continue
            identifier = act_identifier(scheme, ref.get("identifier"))
            jurisdictions = (
                ecli_jurisdictions(identifier)
                if scheme == "ecli"
                else ACT_SCHEMES[scheme]
            )
            works: dict[str, dict[str, Any]] = {}
            statuses = []
            for jurisdiction in jurisdictions:
                try:
                    found = legal.lookup(
                        legal_namespace,
                        scopes=scopes,
                        identifier=identifier,
                        jurisdiction=jurisdiction,
                    )
                except LegalError as exc:
                    raise DemographicError(exc.code, str(exc)) from exc
                statuses.append(found["status"])
                for work in found["works"]:
                    works[work["work_id"]] = work
            subject = {"kind": "series", "id": item["series_id"]}
            revision = item["stated_in"]["release_id"]
            relation = (
                ref.get("relation")
                if ref.get("relation") in LINK_RELATIONS
                else "referenced_in"
            )
            reference = {
                k: ref.get(k) for k in ("scheme", "identifier", "relation", "locator")
            }
            if len(works) == 1:
                (work,) = works.values()
                state = "linked"
                target = {
                    "kind": "legal-work",
                    "namespace": legal_namespace,
                    "id": work["work_id"],
                }
                evidence = {
                    "stated_in": item["stated_in"],
                    "work_title": work.get("title"),
                    "work_kind": work.get("work_kind"),
                    "matched_identifier": identifier,
                    "note": NO_CAUSAL_CLAIM,
                }
            else:
                state = "unresolved"
                target = {
                    "kind": "legal-work",
                    "namespace": legal_namespace,
                    "id": None,
                }
                evidence = {
                    "stated_in": item["stated_in"],
                    "lookup_status": "ambiguous"
                    if works
                    else statuses[-1]
                    if statuses
                    else "not_covered",
                    "note": "the cited act or decision is not acquired (or matches several works); kept as the "
                    "publisher's string",
                }
            link_id = (
                "dm-link:"
                + digest(
                    [
                        namespace,
                        subject,
                        target["id"],
                        scheme,
                        identifier,
                        relation,
                        revision,
                        state,
                    ]
                )[:24]
            )
            if self._insert(
                namespace,
                link_id,
                subject,
                target,
                relation,
                "publisher_reference",
                state,
                reference,
                evidence,
                principal_id,
            ):
                (linked if state == "linked" else unresolved).append(link_id)
            if state == "linked":
                self._supersede(
                    namespace,
                    item["series_id"],
                    "legal-work",
                    target["id"],
                    principal_id,
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
        """Link series to a dossier's current revision by a publisher reference equal to one of its stage keys."""
        from src.domains.political.legislative_dossiers import (
            DossierError,
            LegislativeDossierStore,
        )
        from src.ingestion.lobbying_sources import REFERENCE_SCHEMES, reference_key
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
            raise DemographicError(exc.code, str(exc)) from exc
        keys = stage_keys(dossier)
        linked = []
        for item in self._references(namespace):
            ref = item["reference"]
            scheme = ref.get("scheme")
            if (
                scheme not in DOSSIER_SCHEMES
                or REFERENCE_SCHEMES.get(scheme) != dossier["jurisdiction"]
            ):
                continue
            key = reference_key(scheme, ref.get("identifier"))
            for stage in keys.get(key or "", []):
                subject = {"kind": "series", "id": item["series_id"]}
                target = {
                    "kind": "dossier",
                    "namespace": dossier_namespace,
                    "id": dossier_id,
                    "revision": dossier["revision"],
                }
                relation = (
                    ref.get("relation")
                    if ref.get("relation") in LINK_RELATIONS
                    else "referenced_in"
                )
                link_id = (
                    "dm-link:"
                    + digest(
                        [
                            namespace,
                            subject,
                            "dossier",
                            dossier_namespace,
                            dossier_id,
                            dossier["revision"],
                            key,
                            stage["stage_id"],
                            relation,
                            item["stated_in"]["release_id"],
                        ]
                    )[:24]
                )
                if self._insert(
                    namespace,
                    link_id,
                    subject,
                    target,
                    relation,
                    "publisher_reference",
                    "linked",
                    {
                        "scheme": scheme,
                        "identifier": ref.get("identifier"),
                        "key": key,
                        "locator": ref.get("locator"),
                    },
                    {
                        "stated_in": item["stated_in"],
                        "stage": stage,
                        "dossier_revision": dossier["revision"],
                        "note": NO_CAUSAL_CLAIM,
                    },
                    principal_id,
                ):
                    linked.append(link_id)
                self._supersede(
                    namespace, item["series_id"], "dossier", dossier_id, principal_id
                )
        return {
            "dossier_id": dossier_id,
            "dossier_revision": dossier["revision"],
            "linked": linked,
        }

    # ------------------------------------------------------------------ assertions and candidates

    def assert_link(
        self,
        namespace: str,
        *,
        subject: Mapping[str, Any],
        target: Mapping[str, Any],
        relation: str,
        locator: str,
        statement: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """A user's assertion with a passage locator or dossier revision; it links only once a reviewer accepts it."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if (
            relation not in LINK_RELATIONS
            or not str(locator or "").strip()
            or not str(statement or "").strip()
        ):
            raise DemographicError(
                "invalid_assertion",
                f"an assertion has one of {LINK_RELATIONS}, a locator and a statement",
            )
        side = dict(subject or {})
        if side.get("series_id"):
            subject_view = {
                "kind": "series",
                "id": self.store.series(namespace, side["series_id"])["series_id"],
            }
        elif side.get("definition_id"):
            subject_view = {
                "kind": "definition",
                "id": self.store.definition(namespace, side["definition_id"])[
                    "definition_id"
                ],
            }
        else:
            raise DemographicError(
                "invalid_assertion", "the subject is a series_id or a definition_id"
            )
        target = dict(target or {})
        if target.get("kind") == "legal-work":
            require_scope(scopes, LEGAL_READ)
            from src.kb.legal import LegalError, LegalStore

            try:
                work = LegalStore(self.conn, initialize=False).inspect(
                    target["namespace"], target["id"], scopes=scopes
                )
            except (LegalError, KeyError) as exc:
                raise DemographicError(
                    "not_found", "the asserted legal work is not acquired"
                ) from exc
            revision = None
            evidence_target = {"work_title": work.get("title")}
        elif target.get("kind") == "dossier":
            require_scope(scopes, DOSSIER_READ)
            from src.domains.political.legislative_dossiers import (
                DossierError,
                LegislativeDossierStore,
            )

            try:
                state = LegislativeDossierStore(self.conn, initialize=False)._state(
                    target["namespace"], target["id"]
                )
            except (DossierError, KeyError) as exc:
                raise DemographicError(
                    "not_found", "the asserted dossier is not saved"
                ) from exc
            revision = state["revision"]
            evidence_target = {"dossier_revision": revision}
        else:
            raise DemographicError(
                "invalid_assertion", "the target is a legal-work or a dossier"
            )
        view = {
            "kind": target["kind"],
            "namespace": target["namespace"],
            "id": target["id"],
            "revision": revision,
        }
        link_id = (
            "dm-assertion:"
            + digest(
                [
                    namespace,
                    subject_view,
                    view,
                    relation,
                    locator.strip(),
                    statement.strip(),
                    principal_id,
                ]
            )[:24]
        )
        self._insert(
            namespace,
            link_id,
            subject_view,
            view,
            relation,
            "reviewed_assertion",
            "asserted",
            {"locator": locator.strip()},
            {
                "statement": statement.strip(),
                **evidence_target,
                "note": NO_CAUSAL_CLAIM,
            },
            principal_id,
        )
        return self.link(namespace, link_id, scopes={"operator"})

    def propose_candidates(
        self,
        namespace: str,
        *,
        legal_namespace: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """Shared words between a definition and an act title: a discovery candidate, never a link."""
        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        require_scope(scopes, LEGAL_READ)
        if not table_exists(self.conn, "legal_works"):
            return {"candidates": []}
        works = self.conn.execute(
            "SELECT work_id, title FROM legal_works WHERE namespace=? ORDER BY work_id",
            [legal_namespace],
        ).fetchall()
        candidates = []
        for series in self.store.find_series(namespace):
            definition = series["definition"] or {}
            words = _tokens(definition.get("content", {}).get("source_text")) | _tokens(
                str(definition.get("content", {}).get("concept") or "").replace(
                    "_", " "
                )
            )
            for work_id, title in works:
                if self.conn.execute(
                    "SELECT 1 FROM demographic_links WHERE namespace=? AND subject_id=? AND target_id=? AND "
                    "state IN ('linked', 'accepted')",
                    [namespace, series["series_id"], work_id],
                ).fetchone():
                    continue
                shared = sorted(
                    w for w in words if any(t.startswith(w[:6]) for t in _tokens(title))
                )
                if len(shared) < 2:
                    continue
                link_id = (
                    "dm-candidate:"
                    + digest([namespace, series["series_id"], "legal-work", work_id])[
                        :24
                    ]
                )
                if self._insert(
                    namespace,
                    link_id,
                    {"kind": "series", "id": series["series_id"]},
                    {"kind": "legal-work", "namespace": legal_namespace, "id": work_id},
                    "referenced_in",
                    "discovery",
                    "candidate",
                    None,
                    {
                        "shared_words": shared,
                        "work_title": title,
                        "note": "shared words are a discovery candidate, not a link",
                    },
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
            "UPDATE demographic_links SET state=?, history_json=? WHERE namespace=? AND link_id=?",
            [state, canonical(history), namespace, link["link_id"]],
        )
        return self.link(namespace, link["link_id"], scopes={"operator"})

    def review(self, namespace, link_id, decision, reason, *, principal_id, scopes):
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise DemographicError("invalid_decision", "accept or reject with a reason")
        link = self.link(namespace, link_id, scopes={"operator"})
        if link["state"] not in PENDING_STATES:
            raise DemographicError(
                "invalid_state",
                f"link is {link['state']}; only a pending link is reviewed",
            )
        if (
            decision == "accept"
            and link["created_by"] == principal_id
            and link["state"] == "asserted"
        ):
            raise DemographicError(
                "invalid_decision",
                "an assertion is reviewed by someone other than its author",
            )
        return self._transition(
            namespace,
            link,
            "accepted" if decision == "accept" else "rejected",
            principal_id,
            reason.strip(),
        )

    def revert(self, namespace, link_id, reason, *, principal_id, scopes):
        """Undo a reviewed decision; a publisher reference is the publisher's own statement and is not reverted."""
        authorize(namespace, set(scopes), REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise DemographicError("invalid_decision", "a revert needs a reason")
        link = self.link(namespace, link_id, scopes={"operator"})
        if link["state"] not in {"accepted", "rejected"}:
            raise DemographicError(
                "invalid_state", "only an accepted or rejected link can be reverted"
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
            "relation, basis, state, reference_json, evidence_json, history_json, created_by, created_at_ms FROM "
            "demographic_links WHERE namespace=? AND link_id=?",
            [namespace, link_id],
        ).fetchone()
        if row is None:
            raise DemographicError("not_found", "link is not visible in this namespace")
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
            created_by=row[13],
            created_at_ms=row[14],
        )
        view["link_kind"] = (
            "publisher-reference"
            if view["state"] == "linked"
            else "reviewed-assertion"
            if view["state"] == "accepted"
            else "discovery-candidate"
            if view["state"] == "candidate"
            else "unreviewed-assertion"
            if view["state"] == "asserted"
            else view["state"]
        )
        view["counts_as_link"] = view["state"] in LINKING_STATES
        view["note"] = NO_CAUSAL_CLAIM
        return {"contract": CONTRACT, "namespace": namespace, **view}

    def links(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        subject_id: str | None = None,
        target_id: str | None = None,
        states: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not self.ready():
            return []
        wanted = set(states or STATES)
        rows = self.conn.execute(
            "SELECT link_id FROM demographic_links WHERE namespace=? AND (? IS NULL OR subject_id=?) AND "
            "(? IS NULL OR target_id=?) ORDER BY subject_id, target_kind, created_at_ms, link_id",
            [namespace, subject_id, subject_id, target_id, target_id],
        ).fetchall()
        return [
            v
            for v in (self.link(namespace, r[0], scopes=scopes) for r in rows)
            if v["state"] in wanted
        ]

    def series_citing(
        self, namespace: str, target_id: str, *, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """The series that cite a legal work or dossier, each with the citation and source revision."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        out = []
        for link in self.links(
            namespace, scopes=scopes, target_id=target_id, states=LINKING_STATES
        ):
            series = (
                self.store.series(namespace, link["subject_id"])
                if link["subject_kind"] == "series"
                else None
            )
            out.append(
                {
                    "link_id": link["link_id"],
                    "relation": link["relation"],
                    "basis": link["basis"],
                    "subject_kind": link["subject_kind"],
                    "subject_id": link["subject_id"],
                    "provider": None if series is None else series["provider"],
                    "series_code": None if series is None else series["series_code"],
                    "indicator": None if series is None else series["indicator"],
                    "reference": link["reference"],
                    "stated_in": link["evidence"].get("stated_in"),
                }
            )
        return {"target_id": target_id, "series": out, "note": NO_CAUSAL_CLAIM}
