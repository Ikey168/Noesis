"""A treaty's status for a participant as of a date, a participant's actions and a treaty's statements (#2581, TR08,
TR09).

Answers are built only from stored record revisions and cite each one (source,
record revision, the depositary's revision stamp and as-of time):

* :meth:`TreatiesQueries.status_as_of` - the action chain of one participant
  under one treaty at a date: signature, consent to be bound, entry into force,
  denunciation or withdrawal, with the reservations, declarations and
  objections linked to those actions and the depositary's notes that refer to
  the participant, all verbatim. Selection uses the action, deposit and
  effective dates **as published** and nothing else. An action without a
  published date is ``unclear``; a deposited action whose published effective
  date is after the as-of date is ``pending``; an effective date the source does
  not publish is stated as not published, never computed;
* :meth:`~TreatiesQueries.participant_actions` - a participant's actions over a
  period, filtered by action type and source;
* :meth:`~TreatiesQueries.treaty_statements` - a treaty's reservations,
  declarations and objections verbatim, each objection linked to the
  reservation it objects to where the source links it.

A participant is named by its record key, a geospatial place
(``geospatial:place:<id>``, reached through accepted TR06 matches only) or its
name exactly as a source publishes it (each source answered separately, never
merged). Nothing here gives legal advice, infers an obligation or compliance,
or says what a reservation or objection does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.ingestion.treaties_sources import CONSENT_TYPES, EXIT_TYPES
from src.kb.treaties_records import (
    EXCLUSIONS,
    READ_SCOPE,
    TreatiesError,
    TreatiesStore,
    authorize,
)

STATEMENT_KINDS = ("reservation", "declaration", "objection", "communication", "withdrawal-of-reservation")
BOUNDARY = ("Statuses are the published actions and dates as the depositary states them. No legal advice, no "
            "inference of obligations or compliance and no interpretation of the legal effect of reservations.")


def _day(value: Any) -> str | None:
    text = str(value or "")[:10]
    return text if len(text) == 10 else None


def selection_date(fields: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """(date, field) an action is placed in time by: the action date, else the deposit date, else the effective
    date (for an entry-into-force row), exactly as published."""
    for field in ("action_date", "deposit_date", "effective_date"):
        if fields.get(field):
            return fields[field], field
    return None, None


class TreatiesQueries:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.store = TreatiesStore(conn, initialize=False)

    # ------------------------------------------------------------------ resolution

    def resolve_treaty(self, namespace: str, treaty: str, *, scopes: Iterable[str]) -> list[str]:
        """Treaty keys for a key, a UNTC mtdsg_no, a CELEX number or a CETS number (``CETS 999`` or ``999``)."""
        value = str(treaty or "").strip()
        if value.startswith("treaties:"):
            return [value]
        cets = value.upper().replace("CETS", "").replace("NO.", "").strip()
        keys = []
        for row in self.store.records(namespace, scopes=scopes, kinds=["treaty"]):
            ids = row["record"]["fields"].get("identifiers") or {}
            if value in {ids.get("untc_mtdsg"), ids.get("celex")} or (cets and cets == ids.get("cets")):
                keys.append(row["record_key"])
        return keys

    def resolve_participant(self, namespace: str, participant: str, *, scopes: Iterable[str]
                            ) -> list[dict[str, Any]]:
        """Participant keys with how each was reached; names match exactly as published, per source."""
        value = str(participant or "").strip()
        out: list[dict[str, Any]] = []
        if value.startswith("treaties:"):
            out.append({"participant_key": value, "resolved_by": "record key"})
        elif not value.startswith("geospatial:place:"):
            for row in self.store.records(namespace, scopes=scopes, kinds=["participant"], include_removed=True):
                if str(row["record"]["fields"].get("name_as_published") or "").casefold() == value.casefold():
                    out.append({"participant_key": row["record_key"], "resolved_by": "exact published name"})
        from src.kb.treaties_identity import TreatiesIdentity

        start = value if value.startswith(("treaties:", "geospatial:place:")) else None
        if start:
            for reached in TreatiesIdentity(self.conn, initialize=False).equivalents(namespace, start, scopes=scopes):
                if ":participant:" in reached["record_key"]:
                    out.append({"participant_key": reached["record_key"], "resolved_by": "accepted identity match",
                                "path": reached["path"]})
        seen, unique = set(), []
        for item in out:
            if item["participant_key"] not in seen:
                seen.add(item["participant_key"])
                unique.append(item)
        return unique

    def _rows(self, namespace: str, scopes: Iterable[str], *, treaty_key: str | None = None,
              participant_key: str | None = None, kinds: Iterable[str], depositary_as_of: str | None = None,
              providers: Iterable[str] | None = None) -> list[dict[str, Any]]:
        rows = self.store.records(namespace, scopes=scopes, kinds=kinds, treaty_key=treaty_key,
                                  participant_key=participant_key, providers=providers,
                                  include_removed=bool(depositary_as_of))
        if not depositary_as_of:
            return rows
        out = []
        for row in rows:
            revision = self.store.as_of_depositary(namespace, row["record_key"], depositary_as_of, scopes=scopes)
            if revision is None and not row["native_revision"]:
                revision = row  # a source without revision stamps (CELLAR): the current revision, stated below
            if revision is not None and revision["publication_status"] == "published":
                out.append({**row, **{k: revision[k] for k in revision if k not in {"provider", "record_kind",
                                                                                       "treaty_key",
                                                                                       "participant_key"}}})
        return out

    # ------------------------------------------------------------------ TR08

    def status_as_of(self, namespace: str, treaty: str, participant: str, as_of: str, *, scopes: Iterable[str],
                     depositary_as_of: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        day = _day(as_of)
        if not day:
            raise TreatiesError("invalid_request", "as_of is a YYYY-MM-DD date")
        treaty_keys = self.resolve_treaty(namespace, treaty, scopes=scopes)
        participants = self.resolve_participant(namespace, participant, scopes=scopes)
        base = {"namespace": namespace, "query": "status", "treaty": treaty, "participant": participant, "as_of": day,
                "depositary_as_of": depositary_as_of, "exclusions": list(EXCLUSIONS), "boundary": BOUNDARY}
        if not treaty_keys:
            return {**base, "status": "treaty_not_on_record", "answers": []}
        answers = []
        for treaty_key in treaty_keys:
            treaty_rows = self._rows(namespace, scopes, treaty_key=treaty_key, kinds=["treaty"],
                                     depositary_as_of=depositary_as_of)
            if not treaty_rows:
                continue
            treaty_row = treaty_rows[0]
            for item in participants:
                pkey = item["participant_key"]
                if pkey.split(":")[1] != treaty_key.split(":")[1]:
                    continue  # each source answers for its own treaty key
                answers.append(self._chain(namespace, scopes, treaty_row, pkey, item, day, depositary_as_of))
        answers = [a for a in answers if a["status"] != "no_action_on_record"] or answers
        if not answers:
            return {**base, "status": "no_action_on_record", "treaty_keys": treaty_keys, "answers": [],
                    "notice": "no participant of this treaty matches the request in the acquired sources; absence "
                              "from the acquired records is not a statement about the participant"}
        statuses = {a["status"] for a in answers}
        overall = next((s for s in ("unclear", "pending", "actions_on_record") if s in statuses),
                       "no_action_on_record")
        return {**base, "status": overall, "treaty_keys": treaty_keys, "answers": answers}

    def _chain(self, namespace, scopes, treaty_row, pkey, item, day, depositary_as_of) -> dict[str, Any]:
        actions = self._rows(namespace, scopes, treaty_key=treaty_row["record_key"], participant_key=pkey,
                             kinds=["treaty-action"], depositary_as_of=depositary_as_of)
        statements = self._rows(namespace, scopes, treaty_key=treaty_row["record_key"], participant_key=pkey,
                                kinds=["treaty-statement"], depositary_as_of=depositary_as_of)
        notes = [r for r in self._rows(namespace, scopes, treaty_key=treaty_row["record_key"],
                                       kinds=["treaty-statement"], depositary_as_of=depositary_as_of)
                 if r["record"]["fields"].get("statement_kind") == "footnote"
                 and pkey in (r["record"]["fields"].get("refers_to_participants") or [])]
        chain, later, unclear, pending, notes_out = [], [], [], [], []
        for row in actions:
            fields = row["record"]["fields"]
            when, basis = selection_date(fields)
            entry = {"record_key": row["record_key"], "action_type": fields["action_type"],
                     "action_type_as_published": fields.get("action_type_as_published"),
                     "date_text_as_published": fields.get("date_text_as_published"),
                     "action_date": fields.get("action_date"), "deposit_date": fields.get("deposit_date"),
                     "effective_date": fields.get("effective_date"), "date_used": basis,
                     "citation": row["citation"]}
            if when is None:
                unclear.append({**entry, "reason": "no date is published for this action",
                                "source_text": fields.get("date_text_as_published")})
            elif when <= day:
                chain.append(entry)
                effective = fields.get("effective_date")
                if fields["action_type"] in CONSENT_TYPES | EXIT_TYPES and effective and effective > day:
                    pending.append({**entry, "reason": "deposited on or before the as-of date; the published "
                                                       "effective date is later",
                                    "source_text": fields.get("date_text_as_published")})
            else:
                later.append({"record_key": row["record_key"], "action_type": fields["action_type"],
                              "date": when, "date_used": basis})
        chain.sort(key=lambda e: (selection_date(e)[0] or "", e["record_key"]))
        in_chain = {e["record_key"] for e in chain}
        linked = []
        for row in statements:
            fields = row["record"]["fields"]
            made = fields.get("made_on")
            if (made and made > day) or (not made and fields.get("action_key") not in in_chain):
                continue
            linked.append(self._statement(namespace, scopes, row))
        for row in notes:
            fields = row["record"]["fields"]
            notes_out.append({"record_key": row["record_key"], "anchor": fields.get("anchor"),
                              "text_verbatim": fields["text_verbatim"], "made_on_as_published": fields.get("made_on"),
                              "after_as_of": bool(fields.get("made_on") and fields["made_on"] > day),
                              "citation": row["citation"]})
        consent = [e for e in chain if e["action_type"] in CONSENT_TYPES]
        if consent:
            for row in actions:
                fields = row["record"]["fields"]
                if fields["action_type"] == "entry-into-force" and (fields.get("effective_date") or "") > day:
                    pending.append({"record_key": row["record_key"], "action_type": "entry-into-force",
                                    "effective_date": fields["effective_date"], "citation": row["citation"],
                                    "reason": "consent to be bound is on record; the entry into force for the "
                                              "participant as published is later than the as-of date",
                                    "source_text": fields.get("date_text_as_published")})
        exits = [e for e in chain if e["action_type"] in EXIT_TYPES]
        effective_notes = []
        for entry in consent + exits:
            if not entry["effective_date"]:
                effective_notes.append({"record_key": entry["record_key"],
                                        "note": "the source publishes no effective date for this action; none is "
                                                "computed"})
        if not actions and not statements:
            status = "no_action_on_record"
        elif unclear:
            status = "unclear"
        elif pending:
            status = "pending"
        else:
            status = "actions_on_record"
        return {
            "treaty_key": treaty_row["record_key"], "participant_key": pkey, "resolved_by": item["resolved_by"],
            "resolution_path": item.get("path"), "status": status,
            "chain": chain, "statements": linked, "notes_as_published": notes_out,
            "latest_action_on_record": chain[-1] if chain else None,
            "consent_to_be_bound_on_record": bool(consent), "exit_on_record": bool(exits),
            "pending": pending, "unclear": unclear, "effective_dates": effective_notes,
            "later_actions": later,
            "treaty": {"title_as_published": treaty_row["record"]["fields"].get("title_as_published"),
                       "entry_into_force_as_published": treaty_row["record"]["fields"].get("entry_into_force"),
                       "citation": treaty_row["citation"]},
            "depositary_revision_used": treaty_row["citation"]["depositary_revision"]
            or "not stamped by the source (CELLAR); the current record revision is used",
        }

    def _statement(self, namespace, scopes, row) -> dict[str, Any]:
        fields = row["record"]["fields"]
        out = {"record_key": row["record_key"], "statement_kind": fields["statement_kind"],
               "statement_kind_as_published": fields.get("statement_kind_as_published"),
               "participant_as_published": fields.get("participant_as_published"),
               "text_verbatim": fields["text_verbatim"], "anchor": fields.get("anchor"),
               "made_on_as_published": fields.get("made_on"), "action_key": fields.get("action_key"),
               "action_link_basis": fields.get("action_link_basis"),
               "contact_details_withheld": bool(fields.get("contact_details_withheld")),
               "citation": row["citation"]}
        if fields["statement_kind"] == "objection":
            target = fields.get("objects_to_statement_key")
            if target:
                objected = self.store.records(namespace, scopes=scopes, record_keys=[target], include_removed=True)
                out["objects_to"] = ({"record_key": target, "text_verbatim":
                                      objected[0]["record"]["fields"]["text_verbatim"],
                                      "citation": objected[0]["citation"], "link": "linked by the source"}
                                     if objected else {"record_key": target, "link": "linked by the source; the "
                                                       "objected statement is not on record"})
            else:
                out["objects_to"] = {"link": "not linked by the source",
                                     "as_published": fields.get("objects_to_as_published")}
        return out

    # ------------------------------------------------------------------ TR09

    def participant_actions(self, namespace: str, participant: str, *, scopes: Iterable[str],
                            period_start: str | None = None, period_end: str | None = None,
                            action_types: Iterable[str] | None = None, providers: Iterable[str] | None = None,
                            include_statements: bool = True) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        start, end = _day(period_start), _day(period_end)
        types = set(action_types) if action_types else None
        participants = self.resolve_participant(namespace, participant, scopes=scopes)
        base = {"namespace": namespace, "query": "actions", "participant": participant,
                "period": {"start": start, "end": end}, "action_types": sorted(types) if types else None,
                "providers": sorted(providers) if providers else None, "participants": participants,
                "exclusions": list(EXCLUSIONS), "boundary": BOUNDARY}
        actions, undated, statements = [], [], []
        for item in participants:
            for row in self._rows(namespace, scopes, participant_key=item["participant_key"],
                                  kinds=["treaty-action"], providers=providers):
                fields = row["record"]["fields"]
                if types and fields["action_type"] not in types:
                    continue
                when, basis = selection_date(fields)
                entry = {"record_key": row["record_key"], "treaty_key": row["treaty_key"],
                         "participant_key": item["participant_key"], "provider": row["provider"],
                         "action_type": fields["action_type"],
                         "action_type_as_published": fields.get("action_type_as_published"),
                         "date": when, "date_used": basis, "date_text_as_published":
                         fields.get("date_text_as_published"), "effective_date": fields.get("effective_date"),
                         "citation": row["citation"]}
                if when is None:
                    undated.append(entry)
                elif (not start or when >= start) and (not end or when <= end):
                    actions.append(entry)
            if include_statements:
                for row in self._rows(namespace, scopes, participant_key=item["participant_key"],
                                      kinds=["treaty-statement"], providers=providers):
                    made = row["record"]["fields"].get("made_on")
                    if made and ((start and made < start) or (end and made > end)):
                        continue
                    statements.append(self._statement(namespace, scopes, row))
        actions.sort(key=lambda a: (a["date"], a["treaty_key"], a["record_key"]))
        status = "answered" if actions or statements or undated else (
            "participant_not_on_record" if not participants else "none_in_period")
        return {**base, "status": status, "actions": actions, "undated_actions": undated,
                "statements": statements}

    def treaty_statements(self, namespace: str, treaty: str, *, scopes: Iterable[str],
                          kinds: Iterable[str] | None = None, participant: str | None = None,
                          providers: Iterable[str] | None = None, period_start: str | None = None,
                          period_end: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        wanted = set(kinds or ("reservation", "declaration", "objection"))
        if wanted - set(STATEMENT_KINDS):
            raise TreatiesError("invalid_request", f"kinds are among {STATEMENT_KINDS}")
        start, end = _day(period_start), _day(period_end)
        keys = self.resolve_treaty(namespace, treaty, scopes=scopes)
        allowed = None
        if participant:
            allowed = {p["participant_key"] for p in self.resolve_participant(namespace, participant, scopes=scopes)}
        out = []
        for key in keys:
            for row in self._rows(namespace, scopes, treaty_key=key, kinds=["treaty-statement"], providers=providers):
                fields = row["record"]["fields"]
                if fields["statement_kind"] not in wanted or (allowed is not None and row["participant_key"]
                                                              not in allowed):
                    continue
                made = fields.get("made_on")
                if made and ((start and made < start) or (end and made > end)):
                    continue
                out.append(self._statement(namespace, scopes, row))
        return {"namespace": namespace, "query": "statements", "treaty": treaty, "treaty_keys": keys,
                "kinds": sorted(wanted), "status": "answered" if out else ("treaty_not_on_record" if not keys
                                                                           else "none_on_record"),
                "statements": out, "exclusions": list(EXCLUSIONS), "boundary": BOUNDARY}

    # ------------------------------------------------------------------ evidence

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing the record revision behind it (source, record revision, depositary revision and
        as-of time)."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []

        def add(identifier: str, text: str, citation: Mapping[str, Any] | None) -> None:
            if not citation:
                return
            bibliography.setdefault(citation["revision_id"], {
                "id": citation["revision_id"],
                "text": f"{citation['provider']} {citation['record_key']} (source {citation['source_id']}, revision "
                        f"{citation['revision_no']}, depositary revision "
                        f"{citation.get('depositary_revision') or 'not stamped'}, as of {citation['as_of_ms']} ms, "
                        f"{citation['evidence_origin']} evidence), {citation['locator']}"})
            assertions.append({"id": identifier, "text": text, "kind": "sourced",
                               "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                                 "id": citation["record_key"], "revision": citation["revision_id"],
                                                 "locator": {"section": citation["locator"]}}],
                               "citations": [citation["revision_id"]]})

        for item in answer.get("answers") or []:
            add(f"treaty-{item['treaty_key']}", f"{item['treaty']['title_as_published']}",
                item["treaty"]["citation"])
            for entry in item["chain"]:
                add(f"action-{entry['record_key']}", f"{entry['action_type_as_published'] or entry['action_type']}: "
                    f"{entry['date_text_as_published']} ({entry['date_used']})", entry["citation"])
            for statement in item["statements"]:
                add(f"statement-{statement['record_key']}", statement["text_verbatim"], statement["citation"])
            for note in item["notes_as_published"]:
                add(f"note-{note['record_key']}", note["text_verbatim"], note["citation"])
        for entry in answer.get("actions") or []:
            add(f"action-{entry['record_key']}", f"{entry['action_type_as_published'] or entry['action_type']}: "
                f"{entry['date_text_as_published']}", entry["citation"])
        for statement in answer.get("statements") or []:
            add(f"statement-{statement['record_key']}", statement["text_verbatim"], statement["citation"])
        title = f"{answer.get('query')}: {answer.get('treaty') or ''} {answer.get('participant') or ''}".strip()
        return {"sections": [{"id": answer.get("query", "answer"), "title": f"{title} as of {answer.get('as_of')}",
                              "assertions": assertions}],
                "bibliography": list(bibliography.values()), "exclusions": list(EXCLUSIONS)}
