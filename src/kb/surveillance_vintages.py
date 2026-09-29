"""Compare surveillance vintages and show reporting-delay effects, both vintages cited, no nowcasting (#1917, I08).

* :func:`compare` lists every changed, added or removed value between two
  vintages of a series through the shared comparison of
  :mod:`src.kb.environment_vintages` (:func:`~src.kb.environment_vintages.diff_values`,
  called, not copied). Each change cites both vintages by id, release and
  retrieval time. A change whose value falls under another case-definition
  edition, and the case-definition and geography breaks between the vintages,
  are shown as such so a definition revision is never presented as a data
  revision. The answer says "differences between published vintages; no cause
  is inferred".
* :func:`reporting_delay` lists, per reference period, the values as first
  reported and as reported in each later vintage (each with its reporting date),
  with the source's own reporting-delay note. A period is labelled
  ``incomplete-by-source-note`` only when the source's note says the most recent
  intervals before a release are incomplete; no completeness estimate or nowcast
  is computed.
* :func:`pin` keeps the vintages a downstream view used and :func:`pin_status`
  reports ``stale`` when a newer vintage exists (the shared pin-status logic);
  a pinned view never follows a newer vintage silently.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

from src.ingestion.surveillance_sources import number_key
from src.kb.environment_vintages import diff_values, pin_entry, pin_summary
from src.kb.surveillance import (
    NEVER_SENTENCE,
    READ_SCOPE,
    WRITE_SCOPE,
    SurveillanceError,
    SurveillanceStore,
    authorize,
    digest,
    reference_start,
    table_exists,
)

CONTRACT = "noesis-surveillance-vintage-comparison-v1"
DELAY_CONTRACT = "noesis-surveillance-reporting-delay-v1"
NOTICE = "differences between published vintages; no cause is inferred"
_STEP = {"day": 1, "week": 7}


def _cite(
    store: SurveillanceStore, namespace: str, vintage: dict[str, Any]
) -> dict[str, Any]:
    return {
        **{
            k: vintage[k]
            for k in (
                "vintage_id",
                "sequence",
                "release_at_ms",
                "release_basis",
                "retrieved_at_ms",
                "retrieved_at_basis",
                "native_revision",
                "revision_of",
                "values_changed",
            )
        },
        "source_revision": store.source_revision(namespace, vintage["release_id"]),
    }


def _keyed(
    store: SurveillanceStore,
    namespace: str,
    vintage_id: str,
    versions: dict[str, str | None],
):
    out = {}
    for value in store.value_rows(namespace, vintage_id):
        revision = value["case_definition_revision_id"]
        if revision and revision not in versions:
            versions[revision] = store.definition_revision(namespace, revision)[
                "version"
            ]
        out[value["value_key"]] = {
            # Compared as numbers (288 and 288.0 are one value); the published text stays in value_text.
            "value": number_key(value["value"]),
            "value_text": value["value_text"],
            "unit": None,
            "reference_period": value["reference_period"],
            "reporting_date": value["reporting_date"],
            "case_definition_version": versions.get(revision),
            # The edition a value falls under is part of its status, so a definition change is always listed.
            "status": "|".join(sorted(value["flags"]))
            + f" (case definition {versions.get(revision)})",
        }
    return out


def compare(
    conn: Any,
    namespace: str,
    series_id: str,
    *,
    scopes: Iterable[str],
    left: str | None = None,
    right: str | None = None,
) -> dict[str, Any]:
    """Compare two vintages of a series (default: the previous and the latest by release clock)."""
    authorize(namespace, set(scopes), READ_SCOPE)
    store = SurveillanceStore(conn, initialize=False)
    series = store.series(namespace, series_id)
    vintages = store.vintage_rows(namespace, series_id)
    if not vintages:
        raise SurveillanceError("not_found", "series has no vintages")
    by_id = {v["vintage_id"]: v for v in vintages}
    right_v = by_id.get(right) if right else vintages[-1]
    left_v = by_id.get(left) if left else (vintages[-2] if len(vintages) > 1 else None)
    if right_v is None or (left and left_v is None):
        raise SurveillanceError("not_found", "vintage does not belong to this series")
    if left_v is None:
        return {
            "contract": CONTRACT,
            "series_id": series_id,
            "status": "single_vintage",
            "left": None,
            "right": _cite(store, namespace, right_v),
            "changes": [],
            "notice": NOTICE,
        }
    versions: dict[str, str | None] = {}
    old = _keyed(store, namespace, left_v["vintage_id"], versions)
    new = _keyed(store, namespace, right_v["vintage_id"], versions)
    changes = []
    for change in diff_values(
        old, new, left_id=left_v["vintage_id"], right_id=right_v["vintage_id"]
    ):
        key = change.pop("key")
        before, after = old.get(key), new.get(key)
        sample = after or before
        entry = {
            "reference_period": sample["reference_period"],
            "reporting_date": sample["reporting_date"],
            **change,
            "unit": series["unit"]["label"],
        }
        left_version = None if before is None else before["case_definition_version"]
        right_version = None if after is None else after["case_definition_version"]
        if before is not None and after is not None and left_version != right_version:
            entry["attribution"] = "case-definition-change"
            entry["note"] = (
                f"the value falls under case-definition edition {right_version} in the later vintage and "
                f"{left_version} in the earlier one: a definition change, not a data revision"
            )
        else:
            entry["attribution"] = "published-difference"
        entry["case_definition"] = {"left": left_version, "right": right_version}
        changes.append(entry)
    lo, hi = left_v["release_at_ms"], right_v["release_at_ms"]
    breaks = []
    for item in series["breaks"]:
        first = by_id.get(item["first_vintage_id"])
        if (
            item["kind"] == "case-definition"
            or first is None
            or lo < first["release_at_ms"] <= hi
            or item["first_vintage_id"] == right_v["vintage_id"]
        ):
            breaks.append(item)
    return {
        "contract": CONTRACT,
        "series_id": series_id,
        "status": "compared",
        "provider": series["provider"],
        "kind": series["kind"],
        "unit": series["unit"],
        "left": _cite(store, namespace, left_v),
        "right": _cite(store, namespace, right_v),
        "changes": changes,
        "breaks": breaks,
        "summary": {
            "changed": len(changes),
            "definition_changes": sum(
                1 for c in changes if c["attribution"] == "case-definition-change"
            ),
        },
        "notice": NOTICE,
        "boundary": NEVER_SENTENCE,
    }


def _incomplete_from(note: dict[str, Any] | None, released_on: str) -> str | None:
    """The first day the source's own note calls incomplete for a release, or None when the note says nothing."""
    recent = (note or {}).get("incomplete_recent") or {}
    interval, count = recent.get("interval"), recent.get("count")
    if not isinstance(count, int) or count < 1:
        return None
    released = date.fromisoformat(released_on)
    if interval in _STEP:
        return (released - timedelta(days=_STEP[interval] * count)).isoformat()
    if interval == "month":
        month = released.year * 12 + released.month - 1 - count
        return date(month // 12, month % 12 + 1, 1).isoformat()
    return None


def reporting_delay(
    conn: Any, namespace: str, series_id: str, *, scopes: Iterable[str]
) -> dict[str, Any]:
    """Per reference period: the values as first reported and in each later vintage, with the source's note."""
    authorize(namespace, set(scopes), READ_SCOPE)
    store = SurveillanceStore(conn, initialize=False)
    series = store.series(namespace, series_id)
    periods: dict[str, list[dict[str, Any]]] = {}
    unknown: list[dict[str, Any]] = []
    notes = {}
    for vintage in store.vintage_rows(namespace, series_id):
        revision = store.source_revision(namespace, vintage["release_id"])
        note_id = vintage["metadata"].get("delay_note_id")
        if note_id and note_id not in notes:
            notes[note_id] = store.delay_note(namespace, note_id)
        note = notes.get(note_id)
        cutoff = _incomplete_from(note, revision["published_on"])
        by_period: dict[str, list[dict[str, Any]]] = {}
        for value in store.value_rows(namespace, vintage["vintage_id"]):
            reported = {
                "reporting_date": value["reporting_date"],
                "value": value["value"],
                "value_text": value["value_text"],
                "flags": value["flags"],
            }
            if value["reference_period"] is None:
                unknown.append(
                    {
                        **reported,
                        "vintage_id": vintage["vintage_id"],
                        "release": revision["published_on"],
                    }
                )
                continue
            by_period.setdefault(value["reference_period"], []).append(reported)
        for period, reported in by_period.items():
            labels = []
            if cutoff is not None and reference_start(period) >= cutoff:
                labels.append("incomplete-by-source-note")
            periods.setdefault(period, []).append(
                {
                    "vintage_id": vintage["vintage_id"],
                    "release": revision["published_on"],
                    "native_revision": vintage["native_revision"],
                    "delay_note_id": note_id,
                    "labels": labels,
                    "reported": sorted(
                        reported, key=lambda r: r["reporting_date"] or ""
                    ),
                }
            )
    rows = []
    for period in sorted(periods, key=lambda p: (reference_start(p), p)):
        history = periods[period]
        rows.append(
            {
                "reference_period": period,
                "first_reported": history[0],
                "later": history[1:],
                "changed_after_first": any(
                    h["reported"] != history[0]["reported"] for h in history[1:]
                ),
            }
        )
    return {
        "contract": DELAY_CONTRACT,
        "series_id": series_id,
        "provider": series["provider"],
        "kind": series["kind"],
        "unit": series["unit"],
        "periods": rows,
        "reference_unknown": unknown,
        "delay_notes": list(notes.values()),
        "notice": "values as each vintage published them; a period is labelled incomplete only where the source's "
        "own note says so; no completeness estimate or nowcast is computed",
        "boundary": NEVER_SENTENCE,
    }


def pin(
    conn: Any,
    namespace: str,
    view_key: str,
    vintage_ids: Sequence[str],
    *,
    principal_id: str,
    scopes: Iterable[str],
    now=None,
) -> dict[str, Any]:
    """Keep the vintages a downstream view used; each id is checked against the store before anything is written."""
    authorize(namespace, set(scopes), WRITE_SCOPE, write=True)
    if not str(view_key or "").strip() or not vintage_ids:
        raise SurveillanceError(
            "invalid_pin", "a pin names its view and at least one vintage"
        )
    store = SurveillanceStore(conn, initialize=False, now=now)
    owners = {}
    for vintage_id in vintage_ids:
        row = conn.execute(
            "SELECT series_id FROM surveillance_vintages WHERE namespace=? AND vintage_id=?",
            [namespace, vintage_id],
        ).fetchone()
        if row is None:
            raise SurveillanceError(
                "invalid_pin",
                "a pinned vintage is not visible in this namespace",
                vintage_id=vintage_id,
            )
        owners[vintage_id] = row[0]
    for vintage_id, series_id in owners.items():
        pin_id = "sv-pin:" + digest([namespace, view_key, series_id, vintage_id])[:24]
        if not conn.execute(
            "SELECT 1 FROM surveillance_pins WHERE namespace=? AND pin_id=?",
            [namespace, pin_id],
        ).fetchone():
            conn.execute(
                "INSERT INTO surveillance_pins VALUES (?,?,?,?,?,?,?)",
                [
                    namespace,
                    pin_id,
                    view_key,
                    series_id,
                    vintage_id,
                    principal_id,
                    store.now(),
                ],
            )
    return pin_status(conn, namespace, view_key, scopes={"operator"})


def pin_status(
    conn: Any, namespace: str, view_key: str | None = None, *, scopes: Iterable[str]
) -> dict[str, Any]:
    """Pinned vintages with their own values; a later vintage marks a pin ``stale`` and never replaces it."""
    authorize(namespace, set(scopes), READ_SCOPE)
    if not table_exists(conn, "surveillance_pins"):
        return pin_summary([])
    store = SurveillanceStore(conn, initialize=False)
    rows = conn.execute(
        "SELECT pin_id, view_key, series_id, vintage_id, created_by, created_at_ms FROM surveillance_pins WHERE "
        "namespace=? AND (? IS NULL OR view_key=?) ORDER BY created_at_ms, pin_id",
        [namespace, view_key, view_key],
    ).fetchall()
    entries = []
    for pin_id, key, series_id, vintage_id, by, at in rows:
        vintages = store.vintage_rows(namespace, series_id)
        pinned = next((v for v in vintages if v["vintage_id"] == vintage_id), None)
        if pinned is None:
            # One bad row is reported, never raised: listing pins must survive it.
            entries.append(
                {
                    **pin_entry(vintage_id, None, []),
                    "pin_id": pin_id,
                    "view_key": key,
                    "series_id": series_id,
                    "reason": "the pinned vintage is not a vintage of this series",
                }
            )
            continue
        position = vintages.index(pinned)
        newer = [v["vintage_id"] for v in vintages[position + 1 :]]
        entries.append(
            {
                **pin_entry(vintage_id, series_id, newer, owner_key="series_id"),
                "pin_id": pin_id,
                "view_key": key,
                "pinned_by": by,
                "pinned_at_ms": at,
                "values": store.value_rows(namespace, vintage_id),
            }
        )
    return pin_summary(entries)
