"""Classification and restriction status as of a date, revision history and substance dossiers (#2212, CH09 #2306).

Answers are assembled from the reviewable substance (the provider records an
accepted identity decision joins, :mod:`src.kb.substances_identity`) and the
immutable revisions of its records:

* **status as of a date** - the harmonised classification revision whose
  date of application is on or before the date (with the ATP that set it),
  the Candidate List, Annex XIV and Annex XVII entries whose latest event on
  or before the date is an inclusion or amendment (a removal ends it), and the
  registration status last updated on or before the date. Revisions known
  only for a later date are listed as scheduled, never as in force;
* **history** - every revision with its dates, event, legal act and
  citation;
* **dossier** - identity (with the matches that joined the records), status,
  history, the regulation text behind each cited act, linked product notices,
  materials and literature, and published data points quoted as the source's
  data, with the sources consulted and whether their evidence is fixture or
  live.

A substance with no entries is reported as having **none on record** in the
acquired sources - never as safe, unregulated or not hazardous. No hazard
verdict, safety advice or exposure assessment is produced, and data points
are never combined or ranked.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from src.kb.substances_records import (
    DOSSIER_CONTRACT,
    NEVER,
    READ_SCOPE,
    STATUS_CONTRACT,
    SubstanceError,
    authorize,
    digest,
)
from src.kb.substances_store import SubstanceStore, table_exists

STATUS_TYPES = ("classification", "candidate_listing", "authorisation", "restriction", "registration")
NONE_ON_RECORD = ("No {what} is on record for this substance as of {day} in the acquired sources. This is not a "
                  "statement that the substance is safe, unregulated or not hazardous.")


def _day(value: Any) -> str:
    if value is None:
        return date.today().isoformat()
    text = value.isoformat() if isinstance(value, date) else str(value)
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise SubstanceError("invalid_request", "as_of is an ISO date (YYYY-MM-DD)") from exc


def _cite(record: Mapping[str, Any], revision: Mapping[str, Any]) -> dict[str, Any]:
    source = revision["statement"]["source"]
    return {"record_id": record["record_id"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "provider": record["provider"],
            "subject_key": record["subject_key"], "url": source["url"], "locator": source["locator"],
            "attribution": source["attribution"], "evidence_origin": revision["evidence_origin"],
            "legal_act": revision["legal_act"], "effective_from": revision["effective_from"],
            "event": revision["event"], "observed_at_ms": revision["observed_at_ms"]}


class SubstanceQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        from src.kb.substances_identity import SubstanceIdentity

        self.conn = conn
        self.store = SubstanceStore(conn, initialize=False, now=now)
        self.identity = SubstanceIdentity(conn, initialize=False, now=now)

    # ------------------------------------------------------------------ selection

    def members(self, namespace: str, *, scopes: Iterable[str], query: str | None = None,
                subject_key: str | None = None) -> tuple[list[str], dict[str, Any] | None]:
        """The member records of one reviewable substance, from a subject key or an unambiguous query."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if subject_key:
            known = {s["subject_key"] for s in self.store.subjects(namespace)}
            if subject_key not in known:
                raise SubstanceError("not_found", "no substance record with that subject key is acquired")
            return self.identity.members(namespace, subject_key), None
        if not query:
            raise SubstanceError("invalid_request", "give a query (name, CAS, EC, InChIKey, DTXSID) or a subject key")
        resolved = self.identity.resolve(namespace, query, scopes=scopes)
        if resolved["status"] == "not_found":
            return [], resolved
        if resolved["status"] == "ambiguous":
            raise SubstanceError("ambiguous", "the query reaches several substances that no accepted identity match "
                                              "joins; choose one subject_key", candidates=[
                {"subject_key": s["members"][0]["subject_key"], "members": [m["subject_key"] for m in s["members"]],
                 "pending_candidates": s["pending_candidates"]} for s in resolved["substances"]])
        return [m["subject_key"] for m in resolved["substances"][0]["members"]], resolved

    def _revisions(self, namespace: str, members: list[str], record_type: str, cutoff_seq: int | None):
        for record in self.store.records(namespace, subject_keys=members, record_type=record_type):
            yield record, self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff_seq)

    # ------------------------------------------------------------------ status

    def status_as_of(self, namespace: str, *, scopes: Iterable[str], as_of: Any = None, query: str | None = None,
                     subject_key: str | None = None, cutoff_seq: int | None = None,
                     members: list[str] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        day = _day(as_of)
        resolved = None
        if members is None:
            members, resolved = self.members(namespace, scopes=scopes, query=query, subject_key=subject_key)
        else:
            authorize(namespace, scopes, READ_SCOPE)
        result: dict[str, Any] = {"contract": STATUS_CONTRACT, "namespace": namespace, "as_of": day,
                                  "members": members, "known_up_to_seq": cutoff_seq,
                                  "harmonised_classification": [], "notified_classifications": [],
                                  "candidate_list": [], "authorisation": [], "restriction": [], "registration": [],
                                  "scheduled": []}
        if not members:
            result["statement"] = NONE_ON_RECORD.format(what="substance record", day=day)
            result["resolution"] = resolved
            return result
        for record_type in STATUS_TYPES:
            for record, revisions in self._revisions(namespace, members, record_type, cutoff_seq):
                dated = [r for r in revisions if r["effective_from"] is not None]
                applied = [r for r in dated if r["effective_from"] <= day]
                for later in (r for r in dated if r["effective_from"] > day):
                    result["scheduled"].append({"record_type": record_type, "record_key": record["record_key"],
                                                "applies_from": later["effective_from"],
                                                "as_published": later["statement"]["as_published"],
                                                "citation": _cite(record, later),
                                                "note": "published, but not applicable on the as-of date"})
                if not applied:
                    continue
                current = max(applied, key=lambda r: (r["effective_from"], r["seq"]))
                entry = {"record_key": record["record_key"], "as_published": current["statement"]["as_published"],
                         "event": current["event"], "since": current["effective_from"],
                         "citation": _cite(record, current),
                         "first_dated": min(applied, key=lambda r: (r["effective_from"], r["seq"]))["effective_from"],
                         "revisions_to_date": len(applied)}
                if current["event"] == "removal":
                    entry["state"] = "removed"
                elif record_type == "classification" and current["statement"]["as_published"]["kind"] == "notified":
                    entry["state"] = "notified (quoted as published)"
                else:
                    entry["state"] = "in force" if record_type in {"classification", "restriction"} else \
                        "listed" if record_type in {"candidate_listing", "authorisation"} else "as published"
                if record_type == "authorisation":
                    published = current["statement"]["as_published"]
                    entry["dates_as_published"] = {
                        "latest_application_date": published.get("latest_application_date"),
                        "sunset_date": published.get("sunset_date"),
                        "sunset_date_on_or_before_as_of": bool(published.get("sunset_date")
                                                               and published["sunset_date"] <= day)}
                key = {"classification": "harmonised_classification", "candidate_listing": "candidate_list",
                       "authorisation": "authorisation", "restriction": "restriction",
                       "registration": "registration"}[record_type]
                if record_type == "classification" and current["statement"]["as_published"]["kind"] == "notified":
                    key = "notified_classifications"
                result[key].append(entry)
        for key, what in (("harmonised_classification", "harmonised classification"),
                          ("candidate_list", "SVHC Candidate List entry"),
                          ("authorisation", "Annex XIV authorisation entry"),
                          ("restriction", "Annex XVII restriction entry"), ("registration", "registration status")):
            active = [e for e in result[key] if e["state"] != "removed"]
            if not active:
                result.setdefault("none_on_record", {})[key] = NONE_ON_RECORD.format(what=what, day=day)
        if not any(result[k] for k in ("harmonised_classification", "notified_classifications", "candidate_list",
                                       "authorisation", "restriction", "registration")):
            result["statement"] = NONE_ON_RECORD.format(what="classification, list entry or registration", day=day)
        result["boundary"] = list(NEVER)
        return result

    # ------------------------------------------------------------------ history

    def history(self, namespace: str, *, scopes: Iterable[str], query: str | None = None,
                subject_key: str | None = None, members: list[str] | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        if members is None:
            members, _ = self.members(namespace, scopes=scopes, query=query, subject_key=subject_key)
        else:
            authorize(namespace, scopes, READ_SCOPE)
        events = []
        for record_type in STATUS_TYPES:
            for record, revisions in self._revisions(namespace, members, record_type, None):
                for revision in revisions:
                    events.append({"record_type": record_type, "record_key": record["record_key"],
                                   "effective_from": revision["effective_from"], "event": revision["event"],
                                   "as_published": revision["statement"]["as_published"],
                                   "citation": _cite(record, revision)})
        events.sort(key=lambda e: (e["effective_from"] or "9999", e["record_type"], e["citation"]["revision_id"]))
        return {"namespace": namespace, "members": members, "revisions": events, "count": len(events),
                "note": "every revision is kept; a later one never overwrites an earlier one"}

    # ------------------------------------------------------------------ dossier

    def _notices(self, namespace: str, links: list[dict[str, Any]], scopes: set[str], day: str) -> list[dict]:
        notices: dict[str, dict[str, Any]] = {}
        store = None
        if table_exists(self.conn, "product_safety_revisions"):
            from src.kb.product_safety import ProductSafetyError, ProductSafetyStore

            store = ProductSafetyStore(self.conn, initialize=False)
        for link in links:
            item = notices.setdefault(link["target_id"], {"notice_id": link["target_id"],
                                                          "namespace": link["target_namespace"], "citations": []})
            item["citations"].append({"link_id": link["link_id"], "subject_key": link["subject_key"],
                                      "basis": link["basis"], "matched": link["matched"],
                                      "citing_text": link["citing_text"], "notice_revision": link["target_revision"],
                                      "locator": link["locator"]})
        for item in notices.values():
            if store is None:
                continue
            try:
                head = store.inspect(item["namespace"], item["notice_id"], scopes=scopes, as_of=day)
            except ProductSafetyError as exc:
                item["notice"] = {"status": getattr(exc, "code", "unavailable")}
                continue
            item["notice"] = {"provider": head["provider"], "notice_number": head["notice_number"],
                              "jurisdiction": head["jurisdiction"], "status_as_of": head["status"],
                              "revision_id": (head["revision"] or {}).get("revision_id")}
        return sorted(notices.values(), key=lambda n: n["notice_id"])

    def dossier(self, namespace: str, *, scopes: Iterable[str], as_of: Any = None, query: str | None = None,
                subject_key: str | None = None) -> dict[str, Any]:
        """A cited dossier for one substance as of a date; identity, status, history, links and data points."""
        from src.kb.substances_links import LEGAL_READ, PRODUCTS_READ, SubstanceLinks

        scopes = set(scopes)
        day = _day(as_of)
        members, resolved = self.members(namespace, scopes=scopes, query=query, subject_key=subject_key)
        if not members:
            return {"contract": DOSSIER_CONTRACT, "namespace": namespace, "as_of": day, "query": query,
                    "status": "not_found", "resolution": resolved,
                    "statement": NONE_ON_RECORD.format(what="substance record", day=day)}
        identity = self.identity.substance(namespace, members[0], scopes=scopes)
        status = self.status_as_of(namespace, scopes=scopes, as_of=day, members=members)
        history = self.history(namespace, scopes=scopes, members=members)
        linker = SubstanceLinks(self.conn, initialize=False)
        all_links = linker.links(namespace, members, scopes=scopes)
        legal_links = [x for x in all_links if x["owner"] == "legal"]
        regulations = []
        for link in legal_links:
            text = (linker.regulation_text(namespace, link, scopes=scopes)
                    if LEGAL_READ in scopes or "operator" in scopes else {"status": "not_authorized", "passages": []})
            regulations.append({"link_id": link["link_id"], "work_id": link["target_id"], "cited_as": link["matched"],
                                "citing_text": link["citing_text"], "entry": link["locator"].get("entry"),
                                "for_revision": link["source_revision_id"], "text": text})
        notices = (self._notices(namespace, [x for x in all_links if x["owner"] == "products"], scopes, day)
                   if PRODUCTS_READ in scopes or "operator" in scopes else [])
        points = []
        for record, revisions in self._revisions(namespace, members, "data_point", None):
            for revision in revisions:
                statement = revision["statement"]
                points.append({"record_key": record["record_key"], "as_published": statement["as_published"],
                               "label": statement["label"], "data_version": statement["source"].get("data_version"),
                               "citation": _cite(record, revision)})
        providers = sorted({r["provider"] for r in self.store.records(namespace, subject_keys=members)})
        runs = [r for r in self.store.runs(namespace) if r["provider"] in providers]
        body = {
            "contract": DOSSIER_CONTRACT, "namespace": namespace, "as_of": day, "query": query, "status": "assembled",
            "identity": identity, "status_as_of": status, "history": history["revisions"],
            "regulations": regulations,
            "linked_notices": notices,
            "linked_materials": [x for x in all_links if x["owner"] == "materials"],
            "linked_literature": [x for x in all_links if x["owner"] == "clinical"],
            "data_points": points,
            "data_points_notice": "published values quoted per source; never combined, ranked or interpreted",
            "sources_consulted": [{"source_id": r["source_id"], "provider": r["provider"], "run_id": r["run_id"],
                                   "status": r["status"], "evidence_origin": r["evidence_origin"]} for r in runs],
            "boundary": list(NEVER),
        }
        body["dossier_hash"] = digest({k: v for k, v in body.items() if k != "sources_consulted"})
        return body


__all__ = ["NONE_ON_RECORD", "SubstanceQueries"]
