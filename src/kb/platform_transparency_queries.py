"""Platform transparency answers: political ads by advertiser or election, and moderation statements by platform,
ground and period (#2580, SP09, SP10).

* :meth:`PlatformTransparencyQueries.ads_for_advertiser` - the ads of a
  platform advertiser or declared funding entity, or of every advertiser
  accepted (SP07) as the same as another owner's record (a campaign-finance
  committee, a lobbying registrant, a legal entity), with delivery dates and
  spend and impression ranges **exactly as published**. Ranges are never
  converted to midpoints, point estimates or sums; an ad the source no longer
  returns is shown with its ``not-returned`` revision. ``as_of`` answers with
  the revision on record at that time.
* :meth:`PlatformTransparencyQueries.ads_for_election` - ads linked (SP08) to
  an elections record, grouped by advertiser.
* :meth:`PlatformTransparencyQueries.moderation_statements` - counts of stored
  statements of reasons for a platform and period by decision type, ground and
  category, with the automated-detection and automated-decision flags as
  published. Counts are over *stored* records only; the answer states the
  stored window, the days without a dump on record and the dump versions
  used, and cites them.

Every item cites its source, record revision and as-of time (observation time
and, where published, the source's data refresh time). No user-level
profiling, no inference of coordinated behaviour, no point estimates.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from src.ingestion.platform_transparency_sources import advertiser_key, slug
from src.kb.platform_transparency_identity import (
    PlatformTransparencyIdentity,
    funding_key,
)
from src.kb.platform_transparency_records import (
    EXCLUSIONS,
    NOTICES_SCOPE,
    READ_SCOPE,
    PlatformTransparencyStore,
    authorize,
    cite,
    observed_at,
)

ANSWER_CONTRACT = "noesis-platform-transparency-answer-v1"
RANGES_NOTE = ("spend and impression ranges are shown exactly as the platform published them; they are never "
               "converted to midpoints, point estimates or sums")
NO_RECORD_NOTE = ("no ad on record for this subject in the acquired coverage; this is not evidence that none ran")
COUNT_NOTE = ("counts are over the statements stored from the dumps listed; they are not the platform's totals and "
              "say nothing about days without a dump on record")
DECISION_COLUMNS = ("decision_visibility", "decision_monetary", "decision_provision", "decision_account")


def _days(start: str, end: str) -> list[str]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]


def _values(value: Any) -> list[str]:
    if value in (None, "", []):
        return []
    return [str(v) for v in value] if isinstance(value, list) else [str(value)]


class PlatformTransparencyQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.store = PlatformTransparencyStore(conn, initialize=False, now=now)
        self.identity = PlatformTransparencyIdentity(conn, now=now, initialize=False)

    # ------------------------------------------------------------------ SP09 political ads

    def resolve_subjects(self, namespace: str, subject: str, *, scopes: set[str]) -> list[dict[str, Any]]:
        """Platform subjects for a key, a Google advertiser id, a Meta page id, a declared funding entity name or
        another owner's record (accepted identity only)."""
        text = str(subject or "").strip()
        if text.startswith("platform-transparency:"):
            return [{"record_key": text, "path": "direct"}]
        if re.fullmatch(r"AR\d{6,24}", text):
            return [{"record_key": advertiser_key("google", text), "path": "direct"}]
        if re.fullmatch(r"\d{1,20}", text):
            return [{"record_key": advertiser_key("meta", text), "path": "direct"}]
        if re.fullmatch(r"[Cc]\d{8}", text):
            text = f"campaign-finance:fec:committee:{text.upper()}"
        if ":" in text:
            return [{"record_key": s["record_key"], "path": "accepted-identity", "via": text,
                     "candidate_id": s["candidate_id"], "method": s["method"], "basis": s["basis"],
                     "decision_id": s["decision_id"]}
                    for s in self.identity.subjects_for(namespace, text, scopes=scopes)]
        return [{"record_key": funding_key(text), "path": "direct"}]

    def _ad_view(self, row: Mapping[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
        fields = row["record"]["fields"]
        view = {
            "record_key": row["record_key"], "platform": row["record"]["platform"], "ad_id": fields.get("ad_id"),
            "advertiser_key": row["record"].get("advertiser_key"),
            "advertiser_as_declared": fields.get("advertiser_as_declared"),
            "funding_entity_as_declared": fields.get("funding_entity_as_declared"),
            "delivery_start": fields.get("delivery_start"), "delivery_stop": fields.get("delivery_stop"),
            "spend_as_published": (fields.get("spend_range_as_published") if row["record"]["platform"] == "meta"
                                   else {"bucket_usd": fields.get("spend_bucket_usd_as_published"),
                                         "ranges": fields.get("spend_ranges_as_published")}),
            "impressions_as_published": (fields.get("impressions_range_as_published")
                                         if row["record"]["platform"] == "meta"
                                         else fields.get("impressions_bucket_as_published")),
            "currency": fields.get("currency"), "listing_status": row["record"].get("listing_status"),
            "data_as_of": row["record"].get("data_as_of"), "revision_id": row["revision_id"],
            "revision_no": row["revision_no"], "citation": cite(row),
        }
        if view["listing_status"] == "not-returned":
            view["removal"] = {"revision_id": row["revision_id"], "observed_at": observed_at(row["observed_at_ms"]),
                               "statement": row["record"].get("not_returned", {}).get("statement")}
        view["revisions"] = [{"revision_id": h["revision_id"], "revision_no": h["revision_no"], "change": h["change"],
                              "observed_at": observed_at(h["observed_at_ms"])} for h in history]
        return view

    def ads_for_advertiser(self, namespace: str, advertiser: str, *, scopes: Iterable[str], as_of: Any = None
                           ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        subjects = self.resolve_subjects(namespace, advertiser, scopes=scopes)
        answer: dict[str, Any] = {"contract": ANSWER_CONTRACT, "query": "ads_for_advertiser", "namespace": namespace,
                                  "advertiser": advertiser, "as_of": str(as_of) if as_of else None,
                                  "subjects": subjects, "ranges_note": RANGES_NOTE, "exclusions": list(EXCLUSIONS)}
        keys = {s["record_key"] for s in subjects}
        ads = [a for a in self.store.records(namespace, scopes=scopes, kinds=["ad"])
               if a["advertiser_key"] in keys or any(funding_key(b) in keys for b in
                                                     a["record"]["fields"].get("funding_entity_as_declared") or [])]
        out = []
        for row in ads:
            history = self.store.history(namespace, row["record_key"], scopes=scopes, source_id=row["source_id"])
            if as_of:
                chosen = self.store.as_of(namespace, row["record_key"], as_of, scopes=scopes,
                                          source_id=row["source_id"])
                if chosen is None:
                    continue  # not yet on record at that time
                history = [h for h in history if h["revision_no"] <= chosen["revision_no"]]
                row = {**chosen, "advertiser_key": chosen["record"].get("advertiser_key")}
            out.append(self._ad_view(row, history))
        out.sort(key=lambda v: (v["delivery_start"] or "", v["record_key"]))
        advertisers = {}
        for key in sorted(keys):
            identity = self.identity.identity(namespace, key, scopes=scopes)
            advertisers[key] = {"identity": identity["state"], "links": identity["links"]}
        answer["advertisers"] = advertisers
        if not out:
            return {**answer, "status": "none_on_record", "ads": [], "note": NO_RECORD_NOTE}
        return {**answer, "status": "answered", "ads": out,
                "counts": {"ads": len(out), "listed": sum(v["listing_status"] == "listed" for v in out),
                           "not_returned": sum(v["listing_status"] == "not-returned" for v in out)}}

    def ads_for_election(self, namespace: str, election_id: str, *, scopes: Iterable[str],
                         elections_namespace: str | None = None) -> dict[str, Any]:
        from src.kb.platform_transparency_links import PlatformTransparencyLinks

        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        answer: dict[str, Any] = {"contract": ANSWER_CONTRACT, "query": "ads_for_election", "namespace": namespace,
                                  "election_id": election_id, "ranges_note": RANGES_NOTE,
                                  "exclusions": list(EXCLUSIONS)}
        links = PlatformTransparencyLinks(self.conn, initialize=False).links(namespace, scopes=scopes,
                                                                             kind="election", target_key=election_id)
        if elections_namespace:
            links = [link for link in links if link["target_namespace"] == elections_namespace]
        if not links:
            return {**answer, "status": "none_on_record", "advertisers": [],
                    "note": "no ad is linked to this election in the acquired coverage; this is not evidence that "
                            "none ran"}
        current = {r["record_key"]: r for r in self.store.records(namespace, scopes=scopes, kinds=["ad"],
                                                                   record_keys={link["record_key"] for link in links})}
        grouped: dict[str, list[dict[str, Any]]] = {}
        election = None
        for link in links:
            row = current.get(link["record_key"])
            if row is None:
                continue
            election = election or {"name": link["basis"].get("election_name"),
                                    "date": link["basis"].get("election_date")}
            history = self.store.history(namespace, row["record_key"], scopes=scopes, source_id=row["source_id"])
            view = self._ad_view(row, history)
            view["link"] = {"link_id": link["link_id"], "basis": link["basis"].get("basis"),
                            "linked_revision_id": link["record_revision_id"],
                            "target_revision": link["target_revision"]}
            grouped.setdefault(row["advertiser_key"] or row["record_key"], []).append(view)
        advertisers = [{"advertiser_key": key, "ads": sorted(ads, key=lambda v: (v["delivery_start"] or "",
                                                                                 v["record_key"]))}
                       for key, ads in sorted(grouped.items())]
        return {**answer, "status": "answered", "election": election, "advertisers": advertisers,
                "counts": {"advertisers": len(advertisers), "ads": sum(len(a["ads"]) for a in advertisers)}}

    # ------------------------------------------------------------------ SP10 moderation statements

    def moderation_statements(self, namespace: str, platform: str, *, scopes: Iterable[str], start: str, end: str,
                              ground: str | None = None) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        start, end = str(start)[:10], str(end)[:10]
        wanted = slug(platform)
        answer: dict[str, Any] = {"contract": ANSWER_CONTRACT, "query": "moderation_statements",
                                  "namespace": namespace, "platform": platform, "period": {"start": start, "end": end},
                                  "ground": ground, "note": COUNT_NOTE, "exclusions": list(EXCLUSIONS)}
        dumps = [d for d in self.store.records(namespace, scopes=scopes, kinds=["dump-release"])
                 if slug(d["record"]["fields"].get("platform_uid")) == wanted
                 and start <= d["record"]["fields"]["day"] <= end]
        statements = []
        withdrawn = []
        for row in self.store.records(namespace, scopes=scopes, kinds=["statement-of-reasons"]):
            fields = row["record"]["fields"]
            if wanted not in {slug(fields.get("platform_uid")), slug(fields.get("platform_name"))}:
                continue
            day = str(fields.get("application_date") or "")[:10]
            if not start <= day <= end or (ground and fields.get("decision_ground") != ground):
                continue
            (withdrawn if row["record"].get("listing_status") == "not-returned" else statements).append(row)
        dump_versions = [{"file_name": d["record"]["fields"]["file_name"], "day": d["record"]["fields"]["day"],
                          "variant": d["record"]["fields"]["variant"], "sha256": d["record"]["fields"]["sha256"],
                          "statements_in_file": d["record"]["fields"]["statements"],
                          "revision_id": d["revision_id"], "revision_no": d["revision_no"], "citation": cite(d)}
                         for d in sorted(dumps, key=lambda d: d["record"]["fields"]["day"])]
        covered = sorted({v["day"] for v in dump_versions})
        answer["stored_window"] = {"first_day": covered[0] if covered else None,
                                   "last_day": covered[-1] if covered else None, "days_with_dump": covered,
                                   "days_without_dump": [d for d in _days(start, end) if d not in covered]}
        answer["dump_versions"] = dump_versions
        if not statements:
            return {**answer, "status": "none_on_record", "total": 0, "withdrawn_from_republished_dumps": [
                cite(r) for r in withdrawn]}
        decision_types: Counter[str] = Counter()
        for row in statements:
            fields = row["record"]["fields"]
            for column in DECISION_COLUMNS:
                for value in _values(fields.get(column)):
                    decision_types[value] += 1
        count = lambda column: dict(sorted(Counter(
            str(r["record"]["fields"].get(column)) if r["record"]["fields"].get(column) is not None else "not published"
            for r in statements).items()))
        return {**answer, "status": "answered", "total": len(statements),
                "by_decision_type": dict(sorted(decision_types.items())), "by_ground": count("decision_ground"),
                "by_category": count("category"), "by_content_type": dict(sorted(Counter(
                    v for r in statements for v in _values(r["record"]["fields"].get("content_type"))).items())),
                "automated_detection_as_published": count("automated_detection"),
                "automated_decision_as_published": count("automated_decision"),
                "withdrawn_from_republished_dumps": [cite(r) for r in withdrawn],
                "statements": [{"record_key": r["record_key"], "revision_id": r["revision_id"],
                                "dump_key": r["record"]["fields"].get("dump_key")} for r in statements]}

    def statement_history(self, namespace: str, record_key: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        history = self.store.history(namespace, record_key, scopes=scopes)
        if not history:
            return {"status": "none_on_record", "record_key": record_key}
        return {"status": "answered", "record_key": record_key,
                "revisions": [{"revision_id": h["revision_id"], "revision_no": h["revision_no"], "change": h["change"],
                               "record": h["record"], "citation": cite(h)} for h in history]}

    def takedown_notices(self, namespace: str, recipient: str, *, scopes: Iterable[str]) -> dict[str, Any]:
        """Lumen notices for a recipient; the notices themselves only with the notices scope (researcher terms)."""
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        rows = [r for r in self.store.records(namespace, scopes=scopes, kinds=["takedown-notice"])
                if slug(r["record"]["fields"].get("recipient_name_as_published")) == slug(recipient)]
        answer = {"contract": ANSWER_CONTRACT, "query": "takedown_notices", "namespace": namespace,
                  "recipient": recipient, "count": len(rows), "exclusions": list(EXCLUSIONS)}
        if not rows:
            return {**answer, "status": "none_on_record"}
        if NOTICES_SCOPE not in scopes and "operator" not in scopes:
            return {**answer, "status": "counted", "note": f"notices are returned only with {NOTICES_SCOPE} under "
                                                          "the Lumen researcher terms"}
        return {**answer, "status": "answered",
                "notices": [{"record_key": r["record_key"], **r["record"]["fields"],
                             "listing_status": r["record"].get("listing_status"), "citation": cite(r)} for r in rows]}

    # ------------------------------------------------------------------ evidence bundle

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing the record revision behind it (source, revision and as-of time)."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []

        def add(identifier: str, text: str, citation: Mapping[str, Any] | None) -> None:
            if not citation:
                return
            bibliography.setdefault(citation["revision_id"], {
                "id": citation["revision_id"],
                "text": f"{citation['provider']} {citation['record_key']} (source {citation['source_id']}, revision "
                        f"{citation['revision_no']}, observed {citation.get('observed_at')}, source data as of "
                        f"{citation.get('data_as_of') or 'not published'}, {citation['evidence_origin']} evidence), "
                        f"{citation['locator']}"})
            assertions.append({"id": identifier, "text": text, "kind": "sourced",
                               "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                                 "id": citation["record_key"], "revision": citation["revision_id"],
                                                 "locator": {"section": citation["record_key"]}}],
                               "citations": [citation["revision_id"]]})

        def add_ad(ad: Mapping[str, Any]) -> None:
            add(f"ad-{ad['record_key']}", f"{ad['platform']} ad {ad['ad_id']} by {ad['advertiser_as_declared']} "
                f"(funding entity as declared: {ad['funding_entity_as_declared'] or 'none published'}), delivered "
                f"{ad['delivery_start']} to {ad['delivery_stop'] or 'not published'}, spend as published "
                f"{ad['spend_as_published']}, impressions as published {ad['impressions_as_published']}, "
                f"{ad['listing_status']}", ad["citation"])

        for ad in answer.get("ads") or []:
            add_ad(ad)
        for group in answer.get("advertisers") or []:
            if isinstance(group, Mapping):
                for ad in group.get("ads") or []:
                    add_ad(ad)
        for version in answer.get("dump_versions") or []:
            add(f"dump-{version['file_name']}", f"dump {version['file_name']} ({version['variant']}, sha256 "
                f"{version['sha256'][:12]}...) with {version['statements_in_file']} statements", version["citation"])
        if answer.get("query") == "moderation_statements" and answer.get("status") == "answered":
            assertions.append({"id": "counts", "kind": "derived",
                               "text": f"{answer['total']} stored statements of reasons for {answer['platform']} "
                                       f"{answer['period']['start']} to {answer['period']['end']}: by ground "
                                       f"{answer['by_ground']}; automated decision as published "
                                       f"{answer['automated_decision_as_published']}. {COUNT_NOTE}",
                               "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                                 "id": v["citation"]["record_key"],
                                                 "revision": v["revision_id"]} for v in answer["dump_versions"]],
                               "citations": [v["revision_id"] for v in answer["dump_versions"]]})
        for notice in answer.get("notices") or []:
            add(f"notice-{notice['record_key']}", f"{notice.get('type')} notice {notice.get('notice_id')} to "
                f"{notice.get('recipient_name_as_published')} received {notice.get('date_received')}",
                notice["citation"])
        title = answer.get("advertiser") or answer.get("election_id") or answer.get("platform") or \
            answer.get("recipient")
        return {"sections": [{"id": answer.get("query", "answer"), "title": f"{title}", "assertions": assertions}],
                "bibliography": list(bibliography.values()), "exclusions": list(EXCLUSIONS)}
