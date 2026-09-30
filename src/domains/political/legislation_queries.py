"""A US or UK bill's stage, text version, sponsors and votes as of a date, cited (#2208, LT09).

Answers read the bill's dossier in the existing
:class:`~src.domains.political.legislative_dossiers.LegislativeDossierStore`
(a sibling of :mod:`src.domains.political.queries`), never a copy:

* **stage** - the latest action (US, per provider) or Parliament stage (UK,
  by its first sitting) dated on or before the day; the record revision used
  is named;
* **text version** - the latest GovInfo text version (US) or Bills API
  publication (UK) issued on or before the day, with its version code or type,
  content hash and locator;
* **sponsors** - the sponsor and the cosponsors whose sponsorship began on or
  before the day and had not been withdrawn by then (withdrawn ones listed
  apart); UK sponsors as the bill record lists them;
* **votes** - every linked roll call or division held on or before the day
  with each member's position as published and the member's accepted identity
  match (never a proposed one); unreviewed divisions are listed as unlinked
  candidates;
* **source disagreements** - congress.gov and BILLSTATUS statements that
  differ (latest action, sponsors, cosponsors, laws) are shown side by side,
  never resolved;
* **lobbying** and **enactment** links when present, degrading to an
  ``unavailable`` note when those providers are absent.

A bill without an acquired record is ``none_on_record``. Answers carry no
passage probability, member score, ideology rating or legal-effect reading,
and an ``evidence_bundle`` whose assertions each cite the committed document
revision, provider and observation time behind them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any

from src.kb.legislation import (
    READ_SCOPE,
    REVIEW_BOUNDARY,
    LegislationDossiers,
    LegislationError,
    authorize,
    table_exists,
)

ANSWER_CONTRACT = "noesis-legislation-answer-v1"
EXCLUSIONS = ("passage prediction", "member scoring or ideology rating", "summaries presented as legal effect")


def _day(value: str | None) -> str:
    if value is None:
        return date.today().isoformat()
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise LegislationError("invalid_time", "as_of is an ISO date (YYYY-MM-DD)") from exc


def _laws_as_of(fields: Mapping[str, Any], day: str) -> list[dict[str, Any]]:
    """Public laws the provider cites, only once an action on or before the day records the enactment.

    The laws list carries no date; the ``BecameLaw`` action (or an action naming the law number) dates it.
    """
    actions = [a for a in fields.get("actions") or [] if a["action_date"] <= day]
    return [law for law in fields.get("laws") or []
            if any(a.get("type") == "BecameLaw" or str(law.get("number")) in a["text"] for a in actions)]


def _cite(stage: Mapping[str, Any], dossier_namespace: str) -> dict[str, Any]:
    citation = stage["citation"]
    detail = stage.get("legislation") or {}
    return {"namespace": dossier_namespace, "document_id": citation["document_id"],
            "revision_id": citation["revision_id"], "source_id": citation["source_id"], "url": citation["url"],
            "record_key": detail.get("record_key"), "provider": detail.get("provider"),
            "native_revision": detail.get("native_revision"), "observed_at_ms": stage["observed_at_ms"],
            "evidence_origin": detail.get("evidence_origin")}


class LegislationQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        from src.kb.legislation_identity import LegislationIdentity

        self.conn = conn
        self.now = now or (lambda: int(time.time() * 1000))
        self.dossiers = LegislationDossiers(conn, now=self.now, initialize=False)
        self.identity = LegislationIdentity(conn, now=self.now, initialize=False)

    # ------------------------------------------------------------------ helpers

    def _stages(self, dossier: Mapping[str, Any], kind: str) -> list[dict[str, Any]]:
        return [s for s in dossier["stages"] if (s.get("legislation") or {}).get("record_kind") == kind]

    def _member(self, namespace: str, person: Mapping[str, Any], scopes: set[str]) -> dict[str, Any]:
        key = person.get("member_key")
        identity = (self.identity.identity(namespace, key, scopes=scopes) if key and
                    table_exists(self.conn, "ownership_identity_candidates") else
                    {"state": "unmatched", "links": [], "candidates": []})
        return {**{k: person.get(k) for k in ("member_key", "scheme", "member_id", "name_as_published", "party",
                                              "state", "district", "constituency", "house", "organisation", "role")
                   if person.get(k) is not None},
                "identity": {"state": identity["state"], "links": identity["links"]}}

    # ------------------------------------------------------------------ answer

    def bill_as_of(self, namespace: str, bill_key: str, dossier_namespace: str, *, principal_id: str,
                   scopes: Iterable[str], as_of: str | None = None, revision: int | None = None,
                   lobbying_namespace: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        day = _day(as_of)
        base = {"contract": ANSWER_CONTRACT, "bill_key": bill_key, "as_of": day, "exclusions": list(EXCLUSIONS),
                "review_boundary": REVIEW_BOUNDARY}
        found = self.dossiers.find(namespace, dossier_namespace, bill_key, principal_id)
        if found is None:
            records = (self.dossiers.store.records_for_bill(namespace, bill_key, scopes=scopes)["linked"]
                       if self.dossiers.store.ready() else [])
            return {**base, "status": "none_on_record" if not records else "dossier_not_built",
                    "message": "no acquired record identifies this bill" if not records
                    else "records exist; build the bill's dossier first"}
        dossier = self.dossiers.dossier(namespace, dossier_namespace, found["dossier_id"], principal_id=principal_id,
                                        scopes=scopes, revision=revision)
        jurisdiction = dossier["jurisdiction"]
        answer = {**base, "status": "answered", "jurisdiction": jurisdiction, "dossier_id": dossier["dossier_id"],
                  "dossier_revision": dossier["revision"], "dossier_created_at_ms": dossier["created_at_ms"],
                  "selection_basis": "source-dated records on or before as_of, from the dossier revision named; "
                                     "each part names the record revision used"}
        if jurisdiction == "US":
            answer.update(self._us(namespace, dossier, dossier_namespace, day, scopes))
        else:
            answer.update(self._uk(namespace, dossier, dossier_namespace, day, scopes))
        answer["lobbying"] = self._lobbying(dossier, dossier_namespace, lobbying_namespace, day, scopes)
        answer["enactment"] = self._enactment(namespace, dossier, dossier_namespace)
        answer["evidence_bundle"] = self._bundle(answer, dossier_namespace)
        return answer

    def _us(self, namespace, dossier, dossier_namespace, day, scopes) -> dict[str, Any]:
        stage_by_source = {}
        for kind in ("us-bill", "us-bill-status"):
            for stage in self._stages(dossier, kind):
                fields = stage["legislation"]["fields"]
                actions = [a for a in fields.get("actions") or [] if a["action_date"] <= day]
                stage_by_source[stage["legislation"]["provider"] + (":billstatus" if kind == "us-bill-status"
                                                                     else "")] = {
                    "latest_action": actions[-1] if actions else None, "actions_on_record": len(actions),
                    "laws": _laws_as_of(fields, day),
                    "used": _cite(stage, dossier_namespace)}
        primary = stage_by_source.get("congress-gov") or next(iter(stage_by_source.values()), None)
        versions = sorted((s for s in self._stages(dossier, "us-text-version")
                           if (s["legislation"]["fields"].get("date_issued") or "9999") <= day),
                          key=lambda s: (s["legislation"]["fields"]["date_issued"], s["stage_id"]))
        text_version = None
        if versions:
            fields = versions[-1]["legislation"]["fields"]
            text_version = {**{k: fields.get(k) for k in ("package_id", "version_code", "version_label", "date_issued",
                                                          "content_sha256", "text_locator")},
                            "category": versions[-1]["stage"], "used": _cite(versions[-1], dossier_namespace),
                            "notice": "the official text is referenced by locator and hash, not summarised"}
        bill = next(iter(self._stages(dossier, "us-bill")), None) or next(iter(self._stages(dossier,
                                                                                           "us-bill-status")), None)
        sponsors, withdrawn = [], []
        if bill is not None:
            fields = bill["legislation"]["fields"]
            if (fields.get("introduced_date") or "9999") <= day:
                sponsors += [self._member(namespace, {**s, "role": "sponsor"}, scopes) for s in fields["sponsors"]]
            for cosponsor in fields.get("cosponsors") or []:
                if cosponsor["sponsorship_date"] > day:
                    continue
                view = {**self._member(namespace, {**cosponsor, "role": "cosponsor"}, scopes),
                        "sponsorship_date": cosponsor["sponsorship_date"],
                        "withdrawn_date": cosponsor.get("withdrawn_date")}
                (withdrawn if cosponsor.get("withdrawn_date") and cosponsor["withdrawn_date"] <= day
                 else sponsors).append(view)
        return {
            "stage": {"latest_action": primary["latest_action"] if primary else None,
                      "category": "enacted (public law cited)" if primary and primary["laws"] else None,
                      "used": primary["used"] if primary else None, "by_source": stage_by_source},
            "text_version": text_version,
            "text_versions_on_record": [{"version_code": v["legislation"]["fields"]["version_code"],
                                         "date_issued": v["legislation"]["fields"]["date_issued"],
                                         "used": _cite(v, dossier_namespace)} for v in versions],
            "sponsors": {"current": sponsors, "withdrawn_cosponsors": withdrawn,
                         "used": _cite(bill, dossier_namespace) if bill else None},
            "votes": self._votes(namespace, dossier, dossier_namespace, "us-roll-call", day, scopes),
            "source_disagreements": self._disagreements(dossier, dossier_namespace, day),
        }

    def _uk(self, namespace, dossier, dossier_namespace, day, scopes) -> dict[str, Any]:
        def first_sitting(stage):
            dates = [s["date"] for s in stage["legislation"]["fields"].get("sittings") or [] if s.get("date")]
            return min(dates) if dates else None

        stages = sorted((s for s in self._stages(dossier, "uk-stage") if (first_sitting(s) or "9999") <= day),
                        key=lambda s: (first_sitting(s), s["legislation"]["fields"].get("sort_order") or 0))
        stage = None
        if stages:
            fields = stages[-1]["legislation"]["fields"]
            stage = {"description": fields["description"], "house": fields["house"], "category": stages[-1]["stage"],
                     "sittings": [s for s in fields["sittings"] if (s["date"] or "9999") <= day],
                     "used": _cite(stages[-1], dossier_namespace)}
        publications = sorted((s for s in self._stages(dossier, "uk-publication")
                               if (s["legislation"]["fields"].get("display_date") or "9999") <= day),
                              key=lambda s: (s["legislation"]["fields"]["display_date"], s["stage_id"]))
        text_version = None
        if publications:
            fields = publications[-1]["legislation"]["fields"]
            text_version = {"title": fields["title"], "publication_type": fields["publication_type"],
                            "display_date": fields["display_date"], "links": fields["links"],
                            "used": _cite(publications[-1], dossier_namespace)}
        bill = next(iter(self._stages(dossier, "uk-bill")), None)
        sponsors = []
        if bill is not None:
            sponsors = [self._member(namespace, s, scopes) for s in bill["legislation"]["fields"]["sponsors"]]
        debates = [{"title": s["legislation"]["fields"]["title"], "date": s["legislation"]["fields"]["date"],
                    "house": s["legislation"]["fields"]["house"], "locator": s["citation"]["url"],
                    "contributions": s["legislation"]["fields"]["contributions"], "link_basis": s.get("link_basis"),
                    "used": _cite(s, dossier_namespace)}
                   for s in self._stages(dossier, "uk-debate-reference")
                   if (s["legislation"]["fields"].get("date") or "9999") <= day]
        return {
            "stage": stage,
            "royal_assent": "published" if any(s["stage"] == "adoption" for s in stages) else "not on record",
            "text_version": text_version,
            "sponsors": {"current": sponsors, "withdrawn_cosponsors": [],
                         "note": "as the bill record lists them; the Bills API states no sponsorship dates",
                         "used": _cite(bill, dossier_namespace) if bill else None},
            "votes": self._votes(namespace, dossier, dossier_namespace, "uk-division", day, scopes),
            "debate_references": debates,
            "source_disagreements": [],
        }

    def _votes(self, namespace, dossier, dossier_namespace, kind, day, scopes) -> dict[str, Any]:
        held = []
        for stage in self._stages(dossier, kind):
            fields = stage["legislation"]["fields"]
            if (fields.get("date") or "9999") > day:
                continue
            held.append({
                "record_key": stage["legislation"]["record_key"],
                **{k: fields.get(k) for k in ("chamber", "house", "congress", "session", "roll_number", "division_id",
                                              "number", "date", "question", "title", "result", "measure")
                   if fields.get(k) is not None},
                "totals_as_published": fields.get("totals_as_published") or fields.get("counts_as_published"),
                "link_basis": stage.get("link_basis"),
                "positions": [{**self._member(namespace, p, scopes), "position": p["position"]}
                              for p in fields.get("positions") or []],
                "used": _cite(stage, dossier_namespace),
            })
        candidates = [{"record_key": c["legislation"]["record_key"], "date": c["legislation"]["fields"].get("date"),
                       "title": c["legislation"]["fields"].get("title"), "state": "unlinked candidate",
                       "used": _cite(c, dossier_namespace)}
                      for c in dossier["review_candidates"]
                      if (c.get("legislation") or {}).get("record_kind") == kind
                      and (c["legislation"]["fields"].get("date") or "9999") <= day]
        held.sort(key=lambda v: (v["date"] or "", v["record_key"]))
        return {"held": held, "unlinked_candidates": candidates,
                "notice": "positions as published; identities shown only where a reviewer accepted a match"}

    def _disagreements(self, dossier, dossier_namespace, day) -> list[dict[str, Any]]:
        congress = next(iter(self._stages(dossier, "us-bill")), None)
        status = next(iter(self._stages(dossier, "us-bill-status")), None)
        if congress is None or status is None:
            return []
        left, right = congress["legislation"]["fields"], status["legislation"]["fields"]

        def latest(fields):
            actions = [a for a in fields.get("actions") or [] if a["action_date"] <= day]
            return {"action_date": actions[-1]["action_date"], "text": actions[-1]["text"]} if actions else None

        def cosponsors(fields):
            return sorted(f"{c['member_id']}:{c.get('withdrawn_date') or 'active'}"
                          for c in fields.get("cosponsors") or [] if c["sponsorship_date"] <= day)

        compared = {
            "latest_action": (latest(left), latest(right)),
            "sponsors": (sorted(s["member_id"] for s in left["sponsors"]),
                         sorted(s["member_id"] for s in right["sponsors"])),
            "cosponsors": (cosponsors(left), cosponsors(right)),
            "laws": (sorted(law["number"] for law in _laws_as_of(left, day)),
                     sorted(law["number"] for law in _laws_as_of(right, day))),
        }
        return [{"field": field, "congress_gov": a, "govinfo_billstatus": b, "resolution": "not resolved",
                 "citations": [_cite(congress, dossier_namespace), _cite(status, dossier_namespace)]}
                for field, (a, b) in compared.items() if a != b]

    def _lobbying(self, dossier, dossier_namespace, lobbying_namespace, day, scopes) -> dict[str, Any]:
        if lobbying_namespace is None:
            return {"status": "not_requested"}
        if not table_exists(self.conn, "lobbying_dossier_links"):
            return {"status": "unavailable", "reason": "the Political lobbying feature's links are not present"}
        from src.kb.lobbying import LobbyingError
        from src.kb.lobbying_links import LobbyingDossierLinks

        try:
            entries = LobbyingDossierLinks(self.conn, initialize=False).dossier_entries(
                lobbying_namespace, dossier_namespace, dossier["dossier_id"], dossier["revision"], scopes=scopes)
        except LobbyingError as exc:
            return {"status": "unavailable", "reason": exc.code}
        # The lobbying links store keeps one link per dossier stage carrying the bill identifier; an answer lists
        # each disclosure revision once, with the stages it was linked through.
        grouped: dict[tuple, dict[str, Any]] = {}
        for entry in entries:
            if (entry["date"] or "9999") > day:
                continue
            key = (entry["register"], entry["native_id"], entry["register_revision_id"], entry["link_kind"])
            item = grouped.setdefault(key, {**entry, "stage_ids": [], "link_ids": []})
            item["stage_ids"].append(entry["stage_id"])
            item["link_ids"].append(entry["link_id"])
        entries = list(grouped.values())
        return {"status": "linked" if entries else "none_on_record", "disclosures": entries,
                "notice": "a disclosure naming a bill is not evidence of influence"}

    def _enactment(self, namespace, dossier, dossier_namespace) -> list[dict[str, Any]]:
        if not table_exists(self.conn, "legislation_enactment_links"):
            return []
        from src.kb.legislation_links import LegislationLinks

        return LegislationLinks(self.conn, initialize=False).enactment_links(
            namespace, dossier_namespace, dossier["dossier_id"], dossier_revision=dossier["revision"])

    @staticmethod
    def _bundle(answer: Mapping[str, Any], dossier_namespace: str) -> dict[str, Any]:
        """Assertions with their dependencies on committed document revisions (evidence-bundle shape)."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []

        def add(identifier: str, text: str, used: Mapping[str, Any] | None) -> None:
            if not used:
                return
            bibliography.setdefault(used["revision_id"], {
                "id": used["revision_id"],
                "text": f"{used['provider']} {used['record_key']} (source {used['source_id']}, revision "
                        f"{used['native_revision'] or 'unstamped'}, observed {used['observed_at_ms']}, "
                        f"{used['evidence_origin']} evidence), {used['url']}"})
            assertions.append({"id": identifier, "text": text, "kind": "sourced",
                               "dependencies": [{"kind": "source", "namespace": dossier_namespace,
                                                 "id": used["document_id"], "revision": used["revision_id"],
                                                 "locator": {"section": used["record_key"]}}],
                               "citations": [used["revision_id"]]})

        stage = answer.get("stage") or {}
        if stage.get("latest_action"):
            action = stage["latest_action"]
            add("stage", f"Latest action on {action['action_date']}: {action['text']}", stage["used"])
        elif stage.get("description"):
            add("stage", f"Latest stage: {stage['description']} ({stage['house']})", stage["used"])
        version = answer.get("text_version") or {}
        if version:
            add("text-version", f"Text version in force as of {answer['as_of']}: "
                f"{version.get('version_code') or version.get('publication_type')} "
                f"({version.get('date_issued') or version.get('display_date')})", version["used"])
        sponsors = answer.get("sponsors") or {}
        if sponsors.get("current"):
            add("sponsors", "Sponsors: " + ", ".join(s.get("name_as_published") or s.get("member_key") or "?"
                                                     for s in sponsors["current"]), sponsors.get("used"))
        for vote in (answer.get("votes") or {}).get("held", []):
            add(f"vote-{vote['record_key']}", f"{vote['record_key']} on {vote['date']}: "
                f"{vote.get('result') or vote.get('title') or 'result as published'}", vote["used"])
        return {"sections": [{"id": "bill-as-of", "title": f"{answer['bill_key']} as of {answer['as_of']}",
                              "assertions": assertions}],
                "bibliography": list(bibliography.values())}

    def sponsor_bills(self, namespace: str, member_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Bills a member sponsored or cosponsored, per record, with the dates the records state."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        members = {m["member_key"]: m for m in self.identity.members(namespace, scopes=scopes)}
        member = members.get(member_key)
        if member is None:
            return {"contract": ANSWER_CONTRACT, "member_key": member_key, "status": "none_on_record", "bills": []}
        bills = [a for a in member["appearances"] if a["role"] in {"sponsor", "cosponsor"}]
        return {"contract": ANSWER_CONTRACT, "member_key": member_key, "status": "answered",
                "names_as_published": member["names"],
                "identity": self.identity.identity(namespace, member_key, scopes=scopes),
                "bills": bills, "exclusions": list(EXCLUSIONS)}
