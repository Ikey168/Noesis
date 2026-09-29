"""Shared as-of views over OSS Ecosystems revisions (used by the graph, the queries and the monitor).

Two clocks are kept apart:

* ``at_ms`` - the date asked about: the revision *in effect* then (by the
  source's own date, falling back to the observation time);
* ``acquired_by_ms`` - the knowledge cutoff: only revisions Noesis had
  observed by then are considered. It defaults to the end of the asked date,
  so an answer about a date never uses data acquired after it.

Each view first selects the current revision per source record, then filters.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from src.kb.oss_ecosystem_records import REGISTRY_SOURCES, ms
from src.kb.oss_ecosystem_store import OssEcosystemStore, OssStoreError
from src.kb.oss_ecosystem_versions import UnsupportedConstraint, sort_key

AVAILABLE_STATES = frozenset({"published", "deprecated", "restored"})


def cutoff_ms(value: Any, *, default: int | None = None) -> int:
    """A date means the whole day (UTC, inclusive); an instant means itself."""

    if value is None or value == "":
        if default is None:
            raise OssStoreError("bad_request", "a date is required")
        return int(default)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        day = datetime.fromisoformat(text).replace(tzinfo=UTC)
        return int((day + timedelta(days=1)).timestamp() * 1000) - 1
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OssStoreError(
            "bad_request", f"{text!r} is not an ISO date or instant"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return int(moment.timestamp() * 1000)


def iso(ms_value: int | None) -> str | None:
    if ms_value is None:
        return None
    return (
        datetime.fromtimestamp(ms_value / 1000, tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%S.%f"
        )[:-3]
        + "Z"
    )


def registry_source(coordinate: str) -> str:
    return REGISTRY_SOURCES[coordinate.split(":")[1]]


def release_states(
    store: OssEcosystemStore,
    namespace: str,
    coordinate: str,
    *,
    source: str,
    at_ms: int,
    acquired_by_ms: int,
) -> list[dict[str, Any]]:
    """Every release of a package the source listed, with its state on the date (or why it has none)."""

    ecosystem = coordinate.split(":")[1]
    result = []
    for record in store.records(
        namespace,
        record_type="release_state_revision",
        source=source,
        coordinate=coordinate,
    ):
        history = store.history(record["record_id"], acquired_by_ms=acquired_by_ms)
        if not history:
            continue  # not acquired by the cutoff
        first = history[0]
        published = ms(
            next(
                (
                    r["statement"].get("published_at")
                    for r in history
                    if r["statement"].get("published_at")
                ),
                None,
            )
        )
        in_effect = [r for r in history if r["order_ms"] <= at_ms]
        entry = {
            "version": record["version"],
            "published_at": iso(published),
            "record_id": record["record_id"],
        }
        if published is not None and published > at_ms:
            entry.update(
                {"state": "not_yet_published", "revision_id": first["revision_id"]}
            )
        elif in_effect:
            revision = in_effect[-1]
            entry.update(
                {
                    "state": revision["statement"]["state"],
                    "revision_id": revision["revision_id"],
                    "observed_at_ms": revision["observed_at_ms"],
                }
            )
            if revision["statement"].get("reason"):
                entry["reason"] = revision["statement"]["reason"]
        elif published is not None:
            # Published before the first observation: the state before then was not observed.
            entry.update(
                {
                    "state": first["statement"]["state"]
                    if first["statement"]["state"] == "published"
                    else "published",
                    "revision_id": first["revision_id"],
                    "observed_at_ms": first["observed_at_ms"],
                    "gap": {
                        "from": iso(published),
                        "to": iso(first["order_ms"]),
                        "note": "state before the first observation was not observed",
                    },
                }
            )
        else:
            entry.update(
                {
                    "state": "unknown",
                    "revision_id": first["revision_id"],
                    "gap": {
                        "to": iso(first["order_ms"]),
                        "note": "no publication date and no observation "
                        "on or before the date",
                    },
                }
            )
        try:
            entry["_sort"] = sort_key(ecosystem, record["version"])
        except UnsupportedConstraint:
            entry["_sort"] = None
            entry["unordered"] = (
                "the version does not parse under the ecosystem's rules"
            )
        result.append(entry)
    ordered = sorted(
        (e for e in result if e["_sort"] is not None), key=lambda e: e["_sort"]
    )
    ordered += sorted(
        (e for e in result if e["_sort"] is None), key=lambda e: e["version"]
    )
    for entry in ordered:
        entry.pop("_sort")
    return ordered


def current_statement(
    store: OssEcosystemStore,
    namespace: str,
    record_type: str,
    *,
    source: str,
    coordinate: str,
    version: str | None = None,
    at_ms: int | None = None,
    acquired_by_ms: int | None = None,
) -> dict[str, Any] | None:
    records = store.records(
        namespace,
        record_type=record_type,
        source=source,
        coordinate=coordinate,
        version=version,
    )
    if not records:
        return None
    revision = store.current(
        records[0]["record_id"], acquired_by_ms=acquired_by_ms, at_ms=at_ms
    )
    return revision


def sources_for(
    store: OssEcosystemStore, namespace: str, coordinate: str, record_type: str
) -> list[str]:
    return sorted(
        {
            r["source"]
            for r in store.records(
                namespace, record_type=record_type, coordinate=coordinate
            )
        }
    )


__all__ = [
    "AVAILABLE_STATES",
    "current_statement",
    "cutoff_ms",
    "iso",
    "registry_source",
    "release_states",
    "sources_for",
]
