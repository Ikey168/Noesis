"""What each list stated about a party, or an annex about a control code, as of a date (#1907, S08).

Answers are grouped by list and never merged: the same party on the EU and UN
lists is two statements, with accepted identity decisions shown as links and
unreviewed candidates as candidates. A date is answered only from acquired
snapshots:

* before the first acquired snapshot of a list - ``unknown``;
* covered by a snapshot, or between two snapshots that state the same thing -
  ``listed`` (with the listing revision in force) or ``not_listed_in_snapshot``
  (with the delisting, if one was recorded);
* between two snapshots that state different things - ``unknown``, with both
  statements side by side; nothing is inferred between snapshots;
* after the latest snapshot - that snapshot's statement, marked as such.

No answer carries a screening result, risk score or compliance status.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import Any

from src.ingestion.sanctions_sources import LISTS, REVIEW_BOUNDARY
from src.kb.sanctions import (
    ANSWER_CONTRACT,
    CONTROL_LISTS,
    READ_SCOPE,
    SanctionsError,
    SanctionsStore,
    authorize,
    normalize_identifier,
)

NOTICE = (
    "Per-list source statements as of the date, cited to snapshots and listing revisions. This is not a "
    "screening result, risk score, compliance determination or legal advice, and a similar name is never "
    "treated as the same party."
)


def _day(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise SanctionsError("invalid_request", "as_of must be YYYY-MM-DD") from exc


class SanctionsQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        self.conn = conn
        # Read-only: an uninitialized warehouse answers "not found", it is never written to.
        self.store = SanctionsStore(conn, initialize=False, now=now)

    # ------------------------------------------------------------ resolution of the question

    def find(
        self,
        namespace: str,
        *,
        designation_id: str | None = None,
        list_id: str | None = None,
        list_entry_id: str | None = None,
        identifier: str | None = None,
        name: str | None = None,
    ) -> list[str]:
        """Designations by id, per-list identifier, a stated identifier value or an exactly stated name."""
        if list_id is not None and list_id not in LISTS:
            raise SanctionsError(
                "invalid_request", f"list must be one of {list(LISTS)}"
            )
        clauses, params = ["namespace=?"], [namespace]
        if designation_id:
            clauses.append("designation_id=?")
            params.append(designation_id)
        if list_id:
            clauses.append("list_id=?")
            params.append(list_id)
        if list_entry_id:
            clauses.append("list_entry_id=?")
            params.append(list_entry_id)
        if identifier or name:
            kinds = (
                ("name", "transliteration")
                if name
                else (
                    "passport",
                    "national_id",
                    "imo",
                    "lei",
                    "registration_number",
                    "tax_id",
                    "swift_bic",
                    "call_sign",
                    "other",
                )
            )
            wanted = normalize_identifier(name or identifier)
            if identifier and re.fullmatch(r"IMO\d{7}", wanted):
                wanted = wanted[
                    3:
                ]  # lists write IMO numbers with or without the prefix; stored as the digits
            clauses.append(
                "designation_id IN (SELECT designation_id FROM sanctions_aliases WHERE namespace=? AND "
                "normalized=? AND alias_kind IN (" + ",".join("?" * len(kinds)) + "))"
            )
            params += [namespace, wanted, *kinds]
        if len(params) == 1:
            raise SanctionsError(
                "invalid_request",
                "give a designation id, a list entry, an identifier or a name",
            )
        if not self.store.ready():
            return []
        rows = self.conn.execute(
            "SELECT designation_id FROM sanctions_designations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY list_id, list_entry_id",
            params,
        ).fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------ statement for one designation

    def _snapshots_around(
        self, namespace: str, list_id: str, day: str
    ) -> tuple[Any, Any]:
        before = self.conn.execute(
            "SELECT snapshot_id FROM sanctions_snapshots WHERE namespace=? AND list_id=? AND publication_date<=? "
            "ORDER BY sequence DESC LIMIT 1",
            [namespace, list_id, day],
        ).fetchone()
        after = self.conn.execute(
            "SELECT snapshot_id FROM sanctions_snapshots WHERE namespace=? AND list_id=? AND publication_date>? "
            "ORDER BY sequence LIMIT 1",
            [namespace, list_id, day],
        ).fetchone()
        return (before[0] if before else None), (after[0] if after else None)

    def _member(
        self, namespace: str, snapshot_id: str, designation_id: str
    ) -> str | None:
        row = self.conn.execute(
            "SELECT revision_id FROM sanctions_snapshot_members WHERE namespace=? AND "
            "snapshot_id=? AND designation_id=?",
            [namespace, snapshot_id, designation_id],
        ).fetchone()
        return row[0] if row else None

    def _stated(
        self, namespace: str, revision_id: str, day: str, legal_namespace: str
    ) -> dict[str, Any]:
        revision = self.store.revision(namespace, revision_id)
        statement = revision["statement"] or {}
        return {
            "listing_revision": {
                k: revision[k]
                for k in (
                    "revision_id",
                    "revision_no",
                    "change",
                    "source_revision",
                    "compared_snapshots",
                    "source_dates",
                    "acquired_at_ms",
                )
            },
            "party_kind": statement.get("party_kind"),
            "aliases": self.store.aliases(namespace, revision_id),
            "programmes": [
                self.store.programme(namespace, revision["list_id"], p["code"])
                for p in statement.get("programmes") or []
            ],
            "legal_basis": [
                self._basis(
                    namespace, revision["list_id"], b, day, statement, legal_namespace
                )
                for b in statement.get("legal_basis") or []
            ],
            "remarks": statement.get("remarks") or [],
            "cross_references": statement.get("cross_references") or {},
        }

    def _basis(
        self, namespace, list_id, basis, day, statement, legal_namespace
    ) -> dict[str, Any]:
        view = self.store.legal_basis(namespace, list_id, basis)
        if view["status"] != "resolved":
            return {
                **view,
                "passages": {
                    "status": "unresolved_act",
                    "note": "the list's citation string is kept; "
                    "no Legal work was matched",
                },
            }
        from src.kb.legal import READ_SCOPE as LEGAL_READ
        from src.kb.legal import LegalStore

        legal = LegalStore(self.conn, initialize=False)
        scopes = {LEGAL_READ, f"namespace:{legal_namespace}:read"}
        selection = legal.select_as_of(
            legal_namespace, view["work_id"], day, scopes=scopes
        )
        versions = [c for c in selection["candidates"] if c["state"] == "applies"]
        names = [n["name"] for n in statement.get("names") or []]
        passages = {
            "status": "no_applicable_version",
            "selection_status": selection["status"],
            "tool": "get_legal_passages",
        }
        for candidate in versions:
            for name in names:
                found = legal.passages(
                    legal_namespace,
                    candidate["version_id"],
                    scopes=scopes,
                    contains=name,
                    limit=5,
                )
                if found["status"] == "found":
                    passages = {
                        "status": "found",
                        "version_id": candidate["version_id"],
                        "matched_text": name,
                        "passages": found["passages"],
                        "tool": "get_legal_passages",
                        "note": "passages of the act that state the list's own wording of the name",
                    }
                    break
                passages = {
                    "status": found["status"],
                    "version_id": candidate["version_id"],
                    "tool": "get_legal_passages",
                    "call": {
                        "namespace": legal_namespace,
                        "version_id": candidate["version_id"],
                    },
                }
            if passages["status"] == "found":
                break
        return {
            **view,
            "selection": {
                "status": selection["status"],
                "selected_version_id": selection["selected_version_id"],
            },
            "passages": passages,
        }

    def statement_as_of(
        self,
        namespace: str,
        designation_id: str,
        as_of: str,
        *,
        legal_namespace: str | None = None,
    ) -> dict[str, Any]:
        day = _day(as_of)
        legal_namespace = legal_namespace or namespace
        designation = self.store.designation(namespace, designation_id)
        list_id = designation["list_id"]
        before, after = self._snapshots_around(namespace, list_id, day)
        base = {"list_id": list_id, "designation": designation, "as_of": day}
        if before is None:
            first = self.store.snapshots(namespace, list_id)[:1]
            return {
                **base,
                "status": "unknown",
                "reason": "before the first acquired snapshot of this list",
                "first_snapshot": first[0]["source_revision"] if first else None,
            }
        stated_before = self._member(namespace, before, designation_id)
        stated_after = (
            None if after is None else self._member(namespace, after, designation_id)
        )
        snap_before = self.store.snapshot(namespace, before)
        exact = snap_before["publication_date"] == day
        delisting = self._last_delisting(
            namespace, designation_id, snap_before["sequence"]
        )
        if after is not None and not exact and stated_before != stated_after:
            sides = []
            for snap, revision in ((before, stated_before), (after, stated_after)):
                snapshot = self.store.snapshot(namespace, snap)
                sides.append(
                    {
                        "snapshot": snapshot["source_revision"],
                        "status": "listed" if revision else "not_listed_in_snapshot",
                        **(
                            {
                                "statement": self._stated(
                                    namespace, revision, day, legal_namespace
                                )
                            }
                            if revision
                            else {}
                        ),
                    }
                )
            return {
                **base,
                "status": "unknown",
                "reason": "the list's statement changed between two acquired "
                "snapshots; nothing is inferred between them",
                "statements_side_by_side": sides,
            }
        basis = (
            "snapshot published on the date"
            if exact
            else "latest acquired snapshot; no later snapshot acquired"
            if after is None
            else "bracketing snapshots state the same"
        )
        coverage = {
            "snapshot": snap_before["source_revision"],
            "basis": basis,
            "next_snapshot": self.store.snapshot(namespace, after)["source_revision"]
            if after
            else None,
        }
        if stated_before is None:
            return {
                **base,
                "status": "not_listed_in_snapshot",
                "coverage": coverage,
                "delisting": delisting,
            }
        return {
            **base,
            "status": "listed",
            "coverage": coverage,
            "statement": self._stated(namespace, stated_before, day, legal_namespace),
        }

    def _last_delisting(
        self, namespace: str, designation_id: str, sequence: int
    ) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT r.revision_id FROM sanctions_revisions r JOIN sanctions_snapshots s ON s.namespace=r.namespace "
            "AND s.snapshot_id=r.snapshot_id WHERE r.namespace=? AND r.designation_id=? AND r.change='delisted' "
            "AND s.sequence<=? ORDER BY s.sequence DESC LIMIT 1",
            [namespace, designation_id, sequence],
        ).fetchone()
        if row is None:
            return None
        view = self.store.revision(namespace, row[0])
        return {
            k: view[k]
            for k in (
                "record_type",
                "revision_id",
                "change",
                "source_revision",
                "compared_snapshots",
                "source_dates",
            )
        }

    # ------------------------------------------------------------ public answers

    def history_as_of(
        self,
        namespace: str,
        as_of: str,
        *,
        scopes: Iterable[str],
        designation_id: str | None = None,
        list_id: str | None = None,
        list_entry_id: str | None = None,
        identifier: str | None = None,
        name: str | None = None,
        legal_namespace: str | None = None,
        include_history: bool = True,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        day = _day(as_of)
        ids = self.find(
            namespace,
            designation_id=designation_id,
            list_id=list_id,
            list_entry_id=list_entry_id,
            identifier=identifier,
            name=name,
        )
        from src.kb.sanctions_identity import SanctionsIdentity

        identity = SanctionsIdentity(self.conn, initialize=False)
        by_list: dict[str, list[dict[str, Any]]] = {}
        for designation in ids:
            answer = self.statement_as_of(
                namespace, designation, day, legal_namespace=legal_namespace
            )
            answer["identity"] = identity.links(
                namespace, answer["designation"]["record_key"], scopes=scopes
            )
            if include_history:
                answer["revision_history"] = [
                    {
                        k: r[k]
                        for k in (
                            "revision_id",
                            "revision_no",
                            "change",
                            "source_revision",
                            "compared_snapshots",
                            "source_dates",
                        )
                    }
                    for r in self.store.history(namespace, designation)
                ]
            by_list.setdefault(answer["list_id"], []).append(answer)
        return {
            "contract": ANSWER_CONTRACT,
            "namespace": namespace,
            "as_of": day,
            "query": {
                k: v
                for k, v in {
                    "designation_id": designation_id,
                    "list_id": list_id,
                    "list_entry_id": list_entry_id,
                    "identifier": identifier,
                    "name": name,
                }.items()
                if v
            },
            "matched_by": "exact stated name (a list statement, not an identity)"
            if name
            else "identifier",
            "lists": {k: by_list[k] for k in sorted(by_list)},
            "differences_between_lists": self._differences(by_list),
            "status": "found" if ids else "not_found_in_acquired_lists",
            "coverage_notice": "Only acquired snapshots are searched; absence is not a statement that a party "
            "is not listed.",
            "notice": NOTICE,
            "review_boundary": REVIEW_BOUNDARY,
        }

    @staticmethod
    def _differences(by_list: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        """Attributes the lists state differently for the parties in this answer, side by side."""
        stated: dict[str, dict[str, set[str]]] = {}
        for list_id, answers in by_list.items():
            for answer in answers:
                for alias in (answer.get("statement") or {}).get("aliases") or []:
                    if alias["alias_kind"] in {
                        "date_of_birth",
                        "nationality",
                        "passport",
                        "imo",
                        "national_id",
                    }:
                        stated.setdefault(alias["alias_kind"], {}).setdefault(
                            list_id, set()
                        ).add(alias["value"])
        return [
            {
                "attribute": kind,
                "by_list": {k: sorted(v) for k, v in sorted(per_list.items())},
            }
            for kind, per_list in sorted(stated.items())
            if len(per_list) > 1
            and len({tuple(sorted(v)) for v in per_list.values()}) > 1
        ]

    def lookup(
        self, namespace: str, *, scopes: Iterable[str], **query: Any
    ) -> dict[str, Any]:
        """Current statements (latest acquired snapshot of each list) for matching designations."""
        authorize(namespace, set(scopes), READ_SCOPE)
        latest = (
            self.conn.execute(
                "SELECT max(publication_date) FROM sanctions_snapshots WHERE namespace=?",
                [namespace],
            ).fetchone()[0]
            if self.store.ready()
            else None
        )
        return self.history_as_of(
            namespace,
            latest or date.today().isoformat(),
            scopes=scopes,
            include_history=False,
            **query,
        )

    # ------------------------------------------------------------ control lists

    def control_entry_as_of(
        self,
        namespace: str,
        control_code: str,
        as_of: str,
        *,
        scopes: Iterable[str],
        control_list: str = "eu-dual-use",
        language: str | None = "en",
        legal_namespace: str | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if control_list not in CONTROL_LISTS:
            raise SanctionsError(
                "invalid_request",
                f"control list must be one of {sorted(CONTROL_LISTS)}",
            )
        day = _day(as_of)
        legal_namespace = legal_namespace or namespace
        code = str(control_code or "").strip().upper()
        work_id = self.store.control_list_work(legal_namespace, control_list)
        base = {
            "contract": ANSWER_CONTRACT,
            "namespace": namespace,
            "control_list": control_list,
            "control_code": code,
            "as_of": day,
            "notice": "Annex text as published in each acquired edition; "
            "passage changes are source changes, not a legal-effect or classification assessment.",
            "review_boundary": REVIEW_BOUNDARY,
        }
        if work_id is None:
            return {
                **base,
                "status": "unknown",
                "reason": "no edition of the control list is acquired",
            }
        from src.kb.legal import READ_SCOPE as LEGAL_READ
        from src.kb.legal import LegalStore

        legal = LegalStore(self.conn, initialize=False)
        selection = legal.select_as_of(
            legal_namespace,
            work_id,
            day,
            scopes={LEGAL_READ, f"namespace:{legal_namespace}:read"},
            language=language,
        )
        entries = {
            e["version_id"]: e
            for e in self.store.control_entries(
                namespace, code, control_list, legal_namespace=legal_namespace
            )
        }
        editions = [
            {
                "version_id": c["version_id"],
                "state": c["state"],
                "evidence": c["evidence"],
                "entry": entries.get(c["version_id"]),
                "captured_text": self._captured(legal_namespace, c["version_id"]),
            }
            for c in selection["candidates"]
        ]
        chosen = None
        if selection["status"] == "selected":
            pool = [
                selection["selected_version_id"],
                *selection["equivalent_manifestations"],
            ]
            texts = [
                e for e in editions if e["version_id"] in pool and e["captured_text"]
            ]
            chosen = texts[0] if texts else None
        if selection["status"] != "selected":
            status = selection["status"]
        elif chosen is None:
            status = "edition_text_not_captured"
        else:
            status = "entry_in_edition" if chosen["entry"] else "code_not_in_edition"
        return {
            **base,
            "status": status,
            "work_id": work_id,
            "selection_status": selection["status"],
            "edition": chosen,
            "editions": editions,
            "consolidation_notice": selection.get("consolidation_notice"),
        }

    def _captured(self, legal_namespace: str, version_id: str) -> bool:
        row = self.conn.execute(
            "SELECT content_coverage FROM legal_versions WHERE namespace=? AND version_id=?",
            [legal_namespace, version_id],
        ).fetchone()
        return bool(row and row[0] == "captured-text")

    def compare_control_entry(
        self,
        namespace: str,
        control_code: str,
        left_version_id: str,
        right_version_id: str,
        *,
        scopes: Iterable[str],
        legal_namespace: str | None = None,
    ) -> dict[str, Any]:
        """Passage-level change of one control code between two editions (source change, not legal effect)."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        legal_namespace = legal_namespace or namespace
        from src.kb.legal import READ_SCOPE as LEGAL_READ
        from src.kb.legal import LegalStore

        code = str(control_code or "").strip().upper()
        compared = LegalStore(self.conn, initialize=False).compare_versions(
            legal_namespace,
            left_version_id,
            right_version_id,
            scopes={LEGAL_READ, f"namespace:{legal_namespace}:read"},
        )
        changes = [
            c
            for c in compared["changes"]
            if c["locator"].get("official_norm_id") == code
        ]
        return {
            "contract": compared["contract"],
            "control_code": code,
            "left_version_id": left_version_id,
            "right_version_id": right_version_id,
            "work_id": compared["work_id"],
            "status": "changed" if changes else "unchanged_or_absent_in_both",
            "changes": changes,
            "notice": compared["notice"],
            "review_boundary": REVIEW_BOUNDARY,
        }
