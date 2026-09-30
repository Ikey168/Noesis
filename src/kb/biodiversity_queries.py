"""As-of answers: taxa, occurrences for a taxon or place, conservation status history (#2220, BD08 #2524, BD09 #2525).

Extends the environment query layer (the place model of
:mod:`src.kb.environment_places`, the as-of rules of the environment store)
over the biodiversity records:

* :func:`lookup_taxa` - the provider identities a name or key reaches, their
  checklist versions, reviewed and open identity matches and the dated
  taxonomic status changes between checklist releases.
* :func:`occurrences` - occurrence records **on record at** a date (retrieval
  time), for a taxon (synonyms and other providers' keys join only through
  *accepted* identity matches, which are shown) or a place (published
  uncertainty and generalisation respected; records whose uncertainty reaches
  the boundary are reported as ``uncertain``, never silently included). Every
  row cites the occurrence, its dataset, licence and retrieval time. Counts are
  counts of records returned, never abundance or presence/absence; an empty
  answer says "no occurrence on record".
* :func:`status_history` - every stored assessment in date order with
  category, criteria, scope and citation, global and regional separately, the
  assessment current at the date as the assessor designated it, category
  changes as published (with the published change reason) and never read as
  a trend; the IUCN reference-only licence limits what is returned. A taxon
  without assessments is "not assessed on record" - distinct from IUCN's own
  "Not Evaluated" (NE) or "Data Deficient" (DD).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from src.kb.biodiversity_records import (
    IUCN_CATEGORIES,
    IUCN_REDISTRIBUTION,
    NEVER_SENTENCE,
    NOT_ASSESSED,
    OCCURRENCE_ANSWER_CONTRACT,
    READ_SCOPE,
    STATUS_ANSWER_CONTRACT,
    BiodiversityError,
    authorize,
)
from src.kb.biodiversity_store import BiodiversityStore

COUNT_LABEL = ("count of occurrence records returned by the bounded selections on record at the date; not an "
               "abundance, density or presence/absence measure")
EXPORT_FIELDS = ("category", "year_published", "scope", "latest", "citation", "url")


def _ms(as_of: Any) -> int | None:
    if as_of is None or isinstance(as_of, int):
        return as_of
    text = str(as_of)
    if len(text) == 10:
        text += "T23:59:59.999+00:00"
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def _date(as_of_ms: int | None) -> str | None:
    return None if as_of_ms is None else datetime.fromtimestamp(as_of_ms / 1000, tz=UTC).date().isoformat()


def _identity(conn):
    from src.kb.biodiversity_identity import BiodiversityIdentity

    return BiodiversityIdentity(conn, initialize=False)


def _resolve(conn, namespace: str, taxon: str) -> dict[str, Any]:
    identity = _identity(conn)
    interpreted, subjects = identity.find(namespace, taxon)
    members = sorted({m for s in subjects for m in identity.members(namespace, s)})
    matches = identity.accepted_between(namespace, members)
    return {"query": taxon, "interpreted_as": interpreted, "subjects": subjects, "members": members,
            "accepted_matches": [{k: m[k] for k in ("match_id", "left_key", "right_key", "basis", "evidence_class",
                                                    "reviewer", "reviewed_at_ms", "checklists", "conflicts")}
                                 for m in matches],
            "join_rule": "provider keys beyond the named taxon join only through accepted identity matches"}


# ------------------------------------------------------------------ taxa


def lookup_taxa(conn: Any, namespace: str, query: str, *, scopes: Iterable[str]) -> dict[str, Any]:
    authorize(namespace, scopes, READ_SCOPE)
    store = BiodiversityStore(conn, initialize=False)
    store.require_ready()
    resolved = _resolve(conn, namespace, query)
    identities = []
    for record in store.records(namespace, subject_keys=resolved["members"]):
        if record["record_type"] != "taxon_identity":
            continue
        revision = store.current(namespace, record["record_id"])
        published = revision["statement"]["as_published"]
        identities.append({"subject_key": record["subject_key"], "provider": record["provider"],
                           **{k: published.get(k) for k in ("key_scheme", "native_key", "scientific_name",
                                                           "authorship", "rank", "status", "accepted_key",
                                                           "accepted_name", "checklist", "cross_references")},
                           "revision_id": revision["revision_id"], "retrieved_on": revision["retrieved_on"],
                           "source_url": revision["statement"]["source"]["url"]})
    proposals = [m for s in resolved["subjects"] for m in _identity(conn).matches(namespace, scopes=scopes,
                                                                                  subject_key=s)
                 if m["state"] == "proposed"]
    return {"contract": "noesis-biodiversity-taxa-v1", "namespace": namespace, **resolved,
            "identities": identities,
            "status_changes": [c for s in resolved["members"] for c in store.status_changes(namespace, subject_key=s)],
            "open_proposals": [{k: m[k] for k in ("match_id", "left_key", "right_key", "basis", "evidence_class",
                                                  "conflicts")} for m in proposals],
            "found": bool(resolved["subjects"]),
            "notice": "names, ranks and statuses as each checklist release and provider published them; "
                      + NEVER_SENTENCE}


# ------------------------------------------------------------------ occurrences


def _dataset(store, namespace, key, as_of_ms, cache):
    if key not in cache:
        record = store.find(namespace, "dataset", "gbif", key)
        revision = store.current(namespace, record["record_id"], as_of_ms=as_of_ms) if record else None
        published = revision["statement"]["as_published"] if revision else None
        cache[key] = ({"dataset_key": key, "title": published["title"], "publisher": published.get("publisher"),
                       "licence": published.get("licence"), "doi": published.get("doi"),
                       "citation": published.get("citation"), "version": published.get("version"),
                       "revision_id": revision["revision_id"]} if published else
                      {"dataset_key": key, "status": "dataset metadata not on record"})
    return cache[key]


def _row(store, namespace, record, revision, as_of_ms, cache) -> dict[str, Any]:
    published = revision["statement"]["as_published"]
    return {"gbif_id": published["gbif_id"], "occurrence_id": published.get("occurrence_id"),
            "scientific_name": published.get("scientific_name"), "taxon_key": published.get("taxon_key"),
            "basis_of_record": published.get("basis_of_record"), "event_date": published.get("event_date"),
            "coordinates": published.get("coordinates"),
            "coordinate_uncertainty_m": published.get("coordinate_uncertainty_m"),
            "generalisation": published["generalisation"], "issues": published.get("issues") or [],
            "country_code": published.get("country_code"), "licence": published.get("licence"),
            "dataset": _dataset(store, namespace, published["dataset_key"], as_of_ms, cache),
            "citation": {"record_id": record["record_id"], "revision_id": revision["revision_id"],
                         "url": revision["statement"]["source"]["url"],
                         "api_url": revision["statement"]["source"].get("api_url"),
                         "retrieved_at_ms": revision["observed_at_ms"], "retrieved_on": revision["retrieved_on"]},
            "unknowns": revision["statement"]["unknowns"]}


def occurrences(conn: Any, namespace: str, *, scopes: Iterable[str], taxon: str | None = None,
                place_id: str | None = None, as_of: Any = None) -> dict[str, Any]:
    """Occurrence records on record at ``as_of`` for a taxon and/or a place, each cited; explicit when none."""
    from src.kb.biodiversity_links import classify, place_view

    authorize(namespace, scopes, READ_SCOPE)
    if not taxon and not place_id:
        raise BiodiversityError("invalid_request", "name a taxon or a place")
    store = BiodiversityStore(conn, initialize=False)
    store.require_ready()
    as_of_ms = _ms(as_of)
    resolved = _resolve(conn, namespace, taxon) if taxon else None
    keys = None
    if resolved is not None:
        keys = {m.split(":", 1)[1] for m in resolved["members"] if m.startswith("gbif:")}
    place = None
    if place_id:
        place = place_view(conn, namespace, place_id)
        if place is None:
            raise BiodiversityError("not_found", "place is not visible in this namespace")
    rows, uncertain, removed, excluded, cache = [], [], [], {}, {}
    for record in store.records(namespace, record_type="occurrence"):
        revision = store.current(namespace, record["record_id"], as_of_ms=as_of_ms)
        if revision is None:
            continue
        published = revision["statement"]["as_published"]
        if keys is not None and published.get("taxon_key") not in keys:
            continue
        if revision["event"] == "removed":
            removed.append({"gbif_id": published["gbif_id"], "removed_on": revision["statement"]["effective"]["date"],
                            "basis": revision["statement"]["effective"]["date_basis"],
                            "revision_id": revision["revision_id"]})
            continue
        row = _row(store, namespace, record, revision, as_of_ms, cache)
        if place is not None:
            relation = classify(published, place)
            row["place_relation"] = relation
            if relation["relation"] == "uncertain":
                uncertain.append(row)
                continue
            if relation["relation"] not in {"within", "code"}:
                excluded[relation["relation"]] = excluded.get(relation["relation"], 0) + 1
                continue
        rows.append(row)
    rows.sort(key=lambda r: (r["event_date"] or "", r["gbif_id"]))
    licences = sorted({(r["licence"] or {}).get("id") or "unknown" for r in rows + uncertain})
    return {
        "contract": OCCURRENCE_ANSWER_CONTRACT, "namespace": namespace, "as_of": _date(as_of_ms),
        "as_of_basis": "records retrieved on or before the date (retrieval time), each with its own event date",
        "taxon": resolved, "place": place and {k: place[k] for k in ("place_id", "name", "codes", "extent_m",
                                                                     "geometry_id")},
        "occurrences": rows, "uncertain": uncertain, "removed_on_record": removed, "excluded": excluded,
        "counts": {"records_returned": len(rows), "uncertain_records": len(uncertain), "label": COUNT_LABEL},
        "licences": licences,
        "non_commercial": sorted({r["gbif_id"] for r in rows + uncertain
                                  if (r["licence"] or {}).get("commercial_use") is False}),
        "status": "occurrences on record" if rows or uncertain else "no occurrence on record",
        "notice": NEVER_SENTENCE,
    }


# ------------------------------------------------------------------ conservation status


def _restrict(published: Mapping[str, Any], purpose: str) -> dict[str, Any]:
    """Apply the IUCN reference-only licence to one assessment for an answer or an export."""
    fields = EXPORT_FIELDS if purpose == "export" else (
        "assessment_id", "taxon_id", "scientific_name", "category", "criteria", "assessment_date", "year_published",
        "scope", "latest", "possibly_extinct", "possibly_extinct_in_wild", "change_reason", "citation", "url")
    return {k: published.get(k) for k in fields}


def status_history(conn: Any, namespace: str, taxon: str, *, scopes: Iterable[str], as_of: Any = None,
                   purpose: str = "answer") -> dict[str, Any]:
    authorize(namespace, scopes, READ_SCOPE)
    if purpose not in {"answer", "export"}:
        raise BiodiversityError("invalid_request", "purpose is answer or export")
    store = BiodiversityStore(conn, initialize=False)
    store.require_ready()
    resolved = _resolve(conn, namespace, taxon)
    as_of_ms = _ms(as_of)
    as_of_date = _date(as_of_ms)
    sis = {m.split(":", 1)[1] for m in resolved["members"] if m.startswith("iucn:")}
    items = []
    for record in store.records(namespace, record_type="conservation_assessment"):
        revision = store.current(namespace, record["record_id"])
        published = revision["statement"]["as_published"]
        if published["taxon_id"] not in sis:
            continue
        published_on = (published.get("assessment_date") if purpose == "answer" else None) or \
            published["year_published"]
        if as_of_date and str(published["year_published"]) > as_of_date[:4]:
            continue
        items.append({**_restrict(published, purpose), "category_label": IUCN_CATEGORIES[published["category"]],
                      "published_on": published_on, "licence_tier": published["licence_tier"],
                      "citation_ref": {"record_id": record["record_id"], "revision_id": revision["revision_id"],
                                       "retrieved_on": revision["retrieved_on"],
                                       "url": revision["statement"]["source"]["url"]},
                      "_latest": published["latest"], "_reason": published.get("change_reason"),
                      "_criteria": published.get("criteria")})
    scopes_out: dict[str, dict[str, Any]] = {}
    for item in sorted(items, key=lambda i: (str(i["year_published"]), str(i["published_on"]))):
        scope = item["scope"]
        entry = scopes_out.setdefault(scope["label"], {"scope": scope, "assessments": [], "changes": []})
        previous = entry["assessments"][-1] if entry["assessments"] else None
        if previous and previous["category"] != item["category"]:
            entry["changes"].append({
                "from": {"category": previous["category"], "year_published": previous["year_published"]},
                "to": {"category": item["category"], "year_published": item["year_published"]},
                "change_reason_as_published": item["_reason"],
                "notice": "a published category change; not a trend and not a Noesis assessment"})
        entry["assessments"].append(item)
    for entry in scopes_out.values():
        designated = [a for a in entry["assessments"] if a["_latest"]]
        if as_of_date is None or (designated and str(designated[-1]["year_published"]) <= as_of_date[:4]):
            current = designated[-1] if designated else None
            basis = "the assessor's own latest designation"
        else:
            current = entry["assessments"][-1] if entry["assessments"] else None
            basis = ("the assessor's most recent assessment published by the date (the later latest designation "
                     "was not yet published)")
        entry["current"] = None if current is None else {k: current.get(k) for k in (
            "assessment_id", "category", "category_label", "year_published", "url")} | {"basis": basis}
        for item in entry["assessments"]:
            for key in ("_latest", "_reason", "_criteria"):
                item.pop(key, None)
    ordered = sorted(scopes_out.values(), key=lambda e: (e["scope"]["kind"] != "global", e["scope"]["label"]))
    return {
        "contract": STATUS_ANSWER_CONTRACT, "namespace": namespace, "as_of": as_of_date, "taxon": resolved,
        "scopes": ordered, "status": NOT_ASSESSED if not items else "assessed",
        "not_assessed_notice": ("no assessment on record for this taxon in the covered sources; this is not IUCN "
                                "'Not Evaluated' (NE) and not 'Data Deficient' (DD)") if not items else None,
        "licence": {"tier": "reference-only", "purpose": purpose, "redistribution": IUCN_REDISTRIBUTION,
                    "fields": list(EXPORT_FIELDS) if purpose == "export" else "citation-level fields"},
        "notice": "categories as the assessor published them; no Noesis-derived status, trend or risk score",
    }


# ------------------------------------------------------------------ bundle section


def bundle(conn: Any, namespace: str, *, scopes: Iterable[str], taxon: str | None = None,
           place_id: str | None = None, as_of: Any = None) -> dict[str, Any]:
    """Occurrences and assessment history for a place or taxon bundle, restricted for export."""
    section = {"feature": "biodiversity", "as_of": _date(_ms(as_of)),
               "occurrences": occurrences(conn, namespace, scopes=scopes, taxon=taxon, place_id=place_id,
                                          as_of=as_of)}
    if taxon:
        section["conservation_status"] = status_history(conn, namespace, taxon, scopes=scopes, as_of=as_of,
                                                        purpose="export")
    return section


__all__ = ["COUNT_LABEL", "bundle", "lookup_taxa", "occurrences", "status_history"]
