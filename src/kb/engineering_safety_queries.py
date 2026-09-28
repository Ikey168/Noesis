"""Directives as of a date, recommendation status over time and subject dossiers (ES12 #2073, ES13 #2074).

Every answer is assembled from record revisions and states its semantics:
*as published; not a compliance determination*. The current revision of each
record is selected first (by report status and the source's own date, among
revisions acquired by the cutoff), and only then filtered by subject, so a
later revision never leaks into an earlier date and an unacquired one never
shows up in a replay.

``directives_as_of`` returns directives whose published applicability names
the subject and that were in effect on the date:

* in effect = effective on or before the date and not superseded or revised
  by a directive that was itself effective on or before the date;
* superseded directives appear only in the supersession chain (walked in both
  directions, from the relations the directives state);
* applicability that could not be parsed is returned as *possibly applicable
  - see text* with the verbatim clause when it mentions the subject's
  designation; every other unparsed clause is counted and named, never
  silently dropped.

A subject is matched through :func:`src.kb.engineering_safety_identity.query_keys`,
the equivalence shared with the monitors. A subject with no records is
reported as *none on record*, never as safe.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from src.kb.engineering_safety_identity import SubjectIdentity, key_matches, query_keys
from src.kb.engineering_safety_records import (
    BOUNDARY,
    DIRECTIVES_CONTRACT,
    DOSSIER_CONTRACT,
    NONE_ON_RECORD,
    READ_SCOPE,
    EngineeringSafetyError,
    authorize,
    designation_key,
    digest,
    iso_date,
    name_key,
    require,
)
from src.kb.engineering_safety_store import EngineeringSafetyStore, table_exists

SEMANTICS = "as published; not a compliance determination"
PRODUCTS_READ_SCOPE = "knowledge:products:read"
MAX_LISTED = 25


def _cutoff(as_of: str | None) -> date | None:
    if as_of in (None, ""):
        return None
    parsed = iso_date(as_of)
    if parsed is None or len(str(as_of)) != 10:
        raise EngineeringSafetyError(
            "invalid_request", "as_of must be an ISO date (YYYY-MM-DD)"
        )
    return parsed


def _designation_token(key: str) -> str | None:
    """The designation part of a subject key, as :func:`designation_key` normalises it; ``None`` for non-models."""
    parts = key.split(":")
    if parts[0] in {"aircraft-model", "engine-model"} and len(parts) > 1:
        return designation_key(parts[1]) or None
    if parts[0] in {"vehicle", "component"} and len(parts) > 2:
        return designation_key(parts[2]) or None
    return None


def _serial_state(serial: str, parsed: Mapping[str, Any] | None) -> str:
    """``in-range``, ``out-of-range`` or ``unknown`` for one serial against a parsed serial statement."""
    serials = dict((parsed or {}).get("serials") or {})
    if serials.get("all"):
        return "in-range"
    ranges = serials.get("ranges") or []
    if not ranges:
        return "unknown"
    for start, end in ranges:
        if serial.isdigit() and str(start).isdigit() and str(end).isdigit():
            if int(start) <= int(serial) <= int(end):
                return "in-range"
        elif serial in {start, end}:
            return "in-range"
        else:
            return "unknown"  # non-numeric ranges are never compared by string order
    return "out-of-range"


class EngineeringSafetyQueries:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = EngineeringSafetyStore(conn, initialize=False)
        self.identity = SubjectIdentity(conn, initialize=False)

    # ------------------------------------------------------------ snapshot

    def snapshot(
        self,
        namespace: str,
        kinds: Iterable[str],
        as_of: date | None,
        acquired_by_ms: int | None,
        cutoff_seq: int | None = None,
    ) -> list[dict[str, Any]]:
        """One chosen revision per record (current as of the date among acquired revisions), with its parts."""
        result = []
        for head in self.store.records(namespace, kinds):
            chosen, later = self.store.revision_as_of(
                namespace,
                head["record_id"],
                as_of,
                acquired_by_ms,
                cutoff_seq=cutoff_seq,
            )
            if chosen is None:
                continue
            result.append(
                {
                    **head,
                    "revision": chosen,
                    "parts": self.store.parts(namespace, chosen["revision_id"]),
                    "later_revisions": [r["revision_id"] for r in later],
                }
            )
        return result

    @staticmethod
    def _cite(entry: Mapping[str, Any]) -> dict[str, Any]:
        revision = entry["revision"]
        return {
            k: revision.get(k)
            for k in (
                "revision_id",
                "revision_no",
                "revision_label",
                "revision_date",
                "revision_date_basis",
                "report_status",
                "source_id",
                "document_id",
                "observed_at_ms",
                "url",
            )
            if revision.get(k) is not None
        }

    def _names(self, entry: Mapping[str, Any], patterns) -> list[dict[str, Any]]:
        return [
            s
            for s in entry["parts"]["subjects"]
            if key_matches(s.get("subject_key"), patterns)
        ]

    # ------------------------------------------------------------ directives (ES12)

    def directives_as_of(
        self,
        namespace: str,
        subject: Mapping[str, Any],
        *,
        scopes: Iterable[str],
        as_of: str | None,
        acquired_by_ms: int | None = None,
        serial: str | None = None,
        cutoff_seq: int | None = None,
    ) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        cutoff = _cutoff(as_of)
        patterns, connecting = query_keys(self.identity, namespace, subject)
        directives = self.snapshot(
            namespace, ["directive"], cutoff, acquired_by_ms, cutoff_seq
        )
        by_native = {(d["provider"], d["native_id"]): d for d in directives}

        def effective_by(entry: Mapping[str, Any]) -> bool | None:
            stated = entry["revision"].get("effective_date")
            if stated is None:
                return None
            return cutoff is None or iso_date(stated) <= cutoff

        # Supersession edges from every chosen revision, independent of arrival order.
        superseded_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for entry in directives:
            for relation in entry["parts"]["relations"]:
                if relation["relation"] in {"supersedes", "revises"}:
                    superseded_by.setdefault(
                        (relation["target_provider"], relation["target_native_id"]), []
                    ).append({"by": entry, "relation": relation})

        def chain(entry: Mapping[str, Any]) -> dict[str, Any]:
            def walk_back(node, seen):
                items = []
                for relation in node["parts"]["relations"]:
                    if relation["relation"] not in {"supersedes", "revises"}:
                        continue
                    key = (relation["target_provider"], relation["target_native_id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    target = by_native.get(key)
                    items.append(
                        {
                            "provider": key[0],
                            "native_id": key[1],
                            "relation": relation["relation"],
                            "stated_in": node["revision"]["revision_id"],
                            "text": relation.get("text"),
                            "locator": relation.get("locator"),
                            **(
                                {
                                    "record_id": target["record_id"],
                                    **self._cite(target),
                                    "effective_date": target["revision"].get(
                                        "effective_date"
                                    ),
                                }
                                if target
                                else {"note": "not on record in this namespace"}
                            ),
                        }
                    )
                    if target:
                        items += walk_back(target, seen)
                return items

            def walk_forward(node, seen):
                items = []
                for edge in superseded_by.get(
                    (node["provider"], node["native_id"]), []
                ):
                    successor = edge["by"]
                    key = (successor["provider"], successor["native_id"])
                    if key in seen:
                        continue
                    seen.add(key)
                    effective = effective_by(successor)
                    items.append(
                        {
                            "provider": key[0],
                            "native_id": key[1],
                            "relation": edge["relation"]["relation"],
                            "record_id": successor["record_id"],
                            **self._cite(successor),
                            "effective_date": successor["revision"].get(
                                "effective_date"
                            ),
                            "in_effect_by_as_of": effective,
                        }
                    )
                    items += walk_forward(successor, seen)
                return items

            start = {(entry["provider"], entry["native_id"])}
            return {
                "supersedes": walk_back(entry, set(start)),
                "superseded_by": walk_forward(entry, set(start)),
            }

        in_effect, possibly, not_yet, excluded_serial, unparsed_elsewhere = (
            [],
            [],
            [],
            [],
            [],
        )
        tokens = {_designation_token(p["key"]) for p in patterns} - {None}
        for entry in directives:
            named = self._names(entry, patterns)
            clauses = entry["parts"]["applicability"]
            reason = None
            if named:
                reason = "published applicability names the subject"
            unparsed = [c for c in clauses if c["parse_state"] == "unparsed"]
            mentions = [
                c
                for c in unparsed
                if any(t and t in designation_key(c["text"]) for t in tokens)
            ]
            if not named and not mentions:
                if (
                    unparsed and tokens
                ):  # only a model designation can be named by applicability
                    unparsed_elsewhere.append(
                        {
                            "record_id": entry["record_id"],
                            "provider": entry["provider"],
                            "native_id": entry["native_id"],
                            **self._cite(entry),
                        }
                    )
                continue
            superseders = [
                edge
                for edge in superseded_by.get(
                    (entry["provider"], entry["native_id"]), []
                )
                if effective_by(edge["by"])
            ]
            item = {
                "record_id": entry["record_id"],
                "provider": entry["provider"],
                "native_id": entry["native_id"],
                "authority": entry["authority"],
                "title": entry["revision"].get("title"),
                "effective_date": entry["revision"].get("effective_date"),
                "revision": self._cite(entry),
                "applicability": clauses,
                "matched_subjects": named,
                "required_actions": [
                    s
                    for s in entry["parts"]["statements"]
                    if s["kind"] == "required_action"
                ],
                "compliance_time": [
                    s
                    for s in entry["parts"]["statements"]
                    if s["kind"] == "compliance_time"
                ],
                "chain": chain(entry),
                "connected_by": connecting,
            }
            if superseders:
                continue  # superseded before the date: returned only inside its successors' chains
            effective = effective_by(entry)
            if effective is False:
                not_yet.append(
                    {
                        k: item[k]
                        for k in (
                            "record_id",
                            "provider",
                            "native_id",
                            "effective_date",
                            "revision",
                        )
                    }
                )
                continue
            if serial and named:
                states = {
                    _serial_state(serial, c.get("parsed"))
                    for c in clauses
                    if c["parse_state"] == "parsed"
                }
                if states == {"out-of-range"}:
                    excluded_serial.append(
                        {
                            **{
                                k: item[k]
                                for k in (
                                    "record_id",
                                    "provider",
                                    "native_id",
                                    "revision",
                                )
                            },
                            "applicability": clauses,
                            "note": "serial outside the published range",
                        }
                    )
                    continue
                if "in-range" not in states:
                    possibly.append(
                        {
                            **item,
                            "applicability_state": "possibly applicable — see text",
                            "reason": "the serial could not be compared with the published range",
                        }
                    )
                    continue
            if named and effective:
                in_effect.append(
                    {
                        **item,
                        "applicability_state": "applicable as published",
                        "reason": reason,
                    }
                )
            else:
                possibly.append(
                    {
                        **item,
                        "applicability_state": "possibly applicable — see text",
                        "reason": "effective date not published"
                        if effective is None
                        else "applicability could not be parsed; the clause mentions the designation",
                        "clauses": mentions or clauses,
                    }
                )
        observed = [
            d["revision"].get("observed_at_ms")
            for d in directives
            if d["revision"].get("observed_at_ms")
        ]
        result = {
            "contract": DIRECTIVES_CONTRACT,
            "namespace": namespace,
            "subject": dict(subject),
            "serial": serial,
            "as_of": None if cutoff is None else cutoff.isoformat(),
            "acquired_by_ms": acquired_by_ms,
            "semantics": SEMANTICS,
            "in_effect": in_effect,
            "possibly_applicable": possibly,
            "not_yet_effective": not_yet,
            "outside_serial_range": excluded_serial,
            "unparsed_applicability_elsewhere": {
                "n": len(unparsed_elsewhere),
                "directives": unparsed_elsewhere,
            },
            "data_as_of_ms": max(observed) if observed else None,
            "status": "records on file"
            if in_effect or possibly or not_yet
            else NONE_ON_RECORD,
            "boundary": BOUNDARY,
        }
        result["pins"] = {
            e["record_id"]: e["revision"]["revision_id"]
            for e in in_effect + possibly + not_yet + excluded_serial
        }
        return result

    # ------------------------------------------------------------ search

    def search(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        kinds: Iterable[str],
        text: str | None = None,
        authority: str | None = None,
        native_id: str | None = None,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Records by native id, issuing authority or a source string they state (unmatched subjects included)."""
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        cutoff = _cutoff(as_of)
        needle = (text or "").casefold().strip()
        items = []
        for entry in self.snapshot(namespace, kinds, cutoff, acquired_by_ms):
            if authority and entry["authority"] != authority:
                continue
            if native_id and entry["native_id"] != native_id:
                continue
            if needle:
                haystack = (
                    [entry["revision"].get("title") or ""]
                    + [s["source_string"] for s in entry["parts"]["subjects"]]
                    + [c["text"] for c in entry["parts"]["applicability"]]
                )
                if not any(needle in h.casefold() for h in haystack):
                    continue
            items.append(
                {
                    "record_id": entry["record_id"],
                    "provider": entry["provider"],
                    "record_kind": entry["record_kind"],
                    "native_id": entry["native_id"],
                    "authority": entry["authority"],
                    "title": entry["revision"].get("title"),
                    "effective_date": entry["revision"].get("effective_date"),
                    "revision": self._cite(entry),
                    "subjects": [
                        {
                            k: s.get(k)
                            for k in ("kind", "source_string", "subject_key", "role")
                        }
                        for s in entry["parts"]["subjects"]
                    ],
                }
            )
        limit = max(1, min(int(limit), 200))
        return {
            "namespace": namespace,
            "as_of": None if cutoff is None else cutoff.isoformat(),
            "n": len(items),
            "items": items[:limit],
            "truncated": len(items) > limit,
            "semantics": SEMANTICS,
            "boundary": BOUNDARY,
        }

    # ------------------------------------------------------------ recommendations (ES13)

    def _recommendation(
        self,
        namespace: str,
        entry: Mapping[str, Any],
        cutoff: date | None,
        acquired_by_ms: int | None,
        cutoff_seq: int | None = None,
    ) -> dict[str, Any]:
        history = self.store.response_history(
            namespace, entry["record_id"], acquired_by_ms, cutoff_seq
        )
        dated = [h for h in history if h.get("status_date")]
        visible = [
            h for h in dated if cutoff is None or iso_date(h["status_date"]) <= cutoff
        ]
        undated = [h for h in history if not h.get("status_date")]
        current = (
            visible[-1]
            if visible
            else (undated[-1] if undated and cutoff is None else None)
        )
        addressees = sorted(
            {
                s["source_string"]
                for s in entry["parts"]["subjects"]
                if s.get("role") == "addressee"
            }
            | {h["addressee"] for h in history if h.get("addressee")}
        )
        return {
            "record_id": entry["record_id"],
            "provider": entry["provider"],
            "native_id": entry["native_id"],
            "authority": entry["authority"],
            "addressees": addressees,
            "title": entry["revision"].get("title"),
            "text": [
                s
                for s in entry["parts"]["statements"]
                if s["kind"] == "recommendation_text"
            ],
            "revision": self._cite(entry),
            "status_as_of": current
            or {"status": "no status published as of this date"},
            "response_history": visible
            + ([{**h, "note": "undated as published"} for h in undated]),
            "later_status_changes": [
                h
                for h in dated
                if cutoff is not None and iso_date(h["status_date"]) > cutoff
            ],
            "investigations": [
                r["target_native_id"]
                for r in entry["parts"]["relations"]
                if r["relation"] == "recommendation_of"
            ],
        }

    def recommendations(
        self,
        namespace: str,
        *,
        scopes: Iterable[str],
        addressee: str | None = None,
        subject: Mapping[str, Any] | None = None,
        authority: str | None = None,
        status: str | None = None,
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
        native_id: str | None = None,
    ) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        cutoff = _cutoff(as_of)
        related: set[tuple[str, str]] | None = None
        if subject:
            patterns, _ = query_keys(self.identity, namespace, subject)
            related = set()
            for entry in self.snapshot(
                namespace, ["investigation"], cutoff, acquired_by_ms
            ):
                if self._names(entry, patterns):
                    related |= {
                        (r["target_provider"], r["target_native_id"])
                        for r in entry["parts"]["relations"]
                        if r["relation"] == "issued_recommendation"
                    }
                    related.add(
                        ("investigation", f"{entry['provider']}:{entry['native_id']}")
                    )
        items = []
        for entry in self.snapshot(
            namespace, ["safety_recommendation"], cutoff, acquired_by_ms
        ):
            if authority and entry["authority"] != authority:
                continue
            if native_id and entry["native_id"] != native_id:
                continue
            item = self._recommendation(namespace, entry, cutoff, acquired_by_ms)
            if addressee and name_key(addressee) not in {
                name_key(a) for a in item["addressees"]
            }:
                continue
            if (
                status
                and status.casefold()
                not in str(item["status_as_of"]["status"]).casefold()
            ):
                continue
            if (
                related is not None
                and (entry["provider"], entry["native_id"]) not in related
                and not any(
                    ("investigation", f"{entry['provider']}:{i}") in related
                    for i in item["investigations"]
                )
            ):
                continue
            items.append(item)
        return {
            "namespace": namespace,
            "as_of": None if cutoff is None else cutoff.isoformat(),
            "n": len(items),
            "recommendations": items,
            "semantics": "status as published by the issuing body on each date",
            "boundary": BOUNDARY,
        }

    # ------------------------------------------------------------ dossier (ES13)

    def _investigation(
        self, namespace: str, entry: Mapping[str, Any]
    ) -> dict[str, Any]:
        parts = entry["parts"]
        conflicts = []
        history = self.store.revisions(namespace, entry["record_id"])
        for kind in ("probable_cause", "finding"):
            versions = []
            for revision in history:
                if revision["seq"] > entry["revision"]["seq"]:
                    continue
                texts = [
                    s["text"]
                    for s in self.store.parts(namespace, revision["revision_id"])[
                        "statements"
                    ]
                    if s["kind"] == kind
                ]
                if texts and texts not in [v["texts"] for v in versions]:
                    versions.append(
                        {
                            "revision_id": revision["revision_id"],
                            "report_status": revision.get("report_status"),
                            "revision_date": revision.get("revision_date"),
                            "texts": texts,
                        }
                    )
            if len(versions) > 1:
                conflicts.append(
                    {
                        "record_id": entry["record_id"],
                        "statement": kind,
                        "side_by_side": versions,
                        "note": "differing statements across report revisions; both kept, none chosen",
                    }
                )
        return {
            "record_id": entry["record_id"],
            "provider": entry["provider"],
            "native_id": entry["native_id"],
            "authority": entry["authority"],
            "title": entry["revision"].get("title"),
            "access": entry["revision"].get("access"),
            "language": entry["revision"].get("language"),
            "report_status": entry["revision"].get("report_status"),
            "revision": self._cite(entry),
            "occurrence": parts["occurrence"],
            "probable_cause": [
                s for s in parts["statements"] if s["kind"] == "probable_cause"
            ],
            "findings": [
                s for s in parts["statements"] if s["kind"] in {"finding", "root_cause"}
            ],
            "recommendations_issued": [
                r["target_native_id"]
                for r in parts["relations"]
                if r["relation"] == "issued_recommendation"
            ],
            "_conflicts": conflicts,
        }

    def _recall(
        self, relation: Mapping[str, Any], products_namespace: str | None
    ) -> dict[str, Any]:
        number = relation["target_native_id"]
        if products_namespace and table_exists(self.conn, "product_safety_current"):
            from src.kb.product_safety import notice_id_for

            notice_id = notice_id_for(products_namespace, "nhtsa", number)
            row = self.conn.execute(
                "SELECT revision_id FROM product_safety_current WHERE namespace=? AND notice_id=?",
                [products_namespace, notice_id],
            ).fetchone()
            if row:
                return {
                    "campaign_number": number,
                    "status": "linked",
                    "owner": "products.safety",
                    "notice_id": notice_id,
                    "notice_revision_id": row[0],
                    "basis": "campaign number as cited",
                }
        return {
            "campaign_number": number,
            "status": "unresolved identifier",
            "note": "the recall is owned by the Products safety feature; it is not on record there"
            if products_namespace
            else "no Products namespace was named; kept as the cited identifier",
        }

    def dossier(
        self,
        namespace: str,
        subject: Mapping[str, Any],
        *,
        scopes: Iterable[str],
        as_of: str | None = None,
        acquired_by_ms: int | None = None,
        serial: str | None = None,
        products_namespace: str | None = None,
    ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        if products_namespace:
            require(scopes, PRODUCTS_READ_SCOPE)
            authorize(products_namespace, scopes, PRODUCTS_READ_SCOPE)
        self.store.require_ready()
        cutoff = _cutoff(as_of)
        patterns, connecting = query_keys(self.identity, namespace, subject)
        directives = self.directives_as_of(
            namespace,
            subject,
            scopes=scopes,
            as_of=as_of,
            acquired_by_ms=acquired_by_ms,
            serial=serial,
        )
        pins = dict(directives["pins"])
        investigations, conflicts, occurrences, defects, complaints = [], [], [], [], []
        issued: set[tuple[str, str]] = set()
        unmatched: dict[str, dict[str, Any]] = {}
        entries = self.snapshot(
            namespace,
            ["investigation", "occurrence", "defect_investigation", "complaint"],
            cutoff,
            acquired_by_ms,
        )
        by_native = {
            (e["provider"], e["native_id"]): e
            for e in entries
            if e["record_kind"] == "defect_investigation"
        }
        for entry in entries:
            if not self._names(entry, patterns):
                continue
            pins[entry["record_id"]] = entry["revision"]["revision_id"]
            for other in entry["parts"]["subjects"]:
                if not key_matches(other.get("subject_key"), patterns):
                    unmatched.setdefault(
                        other["source_string"],
                        {
                            "source_string": other["source_string"],
                            "kind": other["kind"],
                            "role": other.get("role"),
                            "status": "source string (no accepted match)",
                        },
                    )
            kind = entry["record_kind"]
            if kind == "investigation":
                item = self._investigation(namespace, entry)
                conflicts += item.pop("_conflicts")
                investigations.append(item)
                issued |= {
                    (entry["provider"], n) for n in item["recommendations_issued"]
                }
            elif kind == "occurrence":
                occurrences.append(
                    {
                        "record_id": entry["record_id"],
                        "provider": entry["provider"],
                        "native_id": entry["native_id"],
                        "revision": self._cite(entry),
                        "occurrence": entry["parts"]["occurrence"],
                        "quantities": entry["parts"]["quantities"],
                        "statements": entry["parts"]["statements"],
                    }
                )
            elif kind == "defect_investigation":
                relations = entry["parts"]["relations"]
                defects.append(
                    {
                        "record_id": entry["record_id"],
                        "native_id": entry["native_id"],
                        "action_type": entry["native_id"][:2],
                        "title": entry["revision"].get("title"),
                        "revision": self._cite(entry),
                        "summary": entry["parts"]["statements"],
                        "upgrades": [
                            {
                                "relation": r["relation"],
                                "native_id": r["target_native_id"],
                                "on_record": (
                                    r["target_provider"],
                                    r["target_native_id"],
                                )
                                in by_native,
                            }
                            for r in relations
                            if r["relation"] in {"upgraded_from", "upgraded_to"}
                        ],
                        "recalls": [
                            self._recall(r, products_namespace)
                            for r in relations
                            if r["relation"] == "cites_recall"
                        ],
                    }
                )
            else:
                complaints.append(
                    {
                        "record_id": entry["record_id"],
                        "native_id": entry["native_id"],
                        "revision": self._cite(entry),
                    }
                )
        recommendations = []
        for entry in self.snapshot(
            namespace, ["safety_recommendation"], cutoff, acquired_by_ms
        ):
            if (entry["provider"], entry["native_id"]) in issued or self._names(
                entry, patterns
            ):
                pins[entry["record_id"]] = entry["revision"]["revision_id"]
                recommendations.append(
                    self._recommendation(namespace, entry, cutoff, acquired_by_ms)
                )
        pending = []
        for pattern in patterns:
            for match in self.identity.candidates(
                namespace, scopes=scopes, subject_key=pattern["key"]
            ):
                if (
                    not match["attached"]
                    and match["candidate_state"] == "proposed"
                    and match["review_state"] != "rejected"
                ):
                    pending.append(
                        {
                            k: match[k]
                            for k in (
                                "match_id",
                                "subject_key",
                                "target_kind",
                                "target_id",
                                "basis",
                                "review_state",
                            )
                        }
                    )
        unknowns = []
        for item in directives["possibly_applicable"]:
            unknowns.append(
                {
                    "kind": "applicability",
                    "record_id": item["record_id"],
                    "note": item["reason"],
                }
            )
        if directives["unparsed_applicability_elsewhere"]["n"]:
            unknowns.append(
                {
                    "kind": "unparsed applicability",
                    "n": directives["unparsed_applicability_elsewhere"]["n"],
                    "note": "directives whose applicability could not be parsed and does not mention the "
                    "designation; listed in the directives section",
                }
            )
        for defect in defects:
            for recall in defect["recalls"]:
                if recall["status"] != "linked":
                    unknowns.append(
                        {
                            "kind": "recall",
                            "record_id": defect["record_id"],
                            "campaign_number": recall["campaign_number"],
                            "note": recall["note"],
                        }
                    )
        if subject.get("product_model_id") or subject.get("entity_id"):
            if not connecting:
                unknowns.append(
                    {
                        "kind": "identity",
                        "note": "no accepted match connects this id to any published "
                        "subject; pending candidates are listed",
                    }
                )
        has_records = bool(
            directives["in_effect"]
            or directives["possibly_applicable"]
            or directives["not_yet_effective"]
            or investigations
            or recommendations
            or occurrences
            or defects
            or complaints
        )
        generation = self.store.generation(namespace)
        dossier = {
            "contract": DOSSIER_CONTRACT,
            "namespace": namespace,
            "subject": dict(subject),
            "as_of": None if cutoff is None else cutoff.isoformat(),
            "acquired_by_ms": acquired_by_ms,
            "status": "records on file" if has_records else NONE_ON_RECORD,
            "sections": {
                "directives": {
                    k: directives[k]
                    for k in (
                        "in_effect",
                        "possibly_applicable",
                        "not_yet_effective",
                        "outside_serial_range",
                        "unparsed_applicability_elsewhere",
                    )
                },
                "investigations": investigations,
                "recommendations": recommendations,
                "defect_investigations": defects,
                "occurrences": occurrences,
                "complaints": {
                    "n": len(complaints),
                    "items": complaints[:MAX_LISTED],
                    "note": "unverified consumer reports as published; never confirmed defects",
                },
            },
            "matches": connecting,
            "pending_candidates": pending,
            "unmatched_subjects": sorted(
                unmatched.values(), key=lambda u: u["source_string"]
            )[:MAX_LISTED],
            "conflicts": conflicts,
            "unknowns": unknowns,
            "sources_consulted": self.store.sources_consulted(namespace),
            "semantics": SEMANTICS
            + "; "
            + (
                "a subject with no records has none on record, which is not a "
                "statement that it is safe"
            ),
            "boundary": BOUNDARY,
            "pins": pins,
            "review_generation": generation,
        }
        dossier["dossier_hash"] = digest(
            {
                "subject": dossier["subject"],
                "as_of": dossier["as_of"],
                "acquired_by_ms": acquired_by_ms,
                "pins": pins,
                "matches": [m["match_id"] for m in connecting],
                "generation": generation,
            }
        )
        return dossier


def export_bundle(
    conn: Any, result: Mapping[str, Any], *, created_at_ms: int | None = None
) -> dict[str, Any]:
    """A dossier or a directives-as-of answer as a ``noesis-evidence-bundle-v1``: one evidence object per revision."""
    from src.evidence_bundle import EvidenceBundleBuilder

    if result.get("contract") not in {DOSSIER_CONTRACT, DIRECTIVES_CONTRACT}:
        raise EngineeringSafetyError(
            "invalid_request",
            "only an assembled dossier or directives answer is exported",
        )
    store = EngineeringSafetyStore(conn, initialize=False)
    namespace = result["namespace"]
    builder = EvidenceBundleBuilder(
        "receipt",
        {
            "domain": "engineering-safety",
            "kind": result["contract"],
            "namespace": namespace,
            "subject": result["subject"],
            "as_of": result["as_of"],
            "result_hash": result.get("dossier_hash") or digest(result.get("pins")),
        },
        created_at_ms=created_at_ms if created_at_ms is not None else store.now(),
    )
    evidence = []
    for record_id, revision_id in sorted(result["pins"].items()):
        head = store.record(namespace, record_id)
        revision = next(
            (
                r
                for r in store.revisions(namespace, record_id)
                if r["revision_id"] == revision_id
            ),
            None,
        )
        if revision is None:
            raise EngineeringSafetyError(
                "invalid_request", "a pinned revision is not on record for its record"
            )
        evidence.append(
            builder.add_object(
                "evidence",
                {
                    "contract": "noesis-engineering-safety-evidence-v1",
                    "record_id": record_id,
                    "revision_id": revision_id,
                    "provider": head["provider"],
                    "native_id": head["native_id"],
                    "authority": head["authority"],
                    "content_digest": revision["content_digest"],
                    "revision_date": revision.get("revision_date"),
                    "locator": {
                        "document_id": revision.get("document_id") or revision_id,
                        "url": revision.get("url"),
                        "source": head["provider"],
                        "cited": True,
                    },
                },
                object_id=f"es-evidence:{revision_id}",
            )
        )
    builder.add_object(
        "receipt",
        dict(result),
        object_id=f"es-answer:{digest(dict(result))[:32]}",
        references=evidence,
        root=True,
    )
    return builder.build()
