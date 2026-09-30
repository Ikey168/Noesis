"""A treaty's status for a participant as of a date, a participant's actions and a treaty's statements (#2581).

* :meth:`TreatyQueries.status_as_of` (TR08) - given a treaty and a participant,
  the chain of actions the depositary records on or before a date (signature,
  consent to be bound, reservations and declarations, withdrawals and
  denunciations, entry into force), selected by the **action and effective
  dates as published** and never by an inferred date. The answer describes the
  depositary record (``record_state``); pending actions (deposited, effective
  later) and unclear ones (no published date, the source's note quoted) are
  returned as such. Each item cites the depositary revision used.
* :meth:`TreatyQueries.participant_actions` and
  :meth:`TreatyQueries.treaty_statements` (TR09) - a participant's actions over
  a period and a treaty's reservations, declarations and objections verbatim,
  filtered by action type, period and source; an objection is linked to the
  objected reservation only where the source links it.
* :meth:`TreatyQueries.history` - every revision of a treaty and its actions,
  with the fields each depositary correction changed.
* :meth:`TreatyQueries.export_bundle` - a ``noesis-evidence-bundle-v1`` citing
  every item with source, record revision and as-of time.

A treaty is named by record key or published identifier (``untc:XXIX-99``,
``celex:22099A0101(01)``, ``cets:990``); accepted TR06 treaty matches add the
other sources' records. A participant is named by participant key, by an ISO
3166-1 code or place id reached through an accepted TR06 match, or by its name
exactly as a source published it (per source; not an identity decision).

Nothing here is legal advice: no obligation, compliance or legal effect of a
reservation is inferred, and treaty texts are linked, not reproduced.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from src.kb.treaties_records import (
    ACTION_TYPES,
    ANSWER_CONTRACT,
    CONSENT_TYPES,
    EXCLUSIONS,
    NOTICE,
    PROVIDERS,
    READ_SCOPE,
    STATEMENT_TYPES,
    TreatiesError,
    authorize,
    digest,
    table_exists,
)
from src.kb.treaties_store import TreatyStore, as_of_day, cite

STATEMENT_KINDS = STATEMENT_TYPES + ("withdrawal", "denunciation")
COVERAGE = ("only the declared, acquired treaties of each source are searched; absence here is not absence of a "
            "treaty action")
_SCHEMES = {"untc": "untc-mtdsg", "celex": "celex", "cets": "cets", "ets": "cets", "unts": "unts-registration",
            "eli": "eli"}


class TreatyQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.store = TreatyStore(conn, initialize=False, now=self.now)

    def _identity(self):
        from src.kb.treaties_identity import TreatiesIdentity

        return TreatiesIdentity(self.conn, initialize=False, now=self.now)

    # ------------------------------------------------------------------ resolution

    def resolve_treaty(self, namespace: str, treaty: str) -> tuple[list[str], dict[str, Any]]:
        text = str(treaty or "").strip()
        if not text:
            raise TreatiesError("invalid_request", "name a treaty by record key or identifier (untc:, celex:, cets:)")
        keys: list[str] = []
        if text.startswith("treaties:treaty:"):
            keys = [text] if self.store.revisions(namespace, text) else []
            basis = "record key"
        else:
            scheme, _, value = text.partition(":") if ":" in text and text.split(":", 1)[0].casefold() in _SCHEMES \
                else ("", "", text)
            wanted = (_SCHEMES.get(scheme.casefold()), value.strip().lstrip("0") or "0")
            for key in self.store.treaty_keys(namespace):
                row = self.store.as_of(namespace, key)
                for ident in self.store.record(row)["fields"].get("identifiers") or []:
                    if (wanted[0] in (None, ident["scheme"])) and str(ident["value"]).lstrip("0") == wanted[1]:
                        keys.append(key)
                        break
            basis = "published identifier (exact)"
        related = []
        if keys and table_exists(self.conn, "treaty_identity_assertions"):
            for key in list(keys):
                for other in self._identity().related_treaties(namespace, key):
                    if other["treaty_key"] not in keys:
                        keys.append(other["treaty_key"])
                        related.append({**other, "from": key})
        return keys, {"treaty": text, "basis": basis, "accepted_treaty_matches": related}

    def resolve_participant(self, namespace: str, participant: str) -> tuple[list[str], dict[str, Any]]:
        text = str(participant or "").strip()
        if not text:
            raise TreatiesError("invalid_request", "name a participant by key, ISO 3166-1 code (iso3166:DE), place id "
                                                   "or name as published")
        if text.startswith("treaties:participant:"):
            return [text], {"participant": text, "basis": "participant key (one source)"}
        if text.startswith(("iso3166:", "place")) or text.isupper() and len(text) in (2, 3):
            links = self._identity().participants_for_place(namespace, text) if table_exists(
                self.conn, "treaty_identity_assertions") else []
            return sorted({x["participant_key"] for x in links}), {
                "participant": text, "basis": "accepted identity match (reviewable, reversible)",
                "matches": [{**{k: x[k] for k in ("participant_key", "candidate_id", "method", "reviewer")},
                             "place_id": x["place"].get("place_id"), "codes": x["place"].get("codes")}
                            for x in links]}
        keys = [p["participant_key"] for p in self.store.participants(namespace)
                if p["name_as_published"].casefold() == text.casefold()]
        return keys, {"participant": text, "basis": "name exactly as each source published it, per source; not an "
                                                    "identity decision"}

    # ------------------------------------------------------------------ views

    def _view(self, namespace: str, row: Mapping[str, Any], known_as_of: str | None = None) -> dict[str, Any]:
        record = self.store.record(row)
        fields = record["fields"]
        revisions = self.store.revisions(namespace, row["record_key"])
        view = {"action_key": row["record_key"], "treaty_key": row["treaty_key"], "provider": row["provider"],
                "participant": {k: fields["participant"].get(k) for k in ("name_as_published", "kind", "key")},
                "action_type": row["action_type"], "action_type_as_published": fields.get("action_type_as_published"),
                "action_date": row["action_date"], "deposit_date": row["deposit_date"],
                "effective_date": row["effective_date"], "date_as_published": fields.get("date_as_published"),
                "date_status": fields.get("date_status"), "citation": cite(row),
                "later_revisions": sum(1 for r in revisions if r["revision_no"] > row["revision_no"]),
                "known_as_of": known_as_of}
        if row["action_type"] in STATEMENT_KINDS and fields.get("text"):
            view["text_verbatim"] = fields["text"]
            view["text_anchor"] = fields.get("text_anchor")
        for key in ("footnotes", "section_note", "withdraws", "period_covered_as_published", "articles_as_published"):
            if fields.get(key):
                view[key] = fields[key]
        if fields.get("objected"):
            view["objected"] = dict(fields["objected"])
        return view

    def _current(self, namespace: str, keys: Sequence[str], known_as_of: str | None) -> list[dict[str, Any]]:
        return [row for row in (self.store.as_of(namespace, key, known_as_of) for key in keys) if row]

    @staticmethod
    def _when(row: Mapping[str, Any]) -> str | None:
        return row["action_date"] or row["deposit_date"] or row["effective_date"]

    def _treaty_view(self, namespace: str, treaty_key: str, known_as_of: str | None) -> dict[str, Any] | None:
        row = self.store.as_of(namespace, treaty_key, known_as_of)
        if row is None:
            return None
        fields = self.store.record(row)["fields"]
        return {"treaty_key": treaty_key, "provider": row["provider"], "title_as_published": row["title"],
                "identifiers": fields.get("identifiers") or [], "depositary": fields.get("depositary"),
                "adoption": fields.get("adoption"), "entry_into_force": fields.get("entry_into_force"),
                "signature": fields.get("signature"), "text_policy": fields.get("text_policy"),
                "text_url": fields.get("text_url"), "attribution": fields.get("attribution"), "citation": cite(row)}

    def _answer(self, namespace: str, query: Mapping[str, Any], status: str, body: Mapping[str, Any]) -> dict[str, Any]:
        from src.ingestion.treaties_sources import LICENCE_DECISIONS

        acquired = {p for (p,) in self.conn.execute("SELECT DISTINCT provider FROM treaty_revisions WHERE namespace=?",
                                                    [namespace]).fetchall()} if self.store.ready() else set()
        return {"contract": ANSWER_CONTRACT, "namespace": namespace, "query": dict(query), "status": status, **body,
                "not_acquired": [{"provider": p, "reason": "declined licence decision (written permission required)"
                                  if LICENCE_DECISIONS[p]["status"] == "declined" else "nothing acquired in this "
                                                                                       "namespace"}
                                 for p in PROVIDERS if p not in acquired],
                "coverage": COVERAGE, "notice": NOTICE, "exclusions": list(EXCLUSIONS)}

    # ------------------------------------------------------------------ TR08

    def status_as_of(self, namespace: str, treaty: str, participant: str, as_of: str, *, scopes: Iterable[str],
                     known_as_of: str | None = None) -> dict[str, Any]:
        """The action chain on record at ``as_of`` for a treaty and a participant, per source, with revisions."""
        authorize(namespace, scopes, READ_SCOPE)
        cutoff = as_of_day(as_of)
        if cutoff is None:
            raise TreatiesError("invalid_request", "give the as-of date (YYYY-MM-DD)")
        known = as_of_day(known_as_of)
        treaty_keys, treaty_basis = self.resolve_treaty(namespace, treaty)
        participant_keys, participant_basis = self.resolve_participant(namespace, participant)
        query = {"treaty": treaty, "participant": participant, "as_of": cutoff, "known_as_of": known}
        connected = {"treaty": treaty_basis, "participant": participant_basis}
        if not treaty_keys:
            return self._answer(namespace, query, "no_treaty_on_record", {"sources": [], "connected_by": connected})
        sources = []
        for treaty_key in treaty_keys:
            rows = self._current(namespace, self.store.action_keys(namespace, treaty_keys=[treaty_key],
                                                                   participant_keys=participant_keys), known)
            treaty_view = self._treaty_view(namespace, treaty_key, known)
            removed = [self._view(namespace, r, known) for r in rows if r["change"] == "removed-by-source"]
            live = [r for r in rows if r["change"] != "removed-by-source"]
            dated = sorted((r for r in live if self._when(r)), key=lambda r: (self._when(r), r["record_key"]))
            chain = [r for r in dated if self._when(r) <= cutoff]
            later = [r for r in dated if self._when(r) > cutoff]
            undated = [r for r in live if not self._when(r)]
            pending = [r for r in chain if r["effective_date"] and r["effective_date"] > cutoff]
            exits = [r for r in chain if r["action_type"] == "denunciation" or (
                r["action_type"] == "withdrawal" and not self.store.record(r)["fields"].get("withdraws"))]
            consent = [r for r in chain if r["action_type"] in CONSENT_TYPES]
            in_force = [r for r in chain if r["action_type"] == "entry-into-force"]
            if exits:
                state = self._state(exits, cutoff, "denunciation-or-withdrawal")
            elif consent:
                state = self._state(consent, cutoff, "consent-to-be-bound")
                if state == "consent-to-be-bound-effective-on-record" or (in_force and "not-published" in state):
                    state = "consent-and-entry-into-force-on-record" if in_force else state
            elif any(r["action_type"] in {"signature", "definitive-signature"} for r in chain):
                state = "signature-only-on-record"
            elif chain:
                state = "statements-only-on-record"
            else:
                state = "no-action-on-record" if not undated else "unclear"
            sources.append({
                "treaty": treaty_view, "record_state": state,
                "record_state_basis": "a description of the actions the source records on or before the date "
                                      "(published action and effective dates); not a statement of legal status",
                "chain": [self._view(namespace, r, known) for r in chain],
                "pending": [self._view(namespace, r, known) for r in pending],
                "unclear": [{**self._view(namespace, r, known),
                             "reason": "the source publishes no date for this item; its note is quoted"}
                            for r in undated],
                "later_actions": [{"action_key": r["record_key"], "action_type": r["action_type"],
                                   "date": self._when(r)} for r in later],
                "removed_by_source": removed})
        status = "answered" if participant_keys and any(s["chain"] or s["unclear"] or s["pending"]
                                                        for s in sources) else "no_action_on_record"
        return self._answer(namespace, query, status, {"as_of": cutoff, "sources": sources,
                                                       "participant_keys": participant_keys,
                                                       "connected_by": connected})

    @staticmethod
    def _state(rows: Sequence[Mapping[str, Any]], cutoff: str, label: str) -> str:
        """Describe deposited actions by their published effective dates only (never an inferred date)."""
        if any(r["effective_date"] and r["effective_date"] <= cutoff for r in rows):
            return f"{label}-effective-on-record"
        if any(r["effective_date"] and r["effective_date"] > cutoff for r in rows):
            return f"{label}-deposited-effective-later"
        return f"{label}-on-record-effective-date-not-published"

    # ------------------------------------------------------------------ TR09

    def participant_actions(self, namespace: str, participant: str, *, scopes: Iterable[str],
                            date_from: str | None = None, date_to: str | None = None,
                            action_types: Sequence[str] | None = None, source: str | None = None,
                            known_as_of: str | None = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        types = list(action_types or [])
        if any(t not in ACTION_TYPES for t in types) or (source and source not in PROVIDERS):
            raise TreatiesError("invalid_request", f"action types are {ACTION_TYPES}; sources are {PROVIDERS}")
        start, end, known = as_of_day(date_from), as_of_day(date_to), as_of_day(known_as_of)
        keys, basis = self.resolve_participant(namespace, participant)
        query = {"participant": participant, "date_from": start, "date_to": end, "action_types": types,
                 "source": source, "known_as_of": known}
        rows = [r for r in self._current(namespace, self.store.action_keys(namespace, participant_keys=keys,
                                                                          provider=source), known)
                if r["change"] != "removed-by-source" and (not types or r["action_type"] in types)]
        in_period = sorted((r for r in rows if self._when(r) and (start is None or self._when(r) >= start)
                            and (end is None or self._when(r) <= end)), key=lambda r: (self._when(r), r["record_key"]))
        undated = [r for r in rows if not self._when(r)]
        treaties = {t: self._treaty_view(namespace, t, known) for t in sorted({r["treaty_key"] for r in rows})}
        status = "answered" if in_period or undated else "no_action_on_record"
        return self._answer(namespace, query, status, {
            "participant_keys": keys, "connected_by": basis, "treaties": list(treaties.values()),
            "actions": [self._view(namespace, r, known) for r in in_period],
            "undated": [{**self._view(namespace, r, known),
                         "reason": "no published date; kept outside the period filter"} for r in undated]})

    def treaty_statements(self, namespace: str, treaty: str, *, scopes: Iterable[str],
                          kinds: Sequence[str] | None = None, participant: str | None = None,
                          source: str | None = None, date_from: str | None = None, date_to: str | None = None,
                          known_as_of: str | None = None) -> dict[str, Any]:
        """Reservations, declarations and objections (and withdrawals) verbatim, objections linked as published."""
        authorize(namespace, scopes, READ_SCOPE)
        wanted = list(kinds or STATEMENT_KINDS)
        if any(k not in STATEMENT_KINDS for k in wanted) or (source and source not in PROVIDERS):
            raise TreatiesError("invalid_request", f"statement kinds are {STATEMENT_KINDS}; sources are {PROVIDERS}")
        start, end, known = as_of_day(date_from), as_of_day(date_to), as_of_day(known_as_of)
        treaty_keys, treaty_basis = self.resolve_treaty(namespace, treaty)
        participant_keys = None
        connected = {"treaty": treaty_basis}
        if participant:
            participant_keys, connected["participant"] = self.resolve_participant(namespace, participant)
        query = {"treaty": treaty, "kinds": wanted, "participant": participant, "source": source,
                 "date_from": start, "date_to": end, "known_as_of": known}
        if not treaty_keys:
            return self._answer(namespace, query, "no_treaty_on_record", {"statements": [],
                                                                         "connected_by": connected})
        rows = [r for r in self._current(namespace, self.store.action_keys(
            namespace, treaty_keys=treaty_keys, participant_keys=participant_keys, provider=source), known)
            if r["change"] != "removed-by-source" and r["action_type"] in wanted
            and (self._when(r) is None and start is None and end is None
                 or self._when(r) and (start is None or self._when(r) >= start) and (end is None or self._when(r) <= end))]
        views = {r["record_key"]: self._view(namespace, r, known) for r in rows}
        all_current = {r["record_key"]: r for r in self._current(namespace, self.store.action_keys(
            namespace, treaty_keys=treaty_keys), known)}
        for view in views.values():
            objected = view.get("objected")
            if objected and objected.get("action_key") in all_current:
                target = all_current[objected["action_key"]]
                view["objected"] = {**objected, "objected_statement": {
                    "action_key": target["record_key"], "participant": target["participant_name"],
                    "action_type": target["action_type"], "text_verbatim": target["text"],
                    "citation": cite(target)}}
            elif objected:
                view["objected"] = {**objected, "objected_statement": None,
                                    "note": "the linked anchor is not an acquired statement"}
            view["objections_linked"] = [
                {"action_key": key, "participant": r["participant_name"], "citation": cite(r)}
                for key, r in sorted(all_current.items()) if r["objected_key"] == view["action_key"]
                and r["change"] != "removed-by-source"]
        ordered = sorted(views.values(), key=lambda v: (v["treaty_key"], self._when_view(v) or "9999",
                                                        v["participant"]["name_as_published"], v["action_key"]))
        return self._answer(namespace, query, "answered" if ordered else "no_statement_on_record", {
            "treaties": [self._treaty_view(namespace, t, known) for t in treaty_keys], "statements": ordered,
            "connected_by": connected,
            "statement_notice": "texts are quoted verbatim as published; an objection is linked to a reservation "
                                "only where the source links it; no legal effect is assessed"})

    @staticmethod
    def _when_view(view: Mapping[str, Any]) -> str | None:
        return view["action_date"] or view["deposit_date"] or view["effective_date"]

    # ------------------------------------------------------------------ history and lookup

    def history(self, namespace: str, treaty: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Every revision of the treaty and its actions, with the fields each revision changed."""
        authorize(namespace, scopes, READ_SCOPE)
        treaty_keys, basis = self.resolve_treaty(namespace, treaty)
        chains = []
        for key in treaty_keys + self.store.action_keys(namespace, treaty_keys=treaty_keys):
            revisions = self.store.revisions(namespace, key)
            previous = None
            items = []
            for row in revisions:
                fields = self.store.record(row)["fields"]
                changed = sorted(k for k in set(fields) | set(previous or {})
                                 if (previous or {}).get(k) != fields.get(k)) if previous is not None else []
                items.append({"citation": cite(row), "changed_fields": changed})
                previous = fields
            chains.append({"record_key": key, "revisions": items})
        return self._answer(namespace, {"treaty": treaty}, "answered" if treaty_keys else "no_treaty_on_record",
                            {"records": chains, "connected_by": basis})

    def lookup(self, namespace: str, *, scopes: Iterable[str], identifier: str | None = None,
               source: str | None = None) -> dict[str, Any]:
        authorize(namespace, scopes, READ_SCOPE)
        if identifier:
            keys, basis = self.resolve_treaty(namespace, identifier)
        else:
            keys, basis = self.store.treaty_keys(namespace, provider=source), {"basis": "every acquired treaty"}
        return self._answer(namespace, {"identifier": identifier, "source": source},
                            "answered" if keys else "no_treaty_on_record",
                            {"treaties": [self._treaty_view(namespace, k, None) for k in keys], "connected_by": basis})

    # ------------------------------------------------------------------ evidence bundle

    def export_bundle(self, answer: Mapping[str, Any], *, created_at_ms: int | None = None) -> dict[str, Any]:
        """A noesis-evidence-bundle-v1 citing every item with source, record revision and as-of time."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        builder = EvidenceBundleBuilder("answer", {"operation": "treaties", "query": answer.get("query")},
                                        created_at_ms=created_at_ms if created_at_ms is not None else self.now())
        refs: list[str] = []
        items: list[tuple[str, Mapping[str, Any]]] = []
        for source in answer.get("sources") or []:
            items.append(("treaty", source["treaty"]))
            for group in ("chain", "pending", "unclear", "removed_by_source"):
                items += [(group, item) for item in source.get(group) or []]
        items += [("treaty", t) for t in answer.get("treaties") or [] if t]
        items += [("action", a) for a in answer.get("actions") or []]
        items += [("undated", a) for a in answer.get("undated") or []]
        items += [("statement", s) for s in answer.get("statements") or []]
        statements = []
        for group, item in items:
            citation = item["citation"]
            object_id = f"treaty-evidence:{group}:{citation['revision_id']}"
            payload = {"kind": "treaty" if group == "treaty" else "treaty-action", "group": group,
                       "record_key": citation["record_key"],
                       "source": {"provider": citation["provider"], "source_id": citation["source_id"],
                                  "locator": citation["locator"], "evidence_origin": citation["evidence_origin"]},
                       "record_revision": {"revision_id": citation["revision_id"],
                                           "revision_no": citation["revision_no"], "change": citation["change"],
                                           "depositary_revision": citation["depositary_revision"],
                                           "depositary_date": citation["depositary_date"]},
                       "as_of": {"on_record_from": citation["on_record_from"],
                                 "retrieved_at_ms": citation["retrieved_at_ms"],
                                 "answer_as_of": answer.get("as_of"), "known_as_of": item.get("known_as_of")},
                       "locator": {"cited": True, "document_id": citation["revision_id"], "url": citation["locator"],
                                   "anchor": item.get("text_anchor")}}
            for key in ("title_as_published", "action_type", "action_date", "deposit_date", "effective_date",
                        "text_verbatim"):
                if item.get(key) is not None:
                    payload[key] = item[key]
            if group != "treaty":
                payload["participant"] = item.get("participant")
            builder.add_object("evidence", payload, object_id=object_id)
            refs.append(object_id)
            builder.add_external_reference(f"source:{citation['record_key']}", citation["locator"], required=False)
            if group == "treaty":
                text = f"{item['title_as_published']} ({citation['record_key']}) as published"
            else:
                when = item.get("action_date") or item.get("deposit_date") or item.get("effective_date") or "undated"
                text = (f"{item['participant']['name_as_published']}: {item['action_type']} ({when}) as published "
                        f"[{group}]")
            statements.append({"statement": text, "status": "cited", "evidence_refs": [object_id]})
            if group == "unclear":
                builder.add_omission(f"{citation['record_key']}: no published date (unclear)", object_id=object_id)
            if group == "pending":
                builder.add_omission(f"{citation['record_key']}: effective after the as-of date (pending)",
                                     object_id=object_id)
        for missing in answer.get("not_acquired") or []:
            builder.add_omission(f"{missing['provider']}: not acquired ({missing['reason']})")
            statements.append({"statement": f"{missing['provider']}: not acquired ({missing['reason']})",
                               "status": "not_found", "evidence_refs": []})
        if not statements:
            statements.append({"statement": f"no treaty record on record ({answer.get('status')})",
                               "status": "not_found", "evidence_refs": []})
        root = {k: answer.get(k) for k in ("contract", "query", "status", "as_of", "coverage", "exclusions")}
        builder.add_object("answer", {"kind": "treaties", **root, "statements": statements, "notice": NOTICE},
                           object_id="treaty-answer:" + digest([answer.get("query"), sorted(set(refs))])[:24],
                           references=sorted(set(refs)), root=True)
        return builder.build()


__all__ = ["COVERAGE", "STATEMENT_KINDS", "TreatyQueries"]
