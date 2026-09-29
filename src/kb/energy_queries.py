"""Generation mix, load, price, flow and capacity as published at an as-of time, with revision history (EN10).

Every answer is per source, side by side: a series is never blended with
another provider's, re-aggregated or gap-filled. Each series shows the vintage
chosen for the as-of time (the latest release *published* at or before it —
provider release date when stated, else retrieval time, labelled), its
publication status, and a citation (source, dataset, release, status, as-of
and acquisition receipt).

A subject is reached through reviewable identity (:mod:`src.kb.energy_identity`):
codes accepted as the same place or plant are included with the match that
connected them; zones or areas that merely declare the place as a member are
listed separately as ``related`` and never counted as the subject. A subject
with nothing on record says so. Revision history reuses the shared vintage
comparison (:func:`src.kb.environment_vintages.diff_values`) by call.
"""

from __future__ import annotations

from src.kb.energy_records import READ_SCOPE
from src.kb.energy_store import EnergyStore, EnergyStoreError, authorize, iso, ms

CONTRACT = "noesis-energy-answer-v1"
NOTICE = ("Figures as published per source and release; sources are side by side, never blended. No forecasting, "
          "dispatch modelling, emissions estimation or trading advice.")


class EnergyQueries:
    def __init__(self, conn, *, now=None):
        self.conn = conn
        self.store = EnergyStore(conn, initialize=False, now=now)

    # ---------------------------------------------------------------- subjects

    def subject_scope(self, namespace, code, *, scopes):
        """Every subject code reachable from ``code`` (any scheme) through accepted identity, plus related areas."""

        from src.kb.energy_identity import EnergyIdentity
        from src.kb.energy_store import table_exists

        known = [s for s in self.store.subjects(namespace, scopes=scopes) if s["code"] == code]
        same, related = [], []
        if table_exists(self.conn, "energy_identity_matches"):
            identity = EnergyIdentity(self.conn, initialize=False)
            if not known:
                # A place or entity id (e.g. ent-eia-plant-99901) reaches the subjects accepted as it.
                for match in identity.matches(namespace, scopes=scopes, state="accepted"):
                    if match["target"]["id"] == code:
                        same.append({"subject": match["subject"],
                                     "match": {k: match[k] for k in ("match_id", "method", "state")},
                                     "basis": f"accepted match to {code}"})
            for subject in known or ([] if same else [{"scheme": None, "code": code}]):
                if subject["scheme"] is None:
                    for match in identity.matches(namespace, scopes=scopes, subject_code=code):
                        subject = match["subject"]
                        break
                    else:
                        continue
                connected = identity.connected(namespace, subject["scheme"], subject["code"], scopes=scopes)
                same += connected["same"]
                related += connected["related"]
        else:
            same = [{"subject": {"scheme": s["scheme"], "code": s["code"]}, "match": None,
                     "basis": "the requested code"} for s in known]
        if not same:
            same = [{"subject": {"scheme": s["scheme"], "code": s["code"]}, "match": None,
                     "basis": "the requested code"} for s in known]
        seen, unique_same = set(), []
        for item in same:
            key = (item["subject"].get("scheme"), item["subject"]["code"])
            if key not in seen:
                seen.add(key)
                unique_same.append(item)
        related = [r for r in related if (r["subject"]["scheme"], r["subject"]["code"]) not in seen]
        return {"same": unique_same, "related": related}

    # ------------------------------------------------------------------ series

    def _series_answer(self, namespace, series, *, scopes, start_ms, end_ms, as_of_ms):
        vintage = self.store.select_vintage(namespace, series["series_id"], scopes=scopes, as_of_ms=as_of_ms)
        if vintage is None:
            return {"series_id": series["series_id"], "provider": series["provider"], "dataset": series["dataset"],
                    "status": "not_published_by_as_of", "values": []}
        record = vintage["record"]
        later = [v for v in self.store.vintages(namespace, series["series_id"], scopes=scopes)
                 if v["published_at_ms"] > (as_of_ms if as_of_ms is not None else float("inf"))]
        return {"series_id": series["series_id"], "provider": series["provider"], "dataset": series["dataset"],
                "record_type": series["record_type"], "title": record["title"], "subject": record["subject"],
                "counterpart": record.get("counterpart"), "facets": record["facets"], "unit": record["unit"],
                "resolution": record["resolution"], "vintage_id": vintage["vintage_id"],
                "publication_status": vintage["status"], "is_revision": vintage["revision_of"] is not None,
                "values": self.store.values(vintage["vintage_id"], start_ms=start_ms, end_ms=end_ms),
                "publisher_figures": record.get("publisher_figures") or {}, "derived_from": record.get("derived_from"),
                "citation": self.store.citation(vintage),
                "later_vintages_after_as_of": [v["vintage_id"] for v in later]}

    def observations(self, namespace, subject, record_types, *, scopes, start=None, end=None, as_of_ms=None):
        """Series of the given record types for a subject, per source, at the vintage published by ``as_of_ms``."""

        authorize(namespace, scopes, READ_SCOPE)
        scope = self.subject_scope(namespace, subject, scopes=scopes)
        start_ms, end_ms = ms(start), ms(end)
        codes = {item["subject"]["code"]: item for item in scope["same"]}
        by_source: dict[str, list] = {}
        for series in self.store.series(namespace, scopes=scopes, subject_codes=set(codes)):
            if series["record_type"] not in record_types:
                continue
            answer = self._series_answer(namespace, series, scopes=scopes, start_ms=start_ms, end_ms=end_ms,
                                         as_of_ms=as_of_ms)
            answer["reached_through"] = codes[series["subject"]["code"]]
            by_source.setdefault(f"{series['provider']}:{series['dataset']}", []).append(answer)
        related = []
        for item in scope["related"]:
            count = len([s for s in self.store.series(namespace, scopes=scopes, subject_codes={item["subject"]["code"]})
                         if s["record_type"] in record_types])
            if count:
                related.append({**item, "series": count,
                                "note": "a different area; query it by its own code (not counted as this subject)"})
        status = "answered" if by_source else "none_on_record"
        return {"contract": CONTRACT, "namespace": namespace, "subject": subject, "record_types": sorted(record_types),
                "window": {"start": start, "end": end}, "as_of": iso(as_of_ms) if as_of_ms is not None else "latest",
                "status": status, "sources": [{"source": key, "series": value} for key, value in sorted(by_source.items())],
                "reached_subjects": scope["same"], "related_subjects": related, "notice": NOTICE,
                **({"message": f"no energy records on record for {subject}"} if status == "none_on_record" else {})}

    def generation_mix(self, namespace, subject, *, scopes, start=None, end=None, as_of_ms=None):
        """Generation by fuel for a zone/country/area and window, per source as published at ``as_of_ms``."""

        answer = self.observations(namespace, subject, {"generation"}, scopes=scopes, start=start, end=end,
                                   as_of_ms=as_of_ms)
        for source in answer["sources"]:
            source["fuels"] = sorted({(s["facets"].get("fuel") or {}).get("code") or "total" for s in source["series"]
                                      if s.get("facets")})
            source["publisher_aggregates"] = [s["series_id"] for s in source["series"]
                                              if (s.get("facets") or {}).get("aggregate_series")]
        answer["query"] = "generation_mix"
        return answer

    # -------------------------------------------------------------- revisions

    def revision_history(self, namespace, series_id, *, scopes, period_start=None):
        """Every vintage of a series (or of one figure) with release dates, status, citation and what changed."""

        from src.kb.environment_vintages import diff_values

        authorize(namespace, scopes, READ_SCOPE)
        vintages = self.store.vintages(namespace, series_id, scopes=scopes)
        if not vintages:
            raise EnergyStoreError("not_found", "series has no vintages")
        entries, previous = [], None
        for vintage in vintages:
            values = {v["start"]: {**v, "status": vintage["status"], "unit": vintage["record"]["unit"]}
                      for v in self.store.values(vintage["vintage_id"])}
            entry = {"vintage_id": vintage["vintage_id"], "sequence": vintage["sequence"],
                     "release": vintage["record"]["release"], "published_at": iso(vintage["published_at_ms"]),
                     "published_at_basis": vintage["release_basis"], "retrieved_at": vintage["record"]["retrieved_at"],
                     "status": vintage["status"], "revision_of": vintage["revision_of"],
                     "citation": self.store.citation(vintage)}
            if period_start is not None:
                figure = values.get(period_start)
                entry["figure"] = None if figure is None else {"value": figure["value"], "flags": figure["flags"]}
            if previous is not None:
                old_id, old_values = previous
                changes = diff_values(old_values, values, left_id=old_id, right_id=vintage["vintage_id"])
                if period_start is not None:
                    changes = [c for c in changes if c["key"] == period_start]
                entry["changes_from_previous"] = changes
            entries.append(entry)
            previous = (vintage["vintage_id"], values)
        record = vintages[-1]["record"]
        return {"contract": CONTRACT, "query": "revision_history", "series_id": series_id, "title": record["title"],
                "provider": record["provider"], "dataset": record["dataset"], "unit": record["unit"],
                "period_start": period_start, "vintages": entries,
                "notice": "each vintage as published, both sides cited; no cause of a revision is inferred"}

    # --------------------------------------------------------------- capacity

    def capacity_as_of(self, namespace, subject, date, *, scopes, as_of_ms=None):
        """Capacity effective on ``date`` for a zone, plant or unit, per source; missing data is unknown."""

        authorize(namespace, scopes, READ_SCOPE)
        day_ms = ms(date)
        knowledge = as_of_ms if as_of_ms is not None else None
        answer = self.observations(namespace, subject, {"capacity"}, scopes=scopes, as_of_ms=knowledge)
        results = []
        for source in answer["sources"]:
            for series in source["series"]:
                if series.get("status") == "not_published_by_as_of":
                    results.append({**series, "capacity_on_date": None, "state": "unknown",
                                    "reason": "no release published by the as-of time"})
                    continue
                record = self.store.vintage(namespace, series["vintage_id"], scopes=scopes)["record"]
                detail = record["capacity"] or {}
                effective_from, effective_to = ms(detail.get("effective_from")), ms(detail.get("effective_to"))
                chosen = None
                for value in self.store.values(series["vintage_id"]):
                    begin, finish = ms(value["start"]), ms(value["end"]) if value["end"] else None
                    if begin <= day_ms and (finish is None or day_ms < finish):
                        chosen = value
                in_effect = ((effective_from is None or effective_from <= day_ms)
                             and (effective_to is None or day_ms < effective_to))
                state = ("known" if chosen is not None and chosen["value"] is not None and in_effect
                         else "not_in_effect" if chosen is not None and not in_effect else "unknown")
                results.append({"series_id": series["series_id"], "provider": series["provider"],
                                "title": series["title"], "subject": series["subject"], "level": detail.get("level"),
                                "effective_from": detail.get("effective_from"), "effective_to": detail.get("effective_to"),
                                "operating_status": detail.get("operating_status"), "unit": series["unit"],
                                "capacity_on_date": chosen["value"] if state == "known" else None,
                                "period": None if chosen is None else {"start": chosen["start"], "end": chosen["end"]},
                                "flags": None if chosen is None else chosen["flags"], "state": state,
                                "publication_status": series["publication_status"], "citation": series["citation"],
                                **({"reason": "no published value covers the date"} if state == "unknown" else {})})
        return {"contract": CONTRACT, "query": "capacity_as_of", "namespace": namespace, "subject": subject,
                "date": date, "as_of": answer["as_of"],
                "status": "answered" if results else "none_on_record", "capacity": results,
                "reached_subjects": answer["reached_subjects"], "related_subjects": answer["related_subjects"],
                "notice": "capacity as published per source; unknown where no published value covers the date"}
