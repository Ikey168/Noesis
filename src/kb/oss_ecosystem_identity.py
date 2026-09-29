"""Reviewable repository and organisation identity for OSS packages (OS06).

Repositories and publisher organisations reach a package only through a
source-stated link proposed as a *candidate* and accepted by review; nothing
is merged and no record is rewritten. The state machine is the one of
:mod:`src.kb.ownership_identity` and :mod:`src.kb.vulnerability_identity`:

* ``proposed`` -> ``accepted`` / ``rejected`` by review, recorded as an entity
  identity decision (``match`` / ``non-match``) in
  :class:`src.kb.entity_history.EntityHistoryStore`;
* ``reverted`` by an ``undo`` decision; a revert never reactivates an earlier
  decision, and re-accepting records a new one;
* a pending candidate is *upgraded* when stronger evidence arrives; a rejected
  or reverted one is proposed again only when the basis or evidence is new.

Repository bases, strongest first: ``archive-tag-corroborated`` (a registry or
deps.dev link, plus a Software Heritage snapshot of that origin holding a tag
that names one of the package's versions), ``registry-declared`` (the
package's own registry metadata), ``deps-dev-related-project`` and
``reviewer-proposed``. A repository claimed by two packages is *flagged*
(``shared_claim``) on both candidates and never resolved; the same name in two
ecosystems is two packages (keys carry the canonical coordinate).

Organisation candidates connect organisation-level publisher declarations to
``canonical_entities`` organisations: ``declared-name`` (an exact normalised
name match, low confidence) or ``reviewer-proposed``. npm scopes (which may be
user scopes) and Maven groupIds are never proposed automatically, and only
organisation entities are ever candidates.

:func:`OssIdentity.reviewed_repositories` is the single equivalence definition
that queries and monitors share; lookups work from either side.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any

from src.kb.oss_ecosystem_records import (
    READ_SCOPE,
    REGISTRY_SOURCES,
    REVIEW_SCOPE,
    WRITE_SCOPE,
    canonical,
    digest,
)
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError, authorize

CONTRACT = "noesis-oss-identity-candidate-v1"
CONFIDENCE = {
    "archive-tag-corroborated": 0.9,
    "registry-declared": 0.8,
    "deps-dev-related-project": 0.7,
    "declared-name": 0.4,
    "reviewer-proposed": 0.5,
}
AUTO_ORGANISATION_KINDS = frozenset(
    {"pypi-organisation", "crates-team", "pom-organisation"}
)
ORGANISATION_TYPES = frozenset(
    {"ORG", "ORGANIZATION", "ORGANISATION", "COMPANY", "organization", "organisation"}
)
_ENTITY_HISTORY_SCOPES = {
    "knowledge:entity-history:write",
    "knowledge:entity-history:review",
    "knowledge:entity-history:execute",
    "knowledge:entity-history:read",
}
_DDL = """
CREATE TABLE IF NOT EXISTS oss_identity_candidates (
  namespace TEXT NOT NULL, candidate_id TEXT NOT NULL, kind TEXT NOT NULL, left_key TEXT NOT NULL,
  right_key TEXT NOT NULL, left_entity TEXT NOT NULL, right_entity TEXT NOT NULL, basis TEXT NOT NULL,
  confidence DOUBLE NOT NULL, evidence_json TEXT NOT NULL, state TEXT NOT NULL, decision_id TEXT,
  review_seq INTEGER NOT NULL, created_by TEXT NOT NULL, created_at_ms BIGINT NOT NULL, history_json TEXT NOT NULL,
  PRIMARY KEY(namespace, candidate_id));
"""


def package_key(coordinate: str) -> str:
    return f"oss-package:{coordinate}"


def repository_key_of(key: str) -> str:
    return f"oss-repository:{key}"


def publisher_key(source: str, kind: str, identifier: str) -> str:
    return f"oss-publisher:{source}:{kind}:{identifier}"


def entity_id(key: str) -> str:
    return "oss-entity:" + digest(key)[:24]


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def version_tags(package: str, version: str) -> set[str]:
    """Tag names that state a version: ``v1.2.0``, ``1.2.0``, ``<name>-1.2.0``, ``<name>@1.2.0``."""

    name = package.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return {
        f"refs/tags/{t}"
        for t in (
            f"v{version}",
            version,
            f"{name}-{version}",
            f"{name}@{version}",
            f"{name}-v{version}",
        )
    }


class OssIdentity:
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
        self.store = OssEcosystemStore(conn, initialize=initialize, now=self.now)
        self.history = EntityHistoryStore(conn, now=self.now, initialize=initialize)
        if initialize:
            conn.execute(_DDL)

    def _ready(self) -> bool:
        return bool(
            self.conn.execute(
                "SELECT 1 FROM information_schema.tables WHERE table_name='oss_identity_candidates'"
            ).fetchone()
        )

    # ------------------------------------------------------------ proposals

    def offer(
        self,
        namespace: str,
        *,
        kind: str,
        left_key: str,
        right_key: str,
        right_entity: str | None = None,
        basis: str,
        evidence: list[Mapping[str, Any]],
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        if (
            basis not in CONFIDENCE
            or not evidence
            or left_key == right_key
            or kind not in {"repository", "organisation"}
        ):
            raise OssStoreError(
                "invalid_candidate",
                "a candidate needs two keys, a known basis and evidence",
            )
        candidate_id = "oss-idc:" + digest([namespace, left_key, right_key])[:24]
        evidence = [dict(e) for e in evidence]
        row = self.conn.execute(
            "SELECT state, basis, evidence_json, history_json FROM oss_identity_candidates "
            "WHERE namespace=? AND candidate_id=?",
            [namespace, candidate_id],
        ).fetchone()
        now = self.now()
        if row is None:
            self.conn.execute(
                "INSERT INTO oss_identity_candidates VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    namespace,
                    candidate_id,
                    kind,
                    left_key,
                    right_key,
                    entity_id(left_key),
                    right_entity or entity_id(right_key),
                    basis,
                    CONFIDENCE[basis],
                    canonical(evidence),
                    "proposed",
                    None,
                    0,
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
        new = basis != old_basis or digest(evidence) != digest(old_evidence)
        if state == "proposed" and CONFIDENCE[basis] > CONFIDENCE[old_basis]:
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
            "UPDATE oss_identity_candidates SET state='proposed', decision_id=NULL, basis=?, "
            "confidence=?, evidence_json=?, history_json=? WHERE namespace=? AND candidate_id=?",
            [
                basis,
                CONFIDENCE[basis],
                canonical(evidence),
                canonical(history),
                namespace,
                candidate_id,
            ],
        )
        return {"candidate_id": candidate_id, "change": change}

    def _tag_evidence(
        self, namespace: str, coordinate: str, repo: str
    ) -> list[dict[str, Any]]:
        versions = {
            r["version"]: r
            for r in self.store.records(
                namespace, record_type="release_state_revision", coordinate=coordinate
            )
            if r["version"]
        }
        package = None
        for record in self.store.records(
            namespace, record_type="release_state_revision", coordinate=coordinate
        ):
            package = self.store.current(record["record_id"])["statement"]["package"]
            break
        found = []
        for record in self.store.records(
            namespace, record_type="archive_provenance", repository_key=repo
        ):
            revision = self.store.current(record["record_id"])
            snapshot = revision["statement"]
            if snapshot.get("detail") != "snapshot" or package is None:
                continue
            names = {b["name"]: b for b in snapshot.get("branches") or []}
            for version in sorted(versions):
                hit = next(
                    (
                        names[t]
                        for t in sorted(version_tags(package, version))
                        if t in names
                    ),
                    None,
                )
                if hit:
                    found.append(
                        {
                            "version": version,
                            "tag": hit["name"],
                            "target_swhid": hit["target_swhid"],
                            "snapshot_swhid": snapshot["snapshot_swhid"],
                            "archive_revision_id": revision["revision_id"],
                        }
                    )
        return found

    def propose_repositories(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Candidates from registry links, deps.dev related projects and archive tags. Idempotent."""

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready(namespace)
        claims: dict[tuple[str, str], dict[str, Any]] = {}
        for repo, links in sorted(self.store.asserted_repositories(namespace).items()):
            for link in links:
                basis = (
                    "registry-declared"
                    if link["source"] in REGISTRY_SOURCES.values()
                    else "deps-dev-related-project"
                    if link["source"] == "deps-dev"
                    else None
                )
                if basis is None:
                    continue
                entry = claims.setdefault(
                    (link["coordinate"], repo), {"basis": basis, "evidence": []}
                )
                if CONFIDENCE[basis] > CONFIDENCE[entry["basis"]]:
                    entry["basis"] = basis
                entry["evidence"].append({"kind": "link-assertion", **link})
        offered = []
        for (coordinate, repo), claim in sorted(claims.items()):
            tags = self._tag_evidence(namespace, coordinate, repo)
            basis, evidence = claim["basis"], sorted(claim["evidence"], key=canonical)
            if tags:
                basis = "archive-tag-corroborated"
                evidence = evidence + [{"kind": "archive-tag", **t} for t in tags]
            offered.append(
                self.offer(
                    namespace,
                    kind="repository",
                    left_key=package_key(coordinate),
                    right_key=repository_key_of(repo),
                    basis=basis,
                    evidence=evidence,
                    principal_id=principal_id,
                    scopes=scopes,
                )
            )
        return {
            "offered": offered,
            "candidates": self.candidates(namespace, scopes=scopes, kind="repository"),
        }

    def _organisations(self) -> list[dict[str, Any]]:
        if not self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='canonical_entities'"
        ).fetchone():
            return []
        rows = self.conn.execute(
            "SELECT canonical_id, preferred_name, entity_type FROM canonical_entities"
        ).fetchall()
        return [
            {"canonical_id": r[0], "name": r[1], "type": r[2]}
            for r in rows
            if str(r[2]) in ORGANISATION_TYPES
        ]

    def propose_organisations(
        self, namespace: str, *, principal_id: str, scopes: Iterable[str]
    ) -> dict[str, Any]:
        """Declared organisation names equal to an organisation entity's name; npm scopes and groupIds never."""

        scopes = set(scopes)
        authorize(namespace, scopes, WRITE_SCOPE, write=True)
        self.store.require_ready(namespace)
        entities: dict[str, list[dict[str, Any]]] = {}
        for entity in self._organisations():
            entities.setdefault(_norm(entity["name"]), []).append(entity)
        offered = []
        for record in self.store.records(
            namespace, record_type="publisher_organisation"
        ):
            revision = self.store.current(record["record_id"])
            for organisation in revision["statement"]["organisations"]:
                if organisation["kind"] not in AUTO_ORGANISATION_KINDS:
                    continue
                declared = organisation.get("name") or organisation["id"]
                if organisation["kind"] == "crates-team":
                    declared = (
                        organisation["id"].split(":")[1]
                        if organisation["id"].count(":") >= 2
                        else declared
                    )
                for entity in entities.get(_norm(declared), []):
                    offered.append(
                        self.offer(
                            namespace,
                            kind="organisation",
                            left_key=publisher_key(
                                record["source"],
                                organisation["kind"],
                                organisation["id"],
                            ),
                            right_key=f"entity:{entity['canonical_id']}",
                            right_entity=entity["canonical_id"],
                            basis="declared-name",
                            evidence=[
                                {
                                    "declared": declared,
                                    "entity_name": entity["name"],
                                    "coordinate": record["coordinate"],
                                    "revision_id": revision["revision_id"],
                                    "note": "an equal name is a weak signal, never an identity",
                                }
                            ],
                            principal_id=principal_id,
                            scopes=scopes,
                        )
                    )
        return {
            "offered": offered,
            "candidates": self.candidates(
                namespace, scopes=scopes, kind="organisation"
            ),
        }

    def propose_link(
        self,
        namespace: str,
        *,
        left_key: str,
        right_key: str,
        reason: str,
        principal_id: str,
        scopes: Iterable[str],
    ) -> dict[str, Any]:
        """A reviewer's own candidate (a repository for a package, or an organisation entity for a publisher)."""

        if not str(reason or "").strip():
            raise OssStoreError(
                "invalid_candidate", "a reviewer proposal needs a reason"
            )
        if left_key.startswith("oss-package:") and right_key.startswith(
            "oss-repository:"
        ):
            kind, right_entity = "repository", None
        elif left_key.startswith("oss-publisher:") and right_key.startswith("entity:"):
            kind, right_entity = "organisation", right_key.split(":", 1)[1]
            if right_entity not in {e["canonical_id"] for e in self._organisations()}:
                raise OssStoreError(
                    "not_an_organisation",
                    "only organisation entities can be matched to a publisher",
                )
        else:
            raise OssStoreError(
                "invalid_candidate",
                "pair a package with a repository or a publisher with an entity",
            )
        return self.offer(
            namespace,
            kind=kind,
            left_key=left_key,
            right_key=right_key,
            right_entity=right_entity,
            basis="reviewer-proposed",
            evidence=[{"reason": reason.strip(), "by": principal_id}],
            principal_id=principal_id,
            scopes=scopes,
        )

    # ------------------------------------------------------------ reads

    def _row(self, namespace: str, candidate_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT candidate_id, kind, left_key, right_key, left_entity, right_entity, basis, confidence, "
            "evidence_json, state, decision_id, review_seq, created_by, created_at_ms, history_json "
            "FROM oss_identity_candidates WHERE namespace=? AND candidate_id=?",
            [namespace, candidate_id],
        ).fetchone()
        if row is None:
            raise OssStoreError(
                "not_found", "identity candidate is not visible in this namespace"
            )
        value = dict(
            zip(
                (
                    "candidate_id",
                    "kind",
                    "left_key",
                    "right_key",
                    "left_entity",
                    "right_entity",
                    "basis",
                    "confidence",
                ),
                row[:8],
            )
        )
        value.update(
            {
                "evidence": json.loads(row[8]),
                "state": row[9],
                "decision_id": row[10],
                "review_seq": row[11],
                "created_by": row[12],
                "created_at_ms": row[13],
                "history": json.loads(row[14]),
            }
        )
        if value["kind"] == "repository":
            others = self.conn.execute(
                "SELECT left_key, state FROM oss_identity_candidates WHERE namespace=? AND right_key=? AND "
                "left_key<>? AND state IN ('proposed','accepted') ORDER BY left_key",
                [namespace, value["right_key"], value["left_key"]],
            ).fetchall()
            if others:
                value["shared_claim"] = [
                    {"package": o[0].split(":", 1)[1], "state": o[1]} for o in others
                ]
                value["flag"] = (
                    "repository claimed by more than one package; not resolved"
                )
        return {
            "contract": CONTRACT,
            "namespace": namespace,
            **value,
            "notice": "a candidate is a reviewable proposal; records are never merged",
        }

    def candidates(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        kind: str | None = None,
        key: str | None = None,
        state: str | None = None,
    ) -> list[dict[str, Any]]:
        """Candidates in the namespace, found from either side of the pair."""

        authorize(namespace, set(scopes), READ_SCOPE)
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT candidate_id FROM oss_identity_candidates WHERE namespace=? AND (? IS NULL OR kind=?) AND "
            "(? IS NULL OR left_key=? OR right_key=?) AND (? IS NULL OR state=?) ORDER BY candidate_id",
            [namespace, kind, kind, key, key, key, state, state],
        ).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def reviewed_repositories(
        self, namespace: str, coordinate: str
    ) -> list[dict[str, Any]]:
        """The accepted repositories of a package - the one equivalence queries and monitors share."""

        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT candidate_id FROM oss_identity_candidates WHERE namespace=? AND kind='repository' AND "
            "left_key=? AND state='accepted' ORDER BY candidate_id",
            [namespace, package_key(coordinate)],
        ).fetchall()
        return [self._row(namespace, r[0]) for r in rows]

    def packages_for_repository(self, namespace: str, repository: str) -> list[str]:
        if not self._ready():
            return []
        rows = self.conn.execute(
            "SELECT left_key FROM oss_identity_candidates WHERE namespace=? AND kind='repository' AND right_key=? "
            "AND state='accepted' ORDER BY left_key",
            [namespace, repository_key_of(repository)],
        ).fetchall()
        return [r[0].split(":", 1)[1] for r in rows]

    # ------------------------------------------------------------ reviews

    def _transition(
        self,
        namespace: str,
        candidate: Mapping[str, Any],
        state: str,
        decision_id: str,
        principal_id: str,
        reason: str,
        *,
        review_seq: int,
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
            "UPDATE oss_identity_candidates SET state=?, decision_id=?, review_seq=?, history_json=? "
            "WHERE namespace=? AND candidate_id=?",
            [
                state,
                decision_id,
                review_seq,
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
        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if decision not in {"accept", "reject"} or not str(reason or "").strip():
            raise OssStoreError("invalid_decision", "accept or reject with a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] != "proposed":
            raise OssStoreError(
                "invalid_state",
                f"candidate is {candidate['state']}; propose again to re-review",
            )
        for side in ("left", "right"):
            self.history.register_entity(
                namespace,
                candidate[f"{side}_entity"],
                [candidate[f"{side}_key"]],
                principal_id=principal_id,
                scopes=_ENTITY_HISTORY_SCOPES,
            )
        seq = int(candidate["review_seq"]) + 1
        recorded = self.history.decide(
            namespace,
            "match" if decision == "accept" else "non-match",
            [candidate["left_entity"], candidate["right_entity"]],
            {
                "candidate_id": candidate_id,
                "basis": candidate["basis"],
                "evidence": candidate["evidence"],
                "reason": reason.strip(),
                "review_seq": seq,
                "shared_claim": candidate.get("shared_claim") or [],
                "provenance": {
                    "producer": "oss.licences",
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
            event_key=f"oss-identity:{namespace}:{candidate_id}",
        )
        return self._transition(
            namespace,
            candidate,
            "accepted" if decision == "accept" else "rejected",
            recorded["decision_id"],
            principal_id,
            reason.strip(),
            review_seq=seq,
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
        """Undo a decision; the candidate becomes ``reverted`` and no earlier decision is reactivated."""

        scopes = set(scopes)
        authorize(namespace, scopes, REVIEW_SCOPE, write=True)
        if not str(reason or "").strip():
            raise OssStoreError("invalid_decision", "a revert needs a reason")
        candidate = self._row(namespace, candidate_id)
        if candidate["state"] not in {"accepted", "rejected"}:
            raise OssStoreError(
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
            review_seq=int(candidate["review_seq"]),
        )


__all__ = [
    "CONFIDENCE",
    "CONTRACT",
    "OssIdentity",
    "entity_id",
    "package_key",
    "publisher_key",
    "repository_key_of",
    "version_tags",
]
