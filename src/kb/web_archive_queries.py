"""What a page said on a date, and according to which archive (#2226, WA10).

A read-only query layer over the capture and TimeMap snapshot records in the
citation preservation store. For each configured archive it returns the nearest
capture before and after the requested date, with the Memento-Datetime and the
distance from that date. It also keeps apart the ways an archive can have no
answer: ``no_capture_on_record`` (a TimeMap read found none),
``archive_unavailable``, ``excluded_by_access_decision`` or
``deferred_by_access_decision``, ``excluded_by_archive`` (robots or
administrative exclusion), ``blocked_by_archive``, ``not_reported_by_aggregator``
and ``not_queried``.

Captures with identical digests (same algorithm and value) are grouped as the
same content, and every archive's record is kept. There is no content diffing
beyond digest equality and no inference about unarchived dates. Common Crawl is
listed as a crawl corpus and never as a TimeGate answer.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

ANSWER_CONTRACT = "noesis-web-archive-as-of-v1"
_NO_ANSWER_PRIORITY = (
    "no_capture_on_record",
    "excluded_by_archive",
    "blocked_by_archive",
    "archive_unavailable",
    "not_reported_by_aggregator",
)


def _brief(capture: dict[str, Any], at_ms: int, cited_url: str) -> dict[str, Any]:
    from src.kb.web_archive_identity import classify

    kind = classify(cited_url, capture)
    return {
        "capture_id": capture["capture_id"],
        "uri_m": capture["uri_m"],
        "uri_r": capture["uri_r"],
        "memento_datetime": capture["memento_datetime"],
        "distance_seconds": abs(capture["memento_at_ms"] - at_ms) // 1000,
        "direction": "before" if capture["memento_at_ms"] <= at_ms else "after",
        "status": capture["status"],
        "digests": capture["digests"],
        "access_condition": capture["access_condition"],
        "resolver": capture["resolver"],
        "match_kind": kind["match_kind"] if kind else None,
        "match_rules": kind["rules"] if kind else [],
    }


def _status_without_captures(snapshots: Iterable[dict[str, Any]]) -> str:
    outcomes = {s["outcome"] for s in snapshots}
    for outcome in _NO_ANSWER_PRIORITY:
        if outcome in outcomes:
            return outcome
    return "not_queried"


def page_as_of(conn: Any, namespace: str, url: str, at: str | int, *, scopes: set[str],
               archives: Iterable[str] | None = None) -> dict[str, Any]:
    """Per-archive nearest-prior and nearest-after captures of ``url`` around ``at``, with citations."""
    from src.ingestion.memento import ARCHIVES, decision_outcome, validate_url
    from src.kb.citation_preservation import READ_SCOPE, CitationPreservationStore, _datetime_ms, _iso, _require

    _require(scopes, READ_SCOPE)
    url = validate_url(url)
    at_ms = _datetime_ms(at)
    store = CitationPreservationStore(conn, initialize=False)
    captures = store.captures_for_url(namespace, url, scopes=scopes)
    snapshots = store.timemaps_for_url(namespace, url, scopes=scopes)
    selected = list(archives) if archives is not None else [
        a for a, spec in ARCHIVES.items() if spec["kind"] != "aggregator"]
    selected += sorted({c["archive_id"] for c in captures} - set(selected) - set(ARCHIVES)) if archives is None else []
    rows, cited_captures, cited_timemaps = [], set(), set()
    for archive_id in selected:
        spec = ARCHIVES.get(archive_id, {"kind": "memento-archive", "access_decision": "in_scope"})
        own = sorted((c for c in captures if c["archive_id"] == archive_id), key=lambda c: c["memento_at_ms"])
        archive_snaps = [s for s in snapshots if s["archive_id"] == archive_id]
        entry: dict[str, Any] = {
            "archive_id": archive_id,
            "archive_kind": "crawl-corpus" if spec["kind"] == "crawl-corpus" else "memento-archive",
            "access_decision": spec["access_decision"],
            "timegate_answer": spec["kind"] != "crawl-corpus",
            "nearest_prior": None,
            "nearest_after": None,
            "capture_count": 0,
            "timemap_ids": sorted(s["timemap_id"] for s in archive_snaps),
        }
        if spec["access_decision"] != "in_scope":
            entry.update(status=decision_outcome(spec), reason=spec.get("decision_reason"))
            rows.append(entry)
            continue
        cited_timemaps.update(entry["timemap_ids"])
        if not own:
            entry["status"] = _status_without_captures(archive_snaps)
            rows.append(entry)
            continue
        prior = [c for c in own if c["memento_at_ms"] <= at_ms]
        after = [c for c in own if c["memento_at_ms"] > at_ms]
        entry.update(status="captures_on_record", capture_count=len(own),
                     nearest_prior=_brief(prior[-1], at_ms, url) if prior else None,
                     nearest_after=_brief(after[0], at_ms, url) if after else None)
        latest_direct = [s for s in archive_snaps if s["resolver"] == archive_id]
        if latest_direct and latest_direct[-1]["outcome"] in {"archive_unavailable", "blocked_by_archive"}:
            entry["last_check"] = latest_direct[-1]["outcome"]
        for side in ("nearest_prior", "nearest_after"):
            if entry[side]:
                cited_captures.add(entry[side]["capture_id"])
        rows.append(entry)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        for side in ("nearest_prior", "nearest_after"):
            capture = row[side]
            if not capture:
                continue
            for digest in capture["digests"]:
                members = groups.setdefault((digest["algorithm"], digest["value"]), [])
                if capture["capture_id"] not in {m["capture_id"] for m in members}:
                    members.append({"capture_id": capture["capture_id"], "archive_id": row["archive_id"],
                                    "memento_datetime": capture["memento_datetime"], "basis": digest["basis"]})
    same_content = [
        {"algorithm": algorithm, "digest": value, "captures": members,
         "archives": sorted({m["archive_id"] for m in members})}
        for (algorithm, value), members in sorted(groups.items()) if len(members) > 1
    ]
    candidates = [row[side] | {"archive_id": row["archive_id"]} for row in rows
                  for side in ("nearest_prior", "nearest_after")
                  if row[side] and row["timegate_answer"]]
    closest = min(candidates, key=lambda c: (c["distance_seconds"], c["archive_id"]), default=None)
    if closest is None:
        statement = "no archive holds a capture of this URL on record"
    else:
        statement = (f"closest capture on record: {closest['archive_id']} at {closest['memento_datetime']}, "
                     f"{closest['distance_seconds']} s {closest['direction']} the requested date")
    return {
        "contract": ANSWER_CONTRACT,
        "namespace": namespace,
        "url": url,
        "requested": _iso(at_ms),
        "status": "answered" if closest else "no_capture_on_record",
        "statement": statement,
        "closest": closest,
        "archives": rows,
        "same_content_groups": same_content,
        "cites": {"capture_ids": sorted(cited_captures), "timemap_ids": sorted(cited_timemaps)},
        "limits": ["digest equality only; no content diffing",
                   "no inference about what the page said between captures",
                   "Common Crawl is a crawl corpus and never a TimeGate answer"],
    }
