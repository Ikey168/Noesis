"""Answers over lobbying declarations for a dossier, an organisation or an office holder (#1911, T08).

Every row cites the register revision (and the export it came from) behind
it. Spend is returned as the declared range with its currency and period;
nothing sums, averages or otherwise collapses ranges across registrants,
registers or years. Declarations from different registers are returned side by
side with their own sources and never reconciled. Links are marked
``explicit-field``, ``reviewed-assertion`` or ``unreviewed-candidate`` and
identity as ``matched`` (a reviewed decision) or ``unmatched`` (the register
string). As-of questions return the revision in force on that date by the
register's own dates. Answers carry no influence, corruption or undeclared-
lobbying claim.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from src.ingestion.lobbying_sources import REGISTERS, REVIEW_BOUNDARY
from src.kb.lobbying import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    LobbyingError,
    LobbyingStore,
    authorize,
    digest,
    normalize_name,
)

LIMITATIONS = [
    "Declarations are what each registrant or official filed; they are not verified by this system.",
    "Spend is the declared range as filed (lower/upper bound, currency, period); ranges are never added, "
    "averaged or turned into a point estimate.",
    "Declarations from different registers are shown side by side and never reconciled.",
    "No influence, corruption or undeclared-lobbying inference is made; an absent declaration is unknown, "
    "not evidence of anything.",
]


def _day(value: Any) -> str | None:
    return (
        None if value in (None, "") else date.fromisoformat(str(value)[:10]).isoformat()
    )


class LobbyingQueries:
    def __init__(self, conn: Any) -> None:
        from src.kb.lobbying_identity import LobbyingIdentity
        from src.kb.lobbying_links import LobbyingDossierLinks

        self.conn = conn
        self.store = LobbyingStore(conn, initialize=False)
        self.links = LobbyingDossierLinks(conn, initialize=False)
        self.identity = LobbyingIdentity(conn, initialize=False)

    def _ready(self) -> bool:
        return self.store.ready()

    def _identity(self, namespace: str, key: str, scopes: set[str]) -> dict[str, Any]:
        state = self.identity.identity(namespace, key, scopes=scopes)
        return {
            "state": state["state"],
            "reviewed_links": [
                {
                    "candidate_id": v["candidate_id"],
                    "records": v["records"],
                    "entities": v["entities"],
                    "basis": v["basis"],
                    "reviewer": v["reviewer"],
                    "decision_id": v["decision_id"],
                }
                for v in state["links"]
            ],
            "open_candidates": [
                {
                    "candidate_id": v["candidate_id"],
                    "records": v["records"],
                    "basis": v["basis"],
                }
                for v in state["candidates"]
            ],
            **(
                {"unavailable": state["unavailable"]}
                if state.get("unavailable")
                else {}
            ),
        }

    def _declaration(
        self, namespace: str, revision: Mapping[str, Any], scopes: set[str]
    ) -> dict[str, Any]:
        """One register revision as filed: registrant, clients, interests, spend ranges and documents, cited."""
        statement = revision["statement"] or {}
        clients = [
            {**c, "identity": self._identity(namespace, c["record_key"], scopes)}
            for c in self.store.clients(revision)
        ]
        return {
            "register": revision["register"],
            "register_label": REGISTERS[revision["register"]]["label"],
            "native_id": revision["native_id"],
            "record_key": revision["record_key"],
            "name_as_filed": statement.get("name"),
            "lifecycle": revision["lifecycle"],
            "change": revision["change"],
            "effective_on": revision["effective_on"],
            "native_version": revision["native_version"],
            "period": statement.get("period"),
            "clients": clients,
            "declared_interests": [
                {
                    k: i[k]
                    for k in ("interest_key", "kind", "text", "code", "references")
                }
                for i in self.store.interests(revision)
            ],
            "spend_ranges": [
                {
                    k: s[k]
                    for k in (
                        "kind",
                        "lower",
                        "upper",
                        "lower_open",
                        "upper_open",
                        "currency",
                        "period",
                        "as_filed",
                        "party",
                    )
                }
                for s in self.store.spend(revision)
            ],
            "declared_grants": [
                {
                    k: g[k]
                    for k in ("source_text", "programme", "amount", "year", "assertion")
                }
                for g in self.store.grants(revision)
            ],
            "documents": [
                {k: d[k] for k in ("kind", "number", "title", "url", "retention")}
                for d in self.store.documents(revision)
            ],
            "unknown": sorted(
                field
                for field, missing in (
                    (
                        "spend",
                        not statement.get("spend")
                        and statement.get("entry_kind") == "registrant",
                    ),
                    (
                        "clients",
                        not statement.get("clients")
                        and statement.get("entry_kind") == "registrant",
                    ),
                    ("statement", revision["statement"] is None),
                )
                if missing
            ),
            "citation": {
                "register_revision_id": revision["revision_id"],
                "revision_no": revision["revision_no"],
                "previous_revision_id": revision["previous_revision_id"],
                "source_id": revision["source_id"],
                "source_revision": revision["source_revision"],
                "observed_at_ms": revision["observed_at_ms"],
            },
        }

    def _last_statement(
        self, namespace: str, entry_id: str, before: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """For a deregistration without a statement, the last statement it ended (still cited to its revision).

        Follows the predecessor chain, which runs in the register's own order.
        """
        current = before
        while current.get("previous_revision_id"):
            current = self.store.revision(namespace, current["previous_revision_id"])
            if current["statement"] is not None:
                return current
        return None

    # ------------------------------------------------------------------ dossier

    def dossier_declared_interests(
        self,
        namespace: str,
        dossier_namespace: str,
        dossier_id: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        as_of: str | None = None,
        include_candidates: bool = True,
    ) -> dict[str, Any]:
        """Who declared an interest in a dossier (or met officials about it), with cited register revisions."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        as_of = _day(as_of)
        dossier = self.links._dossier(
            dossier_namespace, dossier_id, principal_id, scopes
        )
        states = (
            ("linked", "accepted", "candidate")
            if include_candidates
            else ("linked", "accepted")
        )
        links = self.links.links(
            namespace, dossier_namespace, dossier_id, scopes=scopes, states=states
        )
        by_entry: dict[str, list[dict[str, Any]]] = {}
        for link in links:
            by_entry.setdefault(link["entry_id"], []).append(link)
        interests, meetings, not_in_force = [], [], []
        for entry_id, entry_links in sorted(by_entry.items()):
            in_force = self.store.in_force(namespace, entry_id, as_of)
            if in_force is None:
                not_in_force.append(
                    {
                        "entry_id": entry_id,
                        "reason": "no register revision on or before as_of",
                    }
                )
                continue
            cited = [
                link
                for link in entry_links
                if link["revision_id"] == in_force["revision_id"]
            ]
            if not cited:
                not_in_force.append(
                    {
                        "entry_id": entry_id,
                        "in_force_revision_id": in_force["revision_id"],
                        "lifecycle": in_force["lifecycle"],
                        "reason": "the revision in force does not carry the linked declaration",
                        "earlier_links": [
                            {
                                "link_id": link["link_id"],
                                "register_revision_id": link["revision_id"],
                                "link_kind": link["link_kind"],
                            }
                            for link in entry_links
                        ],
                    }
                )
                continue
            declaration = self._declaration(namespace, in_force, scopes)
            row_links = [
                {
                    "link_id": link["link_id"],
                    "link_kind": link["link_kind"],
                    "interest_key": link["interest_key"],
                    "reference": link["reference"],
                    "cited_dossier_revision": link["dossier_revision"],
                    "stage_id": link["stage"].get("stage_id"),
                    "reviewer": link["reviewer"],
                    "evidence": link["evidence"],
                }
                for link in cited
            ]
            if (in_force["statement"] or {}).get("entry_kind") == "meeting":
                meetings.append(
                    {
                        **self.store.meeting(in_force),
                        "links": row_links,
                        "organisations": self._organisations(
                            namespace, in_force, scopes
                        ),
                    }
                )
            else:
                interests.append(
                    {
                        **declaration,
                        "links": row_links,
                        "identity": self._identity(
                            namespace, in_force["record_key"], scopes
                        ),
                    }
                )
        declarant_keys = {row["record_key"] for row in interests}
        met = []
        for revision in self._meeting_revisions(namespace, as_of):
            organisations = self._organisations(namespace, revision, scopes)
            matched = [
                o for o in organisations if set(o["register_records"]) & declarant_keys
            ]
            if matched and revision["revision_id"] not in {
                m["citation"]["revision_id"] for m in meetings
            }:
                met.append(
                    {
                        **self.store.meeting(revision),
                        "organisations": organisations,
                        "basis": "the meeting declaration names a declarant's register number",
                        "dossier_link": None,
                    }
                )
        answer = {
            "contract": ANSWER_CONTRACT,
            "question": "declared interests in a dossier",
            "namespace": namespace,
            "dossier": {
                "namespace": dossier_namespace,
                "dossier_id": dossier_id,
                "revision": dossier["revision"],
                "jurisdiction": dossier["jurisdiction"],
                "procedure_id": dossier["procedure_id"],
            },
            "as_of": as_of,
            "declared_interests": interests,
            "meetings": meetings,
            "declarant_meetings": met,
            "side_by_side": self._side_by_side(
                namespace, [row["record_key"] for row in interests], as_of, scopes
            ),
            "not_in_force": not_in_force,
            "limitations": LIMITATIONS,
            "review_boundary": REVIEW_BOUNDARY,
        }
        return answer

    def _meeting_revisions(
        self, namespace: str, as_of: str | None
    ) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT entry_id FROM lobbying_entries WHERE namespace=? AND entry_kind='meeting' "
            "ORDER BY entry_id",
            [namespace],
        ).fetchall()
        found = []
        for (entry_id,) in rows:
            revision = self.store.in_force(namespace, entry_id, as_of)
            if revision is not None and revision["statement"] is not None:
                found.append(revision)
        return found

    def _organisations(
        self, namespace: str, revision: Mapping[str, Any], scopes: set[str]
    ) -> list[dict[str, Any]]:
        """Organisations as declared, with the register records their stated register number names (never by name)."""
        rows = []
        for org in (revision["statement"] or {}).get("organisations") or []:
            records = []
            for register_id in org.get("register_ids") or []:
                if register_id.get("scheme") == "eu-tr":
                    entry = self.store.find_entry(
                        namespace, "eu-tr", register_id["value"]
                    )
                    if entry:
                        records.append(entry["record_key"])
            rows.append(
                {
                    **org,
                    "register_records": records,
                    "identity": self._identity(namespace, records[0], scopes)
                    if records
                    else {
                        "state": "unmatched",
                        "reviewed_links": [],
                        "open_candidates": [],
                    },
                }
            )
        return rows

    def _side_by_side(
        self, namespace: str, keys: list[str], as_of: str | None, scopes: set[str]
    ) -> list[dict[str, Any]]:
        """Records of one organisation in several registers (reviewed matches only), each with its own statement."""
        groups = []
        seen = set()
        for key in keys:
            if key in seen:
                continue
            identity = self.identity.identity(namespace, key, scopes=scopes)
            members = {key} | {
                r
                for link in identity["links"]
                for r in link["records"]
                if r.startswith("lobbying:")
            }
            if len(members) < 2:
                continue
            seen |= members
            statements = []
            for member in sorted(members):
                register, native = member.split(":", 2)[1:]
                if ":client:" in native:
                    continue
                entry = self.store.find_entry(namespace, register, native)
                revision = entry and self.store.in_force(
                    namespace, entry["entry_id"], as_of
                )
                if revision and revision["statement"]:
                    declaration = self._declaration(namespace, revision, scopes)
                    statements.append(
                        {
                            k: declaration[k]
                            for k in (
                                "register",
                                "native_id",
                                "name_as_filed",
                                "lifecycle",
                                "clients",
                                "spend_ranges",
                                "citation",
                            )
                        }
                    )
            groups.append(
                {
                    "records": sorted(members),
                    "statements": statements,
                    "reconciled": False,
                    "note": "one organisation per a reviewed identity decision; each register's declaration "
                    "is kept as filed",
                }
            )
        return groups

    # ------------------------------------------------------------------ registrant

    def registrant_declarations(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        entry_id: str | None = None,
        register: str | None = None,
        native_id: str | None = None,
        entity_id: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """What a registrant declared over time; an entity id gathers its reviewed register records side by side."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        as_of = _day(as_of)
        entries = []
        if entry_id:
            entries = [self.store.entry(namespace, entry_id)]
        elif register and native_id:
            found = self.store.find_entry(namespace, register, native_id)
            if found is None:
                raise LobbyingError(
                    "not_found", "register entry is not visible in this namespace"
                )
            entries = [found]
        elif entity_id:
            keys = set()
            for candidate in self.identity.candidates(namespace, scopes=scopes):
                if (
                    candidate["state"] == "accepted"
                    and entity_id in candidate["entities"] + candidate["records"]
                ):
                    keys |= {
                        r
                        for r in candidate["records"]
                        if r.startswith("lobbying:") and ":client:" not in r
                    }
            entries = [
                e
                for e in (
                    self.store.find_entry(namespace, *k.split(":", 2)[1:])
                    for k in sorted(keys)
                )
                if e
            ]
        else:
            raise LobbyingError(
                "invalid_request",
                "name an entry, a register and native id, or an entity id",
            )
        records = []
        for entry in entries:
            if entry["entry_kind"] != "registrant":
                raise LobbyingError("invalid_request", "not a registrant entry")
            history = []
            for revision in self.store.history(namespace, entry["entry_id"]):
                declaration = self._declaration(namespace, revision, scopes)
                if revision["statement"] is None:
                    last = self._last_statement(namespace, entry["entry_id"], revision)
                    declaration["ends_revision_id"] = (
                        last["revision_id"] if last else None
                    )
                history.append(declaration)
            in_force = self.store.in_force(namespace, entry["entry_id"], as_of)
            records.append(
                {
                    "entry": entry,
                    "identity": self._identity(namespace, entry["record_key"], scopes),
                    "in_force": None
                    if in_force is None
                    else self._declaration(namespace, in_force, scopes),
                    "history": history,
                    "dossier_links": [
                        {
                            k: link[k]
                            for k in (
                                "link_id",
                                "link_kind",
                                "dossier_namespace",
                                "dossier_id",
                                "dossier_revision",
                                "revision_id",
                                "interest_key",
                            )
                        }
                        for revision in history
                        for link in self.links.links_for_revision(
                            namespace,
                            revision["citation"]["register_revision_id"],
                            scopes=scopes,
                        )
                        if link["state"] in ("linked", "accepted")
                    ],
                }
            )
        return {
            "contract": ANSWER_CONTRACT,
            "question": "registrant declarations over time",
            "namespace": namespace,
            "as_of": as_of,
            "entity_id": entity_id,
            "registrants": records,
            "side_by_side": len(records) > 1,
            "reconciled": False,
            "limitations": LIMITATIONS,
            "review_boundary": REVIEW_BOUNDARY,
        }

    # ------------------------------------------------------------------ office holder

    def official_meetings(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        official_id: str | None = None,
        official_name: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        as_of: str | None = None,
    ) -> dict[str, Any]:
        """Meetings an office holder declared, with organisations as declared and any dossier links."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if not (official_id or official_name):
            raise LobbyingError(
                "invalid_request",
                "name an official id or the official's name as published",
            )
        date_from, date_to, as_of = _day(date_from), _day(date_to), _day(as_of)
        rows = []
        for revision in self._meeting_revisions(namespace, as_of):
            official = (revision["statement"] or {}).get("official") or {}
            if official_id and official.get("id") != official_id:
                continue
            if official_name and normalize_name(official.get("name")) != normalize_name(
                official_name
            ):
                continue
            when = revision["statement"].get("date")
            if (date_from and when < date_from) or (date_to and when > date_to):
                continue
            links = [
                {
                    k: link[k]
                    for k in (
                        "link_id",
                        "link_kind",
                        "dossier_namespace",
                        "dossier_id",
                        "dossier_revision",
                        "reference",
                    )
                }
                for link in self.links.links_for_revision(
                    namespace, revision["revision_id"], scopes=scopes
                )
                if link["state"] in ("linked", "accepted", "candidate")
            ]
            rows.append(
                {
                    **self.store.meeting(revision),
                    "organisations": self._organisations(namespace, revision, scopes),
                    "dossier_links": links,
                }
            )
        rows.sort(
            key=lambda r: (
                r["date"],
                r["citation"]["register"],
                r["citation"]["native_id"],
            )
        )
        return {
            "contract": ANSWER_CONTRACT,
            "question": "meetings declared by an office holder",
            "namespace": namespace,
            "official_id": official_id,
            "official_name": official_name,
            "as_of": as_of,
            "meetings": rows,
            "limitations": LIMITATIONS,
            "review_boundary": REVIEW_BOUNDARY,
        }

    # ------------------------------------------------------------------ export

    def export_report(
        self,
        namespace: str,
        dossier_namespace: str,
        dossier_id: str,
        request_key: str,
        *,
        principal_id: str,
        scopes: Iterable[str],
        as_of: str | None = None,
        report_namespace: str | None = None,
    ) -> dict[str, Any]:
        """A cited evidence bundle for a dossier's declared interests, created as an authored report."""
        from src.kb.authored_reports import AuthoredReportStore

        scopes = set(scopes)
        answer = self.dossier_declared_interests(
            namespace,
            dossier_namespace,
            dossier_id,
            principal_id=principal_id,
            scopes=scopes,
            as_of=as_of,
            include_candidates=False,
        )
        bibliography: dict[str, dict[str, str]] = {}
        assertions, meeting_assertions = [], []

        def cite(citation: Mapping[str, Any]) -> str:
            source = citation["source_revision"]
            bibliography.setdefault(
                source["export_id"],
                {
                    "id": source["export_id"],
                    "text": f"{citation.get('source_id') or 'register export'}, published {source['publication_date']}, "
                    f"sha256 {source['file_sha256']}, {source.get('url') or 'no URL'} "
                    f"({source['evidence_origin']} evidence)",
                },
            )
            return source["export_id"]

        for row in answer["declared_interests"]:
            ranges = (
                "; ".join(
                    f"{s['kind']} {s['currency']} {s['lower'] if s['lower'] is not None else 'open'}-"
                    f"{s['upper'] if s['upper'] is not None else 'open'} "
                    f"({(s['period'] or {}).get('start') or '?'} to {(s['period'] or {}).get('end') or '?'})"
                    for s in row["spend_ranges"]
                )
                or "no spend range declared (unknown)"
            )
            kinds = ", ".join(sorted({link["link_kind"] for link in row["links"]}))
            for index, link in enumerate(row["links"]):
                assertions.append(
                    {
                        "id": f"interest-{row['citation']['register_revision_id']}-{index}",
                        "text": f"{row['register_label']} entry {row['native_id']} ({row['name_as_filed'] or 'unnamed'}) "
                        f"declares an interest linked to this dossier ({kinds}); declared ranges as filed: "
                        f"{ranges}; clients as filed: "
                        f"{', '.join(c['name'] or '?' for c in row['clients']) or 'none declared'}.",
                        "kind": "sourced",
                        "dependencies": [
                            {
                                "kind": "source",
                                "id": row["record_key"],
                                "revision": row["citation"]["register_revision_id"],
                                "namespace": namespace,
                                "locator": {"section": link["interest_key"]},
                            }
                        ],
                        "citations": [cite(row["citation"])],
                    }
                )
        for row in answer["meetings"]:
            official = row["official"] or {}
            meeting_assertions.append(
                {
                    "id": f"meeting-{row['citation']['revision_id']}",
                    "text": f"{official.get('name')} ({official.get('role')}, {official.get('institution')}) declared a "
                    f"meeting on {row['date']} with "
                    f"{', '.join(o['as_declared'] or o['name'] or '?' for o in row['organisations'])}; "
                    f"subject as declared: {row['subject']}.",
                    "kind": "sourced",
                    "dependencies": [
                        {
                            "kind": "source",
                            "id": f"lobbying:{row['citation']['register']}:"
                            f"{row['citation']['native_id']}",
                            "revision": row["citation"]["revision_id"],
                            "namespace": namespace,
                            "locator": {"section": "meeting"},
                        }
                    ],
                    "citations": [cite(row["citation"])],
                }
            )
        sections = [
            {
                "id": "declared-interests",
                "title": "Declared interests",
                "assertions": assertions,
            },
            {
                "id": "meetings",
                "title": "Meetings with officials",
                "assertions": meeting_assertions,
            },
        ]
        if not assertions and not meeting_assertions:
            sections[0]["assertions"].append(
                {
                    "id": "none",
                    "text": "No linked declaration was found.",
                    "kind": "commentary",
                    "dependencies": [],
                    "citations": [],
                }
            )
        # Export sequences are counted per register, so the namespace generation covers every register: the sum
        # of their latest sequences changes whenever any register gains an export.
        per_register = self.conn.execute(
            "SELECT register, max(sequence) FROM lobbying_exports WHERE namespace=? GROUP BY register "
            "ORDER BY register",
            [namespace],
        ).fetchall()
        generation = sum(int(sequence) for _register, sequence in per_register)
        state = digest(
            [[register, int(sequence)] for register, sequence in per_register]
        )[:12]
        content = {
            "title": f"Declared interests in dossier {answer['dossier']['procedure_id']}",
            "sections": sections,
            "snapshot": {
                "id": f"lobbying:{namespace}:{dossier_id}:{answer['dossier']['revision']}:{as_of or 'latest'}:"
                f"{state}",
                "generations": {namespace: int(generation)},
            },
            "bibliography": sorted(bibliography.values(), key=lambda b: b["id"]),
            "limitations": LIMITATIONS,
        }
        report = AuthoredReportStore(self.conn).create(
            report_namespace or dossier_namespace,
            request_key,
            content,
            principal_id=principal_id,
            scopes=scopes,
        )
        return {"report": report, "answer": answer}
