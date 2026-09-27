"""Compare vintages of an environmental series and pin the vintage a view used (E09).

Re-published values (validated air quality replacing provisional values,
corrected ETS emissions, revised generation totals) are stored as new
vintages by :class:`~src.kb.environment_store.EnvironmentStore`, following
the economic provider-vintage pattern: release and retrieval clocks with
their basis labels, the previous vintage as ``revision_of``, and the earlier
vintage kept addressable. This module explains what changed:

* each changed, added or removed period is listed with **both** vintages
  cited (id, release and retrieval time, basis, evidence);
* provisional versus validated status is shown on every value;
* a view pin (:func:`pin_status`) reports ``stale`` when a newer vintage
  exists, and never follows it silently.
"""

from __future__ import annotations

from decimal import Decimal

from src.kb.environment_records import READ_SCOPE
from src.kb.environment_store import EnvironmentStore, EnvironmentStoreError, authorize

CONTRACT = "noesis-environment-vintage-comparison-v1"


def _cite(vintage):
    return {key: vintage[key] for key in ("vintage_id", "sequence", "status", "release_at_ms", "release_at_basis",
                                          "retrieved_at_ms", "retrieved_at_basis", "vintage_basis",
                                          "release_time_status", "revision_of", "run_id", "evidence")}


def compare(conn, namespace, record_id, *, scopes, left=None, right=None):
    """Compare two vintages (default: the previous and the latest)."""

    authorize(namespace, scopes, READ_SCOPE)
    store = EnvironmentStore(conn, initialize=False)
    vintages = store.vintages(namespace, record_id, scopes=scopes)
    if len(vintages) < 1:
        raise EnvironmentStoreError("not_found", "series has no vintages")
    by_id = {v["vintage_id"]: v for v in vintages}
    right_v = by_id.get(right) if right else vintages[-1]
    left_v = by_id.get(left) if left else (vintages[-2] if len(vintages) > 1 else None)
    if right_v is None or (left and left_v is None):
        raise EnvironmentStoreError("not_found", "vintage does not belong to this series")
    if left_v is None:
        return {"contract": CONTRACT, "record_id": record_id, "status": "single_vintage", "left": None,
                "right": _cite(right_v), "changes": []}
    old = {v["start"]: v for v in store.values(left_v["vintage_id"])}
    new = {v["start"]: v for v in store.values(right_v["vintage_id"])}
    changes = []
    for start in sorted(old.keys() | new.keys()):
        before, after = old.get(start), new.get(start)
        if before and after and before["value"] == after["value"] and before["status"] == after["status"]:
            continue
        kind = ("added" if before is None else "removed" if after is None
                else "value_revised" if before["value"] != after["value"] else "status_changed")
        delta = None
        if kind == "value_revised" and before["value"] is not None and after["value"] is not None:
            delta = str(Decimal(after["value"]) - Decimal(before["value"]))
        elif kind == "value_revised":
            kind = "value_filled" if before["value"] is None else "value_withdrawn"
        changes.append({"period_start": start, "change": kind, "delta": delta, "unit": (after or before)["unit"],
                        "left": None if before is None else {"value": before["value"], "status": before["status"],
                                                             "vintage_id": left_v["vintage_id"]},
                        "right": None if after is None else {"value": after["value"], "status": after["status"],
                                                             "vintage_id": right_v["vintage_id"]}})
    record = store.record(namespace, record_id, scopes=scopes)
    return {"contract": CONTRACT, "record_id": record_id, "title": record["content"]["title"],
            "provider": record["provider"], "kind": record["kind"], "status": "compared",
            "left": _cite(left_v), "right": _cite(right_v), "changes": changes,
            "summary": {"changed": len(changes), "left_status": left_v["status"], "right_status": right_v["status"]},
            "notice": "differences between published vintages; no cause is inferred"}


def pin_status(conn, namespace, pins, *, scopes):
    """Staleness of a downstream view's pinned vintages."""

    authorize(namespace, scopes, READ_SCOPE)
    result = []
    for pin in pins:
        row = conn.execute("SELECT record_id, sequence FROM environment_vintages WHERE vintage_id=? AND namespace=?",
                           [pin, namespace]).fetchone()
        if row is None:
            result.append({"vintage_id": pin, "state": "unavailable"})
            continue
        newer = [r[0] for r in conn.execute(
            "SELECT vintage_id FROM environment_vintages WHERE record_id=? AND sequence>? ORDER BY sequence",
            [row[0], row[1]]).fetchall()]
        result.append({"vintage_id": pin, "record_id": row[0], "state": "stale" if newer else "current",
                       "newer_vintages": newer})
    return {"pins": result, "stale": any(p["state"] != "current" for p in result)}
