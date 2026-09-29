"""A contest's cited results-and-polls dossier (#1908, L11/L12).

One answer per contest, each section from its own record owner and cited to
its own source revisions: the contest and its jurisdiction rules, result
vintages with the preliminary-to-certified changes, candidates and lists with
their reviewable identity and dated assertions, poll series kept apart from
results, the constituency's boundary vintages and place link, the caller's
registered forecasts with their certified-only resolution state, and news
evidence. Optional sections that read other bundles' records (places,
forecasts, news) check their scopes when requested. Unknowns are listed
rather than filled in; the answer contains no prediction, poll aggregate or
causal field.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.ingestion.election_sources import REVIEW_BOUNDARY
from src.kb.elections import (
    ANSWER_CONTRACT,
    READ_SCOPE,
    ElectionStore,
    authorize,
    candidate_record_key,
    party_record_key,
    require_scope,
    table_exists,
)

FORECAST_READ = "knowledge:forecasts:read"


def contest_dossier(
    conn: Any,
    namespace: str,
    contest_id: str,
    *,
    principal_id: str,
    scopes: Iterable[str],
    as_of: str | None = None,
    include_places: bool = False,
    forecast_namespace: str | None = None,
    include_news: bool = False,
) -> dict[str, Any]:
    from src.kb.elections_geo import GEO_READ, ElectionGeography
    from src.kb.elections_identity import ElectionIdentity
    from src.kb.elections_polls import ElectionPolls

    scopes = set(scopes)
    authorize(namespace, scopes, READ_SCOPE)
    store = ElectionStore(conn, initialize=False)
    results = store.results(namespace, contest_id, as_of=as_of)
    contest = results["contest"]
    unit = {"scheme": contest["unit_scheme"], "native_id": contest["unit_native_id"]}
    identity = ElectionIdentity(conn, initialize=False)
    unknowns = []
    if results["certified"] is None:
        unknowns.append(
            "no certified result vintage"
            + (" yet" if results["current"] else "; no result at all")
        )
    entries = []
    seen = set()
    for entry in (results["current"] or {}).get("figures", {}).get("entries") or []:
        if entry["key"] in seen:
            continue  # one identity view per entry, whatever the number of published vote modes
        seen.add(entry["key"])
        key = candidate_record_key(contest["election_id"], entry, unit)
        party_key = (
            party_record_key(contest["election_id"], entry["party"])
            if entry.get("party")
            else None
        )
        view = identity.identity(namespace, key, scopes=scopes)
        item = {
            "entry": entry["key"],
            "label_as_published": entry["name"],
            "kind": entry["kind"],
            "record_key": key,
            "identity": view,
            "lineage": identity.lineage(namespace, key, scopes=scopes)
            if table_exists(conn, "election_assertions")
            else {"assertions": [], "conflicts": []},
        }
        if party_key and party_key != key:
            item["party_record_key"] = party_key
            item["party_identity"] = identity.identity(
                namespace, party_key, scopes=scopes
            )
        if view["state"] == "unmatched":
            unknowns.append(
                f"{entry['name']}: no reviewed identity (shown as the source string)"
            )
        if item["lineage"]["conflicts"]:
            unknowns.append(f"{entry['name']}: conflicting succession assertions")
        entries.append(item)
    polls = ElectionPolls(conn, initialize=False).series(
        namespace, scopes=scopes, election_id=contest["election_id"]
    )
    geography = ElectionGeography(conn, initialize=False)
    constituency = results["constituency"]
    boundaries = None
    if constituency is not None:
        vintages = [
            v
            for v in geography.vintages(
                namespace, scopes=scopes, scheme=constituency["scheme"]
            )
            if v["boundary_vintage"] == constituency["boundary_vintage"]
        ]
        boundaries = {
            "geometry_reference": constituency["geometry_ref"],
            "boundary_vintages": vintages,
            "place": None,
        }
        if not vintages:
            unknowns.append(
                "constituency boundary vintage not projected into the Geospatial store"
            )
        if include_places:
            require_scope(scopes, GEO_READ)
            boundaries["place"] = geography.place(
                namespace, constituency["constituency_id"], scopes=scopes
            )
            if boundaries["place"]["state"] != "linked":
                unknowns.append("constituency place link unresolved")
    forecasts = None
    if forecast_namespace is not None:
        require_scope(scopes, FORECAST_READ)
        forecasts = _forecasts(
            conn, forecast_namespace, contest_id, principal_id, scopes
        )
    news = None
    if include_news:
        from src.kb.elections_news import ElectionNews

        news = ElectionNews(conn, initialize=False).evidence(
            namespace, contest_id, scopes=scopes
        )
    return {
        "contract": ANSWER_CONTRACT,
        "contest": contest,
        "election": results["election"],
        "constituency": constituency,
        "as_of": as_of,
        "results": {
            k: results[k]
            for k in ("status", "current", "certified", "history", "changes")
        },
        "candidates_and_lists": entries,
        "poll_series": polls,
        "boundaries": boundaries,
        "forecasts": forecasts,
        "news_evidence": news,
        "unknowns": sorted(set(unknowns)),
        "review_boundary": REVIEW_BOUNDARY,
    }


def _forecasts(
    conn, forecast_namespace, contest_id, principal_id, scopes
) -> list[dict[str, Any]]:
    """The caller's registered election forecasts for the contest: rule and resolution state, no probabilities."""
    from src.kb.forecasts import ForecastError, ForecastStore

    if not table_exists(conn, "election_forecast_rules"):
        return []
    rows = conn.execute(
        "SELECT forecast_id, rule_json FROM election_forecast_rules WHERE forecast_namespace=? AND contest_id=? "
        "ORDER BY forecast_id",
        [forecast_namespace, contest_id],
    ).fetchall()
    ledger = ForecastStore(conn, initialize=False)
    out = []
    for forecast_id, _ in rows:
        try:
            state = ledger.inspect(
                forecast_namespace,
                forecast_id,
                principal_id=principal_id,
                scopes=scopes,
            )
        except ForecastError:
            continue  # another user's forecast stays private
        outcome = state["outcome"]
        out.append(
            {
                "forecast_id": forecast_id,
                "question": state["question"],
                "outcome_rule": state["outcome_rule"],
                "registered_by": state["owner"],
                "resolution_status": outcome["status"],
                "outcome": outcome.get("outcome"),
                "resolution_evidence": outcome.get("evidence") or [],
                "note": "registered by a user; resolved only against the certified vintage; the pack makes none",
            }
        )
    return out
