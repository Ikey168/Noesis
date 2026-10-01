"""Platform transparency answers: political ads by advertiser or election, moderation statements by platform,
ground and period (#2580, SP09, SP10).

* :meth:`PlatformTransparencyQueries.ads_by_advertiser` - the ads of an
  advertiser or funding entity (a Meta page id, a Google advertiser id, a
  subject key, a declared name, or a record another pack owns - an FEC
  committee id, a campaign-finance, elections, lobbying or ownership key -
  reached only through accepted SP07 identity decisions, with the path shown),
  with delivery dates and spend and impression ranges exactly as published;
* :meth:`PlatformTransparencyQueries.ads_for_election` - ads linked to an
  election by SP08 links, grouped by advertiser, with each link's basis and
  the delivery dates relative to election day;
* :meth:`PlatformTransparencyQueries.moderation_statements` - counts of stored
  statements of reasons for a platform and period by decision type, decision
  ground and category, and the automated-detection and automated-decision
  values as published, stating the stored window, the days without a stored
  dump and the dump versions used.

Every item cites its source, record revision and as-of time. Ranges are never
converted into a midpoint, a sum of midpoints or any other point estimate; an
ad that a complete listing no longer returned is shown with that removal
revision. Counts are computed only over stored records and say so. No
user-level profiling, private content or coordination inference.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta
from typing import Any

from src.ingestion.platform_transparency_sources import slug
from src.kb.platform_transparency_identity import PlatformTransparencyIdentity, _norm
from src.kb.platform_transparency_records import (
    EXCLUSIONS,
    READ_SCOPE,
    PlatformTransparencyStore,
    authorize,
    cite,
    iso,
    to_ms,
)

ANSWER_CONTRACT = "noesis-platform-transparency-answer-v1"
RANGES_NOTICE = ("spend and impression ranges are the bounds (and bucket text) each platform published; they are "
                 "never converted to a midpoint, summed or turned into a point estimate")
REMOVAL_NOTICE = ("'not-returned' means a complete listing of the same declared unit no longer returned the ad; the "
                  "platform did not state why")
COUNT_NOTICE = ("counts are computed over the statements of reasons stored in Noesis for the stated window and "
                "dump versions only; they are not the platform's totals")
DECISION_TYPES = ("decision_visibility", "decision_monetary", "decision_provision", "decision_account")
_FEC = re.compile(r"^[Cc]\d{8}$")
FOREIGN_PREFIXES = ("campaign-finance:", "elections:", "lobbying:", "lei:", "gb-coh:", "sec-cik:", "ownership:")


class PlatformTransparencyQueries:
    def __init__(self, conn: Any, *, now: Callable[[], int] | None = None) -> None:
        self.conn = conn
        self.store = PlatformTransparencyStore(conn, initialize=False, now=now)
        self.identity = PlatformTransparencyIdentity(conn, now=now, initialize=False)

    # ------------------------------------------------------------------ SP09 ads

    def subjects_for(self, namespace: str, advertiser: str, scopes: set[str]
                      ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
        """The subjects an advertiser argument names, each with the path that reached it."""
        text = str(advertiser or "").strip()
        subjects = self.identity.subjects(namespace, scopes=scopes)
        paths: dict[str, list[dict[str, Any]]] = {}
        if _FEC.fullmatch(text):
            text = f"campaign-finance:fec:committee:{text.upper()}"
        if text.startswith(FOREIGN_PREFIXES):
            for subject in subjects:
                for match in self.identity.accepted(namespace, subject["record_key"], scopes=scopes):
                    if match["record_key"] == text:
                        paths[subject["record_key"]] = [{"step": "record", "key": text},
                                                        {"step": "identity", "decision": match["candidate_id"],
                                                         "method": match["method"], "basis": match["basis"],
                                                         "from": text, "to": subject["record_key"]}]
        else:
            if re.fullmatch(r"AR\d{10,30}", text):
                keys = {f"platform-transparency:google:advertiser:{text}"}
            elif re.fullmatch(r"\d{1,20}", text):
                keys = {f"platform-transparency:meta:advertiser:{text}"}
            elif text.startswith("platform-transparency:"):
                keys = {text}
            else:
                keys = {s["record_key"] for s in subjects if _norm(text) in {_norm(n) for n in s["names"]}}
            via = "subject key or published id" if text.startswith("platform-transparency:") or text in \
                "".join(keys) else "declared name as published"
            for key in keys:
                paths[key] = [{"step": "subject", "key": key, "via": via}]
        chosen = [s for s in subjects if s["record_key"] in paths]
        return chosen, paths

    def ad_view(self, namespace: str, row: Mapping[str, Any], scopes: set[str]) -> dict[str, Any]:
        fields = row["record"]["fields"]
        history = self.store.history(namespace, row["record_key"], scopes=scopes, source_id=row["source_id"])
        view = {
            "record_key": row["record_key"], "platform": row["platform"], "provider": row["provider"],
            "ad_id": fields.get("ad_id"), "advertiser_key": row["advertiser_key"],
            "advertiser_as_declared": fields.get("advertiser_as_declared"),
            "funding_entity_as_declared": fields.get("funding_entity_as_declared"),
            "delivery": {"start": fields.get("ad_delivery_start_time") or fields.get("date_range_start"),
                         "stop": fields.get("ad_delivery_stop_time") or fields.get("date_range_end"),
                         "num_of_days_as_published": fields.get("num_of_days")},
            "spend_range_as_published": fields.get("spend"), "currency_as_published": (
                fields.get("currency") or (fields.get("spend") or {}).get("currency")),
            "impressions_range_as_published": fields.get("impressions"),
            "listing_state": fields.get("listing_state"),
            "revision": {"revision_id": row["revision_id"], "revision_no": row["revision_no"],
                         "change": row["change"]},
            "revisions": [{"revision_id": h["revision_id"], "revision_no": h["revision_no"], "change": h["change"],
                           "observed_at": iso(h["observed_at_ms"]), "source_as_of": h["source_as_of"]}
                          for h in history],
            "citation": cite(row),
        }
        if fields.get("listing_state") == "not-returned":
            view["removal"] = {"revision_id": row["revision_id"], "observed_at": iso(row["observed_at_ms"]),
                               "basis": fields.get("not_returned_basis"), "notice": REMOVAL_NOTICE}
        return view

    def _ads_of(self, namespace: str, subject: Mapping[str, Any], scopes: set[str], as_of: Any
                ) -> list[dict[str, Any]]:
        from src.kb.platform_transparency_links import ad_keys_of

        keys = ad_keys_of(self.store, namespace, subject, scopes)
        return self.store.records(namespace, scopes=scopes, kinds=["ad"], record_keys=keys, as_of=as_of)

    def ads_by_advertiser(self, namespace: str, advertiser: str, *, scopes: Iterable[str], as_of: Any = None
                          ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        at = to_ms(as_of)
        answer: dict[str, Any] = {"contract": ANSWER_CONTRACT, "query": "ads_by_advertiser", "namespace": namespace,
                                  "advertiser": advertiser, "as_of": iso(at), "exclusions": list(EXCLUSIONS),
                                  "ranges_notice": RANGES_NOTICE}
        subjects, paths = self.subjects_for(namespace, advertiser, scopes)
        groups = []
        for subject in subjects:
            ads = [self.ad_view(namespace, row, scopes) for row in self._ads_of(namespace, subject, scopes, at)]
            groups.append({"subject_key": subject["record_key"], "kind": subject["kind"],
                           "platform": subject["platform"], "names_as_published": subject["names"],
                           "identifiers_as_published": subject["identifiers"], "path": paths[subject["record_key"]],
                           "identity": self.identity.identity(namespace, subject["record_key"], scopes=scopes),
                           "ads": ads, "ads_listed": sum(1 for a in ads if a["listing_state"] == "listed"),
                           "ads_not_returned": sum(1 for a in ads if a["listing_state"] == "not-returned")})
        found = any(g["ads"] for g in groups)
        answer.update(status="answered" if found else "none_on_record", advertisers=groups,
                      removal_notice=REMOVAL_NOTICE,
                      note=None if found else "no ad of this advertiser is on record in the acquired coverage (or "
                                              "no accepted identity decision reaches one)")
        return answer

    def ads_for_election(self, namespace: str, election_id: str, *, scopes: Iterable[str], as_of: Any = None
                         ) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        from src.kb.platform_transparency_links import PlatformTransparencyLinks

        at = to_ms(as_of)
        answer: dict[str, Any] = {"contract": ANSWER_CONTRACT, "query": "ads_for_election", "namespace": namespace,
                                  "election_id": election_id, "as_of": iso(at), "exclusions": list(EXCLUSIONS),
                                  "ranges_notice": RANGES_NOTICE, "removal_notice": REMOVAL_NOTICE}
        links = PlatformTransparencyLinks(self.conn, initialize=False).links(namespace, scopes=scopes, kind="election",
                                                                             target_key=election_id)
        groups: dict[str, dict[str, Any]] = {}
        seen: set[tuple[str, str]] = set()
        for link in links:
            if at is not None and link["created_at_ms"] > at:
                continue
            rows = self.store.records(namespace, scopes=scopes, record_keys=[link["record_key"]], as_of=at)
            if not rows or (link["subject_key"], link["record_key"]) in seen:
                continue
            seen.add((link["subject_key"], link["record_key"]))
            row = rows[0]
            group = groups.setdefault(link["subject_key"], {"subject_key": link["subject_key"], "ads": []})
            group["ads"].append({**self.ad_view(namespace, row, scopes),
                                 "link": {"link_id": link["link_id"], "linked_revision_id": link["record_revision_id"],
                                          "basis": link["basis"]}})
        answer["election"] = links[0]["basis"].get("election_name") if links else None
        answer.update(status="answered" if groups else "none_on_record", advertisers=list(groups.values()),
                      note=None if groups else "no ad is linked to this election (no accepted identity decision, "
                                               "or no ad on record)")
        return answer

    # ------------------------------------------------------------------ SP10 moderation statements

    def moderation_statements(self, namespace: str, platform: str, *, scopes: Iterable[str], start: str,
                              end: str, ground: str | None = None, as_of: Any = None,
                              include_statements: bool = False, limit: int = 50) -> dict[str, Any]:
        scopes = set(scopes)
        authorize(namespace, scopes, READ_SCOPE)
        at = to_ms(as_of)
        key = slug(platform)
        start, end = str(start)[:10], str(end)[:10]
        answer: dict[str, Any] = {
            "contract": ANSWER_CONTRACT, "query": "moderation_statements", "namespace": namespace,
            "platform": key, "period": {"start": start, "end": end, "basis": "the dump day (the day the DSA "
                                        "Transparency Database received the statement)"},
            "ground": ground, "as_of": iso(at), "exclusions": list(EXCLUSIONS), "count_notice": COUNT_NOTICE}
        if end < start:
            return {**answer, "status": "invalid_request", "note": "the period ends before it starts"}
        dumps = [d for d in self.store.records(namespace, scopes=scopes, kinds=["dump-release"], platform=key,
                                               as_of=at)
                 if start <= d["record"]["fields"]["date"] <= end]
        days, day = [], date.fromisoformat(start)
        while day <= date.fromisoformat(end) and len(days) <= 366:
            days.append(day.isoformat())
            day += timedelta(days=1)
        stored_days = sorted({d["record"]["fields"]["date"] for d in dumps})
        answer["dump_versions"] = [{"dump_key": d["record_key"], "date": d["record"]["fields"]["date"],
                                    "version": d["record"]["fields"]["version"],
                                    "sha1_as_published": d["record"]["fields"]["sha1_as_published"],
                                    "statements_in_dump": d["record"]["fields"]["statements"],
                                    "revision_id": d["revision_id"], "revision_no": d["revision_no"],
                                    "citation": cite(d)} for d in sorted(dumps, key=lambda d: d["record_key"])]
        answer["stored_window"] = {"first_day": stored_days[0] if stored_days else None,
                                   "last_day": stored_days[-1] if stored_days else None,
                                   "days_with_a_stored_dump": stored_days,
                                   "days_without_a_stored_dump": [d for d in days if d not in stored_days],
                                   "complete": bool(stored_days) and len(stored_days) == len(days)}
        if not dumps:
            return {**answer, "status": "none_on_record", "counts": None,
                    "note": "no dump of this platform for the period is stored; nothing is counted"}
        statements = []
        for dump in dumps:
            statements += self.store.records(namespace, scopes=scopes, kinds=["statement-of-reasons"],
                                             dump_key=dump["record_key"], as_of=at)
        if ground:
            statements = [s for s in statements if s["record"]["fields"].get("decision_ground") == ground]
        by_type: dict[str, Counter] = {t: Counter() for t in DECISION_TYPES}
        by_ground, by_category, detection, decision = Counter(), Counter(), Counter(), Counter()
        by_type_and_ground: Counter = Counter()
        for row in statements:
            fields = row["record"]["fields"]
            for decision_type in DECISION_TYPES:
                for value in fields.get(decision_type) or []:
                    by_type[decision_type][value] += 1
                    by_type_and_ground[(decision_type, value, fields.get("decision_ground"))] += 1
            by_ground[fields.get("decision_ground") or "not published"] += 1
            by_category[fields.get("category") or "not published"] += 1
            detection[fields.get("automated_detection") or "not published"] += 1
            decision[fields.get("automated_decision") or "not published"] += 1
        answer.update(
            status="answered", statements_counted=len(statements),
            counts={
                "by_decision_type": {t: dict(sorted(c.items())) for t, c in by_type.items()},
                "by_decision_ground": dict(sorted(by_ground.items())),
                "by_decision_type_and_ground": [{"decision_type": t, "value": v, "decision_ground": g, "count": n}
                                                for (t, v, g), n in sorted(by_type_and_ground.items(),
                                                                           key=lambda i: tuple(map(str, i[0])))],
                "by_category": dict(sorted(by_category.items())),
                "automated_detection_as_published": dict(sorted(detection.items())),
                "automated_decision_as_published": dict(sorted(decision.items())),
            })
        if include_statements:
            answer["statements"] = [{"record_key": s["record_key"], "uuid": s["record"]["fields"]["uuid"],
                                     "decision_ground": s["record"]["fields"].get("decision_ground"),
                                     "category": s["record"]["fields"].get("category"),
                                     **{t: s["record"]["fields"].get(t) for t in DECISION_TYPES},
                                     "automated_detection": s["record"]["fields"].get("automated_detection"),
                                     "automated_decision": s["record"]["fields"].get("automated_decision"),
                                     "application_date": s["record"]["fields"].get("application_date"),
                                     "dump_key": s["dump_key"], "citation": cite(s)}
                                    for s in statements[:max(0, int(limit))]]
        return answer

    # ------------------------------------------------------------------ evidence bundles

    @staticmethod
    def evidence_bundle(answer: Mapping[str, Any]) -> dict[str, Any]:
        """Assertions each citing the record revision behind it (source, record revision and as-of time)."""
        bibliography: dict[str, dict[str, Any]] = {}
        assertions = []

        def add(identifier: str, text: str, citation: Mapping[str, Any] | None) -> None:
            if not citation:
                return
            bibliography.setdefault(citation["revision_id"], {
                "id": citation["revision_id"],
                "text": f"{citation['provider']} {citation['record_key']} (source {citation['source_id']}, revision "
                        f"{citation['revision_no']}, source as of {citation.get('source_as_of') or 'not stated'}, "
                        f"recorded {citation.get('observed_at') or citation.get('observed_at_ms')}, "
                        f"{citation['evidence_origin']} evidence), {citation['locator']}"})
            assertions.append({"id": identifier, "text": text, "kind": "sourced",
                               "dependencies": [{"kind": "source", "namespace": answer.get("namespace"),
                                                 "id": citation["record_key"], "revision": citation["revision_id"],
                                                 "locator": {"section": citation.get("dump_key") or
                                                             citation["record_key"]}}],
                               "citations": [citation["revision_id"]]})

        for group in answer.get("advertisers") or []:
            for ad in group.get("ads") or []:
                spend = ad.get("spend_range_as_published") or {}
                impressions = ad.get("impressions_range_as_published") or {}
                add(f"ad-{ad['record_key']}",
                    f"{ad['platform']} ad {ad['ad_id']} by {ad['advertiser_as_declared']} "
                    f"(funding entity as declared: {ad['funding_entity_as_declared']}), delivered "
                    f"{ad['delivery']['start']} to {ad['delivery']['stop']}, spend as published "
                    f"{spend.get('lower_bound')}-{spend.get('upper_bound')} {ad.get('currency_as_published')}, "
                    f"impressions as published {impressions.get('as_published', impressions)}, listing state "
                    f"{ad['listing_state']}", ad["citation"])
        for dump in answer.get("dump_versions") or []:
            add(f"dump-{dump['dump_key']}", f"DSA Transparency Database dump {dump['dump_key']} (SHA-1 "
                f"{dump['sha1_as_published']}, {dump['statements_in_dump']} statements)", dump["citation"])
        for statement in answer.get("statements") or []:
            add(f"sor-{statement['record_key']}", f"statement of reasons {statement['uuid']}: ground "
                f"{statement['decision_ground']}, category {statement['category']}", statement["citation"])
        title = answer.get("advertiser") or answer.get("election_id") or answer.get("platform")
        return {"sections": [{"id": answer.get("query", "answer"), "title": f"{title} as of "
                              f"{answer.get('as_of') or 'latest'}", "assertions": assertions}],
                "bibliography": list(bibliography.values()), "exclusions": list(EXCLUSIONS),
                "ranges_notice": RANGES_NOTICE}
