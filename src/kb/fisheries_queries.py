"""Vessel status as of a date, and effort and catch aggregates with revisions (#2222, FI09 #2329, FI10 #2332).

Answers are assembled from immutable ``noesis-fisheries-record-v1``
revisions, the reviewable vessel identity of
:mod:`src.kb.fisheries_identity` and its citation links:

* **vessel status** - for a vessel identifier and a date: every
  authorisation (register, register number, validity period as published,
  whether a later snapshot removed it) and every IUU listing and delisting,
  each with its snapshot revision, snapshot date, retrieval time and the
  identity match that connected it; the identity history up to the date;
  sanctions designations that state the vessel's IMO; and the coverage (which
  registers and lists, at which snapshot dates, were consulted). A vessel with
  nothing on record is reported as having **none on record in the covered
  registers** - never as legal, compliant or authorised elsewhere;
* **aggregates** - for an area, flag state or species and a period: GFW
  effort aggregates and FishStat catch observations *side by side*, never
  combined into a derived indicator, each with its release or dataset version,
  status flags, unit and method note, the values of earlier releases, and cells
  of the bounded selection that are **not published** (never zero).

Both answers export as ``noesis-evidence-bundle-v1`` bundles. No answer infers
illegal fishing or recommends enforcement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from typing import Any

from src.kb.fisheries_records import (
    AGGREGATE_CONTRACT,
    NEVER,
    READ_SCOPE,
    STATUS_CONTRACT,
    FisheriesError,
    authorize,
    digest,
    flag_code,
)
from src.kb.fisheries_store import FisheriesStore

NONE_ON_RECORD = ("No authorisation or IUU listing is on record for this vessel as of {day} in the covered registers "
                  "and lists ({lists}). This is not a statement that the vessel is legal, compliant or authorised "
                  "elsewhere, nor that it is not.")
NOT_PUBLISHED = "not published in the consulted release for this cell; a missing value is not zero"
LIST_KINDS = ("authorised-vessels", "iuu-vessels")


def _day(value: Any) -> str:
    if value is None:
        return date.today().isoformat()
    text = value.isoformat() if isinstance(value, date) else str(value)
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise FisheriesError("invalid_request", "dates are ISO dates (YYYY-MM-DD)") from exc


def _cite(record: Mapping[str, Any], revision: Mapping[str, Any]) -> dict[str, Any]:
    source = revision["statement"]["source"]
    return {"record_id": record["record_id"], "revision_id": revision["revision_id"],
            "revision_no": revision["revision_no"], "provider": record["provider"],
            "subject_key": record["subject_key"], "url": source["url"], "locator": source["locator"],
            "attribution": source["attribution"], "snapshot_date": revision["snapshot_date"],
            "release": source.get("release"), "dataset_version": source.get("dataset_version"),
            "retrieved_at_ms": revision["observed_at_ms"], "evidence_origin": revision["evidence_origin"],
            "event": revision["event"], "effective_from": revision["effective_from"]}


def _flag_matches(published: Any, wanted: str | None) -> bool:
    if not wanted:
        return True
    return str(published or "").upper() == wanted.upper() or (
        flag_code(published) is not None and flag_code(published) == flag_code(wanted))


class FisheriesQueries:
    def __init__(self, conn: Any, *, now=None) -> None:
        from src.kb.fisheries_identity import FisheriesIdentity, FisheriesLinks

        self.conn = conn
        self.store = FisheriesStore(conn, initialize=False, now=now)
        self.identity = FisheriesIdentity(conn, initialize=False, now=now)
        self.links = FisheriesLinks(conn, initialize=False, now=now)

    # ------------------------------------------------------------------ vessel

    def _vessel_members(self, namespace: str, query: str | None, subject_key: str | None):
        if subject_key:
            return self.identity.members(namespace, subject_key), {"interpreted_as": "subject_key",
                                                                   "entry_records": [subject_key]}
        if not query:
            raise FisheriesError("invalid_request", "give a vessel identifier (IMO, register number, GFW vessel id, "
                                                    "list entry or call sign) or a subject key")
        interpreted, found = self.identity.find(namespace, query)
        if not found:
            return [], {"interpreted_as": interpreted, "entry_records": []}
        clusters = self.identity.clusters(namespace)
        roots = sorted({clusters.get(k, k) for k in found})
        if len(roots) > 1:
            raise FisheriesError("ambiguous", "the identifier reaches records that no accepted identity match joins; "
                                              "choose one subject_key",
                                 candidates=[{"subject_key": r, "members": self.identity.members(namespace, r)}
                                             for r in roots])
        return self.identity.members(namespace, roots[0]), {"interpreted_as": interpreted, "entry_records": found}

    def _connecting(self, namespace: str, subject: str, entry: list[str], scopes: set[str]) -> list[dict[str, Any]]:
        if subject in entry:
            return []
        return [{"match_id": m["match_id"], "records": [m["left_key"], m["right_key"]], "basis": m["basis"],
                 "evidence": m["evidence"], "decision_id": m["decision_id"], "reviewer": m["reviewer"]}
                for m in self.identity.matches(namespace, scopes=scopes, subject_key=subject)
                if m["state"] == "accepted"]

    def _coverage(self, namespace: str, day: str) -> list[dict[str, Any]]:
        lists: dict[str, dict[str, Any]] = {}
        for snap in self.store.snapshots(namespace):
            if snap["list_kind"] not in LIST_KINDS:
                continue
            item = lists.setdefault(snap["list_key"], {"list_key": snap["list_key"], "provider": snap["provider"],
                                                       "list_kind": snap["list_kind"], "snapshots_consulted": [],
                                                       "later_snapshots": []})
            bucket = "snapshots_consulted" if (snap["snapshot_date"] or "0000") <= day else "later_snapshots"
            item[bucket].append({"snapshot_date": snap["snapshot_date"], "retrieved_at_ms": snap["retrieved_at_ms"],
                                 "entries": snap["entry_count"], "evidence_origin": snap["evidence_origin"],
                                 "url": snap["url"]})
        return [lists[k] for k in sorted(lists)]

    def vessel_status_as_of(self, namespace: str, *, scopes: Iterable[str], as_of: Any = None,
                            query: str | None = None, subject_key: str | None = None,
                            cutoff_seq: int | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        day = _day(as_of)
        members, resolution = self._vessel_members(namespace, query, subject_key)
        coverage = self._coverage(namespace, day)
        body: dict[str, Any] = {
            "contract": STATUS_CONTRACT, "namespace": namespace, "as_of": day, "query": query,
            "members": members, "resolution": resolution, "authorisations": [], "listings": [], "later_events": [],
            "identity_history": None, "sanctions": [], "coverage": coverage, "boundary": list(NEVER)}
        lists_text = ", ".join(c["list_key"] for c in coverage) or "none acquired"
        if not members:
            body["statement"] = NONE_ON_RECORD.format(day=day, lists=lists_text)
            body["answer_hash"] = digest({k: v for k, v in body.items()})
            return body
        entry = resolution["entry_records"]
        for record in self.store.records(namespace, subject_keys=members):
            if record["record_type"] not in {"authorisation", "listing"}:
                continue
            revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff_seq)
            known = [r for r in revisions if r["event"] != "removed" or (r["effective_from"] or "") <= day]
            for later in (r for r in revisions if r["event"] == "removed" and (r["effective_from"] or "") > day):
                body["later_events"].append({"record_key": record["record_key"], "event": "removed",
                                             "date": later["effective_from"], "citation": _cite(record, later)})
            if not known:
                continue
            current = known[-1]
            published = current["statement"]["as_published"]
            item = {"record_key": record["record_key"], "provider": record["provider"],
                    "subject_key": record["subject_key"], "as_published": published,
                    "citation": _cite(record, current),
                    "connected_by": self._connecting(namespace, record["subject_key"], entry, scopes),
                    "revisions_to_date": len(known)}
            if record["record_type"] == "authorisation":
                start, end = published.get("valid_from"), published.get("valid_to")
                if current["event"] == "removed":
                    item["state"] = f"removed from the register (absent from the snapshot of {current['effective_from']})"
                elif (start or "0000") <= day and (end is None or day <= end):
                    item["state"] = "within the published authorisation period"
                elif start and start > day:
                    item["state"] = "published authorisation period starts after the date"
                else:
                    item["state"] = "published authorisation period ended before the date"
                body["authorisations"].append(item)
                continue
            listed, delisted = published.get("listed_on"), published.get("delisted_on")
            if current["event"] == "removed":
                item["state"] = f"no longer in the list snapshot of {current['effective_from']}"
            elif delisted and delisted <= day:
                item["state"] = f"delisted on {delisted} (as published)"
            elif listed and listed <= day:
                item["state"] = "listed on the date (as published)"
            else:
                item["state"] = "not yet listed on the date"
                body["later_events"].append({"record_key": record["record_key"], "event": "listed", "date": listed,
                                             "citation": item["citation"]})
            if delisted and delisted > day and listed and listed <= day:
                body["later_events"].append({"record_key": record["record_key"], "event": "delisted",
                                             "date": delisted, "citation": item["citation"]})
            item["listing_history"] = {"listed_on": listed, "delisted_on": delisted,
                                       "stated_reason": published.get("stated_reason")}
            if record["provider"] == "combined-iuu":
                item["originating_listings"] = published.get("originating_listings")
                item["independent_confirmation"] = False
            body["listings"].append(item)
        body["identity_history"] = self.identity.identity_history(namespace, members, as_of=day,
                                                                  cutoff_seq=cutoff_seq)
        body["sanctions"] = [{"link_id": x["link_id"], "subject_key": x["subject_key"], "designation_id":
                              x["target_id"], "designation_revision": x["target_revision"], "matched": x["matched"],
                              "citing_text": x["citing_text"], "list_id": x["locator"].get("list_id")}
                             for x in self.links.links(namespace, members, scopes=scopes, owner="sanctions")]
        body["pending_candidates"] = [{"match_id": m["match_id"], "records": [m["left_key"], m["right_key"]],
                                       "basis": m["basis"]}
                                      for m in self.identity.matches(namespace, scopes=scopes, state="proposed")
                                      if m["left_key"] in members or m["right_key"] in members]
        if not body["authorisations"] and not body["listings"]:
            body["statement"] = NONE_ON_RECORD.format(day=day, lists=lists_text)
        body["answer_hash"] = digest(body)
        return body

    # ------------------------------------------------------------------ aggregates

    def _series(self, namespace: str, record: Mapping[str, Any], cutoff_seq: int | None) -> dict[str, Any]:
        revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff_seq)
        current = revisions[-1]
        published = current["statement"]["as_published"]
        value_key = "quantity" if record["record_type"] == "catch_observation" else "value"
        history = [{"release": r["statement"]["source"].get("release")
                    or r["statement"]["source"].get("dataset_version"),
                    "value": r["statement"]["as_published"].get(value_key),
                    "status_flags": r["statement"]["as_published"].get("status_flags"),
                    "revision_id": r["revision_id"]} for r in revisions]
        changes = [{"from_release": a["release"], "to_release": b["release"], "from_value": a["value"],
                    "to_value": b["value"], "from_status_flags": a["status_flags"],
                    "to_status_flags": b["status_flags"]}
                   for a, b in zip(history, history[1:]) if (a["value"], a["status_flags"]) != (b["value"],
                                                                                              b["status_flags"])]
        return {"record_key": record["record_key"], "as_published": published, "citation": _cite(record, current),
                "releases": history, "changes_between_releases": changes}

    def aggregates(self, namespace: str, *, scopes: Iterable[str], area: str | None = None, flag: str | None = None,
                   species: str | None = None, period_from: Any = None, period_to: Any = None,
                   cutoff_seq: int | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        self.store.require_ready()
        if not (area or flag or species):
            raise FisheriesError("invalid_request", "give an area, a flag state or a species")
        start, end = _day(period_from or "1900-01-01"), _day(period_to)
        effort, catch = [], []
        for record in self.store.records(namespace):
            if record["record_type"] not in {"effort_aggregate", "catch_observation"}:
                continue
            published = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff_seq)
            if not published:
                continue
            value = published[-1]["statement"]["as_published"]
            if area and str(value["area"]["code"]).casefold() != str(area).casefold():
                continue
            if not _flag_matches(value.get("flag"), flag):
                continue
            if species and (record["record_type"] != "catch_observation" or value["species"] != species.upper()):
                continue
            period = value["period"]
            if period["to"] < start or period["from"] > end:
                continue
            (catch if record["record_type"] == "catch_observation" else effort).append(
                self._series(namespace, record, cutoff_seq))
        not_published = self._unpublished(namespace, catch, area=area, flag=flag, species=species, start=start,
                                           end=end, cutoff_seq=cutoff_seq)
        releases = [{"list_key": s["list_key"], "release": s["release"], "retrieved_at_ms": s["retrieved_at_ms"],
                     "evidence_origin": s["evidence_origin"]}
                    for s in self.store.snapshots(namespace) if s["list_kind"] in {"capture-production",
                                                                                  "fishing-effort"}]
        body = {"contract": AGGREGATE_CONTRACT, "namespace": namespace,
                "request": {"area": area, "flag": flag, "species": species, "period_from": start, "period_to": end},
                "effort": sorted(effort, key=lambda e: e["record_key"]),
                "catch": sorted(catch, key=lambda c: c["record_key"]), "not_published": not_published,
                "releases_consulted": releases,
                "presentation": "effort and catch are reported side by side from different publishers and methods; "
                                "they are never combined into a derived indicator",
                "effort_notice": "GFW apparent fishing effort is a model estimate from AIS, not confirmed fishing",
                "boundary": list(NEVER)}
        body["answer_hash"] = digest(body)
        return body

    def _unpublished(self, namespace, catch, *, area, flag, species, start, end, cutoff_seq) -> list[dict]:
        """Cells of the bounded FishStat selection (areas, species, flags seen for the area) with no published value."""
        seen = []
        for record in self.store.records(namespace, record_type="catch_observation"):
            revisions = self.store.revisions(namespace, record["record_id"], cutoff_seq=cutoff_seq)
            if revisions:
                seen.append(revisions[-1]["statement"]["as_published"])
        if area:
            seen = [v for v in seen if str(v["area"]["code"]).casefold() == str(area).casefold()]
        if not seen:
            return []
        years = sorted({int(v["period"]["from"][:4]) for v in seen})
        latest_release = max(v.get("release") or "" for v in seen) or None
        cells = {(v["area"]["code"], v["flag"], v["species"]) for v in seen
                 if _flag_matches(v["flag"], flag) and (not species or v["species"] == species.upper())}
        have = {(c["as_published"]["area"]["code"], c["as_published"]["flag"], c["as_published"]["species"],
                 int(c["as_published"]["period"]["from"][:4])) for c in catch}
        missing = []
        for code, flag_value, species_value in sorted(cells):
            for year in years:
                if not (start[:4] <= str(year) <= end[:4]) or (code, flag_value, species_value, year) in have:
                    continue
                missing.append({"area": code, "flag": flag_value, "species": species_value, "year": year,
                                "release": latest_release, "status": NOT_PUBLISHED})
        return missing

    # ------------------------------------------------------------------ evidence bundles

    def export_bundle(self, namespace: str, answer: Mapping[str, Any], *, scopes: Iterable[str]) -> dict[str, Any]:
        """An evidence bundle (noesis-evidence-bundle-v1) citing every record revision the answer used."""
        from src.evidence_bundle.builder import EvidenceBundleBuilder

        authorize(namespace, set(scopes), READ_SCOPE)
        kind = "vessel-status" if answer.get("contract") == STATUS_CONTRACT else "aggregates"
        builder = EvidenceBundleBuilder("receipt", {"operation": f"fisheries-{kind}", "namespace": namespace,
                                                    "answer_hash": answer.get("answer_hash")}, created_at_ms=0)
        refs = []
        items = (answer.get("authorisations", []) + answer.get("listings", []) if kind == "vessel-status"
                 else answer.get("effort", []) + answer.get("catch", []))
        for item in items:
            citation = item["citation"]
            revision = self.store.revision(namespace, citation["revision_id"])
            object_id = citation["revision_id"]
            builder.add_object("evidence", {"kind": "fisheries-record", "record_id": citation["record_id"],
                                            "revision_id": citation["revision_id"], "statement": revision["statement"],
                                            "locator": {"cited": bool(revision["document_id"]),
                                                        "document_id": revision["document_id"],
                                                        "url": citation["url"],
                                                        "locator": citation["locator"],
                                                        "snapshot_date": citation["snapshot_date"],
                                                        "release": citation["release"]},
                                            "acquisition": {"retrieved_at_ms": citation["retrieved_at_ms"],
                                                            "evidence_origin": citation["evidence_origin"]}},
                               object_id=object_id)
            refs.append(object_id)
            builder.add_external_reference(f"source:{citation['record_id']}", citation["url"], required=False)
        for link in answer.get("sanctions", []):
            object_id = link["link_id"]
            builder.add_object("evidence", {"kind": "sanctions-citation", **link,
                                            "locator": {"cited": False, "designation_id": link["designation_id"],
                                                        "revision_id": link["designation_revision"]}},
                               object_id=object_id)
            refs.append(object_id)
        builder.add_object("receipt", {"kind": f"fisheries-{kind}", **dict(answer)},
                           object_id=f"fisheries-{kind}:{answer.get('answer_hash')}", references=refs, root=True)
        if answer.get("statement"):
            builder.add_omission(answer["statement"])
        for cell in answer.get("not_published", []):
            builder.add_omission(f"{cell['area']}/{cell['flag']}/{cell['species']}/{cell['year']}: {cell['status']}")
        for candidate in answer.get("pending_candidates", []):
            builder.add_omission(f"unreviewed identity candidate {candidate['match_id']} ({candidate['basis']}) not "
                                 "used")
        return {"bundle": builder.build(), "boundary": list(NEVER)}


__all__ = ["FisheriesQueries", "NONE_ON_RECORD", "NOT_PUBLISHED"]
